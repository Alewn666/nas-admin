#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""nas-admin —— 管理 NAS 的高权限网页（只在局域网跑，绝不映射公网）。

设计与约定（与「作文批改 / 背诵助手」同一套工程约定，见
`essay-revise/docs/设计规范_风格统一.md`）：
  · 单文件 Flask，端口 8091，**口令只从环境变量读**（不写进代码仓库）：明文 `NAS_ADMIN_PW`
    或 **哈希 `NAS_ADMIN_PW_HASH`**（`sha256:<salt>.<dk>`，PBKDF2-SHA256 带盐；给哈希就不落明文），
    没配口令 → 整站 fail-closed（除 /api/health 全部 503），页面只显示"未配置口令"横幅
  · 登录走服务端 session（HttpOnly cookie），错 5 次锁 30 秒（按 IP 计）
  · 高危操作**服务端二次确认**：删目录要输完整路径、删文件要输文件名、
    重启输「重启」、关机输「关机」、清 docker 垃圾输「清理」、删容器要输容器名
  · 一切危险动作落审计日志 `data/audit.log`（JSONL：时间/IP/动作/参数/返回码）

五大模块：
  1. 总览    —— CPU/内存/温度/磁盘/负载/uptime/IP/容器概况（纯 /proc + statvfs，零外部命令）
  2. 容器    —— docker ps 全量 + 资源占用 + 启停/重启/删除 + 日志 tail
  3. 文件    —— 浏览/上传/下载/编辑/保存/建目录/删除（root 权限，整个文件系统）
  4. 命令    —— root 命令执行框（bash，30 秒超时，输出回显，全程审计）
  5. 危险区  —— 重启 NAS / 关机 / 清理 docker 垃圾 / 看审计日志

⚠️ 所有子进程出口集中在 `run()` / `run_shell()` / `spawn_detached()` 三个函数里，
   测试（test_admin.py）打桩这三处即可零真机副作用。
"""
import hmac
import hashlib
import json
import os
import random
import re
import socket
import subprocess
import tempfile
import threading
import time
import urllib.request
import zipfile
from collections import deque
from datetime import datetime, timedelta

from flask import Flask, Response, jsonify, render_template, request, send_file, session

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("NAS_ADMIN_DATA", os.path.join(BASE_DIR, "data"))
AUDIT_PATH = os.path.join(DATA_DIR, "audit.log")
# 文件管理的根：生产是 "/"（整个 NAS），测试指到临时目录
FS_ROOT = os.environ.get("NAS_ADMIN_FS_ROOT", "/")
# 容器宿主模式：=1 时所有 shell/系统命令垫 nsenter 进宿主机 PID1 的命名空间执行
# （容器必须 --privileged --pid=host；这样 docker/systemctl/文件全都管到宿主机，
#   不用挂 docker.sock 也行）。生产 systemd 跑法保持 0，行为不变。
NSENTER = os.environ.get("NAS_ADMIN_NSENTER", "0") == "1"
NSPREFIX = ["nsenter", "-t", "1", "-m", "-u", "-i", "-n", "-p", "--"]

# 口令有两种给法，二选一：
#   ① NAS_ADMIN_PW=明文          —— 老写法，NAS 那份在用（docker env-file 直接给）
#   ② NAS_ADMIN_PW_HASH=sha256:<16位salt_hex>.<32字节dk_hex>  —— 推荐，磁盘不落明文
# 两者都给时 **以哈希为准**（明文被忽略），这样误留的明文不会变成实际口令。
# 登录时用户仍敲明文，服务端用同样盐现算哈希再比对（hmac.compare_digest 定时安全）。
PASSWORD = os.environ.get("NAS_ADMIN_PW", "").strip()
PASSWORD_HASH = os.environ.get("NAS_ADMIN_PW_HASH", "").strip().lower()
_PBKDF2_ROUNDS = int(os.environ.get("NAS_ADMIN_PBKDF2_ROUNDS", "390000"))


def _pbkdf2(pw: str, salt_hex: str, rounds: int = None) -> bytes:
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), bytes.fromhex(salt_hex),
                             rounds or _PBKDF2_ROUNDS, dklen=32)
    return dk


def hash_password(pw: str, salt_hex: str = None, rounds: int = None) -> str:
    """生成 'sha256:<salt_hex>.<dk_hex>' 形式的口令哈希（PBKDF2-SHA256，带盐）。
    只传 pw 会自动生成随机盐；传 salt_hex 则复用该盐（比对时走这条路）。"""
    salt_hex = salt_hex or os.urandom(16).hex()
    return "sha256:%s.%s" % (salt_hex, _pbkdf2(pw, salt_hex, rounds).hex())


def _pw_ok(candidate: str) -> bool:
    """校验口令：配了哈希就比哈希，否则比明文。"""
    if not candidate:
        return False
    if PASSWORD_HASH.startswith("sha256:") and "." in PASSWORD_HASH:
        try:
            salt_hex, want_hex = PASSWORD_HASH[len("sha256:"):].split(".", 1)
            got = _pbkdf2(candidate, salt_hex).hex()
        except ValueError:
            return False
        return hmac.compare_digest(got, want_hex)
    if PASSWORD:
        return hmac.compare_digest(candidate.encode(), PASSWORD.encode())
    return False
BIND = os.environ.get("NAS_ADMIN_BIND", "0.0.0.0")
PORT = int(os.environ.get("NAS_ADMIN_PORT", "8091"))
VERSION = "20261018f"   # ⚠️ 改 static/css / static/js 必须换（模板用它当缓存戳），否则浏览器继续跑旧文件

LOCK_FAILS = 5          # 连错几次锁
LOCK_SECS = 30          # 锁多久
SESSION_DAYS = max(1, int(os.environ.get("NAS_ADMIN_SESSION_DAYS", "30")))  # 记住登录多少天
EXEC_TIMEOUT = 30       # 命令执行超时（秒）
EXEC_MAX_OUT = 200_000  # 命令输出上限（字符）
READ_MAX = 1_000_000    # 文件编辑读取上限（字节）

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("NAS_ADMIN_SECRET") or "%032x" % random.getrandbits(128)
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
# 用户要求「不要每次打开都输口令」：cookie 做成**持久**的（带 Expires），默认记 30 天；
# 之前是会话级 cookie，关浏览器就没了 —— 那才是"每次都要输"的第一个原因。
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=SESSION_DAYS)

_audit_lock = threading.Lock()
_fail_lock = threading.Lock()
_fails = {}                       # ip -> {"n": 连错次数, "until": 解锁时间戳}
_SESS = {}                        # jti -> {"ip":..., "ts":...}  服务端会话表（登出即吊销）
SESS_PATH = os.path.join(DATA_DIR, "sessions.json")   # 会话表落盘：容器重建/重启不掉登录
_sess_lock = threading.Lock()


def _sess_save():
    """会话表原子写盘（只存 jti/ip/时间，不含口令，600）。失败不致命。"""
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with _sess_lock:
            tmp = SESS_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(_SESS, f, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, SESS_PATH)
    except Exception:
        pass


def _sess_load():
    """启动时把会话表读回来。否则每次重建容器 _SESS 清空 → 全员被踢下线
    （第二个"每次都要输口令"的原因，部署后尤其明显）。"""
    try:
        with open(SESS_PATH, "r", encoding="utf-8") as f:
            d = json.load(f)
        if not isinstance(d, dict):
            return
        now = time.time()
        for k, v in d.items():
            if not isinstance(v, dict):
                continue
            ts = float(v.get("ts", 0))
            if now - ts < SESSION_DAYS * 86400:
                _SESS[str(k)] = {"ip": str(v.get("ip", ""))[:64], "ts": ts}
    except Exception:
        pass


_sess_load()


def _prune_sess(maxn=500):
    """会话表瘦身：过期（超过 SESSION_DAYS 天）+ 超过 maxn 条的老会话清掉，变了才落盘。"""
    cutoff = time.time() - SESSION_DAYS * 86400
    changed = False
    for k in [k for k, v in _SESS.items() if v.get("ts", 0) < cutoff]:
        _SESS.pop(k, None)
        changed = True
    if len(_SESS) > maxn:            # 还多就砍到 maxn（老的先出，dict 保持插入序）
        for k in list(_SESS)[:len(_SESS) - maxn]:
            _SESS.pop(k, None)
            changed = True
    if changed:
        _sess_save()
_cpu_lock = threading.Lock()


# ============================ 子进程出口（测试打桩这三处） ============================
def _hostify(cmd):
    """容器宿主模式：垫 nsenter 进宿主机 PID1 的命名空间（容器要 --privileged --pid=host），
    docker/systemctl/任何命令都真正在宿主机上执行，等价于在宿主机上跑。"""
    if not NSENTER or not cmd:
        return list(cmd)
    return NSPREFIX + list(cmd)


def run(cmd, timeout=20):
    """跑一条 argv（不经过 shell）。返回 (rc, stdout, stderr)。
    ⚠️ 非 root 且非宿主模式（开发/测试态 uid990 没有 docker 组）时给 docker 命令垫
    sudo -n（免密已配）；容器宿主模式里 nsenter 后以宿主机 root 执行，不用垫；
    生产 systemd 跑法跑 root，geteuid()==0 也不垫。"""
    cmd = _hostify(cmd)
    if not NSENTER and cmd and cmd[0] == "docker" and os.geteuid() != 0:
        cmd = ["sudo", "-n"] + cmd
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           errors="replace")
        return p.returncode, p.stdout, p.stderr
    except FileNotFoundError as e:
        return 127, "", "命令不存在: %s" % e
    except subprocess.TimeoutExpired:
        return 124, "", "超时（%ss）" % timeout
    except Exception as e:                                   # pragma: no cover
        return 1, "", str(e)


def run_shell(cmd, timeout=EXEC_TIMEOUT):
    """跑一条 shell 命令（用户在「命令」页输入的）。返回 (rc, stdout, stderr, 耗时ms)。"""
    t0 = time.time()
    # 宿主模式下 cwd 只能给 "/"：进了宿主机挂载命名空间后 /host 这个挂载点不存在了
    cwd = "/" if NSENTER else (FS_ROOT if FS_ROOT != "/" else "/")
    argv = ["/bin/bash", "-lc", cmd]
    if NSENTER:
        argv = NSPREFIX + argv
    try:
        p = subprocess.run(argv, capture_output=True, text=True,
                           timeout=timeout, errors="replace", cwd=cwd)
        return p.returncode, p.stdout, p.stderr, int((time.time() - t0) * 1000)
    except subprocess.TimeoutExpired:
        return 124, "", "超时（%ss，命令可能还在后台跑）" % timeout, int((time.time() - t0) * 1000)
    except Exception as e:                                   # pragma: no cover
        return 1, "", str(e), int((time.time() - t0) * 1000)


def spawn_detached(cmd):
    """后台脱离进程（重启/关机：先让 HTTP 响应跑出去，再动作）。"""
    subprocess.Popen(_hostify(cmd), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     stdin=subprocess.DEVNULL, start_new_session=True)
    return True


# ============================ 审计日志 ============================
def audit(kind, action, detail="", rc=None, ip=None):
    rec = {"ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
           "ip": ip or client_ip(), "kind": kind, "action": action,
           "detail": str(detail)[:400]}
    if rc is not None:
        rec["rc"] = rc
    line = json.dumps(rec, ensure_ascii=False)
    try:
        with _audit_lock:
            os.makedirs(DATA_DIR, exist_ok=True)
            with open(AUDIT_PATH, "a", encoding="utf-8") as f:
                f.write(line + "\n")
    except Exception:                                        # pragma: no cover
        pass
    return rec


def client_ip():
    return request.headers.get("X-Forwarded-For", request.remote_addr or "?").split(",")[0].strip()


# ============================ 登录门 ============================
def _locked(ip):
    with _fail_lock:
        f = _fails.get(ip)
        return bool(f and f["until"] > time.time())


def _bad_password(ip):
    with _fail_lock:
        f = _fails.setdefault(ip, {"n": 0, "until": 0})
        f["n"] += 1
        if f["n"] >= LOCK_FAILS:
            f["until"] = time.time() + LOCK_SECS
            f["n"] = 0
            audit("auth", "lockout", "连错 %d 次" % LOCK_FAILS, ip=ip)


def _ok_password(ip):
    with _fail_lock:
        _fails.pop(ip, None)


def configured():
    return bool(PASSWORD or PASSWORD_HASH)


def authed():
    if not session.get("adm"):
        return False
    # 服务端吊销表：登出后**旧 cookie 副本**立刻失效。Flask session 是客户端签名 cookie，
    # session.clear() 只清"发出去的这一份"，管不住别人手里的副本 —— 这是能跑 root
    # 命令的面板，必须服务端可吊销（jti 一登出就从 _SESS 抹掉）。
    jti = session.get("jti")
    return bool(jti) and jti in _SESS


@app.before_request
def _gate():
    # fail-closed：没配口令就整站不可用（除健康检查）
    if request.path == "/api/health":
        return None
    if not configured():
        if request.path.startswith("/api/"):
            return jsonify({"error": "not_configured", "msg": "服务端还没配 NAS_ADMIN_PW 或 NAS_ADMIN_PW_HASH 口令"}), 503
        return None          # 页面照常渲染，由前端显示"未配置"横幅
    if request.path in ("/api/login",):
        return None
    if request.path.startswith("/static/") or request.path == "/":
        return None
    if not authed():
        return jsonify({"error": "need_login"}), 401
    return None


@app.route("/")
def index():
    return render_template("index.html", configured=configured(), v=VERSION, root=FS_ROOT)


@app.route("/api/health")
def health():
    return jsonify({"ok": True, "configured": configured(), "service": "nas-admin"})


@app.route("/api/login", methods=["POST"])
def login():
    ip = client_ip()
    if not configured():
        return jsonify({"error": "not_configured"}), 503
    if _locked(ip):
        return jsonify({"error": "locked", "msg": "连错太多次，请等 %d 秒" % LOCK_SECS}), 429
    pw = str((request.get_json(silent=True) or {}).get("password", ""))
    if _pw_ok(pw):
        _ok_password(ip)
        session["adm"] = 1
        session["who"] = ip
        session.permanent = True            # 持久 cookie（默认 30 天），别让人每次打开都输口令
        _prune_sess()
        jti = os.urandom(16).hex()          # 会话票据：登出/吊销靠它
        _SESS[jti] = {"ip": ip, "ts": time.time()}
        session["jti"] = jti
        _sess_save()                        # 落盘：容器重启/重建后依然认这张票
        audit("auth", "login", "登录成功", ip=ip)
        return jsonify({"ok": True})
    _bad_password(ip)
    audit("auth", "login_fail", "", ip=ip)
    return jsonify({"error": "bad_password", "msg": "口令不正确"}), 401


@app.route("/api/logout", methods=["POST"])
def logout():
    _SESS.pop(session.get("jti"), None)     # 服务端吊销：手握旧 cookie 副本的立刻失效
    _sess_save()
    session.clear()
    return jsonify({"ok": True})


# ============================ 1. 总览 ============================
def _read(path, default=""):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except Exception:
        return default


def _cpu_pct(window=1.0):
    """两次采样 /proc/stat 求 CPU 占用。

    ⚠️ 口径（2026-10-17 修）：/proc/stat 各字段是
        user nice system idle iowait irq softirq steal guest guest_nice
    旧版用「前 4 项之和」当总时长、只拿第 4 项当 idle —— 于是 **iowait 和 steal 被算成忙**。
    NAS 在大量用 swap（换页时 iowait 很高），页面因此常显示 97~99%，而 top 只有 20%。
    正解（与 top 一致）：busy = 总时长 - idle - iowait，steal 单列（被宿主机偷走的时间不算我们的忙）。
    ⚠️ 窗口 0.4s 太窄：IO 尖峰会被整窗放大成 100%，改 1.0s（页面 3 秒刷新一次，够用）。
    """
    def snap():
        out = {}
        for ln in _read("/proc/stat").splitlines():
            if not ln.startswith("cpu"):
                continue
            p = ln.split()
            if not (p[0] == "cpu" or p[0][3:].isdigit()):
                continue
            v = list(map(int, p[1:])) + [0] * 10
            key = "total" if p[0] == "cpu" else int(p[0][3:])
            # user nice system idle iowait irq softirq steal
            out[key] = {"total": sum(v[0:8]), "idle": v[3], "iowait": v[4], "steal": v[7]}
        return out

    with _cpu_lock:
        a = snap()
        time.sleep(window)
        b = snap()

    def pct(k):
        if k not in a or k not in b:
            return None
        dt = b[k]["total"] - a[k]["total"]
        if dt <= 0:
            return None
        idle = b[k]["idle"] - a[k]["idle"]
        iow = b[k]["iowait"] - a[k]["iowait"]
        busy = dt - idle - iow
        if busy < 0:
            busy = 0
        return round(100.0 * busy / dt, 1)

    core_keys = sorted(k for k in b if k != "total")   # 只剩 int 核号（"total" 是 str，混排会 TypeError）
    cores = [pct(k) for k in core_keys]
    return {"total": pct("total"), "cores": cores}


def _mem():
    info = {}
    for ln in _read("/proc/meminfo").splitlines():
        if ":" in ln:
            k, v = ln.split(":", 1)
            info[k] = v.strip().split()[0] if v.strip() else "0"
    def kb(k):
        try:
            return int(info.get(k, 0))
        except ValueError:
            return 0
    total = kb("MemTotal")
    avail = kb("MemAvailable")
    return {"total_mb": round(total / 1024), "used_mb": round((total - avail) / 1024),
            "avail_mb": round(avail / 1024),
            "swap_total_mb": round(kb("SwapTotal") / 1024),
            "swap_used_mb": round((kb("SwapTotal") - kb("SwapFree")) / 1024)}


def _disks():
    out, seen = [], set()
    try:
        with open("/proc/mounts", "r", encoding="utf-8", errors="replace") as f:
            mounts = [ln.split() for ln in f]
    except Exception:
        mounts = []
    for m in mounts:
        dev, mp = m[0], m[1]
        if not dev.startswith("/dev/") or mp in seen or len(out) >= 12:
            continue
        try:
            st = os.statvfs(mp)
        except Exception:
            continue
        seen.add(mp)
        total = st.f_blocks * st.f_frsize
        free = st.f_bavail * st.f_frsize
        used = total - free
        out.append({"mount": mp.replace("\\040", " "), "dev": dev,
                    "total_gb": round(total / 1024 ** 3, 1),
                    "used_gb": round(used / 1024 ** 3, 1),
                    "free_gb": round(free / 1024 ** 3, 1),
                    "used_pct": round(100.0 * used / total, 1) if total else 0})
    out.sort(key=lambda d: -d["total_gb"])
    return out


def _temps():
    out = []
    base = "/sys/class/thermal"
    try:
        for name in sorted(os.listdir(base)):
            if not name.startswith("thermal_zone"):
                continue
            t = _read(os.path.join(base, name, "type")).strip() or name
            raw = _read(os.path.join(base, name, "temp")).strip()
            if raw.lstrip("-").isdigit():
                out.append({"zone": t, "c": round(int(raw) / 1000.0, 1)})
    except Exception:
        pass
    return out


def _load():
    p = _read("/proc/loadavg").split()
    return {"load1": float(p[0]), "load5": float(p[1]), "load15": float(p[2])}


def _uptime():
    try:
        s = float(_read("/proc/uptime", "0").split()[0])
    except Exception:
        s = 0
    d = int(s // 86400)
    h = int((s % 86400) // 3600)
    m = int((s % 3600) // 60)
    return {"seconds": int(s), "text": "%d 天 %d 小时 %d 分" % (d, h, m)}


# ============================ 历史采样（图表用） ============================
# 图表要的是「过去一段时间的走势」，/api/overview 只有单点，所以在服务端留一个
# 轻量环形缓冲：后台线程每 HIST_INTERVAL 秒读一次 /proc，append 一个点。
# 720 点 × 5s = 60 分钟，内存占用可忽略（每点 ~5 个 float）。
HIST_INTERVAL = max(0.2, float(os.environ.get("NAS_ADMIN_HIST_INTERVAL", "5")))
HIST_MAX = max(12, int(os.environ.get("NAS_ADMIN_HIST_MAX", "720")))
_HIST = deque(maxlen=HIST_MAX)
_HIST_LOCK = threading.Lock()
_HIST_STARTED = False
_HIST_START_LOCK = threading.Lock()
_hist_prev = None


def _stat_snap():
    """读一次 /proc/stat 汇总行；口径与 _cpu_pct 一致（busy = total - idle - iowait）。"""
    for ln in _read("/proc/stat").splitlines():
        if ln.startswith("cpu "):
            v = list(map(int, ln.split()[1:])) + [0] * 10
            return {"total": sum(v[0:8]), "idle": v[3], "iowait": v[4]}
    return None


def _hist_sample():
    """采一个点：CPU 用「与上一采样点」的差值算（=这段时间的平均占用），内存直接读。
    第一个点没有参照物，跳过不写（否则会画出一个假 0%）。"""
    global _hist_prev
    cur = _stat_snap()
    if cur is None:
        return False
    prev, _hist_prev = _hist_prev, cur
    if prev is None:
        return False
    dt = cur["total"] - prev["total"]
    if dt <= 0:
        return False
    busy = dt - (cur["idle"] - prev["idle"]) - (cur["iowait"] - prev["iowait"])
    m = _mem()
    with _HIST_LOCK:
        _HIST.append({"t": int(time.time()),
                      "cpu": round(max(0.0, 100.0 * busy / dt), 1),
                      "mem_used": m["used_mb"], "mem_total": m["total_mb"],
                      "swap_used": m["swap_used_mb"]})
    return True


def _hist_loop():
    while True:
        try:
            _hist_sample()
        except Exception:
            pass                      # 采样失败绝不能把线程搞死（图表空一阵子没关系）
        time.sleep(HIST_INTERVAL)


def _hist_start():
    global _HIST_STARTED
    if _HIST_STARTED:
        return
    with _HIST_START_LOCK:
        if _HIST_STARTED:
            return
        _HIST_STARTED = True
        threading.Thread(target=_hist_loop, name="hist-sampler", daemon=True).start()


@app.route("/api/charts")
def charts():
    _hist_start()
    try:
        minutes = max(1, min(60, int(request.args.get("minutes", "15"))))
    except ValueError:
        minutes = 15
    cutoff = time.time() - minutes * 60
    with _HIST_LOCK:
        pts = [p for p in _HIST if p["t"] >= cutoff]
    return jsonify({"interval": HIST_INTERVAL, "minutes": minutes,
                    "now": int(time.time()), "points": pts})


@app.route("/api/overview")
def overview():
    _hist_start()          # 打开页面就开始攒历史，不用等用户点图表
    docker_err = ""
    container_running = container_total = 0
    rc, out, err = run(["docker", "ps", "-q"], timeout=10)
    if rc == 0:
        container_running = len([x for x in out.split() if x.strip()])
        rc2, out2, _ = run(["docker", "ps", "-aq"], timeout=10)
        if rc2 == 0:
            container_total = len([x for x in out2.split() if x.strip()])
    else:
        docker_err = (err or out or "docker 不可用").strip()[:200]
    ips = []
    rc3, out3, _ = run(["hostname", "-I"], timeout=5)
    if rc3 == 0:
        ips = out3.split()
    return jsonify({
        "host": {"name": socket.gethostname(),
                 "kernel": _read("/proc/version").split()[2] if _read("/proc/version") else "",
                 "ips": ips, "now": datetime.now().strftime("%Y-%m-%d %H:%M:%S")},
        "cpu": _cpu_pct(), "mem": _mem(), "load": _load(), "uptime": _uptime(),
        "temps": _temps(), "disks": _disks(),
        "containers": {"running": container_running, "total": container_total,
                       "error": docker_err},
    })


# ============================ 1b. AI 余额（DeepSeek）============================
# 余额要打外面的 API（api.deepseek.com/user/balance），**必须缓存**：总览每 5 秒
# 自动刷一次，绝不能每次都出去问。默认 5 分钟缓存（NAS_ADMIN_BAL_TTL 秒），
# 卡片上的 ↻ 按钮走 ?refresh=1 强制刷新。
# key 不配在本服务里：优先环境变量 NAS_ADMIN_DS_KEY，否则读作文批改生产 config
# 里的 deepseek 配置（key 只留一份，两边永远一致）。本接口在登录门之后，key 绝不回前端。
DS_TTL = max(30, int(os.environ.get("NAS_ADMIN_BAL_TTL", "300")))
_bal_lock = threading.Lock()
_bal_cache = {"data": None, "ts": 0.0}


def _ds_key():
    k = os.environ.get("NAS_ADMIN_DS_KEY", "").strip()
    if k:
        return k
    p = os.environ.get("NAS_ADMIN_DS_CFG") or os.path.join(
        FS_ROOT, "vol1/1000/项目/作文批改/data/config.json")
    try:
        cfg = json.load(open(p, encoding="utf-8"))
    except Exception:
        return ""
    for prof in cfg.get("profiles") or []:
        if "deepseek" in (prof.get("base_url") or "").lower():
            key = (prof.get("api_key") or "").strip()
            if key:
                return key
    return ""


def _fetch_balance(key):
    """真去 DeepSeek 查一次余额；网络/解析失败抛异常，由调用方决定回旧值还是报错。"""
    req = urllib.request.Request(
        "https://api.deepseek.com/user/balance",
        headers={"Authorization": "Bearer " + key})
    with urllib.request.urlopen(req, timeout=10) as r:
        j = json.loads(r.read().decode("utf-8"))
    infos = j.get("balance_infos") or []
    cny = next((x for x in infos if (x.get("currency") or "").upper() == "CNY"),
               infos[0] if infos else {})
    return {"ok": True,
            "is_available": bool(j.get("is_available")),
            "total": float(cny.get("total_balance") or 0),
            "granted": float(cny.get("granted_balance") or 0),
            "topped_up": float(cny.get("topped_up_balance") or 0),
            "currency": (cny.get("currency") or "CNY").upper(),
            "ts": int(time.time())}


@app.route("/api/balance")
def balance():
    force = request.args.get("refresh") == "1"
    now = time.time()
    with _bal_lock:
        cached, cts = _bal_cache["data"], _bal_cache["ts"]
    if not force and cached and now - cts < DS_TTL:
        return jsonify(dict(cached, cached=True))
    key = _ds_key()
    if not key:
        return jsonify({"ok": False, "error": "没找到 DeepSeek key（NAS_ADMIN_DS_KEY 或作文批改 config）",
                        "ts": int(now)})
    try:
        data = _fetch_balance(key)
    except Exception as e:
        # 刷新失败不清旧值：有旧余额就照常显示 + 标注 stale（网络抖动不该把卡片打空）
        if cached:
            return jsonify(dict(cached, cached=True, stale=True, err=str(e)[:200]))
        return jsonify({"ok": False, "error": str(e)[:200], "ts": int(now)})
    with _bal_lock:
        _bal_cache["data"], _bal_cache["ts"] = data, time.time()
    return jsonify(dict(data, cached=False))


# ============================ 2. 容器 ============================
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


def _need_name(name):
    if not NAME_RE.match(str(name or "")):
        return "容器名不合法"
    return None


@app.route("/api/containers")
def containers():
    stats = request.args.get("stats") == "1"
    rc, out, err = run(["docker", "ps", "-a", "--format",
                        "{{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}"], timeout=15)
    if rc != 0:
        return jsonify({"error": "docker_error", "msg": (err or out).strip()[:300]}), 502
    items = []
    for ln in out.splitlines():
        p = (ln.split("\t") + ["", "", ""])[:4]
        if p[0]:
            items.append({"name": p[0], "image": p[1], "status": p[2], "ports": p[3],
                          "up": p[2].lower().startswith("up")})
    stat_map = {}
    if stats and items:
        rc2, out2, _ = run(["docker", "stats", "--no-stream", "--format",
                            "{{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}"], timeout=25)
        if rc2 == 0:
            for ln in out2.splitlines():
                q = ln.split("\t")
                if len(q) >= 3:
                    stat_map[q[0]] = {"cpu": q[1], "mem": q[2]}
    for it in items:
        it.update(stat_map.get(it["name"], {}))
    items.sort(key=lambda x: (not x["up"], x["name"]))
    return jsonify({"containers": items})


@app.route("/api/container", methods=["POST"])
def container_action():
    b = request.get_json(silent=True) or {}
    name, action = str(b.get("name", "")), str(b.get("action", ""))
    if e := _need_name(name):
        return jsonify({"error": "bad_name", "msg": e}), 400
    if action in ("start", "stop", "restart"):
        rc, out, err = run(["docker", action, name], timeout=60)
        audit("container", action, name, rc=rc)
        if rc == 0:
            return jsonify({"ok": True, "msg": out.strip()[-300:]})
        return jsonify({"error": "docker_error", "msg": (err or out).strip()[:300]}), 502
    if action == "rm":
        if str(b.get("confirm", "")) != name:
            return jsonify({"error": "need_confirm",
                            "msg": "删除容器要输入容器名「%s」确认" % name}), 400
        rc, out, err = run(["docker", "rm", "-f", name], timeout=60)
        audit("container", "rm", name, rc=rc)
        if rc == 0:
            return jsonify({"ok": True, "msg": out.strip()[-300:]})
        return jsonify({"error": "docker_error", "msg": (err or out).strip()[:300]}), 502
    return jsonify({"error": "bad_action", "msg": "不支持的动作: %s" % action}), 400


@app.route("/api/container/logs")
def container_logs():
    name = str(request.args.get("name", ""))
    if e := _need_name(name):
        return jsonify({"error": "bad_name", "msg": e}), 400
    try:
        tail = max(10, min(1000, int(request.args.get("tail", 200))))
    except ValueError:
        tail = 200
    rc, out, err = run(["docker", "logs", "--tail", str(tail), name], timeout=20)
    text = (out + ("\n--- stderr ---\n" + err if err.strip() else ""))
    if rc != 0 and not text.strip():
        text = (err or out).strip()
    audit("container", "logs", "%s(tail=%d)" % (name, tail), rc=rc)
    return jsonify({"name": name, "tail": tail, "text": text[-EXEC_MAX_OUT:]})


# ============================ 3. 文件 ============================
def _safe(path):
    """返回 (realpath, err)。FS_ROOT 是 '/' 时整个文件系统都可管（这就是"高权限"）。"""
    p = str(path or "")
    if not p.startswith("/"):
        p = os.path.join("/", p)
    real = os.path.realpath(p)
    root = os.path.realpath(FS_ROOT)
    if root != "/" and not (real == root or real.startswith(root + os.sep)):
        return None, "越界：不允许访问 %s" % real
    # 注：FS_ROOT 自己必须能列/能进（测试和生产都要），"不能删根"由 fs_delete 单独挡
    return real, None


@app.route("/api/fs")
def fs_list():
    path, err = _safe(request.args.get("path", "/"))
    if err:
        return jsonify({"error": "bad_path", "msg": err}), 400
    if not os.path.isdir(path):
        return jsonify({"error": "not_dir", "msg": "不是目录: %s" % path}), 400
    entries = []
    try:
        with os.scandir(path) as it:
            for e in it:
                try:
                    st = e.stat(follow_symlinks=False)
                    is_dir = e.is_dir()
                    link = os.readlink(e.path) if e.is_symlink() else ""
                    entries.append({
                        "name": e.name, "dir": bool(is_dir),
                        "size": st.st_size, "mtime": int(st.st_mtime),
                        "mode": oct(st.st_mode)[-4:],
                        "symlink": link,
                    })
                except Exception as ex:
                    entries.append({"name": e.name, "dir": False, "size": -1,
                                    "mtime": 0, "mode": "????", "error": str(ex)[:80]})
    except PermissionError:
        return jsonify({"error": "no_perm", "msg": "没有权限读这个目录"}), 403
    except FileNotFoundError:
        return jsonify({"error": "no_such", "msg": "目录不存在"}), 404
    entries.sort(key=lambda x: (not x["dir"], x["name"].lower()))
    parts = [p for p in path.split("/") if p]
    crumbs, cur = [], ""
    for p in parts:
        cur += "/" + p
        crumbs.append({"name": p, "path": cur})
    # FS 根目录没有上一级（否则"上级"会越界 400，前端看着像坏了）
    root_real = os.path.realpath(FS_ROOT)
    parent = path if path == root_real else (os.path.dirname(path.rstrip("/")) or "/")
    return jsonify({"path": path, "parent": parent,
                    "crumbs": crumbs, "entries": entries[:1000],
                    "truncated": len(entries) > 1000})


@app.route("/api/fs/read")
def fs_read():
    path, err = _safe(request.args.get("path", ""))
    if err:
        return jsonify({"error": "bad_path", "msg": err}), 400
    if not os.path.isfile(path):
        return jsonify({"error": "not_file", "msg": "不是文件: %s" % path}), 400
    try:
        size = os.path.getsize(path)
        if size > READ_MAX:
            return jsonify({"error": "too_big", "msg": "文件 %d 字节，超过 1MB 编辑上限" % size}), 400
        with open(path, "rb") as f:
            raw = f.read(READ_MAX)
        if b"\x00" in raw[:8192]:
            return jsonify({"error": "binary", "msg": "二进制文件，不能在线编辑"}), 400
        return jsonify({"path": path, "content": raw.decode("utf-8", errors="replace"),
                        "size": size})
    except PermissionError:
        return jsonify({"error": "no_perm", "msg": "没有权限读"}), 403
    except FileNotFoundError:
        return jsonify({"error": "no_such", "msg": "文件不存在"}), 404


@app.route("/api/fs/save", methods=["POST"])
def fs_save():
    b = request.get_json(silent=True) or {}
    path, err = _safe(b.get("path", ""))
    if err:
        return jsonify({"error": "bad_path", "msg": err}), 400
    content = b.get("content")
    if not isinstance(content, str) or len(content.encode()) > READ_MAX:
        return jsonify({"error": "bad_content", "msg": "内容为空或超过 1MB"}), 400
    try:
        os.makedirs(os.path.dirname(path) or "/", exist_ok=True)
        tmp = path + ".nasadmin.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)                      # 原子替换，写坏不会留半截
        audit("fs", "save", path)
        return jsonify({"ok": True, "bytes": len(content.encode())})
    except PermissionError:
        return jsonify({"error": "no_perm", "msg": "没有权限写"}), 403
    except Exception as e:
        return jsonify({"error": "write_error", "msg": str(e)[:200]}), 500


@app.route("/api/fs/mkdir", methods=["POST"])
def fs_mkdir():
    b = request.get_json(silent=True) or {}
    path, err = _safe(b.get("path", ""))
    if err:
        return jsonify({"error": "bad_path", "msg": err}), 400
    try:
        os.makedirs(path, exist_ok=False)
        audit("fs", "mkdir", path)
        return jsonify({"ok": True, "path": path})
    except FileExistsError:
        return jsonify({"error": "exists", "msg": "已经存在"}), 409
    except Exception as e:
        return jsonify({"error": "mkdir_error", "msg": str(e)[:200]}), 500


@app.route("/api/fs/upload", methods=["POST"])
def fs_upload():
    path, err = _safe(request.form.get("path", ""))
    if err:
        return jsonify({"error": "bad_path", "msg": err}), 400
    if not os.path.isdir(path):
        return jsonify({"error": "not_dir", "msg": "目标不是目录"}), 400
    files = request.files.getlist("files")
    saved, errs = [], []
    for f in files:
        if not f or not f.filename:
            continue
        name = os.path.basename(f.filename).replace("\x00", "")
        dest, e2 = _safe(os.path.join(path, name))
        if e2:
            errs.append(name + ": " + e2)
            continue
        try:
            f.save(dest)
            saved.append(name)
        except Exception as ex:
            errs.append("%s: %s" % (name, str(ex)[:80]))
    audit("fs", "upload", "%s <- %d 个" % (path, len(saved)))
    return jsonify({"ok": bool(saved), "saved": saved, "errors": errs})


@app.route("/api/fs/download")
def fs_download():
    path, err = _safe(request.args.get("path", ""))
    if err:
        return jsonify({"error": "bad_path", "msg": err}), 400
    if not os.path.isfile(path):
        return jsonify({"error": "not_file", "msg": "不是文件"}), 400
    audit("fs", "download", path)
    return send_file(path, as_attachment=True, download_name=os.path.basename(path))


# ---- 文件夹打包下载 ----
# 规矩（照拷 NAS 会出事）：
#   1) 打包到临时目录再发，不占内存（大目录也能发）；
#   2) **不跟随符号链接**（否则 /vol1 里一个软链到 / 就能把整个盘打包出去）；
#   3) 有总量上限（默认 4GB），超了直接拒绝并说明，别把 NAS 磁盘塞满；
#   4) 跳过打包文件自己 / 临时文件；发完删临时包。
FOLDER_ZIP_MAX = int(os.environ.get("NAS_ADMIN_ZIP_MAX_MB", "1024")) * 1024 * 1024   # 默认 1GB（NAS 磁盘小）
FOLDER_MAX_FILES = int(os.environ.get("NAS_ADMIN_ZIP_MAX_FILES", "200000"))


@app.route("/api/fs/download_folder")
def fs_download_folder():
    """把一个目录打包成 zip 流式返回（文件夹不能单发，必须打包）。"""
    path, err = _safe(request.args.get("path", ""))
    if err:
        return jsonify({"error": "bad_path", "msg": err}), 400
    if not os.path.isdir(path):
        return jsonify({"error": "not_dir", "msg": "不是目录: %s" % path}), 400
    if path == os.path.realpath("/"):
        return jsonify({"error": "refused", "msg": "拒绝打包根目录（先进入具体目录再下载）"}), 400
    # ⚠️ 容器宿主模式：FS_ROOT=/host 就是整机根。允许打包它 = 边扫边压 1GB，
    #    实测会把请求拖到超时、把磁盘写满（2026-10-18 真机踩到）。
    #    规矩：FS 根自身、以及「比 FS 根只多一层」的顶层盘（/host/vol1 这种）一律拒绝。
    #    真机上 /host/vol1 = 4.6TB 的存储池，压它是灾难；具体项目目录照常可压。
    rel = os.path.relpath(path, os.path.realpath(FS_ROOT))
    if rel == ".":
        return jsonify({"error": "too_broad",
                        "msg": "这里就是文件根，请先进入具体项目目录再打包"}), 400
    depth = len([x for x in rel.split(os.sep) if x])
    if FS_ROOT != "/" and depth == 1 and os.path.basename(path) in ("vol1", "vol2", "vol3", "vol4",
                                                                    "data", "mnt", "var", "usr",
                                                                    "etc", "root", "home"):
        return jsonify({"error": "too_broad",
                        "msg": "这个目录太大（整个存储池/系统盘），请先进入具体项目目录再打包"}), 400

    base = os.path.basename(path.rstrip("/")) or "root"
    tmp_fd, tmp_path = tempfile.mkstemp(prefix="nasadmin_dl_", suffix=".zip")
    os.close(tmp_fd)
    total = 0
    nfiles = 0
    skipped = []
    try:
        with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
            for root, dirs, files in os.walk(path, followlinks=False):
                # 不让 os.walk 钻进符号链接目录
                dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(root, d))]
                for fn in files:
                    full = os.path.join(root, fn)
                    if os.path.islink(full):
                        skipped.append(full)
                        continue
                    if full == tmp_path:
                        continue
                    try:
                        sz = os.path.getsize(full)
                    except OSError:
                        skipped.append(full)
                        continue
                    total += sz
                    nfiles += 1
                    if total > FOLDER_ZIP_MAX:
                        raise _ZipTooBig()
                    if nfiles > FOLDER_MAX_FILES:
                        raise _ZipTooBig()
                    try:
                        zf.write(full, os.path.relpath(full, path))
                    except (PermissionError, OSError):
                        skipped.append(full)       # 单个文件没权限：跳过并继续，不整包失败
    except _ZipTooBig:
        _unlink_quiet(tmp_path)
        return jsonify({"error": "too_big",
                        "msg": "目录太大（上限 %d MB / %d 个文件），请选更小的目录"
                               % (FOLDER_ZIP_MAX // 1024 // 1024, FOLDER_MAX_FILES)}), 413
    except Exception as e:
        _unlink_quiet(tmp_path)
        return jsonify({"error": "zip_error", "msg": str(e)[:200]}), 500

    audit("fs", "download_folder", "%s（%d 个文件，%d 字节）" % (path, nfiles, total))
    # ⚠️ 自己包一个 FileWrapper，**发完在 finally 里删临时包**。
    #    早先只靠 resp.call_on_close()：Flask 在某些情况下（客户端中途断开、测试里直接
    #    读 .data、被前置代理接管）根本不会触发 → /tmp 里堆了一串 nasadmin_dl_*.zip（真机踩到）。
    #    wrapper 的 finally 由 WSGI 服务器在响应结束时调用，是最靠得住的删除时机。
    download_name = base + ".zip"

    from werkzeug.wsgi import FileWrapper
    try:
        fobj = open(tmp_path, "rb")
    except OSError as e:
        _unlink_quiet(tmp_path)
        return jsonify({"error": "zip_error", "msg": str(e)[:200]}), 500

    class _DeletingFileWrapper(FileWrapper):
        """发完自动删临时 zip：close() 由 WSGI 服务器在响应结束时调用，
        比 resp.call_on_close 更靠得住（后者在客户端中途断开等情况下不一定触发）。"""
        def close(self):
            try:
                super().close()
            finally:
                _unlink_quiet(tmp_path)

    resp = Response(_DeletingFileWrapper(fobj), mimetype="application/zip")
    resp.headers["Content-Disposition"] = _attach_header(download_name)
    resp.headers["Content-Length"] = str(os.path.getsize(tmp_path))
    resp.headers["Cache-Control"] = "no-store"
    resp.call_on_close(lambda: _unlink_quiet(tmp_path))    # 双保险
    return resp


def _attach_header(filename):
    """Content-Disposition：中文名走 RFC 5987，另给 ASCII 兜底。"""
    from urllib.parse import quote
    ascii_fallback = filename.encode("ascii", "replace").decode("ascii").replace('"', "_")
    return "attachment; filename=\"%s\"; filename*=UTF-8''%s" % (ascii_fallback, quote(filename))


def _sweep_zip_leftovers(max_age=3600):
    """启动时清掉上次遗留的临时 zip（进程被 kill 时 wrapper 来不及删的那部分）。
    只清自己前缀的、且超过 max_age 秒的，别动别人正在发的包。"""
    try:
        d = tempfile.gettempdir()
        now = time.time()
        for n in os.listdir(d):
            if not (n.startswith("nasadmin_dl_") and n.endswith(".zip")):
                continue
            p = os.path.join(d, n)
            try:
                if now - os.path.getmtime(p) > max_age:
                    os.unlink(p)
            except OSError:
                pass
    except Exception:
        pass


_sweep_zip_leftovers()


class _ZipTooBig(Exception):
    pass


def _unlink_quiet(p):
    try:
        os.unlink(p)
    except OSError:
        pass


@app.route("/api/fs/delete", methods=["POST"])
def fs_delete():
    b = request.get_json(silent=True) or {}
    raw = str(b.get("path", ""))
    # 删根永远拒绝，且放在越界检查**之前**（错误码稳定是 refused，测试与前端只认这一个）
    if raw in ("/", "", os.path.realpath(FS_ROOT)):
        return jsonify({"error": "refused", "msg": "拒绝删除根目录"}), 400
    path, err = _safe(raw)
    if err:
        return jsonify({"error": "bad_path", "msg": err}), 400
    if path == "/" or path == os.path.realpath(FS_ROOT):
        return jsonify({"error": "refused", "msg": "拒绝删除根目录"}), 400
    if not os.path.exists(path):
        return jsonify({"error": "no_such", "msg": "不存在"}), 404
    is_dir = os.path.isdir(path) and not os.path.islink(path)
    need = path if is_dir else os.path.basename(path)
    if str(b.get("confirm", "")) != need:
        return jsonify({"error": "need_confirm",
                        "msg": ("删除目录要输入完整路径" if is_dir else "删除文件要输入文件名")
                               + "「%s」确认" % need}), 400
    try:
        if is_dir:
            import shutil
            shutil.rmtree(path)
        else:
            os.remove(path)
        audit("fs", "delete", path)
        return jsonify({"ok": True, "path": path})
    except PermissionError:
        return jsonify({"error": "no_perm", "msg": "没有权限删除"}), 403
    except Exception as e:
        return jsonify({"error": "delete_error", "msg": str(e)[:200]}), 500


# ============================ 4. 命令执行 ============================
@app.route("/api/exec", methods=["POST"])
def exec_cmd():
    cmd = str((request.get_json(silent=True) or {}).get("cmd", "")).strip()
    if not cmd:
        return jsonify({"error": "empty", "msg": "命令不能为空"}), 400
    if len(cmd) > 4000:
        return jsonify({"error": "too_long", "msg": "命令太长"}), 400
    rc, out, err, ms = run_shell(cmd)
    audit("exec", "shell", cmd, rc=rc)
    return jsonify({"cmd": cmd, "rc": rc, "ms": ms,
                    "out": out[:EXEC_MAX_OUT], "err": err[:EXEC_MAX_OUT],
                    "truncated": len(out) > EXEC_MAX_OUT or len(err) > EXEC_MAX_OUT})


@app.route("/api/audit")
def audit_list():
    try:
        n = max(10, min(500, int(request.args.get("tail", 100))))
    except ValueError:
        n = 100
    rows = []
    try:
        with open(AUDIT_PATH, "r", encoding="utf-8") as f:
            lines = f.readlines()
        for ln in lines[-n:]:
            try:
                rows.append(json.loads(ln))
            except Exception:
                pass
    except FileNotFoundError:
        pass
    return jsonify({"rows": rows, "total": len(rows)})


# ============================ 5. 危险区 ============================
CONFIRM_TOKENS = {"reboot": "重启", "shutdown": "关机", "docker_prune": "清理"}
SYSTEM_ACTIONS = set(CONFIRM_TOKENS)


@app.route("/api/system", methods=["POST"])
def system_action():
    b = request.get_json(silent=True) or {}
    action = str(b.get("action", ""))
    if action not in SYSTEM_ACTIONS:
        return jsonify({"error": "bad_action", "msg": "不支持的动作"}), 400
    token = CONFIRM_TOKENS[action]
    if str(b.get("confirm", "")).strip() != token:
        return jsonify({"error": "need_confirm",
                        "msg": "这个动作要输入「%s」确认" % token}), 400
    if action in ("reboot", "shutdown"):
        audit("system", action, "下发", ip=client_ip())
        spawn_detached(["/bin/sh", "-c",
                        "sleep 3; systemctl %s" % ("reboot" if action == "reboot" else "poweroff")])
        return jsonify({"ok": True, "msg": "已下发：%s —— 3 秒后执行，NAS 会在 1 分钟内%s"
                       % (action, "重启" if action == "reboot" else "关机")})
    # docker_prune：清悬空镜像/停止的容器/无用网络（**不带 --volumes**，不动数据卷）
    rc, out, err = run(["docker", "system", "prune", "-f"], timeout=300)
    audit("system", "docker_prune", "", rc=rc)
    if rc == 0:
        return jsonify({"ok": True, "msg": "清理完成", "out": out[-4000:]})
    return jsonify({"error": "prune_error", "msg": (err or out).strip()[:300]}), 502


@app.errorhandler(404)
def _404(e):
    if request.path.startswith("/api/"):
        return jsonify({"error": "not_found"}), 404
    return "not found", 404


if __name__ == "__main__":
    os.makedirs(DATA_DIR, exist_ok=True)
    app.run(host=BIND, port=PORT, threaded=True)
