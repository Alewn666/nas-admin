#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""nas-admin 后端测试 —— **零真机副作用**：
   ① 子进程三出口（run / run_shell / spawn_detached）全部打桩
   ② 文件管理指到临时目录（NAS_ADMIN_FS_ROOT=tmp）
   ③ 数据目录指到临时目录（NAS_ADMIN_DATA=tmp），审计日志不碰生产

跑法：python3 test_admin.py
"""
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp(prefix="nasadmin_test_")
FS = os.path.join(TMP, "fs")
DATA = os.path.join(TMP, "data")
os.makedirs(FS, exist_ok=True)
os.makedirs(DATA, exist_ok=True)

os.environ["NAS_ADMIN_PW"] = "testpw"
os.environ["NAS_ADMIN_DATA"] = DATA
os.environ["NAS_ADMIN_FS_ROOT"] = FS
os.environ["NAS_ADMIN_SECRET"] = "fixed-test-secret"
os.environ["NAS_ADMIN_HIST_INTERVAL"] = "3600"   # 图表采样线程别在测试里乱插点（自己手动驱动）

PASS = FAIL = 0
CASES = []


def load(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, "app.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


MOD = load("nasadmin_a")

# ---------------- 子进程打桩 ----------------
DOCKER_PS_A = "blog\tblog:latest\tUp 3 days\t0.0.0.0:80->8080/tcp\n" \
              "essay-revise\te ssay-revise:latest\tUp 5 hours\t0.0.0.0:8083->8083/tcp\n".replace("e ssay", "essay")
DOCKER_STATS = "blog\t0.15%\t50MiB / 2.011GiB\ne ssay\t1.20%\t90MiB / 2.011GiB".replace("e ssay", "essay")


class FakeRun:
    """按完整 argv 分发的假 docker；所有调用都记录在 .calls 里。
    ⚠️ 别用 " ".join(cmd[:3]) 匹配 —— `docker ps -a --format ...` 前三段是
       `docker ps -a`，永远匹配不到 `docker ps -a --format`（踩过一次）。"""
    def __init__(self):
        self.calls = []

    def __call__(self, cmd, timeout=20):
        self.calls.append(list(cmd))
        j = " ".join(cmd)
        if cmd[0] == "hostname":
            return 0, "192.168.1.100 \n", ""
        if cmd[:2] == ["docker", "ps"] and cmd[-1] in ("-q", "-aq"):
            return 0, "id1\nid2\n", ""
        if j.startswith("docker ps -a --format"):
            return 0, DOCKER_PS_A, ""
        if j.startswith("docker stats --no-stream"):
            return 0, DOCKER_STATS, ""
        if j.startswith("docker logs"):
            return 0, "out-line-1\nout-line-2\n", "err-line\n"
        if j.startswith("docker system prune"):
            return 0, "Total reclaimed space: 1.2GB\n", ""
        if cmd[0] == "docker" and cmd[1] in ("start", "stop", "restart", "rm"):
            return 0, cmd[1] + " " + cmd[-1], ""
        return 0, "", ""


class FakeShell:
    def __init__(self):
        self.calls = []

    def __call__(self, cmd, timeout=30):
        self.calls.append((cmd, timeout))
        if cmd == "echo hi":
            return 0, "hi\n", "", 5
        if cmd == "failcmd":
            return 1, "", "boom", 3
        return 0, "ran: " + cmd + "\n", "", 7


class FakeSpawn:
    def __init__(self):
        self.calls = []

    def __call__(self, cmd):
        self.calls.append(list(cmd))
        return True


FAKE_RUN, FAKE_SHELL, FAKE_SPAWN = FakeRun(), FakeShell(), FakeSpawn()
MOD.run = FAKE_RUN
MOD.run_shell = FAKE_SHELL
MOD.spawn_detached = FAKE_SPAWN


def ck(cond, name, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ✔ " + name)
    else:
        FAIL += 1
        print("  ✘ " + name + ("　" + str(extra)[:160] if extra else ""))
    CASES.append((bool(cond), name))


def fresh_client():
    MOD._fails.clear()
    return MOD.app.test_client()


def login(c, pw="testpw"):
    return c.post("/api/login", json={"password": pw})


def authed_client():
    c = fresh_client()
    r = login(c)
    assert r.status_code == 200, r.data
    return c


print("== 1. 门禁与健康 ==")
c = fresh_client()
r = c.get("/api/health")
ck(r.status_code == 200 and r.get_json()["ok"] is True and r.get_json()["configured"] is True,
   "健康检查 ok 且 configured")
r = c.get("/api/overview")
ck(r.status_code == 401 and r.get_json()["error"] == "need_login", "未登录的接口一律 401")
r = c.get("/")
ck(r.status_code == 200 and b"NAS" in r.data, "页面 200（不登录也能看壳）")
r = login(c, "wrong")
ck(r.status_code == 401, "错口令 401")
for _ in range(4):
    login(c, "wrong")
r = login(c, "wrong")
ck(r.status_code == 429 and r.get_json()["error"] == "locked", "连错 5 次锁 30 秒（429）")
c = fresh_client()
ck(login(c, "testpw").status_code == 200, "对口令 200")
ck(login(c, "testpw").status_code == 200 and c.get("/api/health").status_code == 200,
   "成功登录后新会话可用")

print("== 2. 总览 ==")
c = authed_client()
j = c.get("/api/overview").get_json()
ck(all(k in j for k in ("host", "cpu", "mem", "load", "uptime", "temps", "disks", "containers")),
   "总览字段齐全", list(j))
ck(isinstance(j["disks"], list) and len(j["disks"]) >= 1, "读到真实磁盘（statvfs）", j["disks"])
ck(j["containers"]["running"] == 2 and j["containers"]["total"] == 2, "容器概况来自打桩 docker ps",
   j["containers"])
ck(j["host"]["ips"] == ["192.168.1.100"], "IP 来自 hostname -I 打桩", j["host"])
ck(isinstance(j["cpu"]["total"], (int, float)) and j["cpu"]["total"] >= 0, "CPU 占用是真采样值",
   j["cpu"]["total"])
ck(j["mem"]["total_mb"] > 0 and j["mem"]["swap_total_mb"] >= 0, "内存/swap 读 /proc", j["mem"])

print("== 2b. 图表历史（/api/charts） ==")
c = authed_client()
j = c.get("/api/charts").get_json()
ck(set(("interval", "minutes", "now", "points")).issubset(j), "图表字段齐全", list(j))
ck(isinstance(j["points"], list) and j["interval"] > 0, "返回采样点数组", j["interval"])
# 直接驱动采样器：第一点只记参照（否则会画一个假 0%），第二点才落库
MOD._hist_prev = None
with MOD._HIST_LOCK:
    MOD._HIST.clear()
ck(MOD._hist_sample() is False, "第一个采样点只记参照、不写假 0%")
time.sleep(0.35)
ck(MOD._hist_sample() is True, "第二个采样点真的落进环形缓冲")
with MOD._HIST_LOCK:
    pts = list(MOD._HIST)
ck(len(pts) == 1 and 0 <= pts[0]["cpu"] <= 100 and pts[0]["mem_total"] > 0,
   "采样点数值合理（0~100%，内存有真值）", pts[-1] if pts else None)
j = c.get("/api/charts?minutes=15").get_json()
ck(len(j["points"]) >= 1 and "cpu" in j["points"][-1], "接口读到刚采的点")
with MOD._HIST_LOCK:
    MOD._HIST.append({"t": int(time.time()) - 9999, "cpu": 1.0,
                      "mem_used": 1, "mem_total": 1, "swap_used": 0})
j = c.get("/api/charts?minutes=1").get_json()
ck(all(p["t"] >= int(time.time()) - 61 for p in j["points"]), "minutes 过滤掉窗口外的老点")
ck(c.get("/api/charts?minutes=abc").status_code == 200, "非法 minutes 回退默认而不是 500")
ck(MOD.app.test_client().get("/api/charts").status_code == 401, "未登录读不到图表（走同一道门）")

print("== 3. 容器管理 ==")
FAKE_RUN.calls.clear()
j = c.get("/api/containers").get_json()
names = [x["name"] for x in j["containers"]]
ck(names == ["blog", "essay-revise"], "容器列表解析（运行中在前）", names)
ck(j["containers"][0]["up"] is True and "Up 3 days" in j["containers"][0]["status"], "状态字段")
FAKE_RUN.calls.clear()
j = c.get("/api/containers?stats=1").get_json()
ck("cpu" in j["containers"][0] and j["containers"][0]["cpu"] == "0.15%", "带资源占用",
   j["containers"][0])
ck(any(x[:2] == ["docker", "stats"] for x in FAKE_RUN.calls), "stats=1 才调 docker stats")
FAKE_RUN.calls.clear()
r = c.post("/api/container", json={"name": "blog", "action": "restart"})
ck(r.status_code == 200 and r.get_json()["ok"] and ["docker", "restart", "blog"] in FAKE_RUN.calls,
   "重启容器落到 docker restart")
r = c.post("/api/container", json={"name": "blog; rm -rf /", "action": "rm"})
ck(r.status_code == 400 and r.get_json()["error"] == "bad_name", "容器名注入被挡（非白名单字符）")
r = c.post("/api/container", json={"name": "blog", "action": "rm", "confirm": "nope"})
ck(r.status_code == 400 and r.get_json()["error"] == "need_confirm", "删容器不确认 → 400")
r = c.post("/api/container", json={"name": "blog", "action": "rm", "confirm": "blog"})
ck(r.status_code == 200 and r.get_json()["ok"], "输对容器名才真删")
r = c.post("/api/container", json={"name": "blog", "action": "explode"})
ck(r.status_code == 400 and r.get_json()["error"] == "bad_action", "非法动作 400")
j = c.get("/api/container/logs?name=blog&tail=50").get_json()
ck("out-line-1" in j["text"] and "err-line" in j["text"], "日志含 stdout+stderr", j["text"][:60])

print("== 4. 文件管理（FS_ROOT 指到临时目录） ==")
os.makedirs(os.path.join(FS, "proj"), exist_ok=True)
with open(os.path.join(FS, "proj", "a.txt"), "w", encoding="utf-8") as f:
    f.write("hello 原子")
with open(os.path.join(FS, "bin.dat"), "wb") as f:
    f.write(b"\x00\x01\x02binary")
j = c.get("/api/fs?path=" + FS).get_json()
names = [e["name"] for e in j["entries"]]
ck("proj" in names and "bin.dat" in names and "a.txt" not in names, "列目录", names)
ck(j["crumbs"], "面包屑路径", j["crumbs"])
r = c.get("/api/fs?path=/etc")
ck(r.status_code == 400 and r.get_json()["error"] == "bad_path", "越界访问 FS_ROOT 外 → 400")
r = c.get("/api/fs?path=/etc/../etc")
ck(r.status_code == 400, "FS_ROOT 外的路径穿越也被挡")
j = c.get("/api/fs/read?path=" + os.path.join(FS, "proj", "a.txt")).get_json()
ck(j["content"] == "hello 原子", "读文本", j["content"])
r = c.get("/api/fs/read?path=" + os.path.join(FS, "bin.dat"))
ck(r.status_code == 400 and r.get_json()["error"] == "binary", "二进制文件拒绝在线编辑")
NEW = os.path.join(FS, "proj", "a.txt")
r = c.post("/api/fs/save", json={"path": NEW, "content": "改过的内容\n第二行"})
ck(r.status_code == 200 and r.get_json()["ok"], "保存成功")
ck(open(NEW, encoding="utf-8").read() == "改过的内容\n第二行", "保存内容真的落盘")
ck(not os.path.exists(NEW + ".nasadmin.tmp"), "原子写不留 .tmp")
r = c.post("/api/fs/save", json={"path": "/etc/passwd", "content": "x"})
ck(r.status_code == 400, "写越界路径被挡")
r = c.post("/api/fs/mkdir", json={"path": os.path.join(FS, "newdir")})
ck(r.status_code == 200 and os.path.isdir(os.path.join(FS, "newdir")), "建目录成功")
r = c.post("/api/fs/mkdir", json={"path": os.path.join(FS, "newdir")})
ck(r.status_code == 409, "重复建目录 409")
data = {"path": FS, "files": [(io.BytesIO(b"upload-data"), "up.txt")]}
r = c.post("/api/fs/upload", data=data, content_type="multipart/form-data")
ck(r.status_code == 200 and os.path.exists(os.path.join(FS, "up.txt")) and
   open(os.path.join(FS, "up.txt"), "rb").read() == b"upload-data", "上传落盘")
r = c.get("/api/fs/download?path=" + os.path.join(FS, "up.txt"))
ck(r.status_code == 200 and r.data == b"upload-data" and
   "attachment" in r.headers.get("Content-Disposition", ""), "下载带 attachment 头")
r = c.post("/api/fs/delete", json={"path": os.path.join(FS, "up.txt"), "confirm": "wrong"})
ck(r.status_code == 400 and r.get_json()["error"] == "need_confirm", "删文件要输文件名")
r = c.post("/api/fs/delete", json={"path": os.path.join(FS, "up.txt"), "confirm": "up.txt"})
ck(r.status_code == 200 and not os.path.exists(os.path.join(FS, "up.txt")), "文件名对了才删")
r = c.post("/api/fs/delete", json={"path": os.path.join(FS, "newdir"), "confirm": "newdir"})
ck(r.status_code == 400, "删**目录**要完整路径（只给名字不算）")
r = c.post("/api/fs/delete", json={"path": os.path.join(FS, "newdir"),
                                   "confirm": os.path.join(FS, "newdir")})
ck(r.status_code == 200 and not os.path.exists(os.path.join(FS, "newdir")), "完整路径确认后删掉")
r = c.post("/api/fs/delete", json={"path": "/", "confirm": "/"})
ck(r.status_code == 400 and r.get_json()["error"] == "refused", "拒绝删除根")

print("== 5. 命令执行 ==")
FAKE_SHELL.calls.clear()
j = c.post("/api/exec", json={"cmd": "echo hi"}).get_json()
ck(j["rc"] == 0 and j["out"] == "hi\n" and j["ms"] == 5, "命令输出回显", j)
ck(FAKE_SHELL.calls and FAKE_SHELL.calls[0][0] == "echo hi", "走 run_shell（打桩收到）")
r = c.post("/api/exec", json={"cmd": ""})
ck(r.status_code == 400, "空命令 400")
j = c.post("/api/exec", json={"cmd": "failcmd"}).get_json()
ck(j["rc"] == 1 and j["err"] == "boom", "非零返回码原样带回来")
rows = c.get("/api/audit?tail=20").get_json()["rows"]
ck(any(x["kind"] == "exec" and x["action"] == "shell" for x in rows), "命令进了审计日志", rows[-1])

print("== 6. 危险区（确认词服务端校验） ==")
FAKE_SPAWN.calls.clear()
r = c.post("/api/system", json={"action": "reboot", "confirm": "reboot"})
ck(r.status_code == 400 and r.get_json()["error"] == "need_confirm", "重启必须输中文「重启」")
r = c.post("/api/system", json={"action": "reboot", "confirm": "重启"})
ck(r.status_code == 200 and FAKE_SPAWN.calls and "reboot" in FAKE_SPAWN.calls[-1][-1],
   "确认后下发重启（后台脱离进程）", FAKE_SPAWN.calls)
r = c.post("/api/system", json={"action": "shutdown", "confirm": "关机"})
ck(r.status_code == 200 and "poweroff" in FAKE_SPAWN.calls[-1][-1], "关机下发 poweroff")
FAKE_RUN.calls.clear()
r = c.post("/api/system", json={"action": "docker_prune", "confirm": "清理"})
ck(r.status_code == 200 and r.get_json()["ok"], "清理完成")
ck(["docker", "system", "prune", "-f"] in FAKE_RUN.calls and
   not any("--volumes" in x for x in FAKE_RUN.calls), "prune 不带 --volumes（不碰数据卷）",
   [x for x in FAKE_RUN.calls if x[:2] == ["docker", "system"]])
r = c.post("/api/system", json={"action": "shutdown", "confirm": "nope"})
ck(r.status_code == 400 and r.get_json()["error"] == "need_confirm", "错确认词 → 400 need_confirm")
n_before = len(FAKE_SPAWN.calls)
c.post("/api/system", json={"action": "shutdown", "confirm": "nope"})
ck(len(FAKE_SPAWN.calls) == n_before, "错确认词绝不 spawn 进程")
r = c.post("/api/system", json={"action": "rm_rf", "confirm": "重启"})
ck(r.status_code == 400, "未注册的系统动作 400")

print("== 7. fail-closed：没配口令整站停用 ==")
os.environ.pop("NAS_ADMIN_PW", None)
MOD2 = load("nasadmin_b")
MOD2.run = FAKE_RUN
c2 = MOD2.app.test_client()
r = c2.get("/api/health")
ck(r.status_code == 200 and r.get_json()["configured"] is False, "健康检查仍可用但 configured=false")
ck(c2.get("/api/overview").status_code == 503, "没配口令：接口 503 fail-closed")
ck(c2.post("/api/login", json={"password": "x"}).status_code == 503, "没配口令：登录也 503")
ck(c2.get("/").status_code == 200, "没配口令：页面照常渲染（顶部红横幅）")
os.environ["NAS_ADMIN_PW"] = "testpw"

print("== 8. 容器宿主模式（NAS_ADMIN_NSENTER=1 → 命令垫 nsenter 进宿主机） ==")
os.environ["NAS_ADMIN_NSENTER"] = "1"
MOD3 = load("nasadmin_c")
os.environ.pop("NAS_ADMIN_NSENTER", None)
ck(MOD3.NSENTER is True, "NAS_ADMIN_NSENTER=1 被读到")
pref = MOD3._hostify(["docker", "ps"])
ck(pref[:2] == ["nsenter", "-t"] and pref[-2:] == ["docker", "ps"],
   "docker 命令垫 nsenter -t 1 前缀", pref)
ck(MOD3._hostify(["echo", "hi"]) == MOD3.NSPREFIX + ["echo", "hi"], "任意 argv 都垫前缀")
ck(MOD3._hostify([]) == [], "空 argv 不炸")
# 宿主模式下 run() 不该再垫 sudo（nsenter 后就是宿主机 root）
CAP = []
def fake_run(cmd, **kw):          # ⚠️ 必须收 **kw：run() 会传 capture_output/text/errors，
    CAP.append(list(cmd))         #   签名不收 kwargs 会在调用点就 TypeError，CAP 永远空（踩过）
    return 0, "", ""
_orig = MOD3.subprocess.run
MOD3.subprocess.run = fake_run
MOD3.run(["docker", "ps", "-q"])
MOD3.subprocess.run = _orig
ck(CAP and CAP[0][:1] == ["nsenter"], "宿主模式 run() 走 nsenter 而不是 sudo", CAP)
ck("sudo" not in CAP[0], "宿主模式不再垫 sudo -n", CAP)

print("== 9. 登出即吊销（服务端 _SESS；旧 cookie 副本立刻 401） ==")
with MOD.app.test_client() as c2:
    rv = c2.post("/api/login", json={"password": "testpw"})
    ck(rv.status_code == 200, "9a-登录成功", rv.status_code)
    raw = rv.headers.get("Set-Cookie", "")
    tok = raw.split("session=")[1].split(";")[0] if "session=" in raw else ""
    ck(bool(tok), "9b-拿到签名 cookie", raw[:70])
    with c2.session_transaction() as ss:
        jti = ss.get("jti", "")
    ck(bool(jti) and jti in MOD._SESS, "9c-jti 登记进服务端会话表", jti)
    # 干净 client 只带这份 cookie = "手握副本的人"；吊销前必须有效（防断言空转）。
    # ⚠️ werkzeug 3 的 test client **忽略 headers 里显式 Cookie**（实测 401 = 假绿），
    #    必须走 set_cookie；旧版签名 (host, key, value) 给个 TypeError 兜底。
    c3 = MOD.app.test_client()
    try:
        c3.set_cookie("session", tok, domain="localhost")
    except TypeError:
        c3.set_cookie("localhost", "session", tok)
    rv = c3.get("/api/fs?path=" + FS)      # ⚠️ 必须用 FS 根内路径：本套件 FS_ROOT=tmp（越界 400 是对的）
    ck(rv.status_code == 200, "9d-吊销前旧 cookie 有效（证明本组断言不空转）", rv.status_code)
    rv = c2.post("/api/logout")
    ck(rv.status_code == 200 and rv.get_json().get("ok"), "9e-登出 200", rv.status_code)
    ck(jti not in MOD._SESS, "9f-登出后 jti 已从会话表抹掉")
    rv = c3.get("/api/fs?path=" + FS)
    ck(rv.status_code == 401, "9g-同一份旧 cookie 立刻 401（真吊销）", rv.status_code)

print("== 10. 记住登录（持久 cookie + 重启不掉线） ==")
ck(MOD.app.config["PERMANENT_SESSION_LIFETIME"].days >= 30, "会话有效期 ≥30 天",
   MOD.app.config["PERMANENT_SESSION_LIFETIME"])
c4 = MOD.app.test_client()
rv = c4.post("/api/login", json={"password": "testpw"})
raw = rv.headers.get("Set-Cookie", "")
ck("Expires=" in raw and "session=" in raw, "cookie 带 Expires（关浏览器也记得住）", raw[:120])
ck("HttpOnly" in raw, "持久 cookie 仍是 HttpOnly")
with c4.session_transaction() as ss:
    jti4 = ss.get("jti", "")
ck(os.path.exists(MOD.SESS_PATH), "会话表已落盘 sessions.json")
ck(oct(os.stat(MOD.SESS_PATH).st_mode & 0o777) == "0o600", "sessions.json 权限 600",
   oct(os.stat(MOD.SESS_PATH).st_mode & 0o777))
rawtxt = io.open(MOD.SESS_PATH, encoding="utf-8").read()
ck(jti4 in rawtxt, "文件里有这张票")
ck("testpw" not in rawtxt, "文件里没有口令明文")
# 模拟「重建容器」：把 app.py 重新加载一遍（同一 DATA 目录）→ 旧 cookie 仍应有效
MOD3 = load("nasadmin_c")
c5 = MOD3.app.test_client()
tok4 = raw.split("session=")[1].split(";")[0] if "session=" in raw else ""
try:
    c5.set_cookie("session", tok4, domain="localhost")
except TypeError:
    c5.set_cookie("localhost", "session", tok4)
ck(c5.get("/api/fs?path=" + FS).status_code == 200, "重启后旧 cookie 仍有效（不用再输口令）")
# 登出必须同步落盘：注销后连"重启"也不认这张票
ck(c4.post("/api/logout").status_code == 200, "登出 200")
MOD4 = load("nasadmin_d")
c6 = MOD4.app.test_client()
try:
    c6.set_cookie("session", tok4, domain="localhost")
except TypeError:
    c6.set_cookie("localhost", "session", tok4)
ck(c6.get("/api/fs?path=" + FS).status_code == 401, "登出后连重启也不认旧票（吊销落盘）")

print("== 11. 文件夹打包下载（/api/fs/download_folder） ==")
import zipfile as _zf
c = authed_client()
# 造一棵小树：d1/a.txt, d1/sub/b.txt, d1/emptydir/
os.makedirs(os.path.join(FS, "d1", "sub"), exist_ok=True)
os.makedirs(os.path.join(FS, "d1", "emptydir"), exist_ok=True)
io.open(os.path.join(FS, "d1", "a.txt"), "w", encoding="utf-8").write("hello-a")
io.open(os.path.join(FS, "d1", "sub", "b.txt"), "w", encoding="utf-8").write("hello-b")
rv = c.get("/api/fs/download_folder?path=" + FS + "/d1")
ck(rv.status_code == 200 and rv.mimetype == "application/zip", "文件夹 → 返回 zip",
   "%s %s" % (rv.status_code, rv.mimetype))
ck(".zip" in (rv.headers.get("Content-Disposition") or ""), "是附件下载（带 .zip 文件名）",
   rv.headers.get("Content-Disposition"))
zin = _zf.ZipFile(io.BytesIO(rv.data))
names = sorted(zin.namelist())
ck("a.txt" in names and "sub/b.txt" in names, "zip 里保持了相对目录结构", names)
ck(zin.read("a.txt") == b"hello-a" and zin.read("sub/b.txt") == b"hello-b", "文件内容一致")
ck(all(not n.startswith("/") and ".." not in n.split("/") for n in names), "zip 内无绝对路径/穿越")
zin.close()
# 符号链接必须跳过（否则一个软链到 / 就能把整盘打包出去）
try:
    os.symlink(FS, os.path.join(FS, "d1", "link_to_root"))
    rv2 = c.get("/api/fs/download_folder?path=" + FS + "/d1")
    z2 = _zf.ZipFile(io.BytesIO(rv2.data))
    ck("link_to_root" not in z2.namelist() and not any("link_to_root" in n for n in z2.namelist()),
       "符号链接被跳过（不跟随，防打包出界）", z2.namelist())
    z2.close()
    os.unlink(os.path.join(FS, "d1", "link_to_root"))
except (OSError, NotImplementedError):
    ck(True, "符号链接被跳过（环境不支持 symlink，跳过）")
rv = c.get("/api/fs/download_folder?path=" + FS + "/a_missing_dir")
ck(rv.status_code == 400 and rv.get_json()["error"] == "not_dir", "不存在的目录 → 400 not_dir")
rv = c.get("/api/fs/download_folder?path=/")
ck(rv.status_code in (400, 403), "拒绝打包根目录", rv.status_code)
# 范围太广的目录（FS_ROOT 本身 / 顶层子目录）也拒绝 —— 否则边扫边压能把盘写满
rv = c.get("/api/fs/download_folder?path=" + FS)
ck(rv.status_code == 400 and rv.get_json()["error"] == "too_broad",
   "拒绝打包 FS 根（等于整盘）", rv.get_json().get("msg"))
os.makedirs(os.path.join(FS, "vol1"), exist_ok=True)
rv = c.get("/api/fs/download_folder?path=" + FS + "/vol1")
ck(rv.status_code == 400 and rv.get_json()["error"] == "too_broad",
   "拒绝打包存储池顶层目录（vol1 这类）", rv.get_json().get("msg"))
rv = c.get("/api/fs/download_folder?path=" + FS + "/d1")
ck(rv.status_code == 200, "具体项目目录照常可打包")
rv = c.get("/api/fs/download_folder?path=" + FS + "/d1/a.txt")
ck(rv.status_code == 400 and rv.get_json()["error"] == "not_dir", "文件走 download_folder → 400（不是目录）")
ck(MOD.app.test_client().get("/api/fs/download_folder?path=" + FS + "/d1").status_code == 401,
   "未登录不能打包下载")
# 打包大目录上限（把上限临时调成 1 字节 → 必然超）
_old = MOD.FOLDER_ZIP_MAX
MOD.FOLDER_ZIP_MAX = 1
rv = c.get("/api/fs/download_folder?path=" + FS + "/d1")
ck(rv.status_code == 413 and rv.get_json()["error"] == "too_big", "超上限 → 413 并说明",
   "%s %s" % (rv.status_code, rv.data[:80]))
MOD.FOLDER_ZIP_MAX = _old
# 单文件下载仍在（对照）
rv = c.get("/api/fs/download?path=" + FS + "/d1/a.txt")
ck(rv.status_code == 200 and rv.data == b"hello-a", "单文件下载仍正常")

# ⚠️ 临时 zip 必须清掉：早先只靠 call_on_close，真机上 /tmp 堆了 6 个没删的包。
# 断言只看「本次新增」：上面那些 a.txt/符号链接用例是**故意**直接读 .data（不 close）的，
# 它们本来就会留文件（Flask test client 在响应没被读完/关闭前不触发 cleanup）。
import glob as _glob
_dlpat = os.path.join(tempfile.gettempdir(), "nasadmin_dl_*.zip")
_before = set(_glob.glob(_dlpat))
c2 = authed_client()
_rv = c2.get("/api/fs/download_folder?path=" + FS + "/d1")
with _rv:
    _body = b"".join(_rv.response)      # 按 WSGI 服务器的姿势读完
    _rv.close()                          # 再关闭 → 触发删除
ck(_body[:2] == b"PK" and len(_body) > 50, "打包响应是合法 zip 流", len(_body))
_new = set(_glob.glob(_dlpat)) - _before
ck(not _new, "响应正常关闭后临时 zip 立刻清理（不留垃圾）", list(_new)[:3])
# 启动清理：伪造一个 2 小时前的旧包 → 重新加载模块应把它扫掉
stale = os.path.join(tempfile.gettempdir(), "nasadmin_dl_STALE0001.zip")
io.open(stale, "wb").write(b"x")
os.utime(stale, (time.time() - 7200, time.time() - 7200))
load("nasadmin_sweep")
ck(not os.path.exists(stale), "启动时清理陈旧临时包（进程被 kill 的残留）")
# 刚生成的新包不能被误删（兄弟进程正在发）
fresh = os.path.join(tempfile.gettempdir(), "nasadmin_dl_FRESH0001.zip")
io.open(fresh, "wb").write(b"x")
load("nasadmin_sweep2")
ck(os.path.exists(fresh), "启动清理不误删刚生成的包")
os.unlink(fresh)

print("== 14. DeepSeek 余额（打桩，零外网） ==")
_bal_calls = []


def _fake_fetch(key):
    _bal_calls.append(key)
    return {"ok": True, "is_available": False, "total": -0.02, "granted": 0.0,
            "topped_up": -0.02, "currency": "CNY", "ts": int(time.time())}


def _fake_boom(key):
    _bal_calls.append(key)
    raise OSError("connection reset")


# 无 key：直接回 ok=False，且**绝不发出网络请求**（_fetch 不会被调用）
MOD._bal_cache["data"], MOD._bal_cache["ts"] = None, 0.0
MOD._ds_key = lambda: ""
c = authed_client()
r = c.get("/api/balance")
ck(r.status_code == 200 and r.get_json()["ok"] is False and "没找到" in r.get_json()["error"],
   "没配 key：报错不抛异常", r.get_json())
ck(not _bal_calls, "没配 key：不会真去打网络")

# 有 key（打桩）：真解析 + 负数余额标 bad 的数据都在
MOD._ds_key = lambda: "sk-fake"
MOD._fetch_balance = _fake_fetch
r = c.get("/api/balance")
j = r.get_json()
ck(r.status_code == 200 and j["ok"] is True and j["total"] == -0.02 and j["is_available"] is False,
   "查到余额：total/is_available 解析正确", j)
ck(len(_bal_calls) == 1, "第一次真查了一次", _bal_calls)

# 缓存：5 秒自动刷新不能每次都打外面 API —— 第二次请求必须走缓存
r = c.get("/api/balance")
j = r.get_json()
ck(len(_bal_calls) == 1 and j.get("cached") is True and j["total"] == -0.02,
   "第二次走服务端缓存（不重复打 DeepSeek）", (len(_bal_calls), j))

# refresh=1 强制刷新：缓存失效、重新查询
r = c.get("/api/balance?refresh=1")
j = r.get_json()
ck(len(_bal_calls) == 2 and j.get("cached") is False, "?refresh=1 强制重新查询", (len(_bal_calls), j))

# 网络挂了 + 有旧值：返回旧值并标 stale，不清空
MOD._fetch_balance = _fake_boom
r = c.get("/api/balance?refresh=1")
j = r.get_json()
ck(j["ok"] is True and j.get("stale") is True and j["total"] == -0.02,
   "刷新失败但有旧值：照常显示并标（旧值）", j)

# 网络挂了 + 无旧值：干净报错
MOD._bal_cache["data"], MOD._bal_cache["ts"] = None, 0.0
r = c.get("/api/balance")
j = r.get_json()
ck(j["ok"] is False and "connection reset" in j["error"], "刷新失败且无旧值：报错文案带原因", j)

# 未登录要 401（余额属于敏感信息，和其它接口一样进门禁）
c = fresh_client()
ck(c.get("/api/balance").status_code == 401, "未登录的余额接口 401")
# key 绝不出现在响应里
MOD._fetch_balance = _fake_fetch
c = authed_client()
ck(b"sk-fake" not in c.get("/api/balance?refresh=1").data, "响应里绝不带 key")

print("== 15. 口令哈希模式（NAS_ADMIN_PW_HASH，磁盘不落明文） ==")
# 用低轮数跑，保证测试快；生产默认 390000
os.environ.pop("NAS_ADMIN_PW", None)
os.environ["NAS_ADMIN_PBKDF2_ROUNDS"] = "1000"
SALT = "00112233445566778899aabbccddeeff"
os.environ["NAS_ADMIN_PW_HASH"] = MOD.hash_password("hashtestpw", salt_hex=SALT, rounds=1000)
ck(os.environ["NAS_ADMIN_PW_HASH"].startswith("sha256:" + SALT + "."),
   "生成的哈希带盐前缀", os.environ["NAS_ADMIN_PW_HASH"][:30])

# ⚠️ 轮数环境变量必须在 load() 之前就位，并且**一直保持到所有哈希用例加载完**：
#    load() 在导入时读 _PBKDF2_ROUNDS；太早 pop 会让后面模块用 390000 轮去验
#    一个 1000 轮算出来的哈希 → 一切口令都判失败（假红，已踩过一次）。
MODH = load("nasadmin_hash")
ck(MODH._PBKDF2_ROUNDS == 1000, "模块读到的轮数是 1000", MODH._PBKDF2_ROUNDS)
ck(MODH.configured() is True, "只配哈希没配明文 → configured=True")
ck(len(MODH.PASSWORD_HASH) > 60, "哈希值足够长（含盐+dk）", len(MODH.PASSWORD_HASH))

ch = MODH.app.test_client()
# 正确口令要能登进去 —— 这是「用户敲明文、服务端比哈希」的核心断言
ck(ch.post("/api/login", json={"password": "hashtestpw"}).status_code == 200,
   "哈希模式：正确明文口令登录成功")
ck(ch.post("/api/login", json={"password": "WjlWjl2012"}).status_code == 401,
   "哈希模式：口令大小写错 → 401")
ck(ch.post("/api/login", json={"password": ""}).status_code == 401, "哈希模式：空口令 → 401")
ck(ch.post("/api/login", json={"password": "hashtestpw "}).status_code == 401,
   "哈希模式：多一个空格 → 401（不许 trim 宽容）")
ck(ch.post("/api/login", json={"password": "hashtestpw0"}).status_code == 401,
   "哈希模式：前缀更长的错口令 → 401")
# ⚠️ 必须开新 client：ch 上一行已经登录成功、session 还在，老 client 会得 200
ck(MODH.app.test_client().get("/api/overview").status_code == 401, "哈希模式：未登录接口仍 401")
# 明文那个环境变量**根本不该出现**在配置里
ck(not MODH.PASSWORD, "哈希模式下 PASSWORD 明文变量为空")
# 响应里绝不能漏出哈希本身
r = ch.post("/api/login", json={"password": "nope"})
ck(b"sha256:" not in r.data and os.environ["NAS_ADMIN_PW_HASH"].encode() not in r.data,
   "登录失败响应里不泄露哈希值")

print("== 16. 明文+哈希同配 → 以哈希为准（误留的明文不生效） ==")
os.environ["NAS_ADMIN_PW"] = "decoy-plaintext"
MODB = load("nasadmin_both")
ck(MODB._pw_ok("hashtestpw") is True, "同配时正确口令（哈希那套）通过")
ck(MODB._pw_ok("decoy-plaintext") is False,
   "同配时误留的明文口令**不生效**", "decoy 居然通过了")
ck(MODB._pw_ok("wrong") is False, "同配时错口令仍拒")
os.environ.pop("NAS_ADMIN_PW", None)
os.environ.pop("NAS_ADMIN_PBKDF2_ROUNDS", None)   # 后面的用例回到默认轮数

print("== 17. 哈希格式异常时 fail-closed（不许放行） ==")
os.environ["NAS_ADMIN_PW_HASH"] = "sha256:not-hex.deadbeef"
MODX = load("nasadmin_bad")
ck(MODX.configured() is True, "格式坏但有值 → 仍算已配置")
ck(MODX._pw_ok("anything") is False, "salt 非法 → 拒绝一切口令（fail-closed）")
os.environ["NAS_ADMIN_PW_HASH"] = "garbage-no-prefix"
MODX2 = load("nasadmin_bad2")
ck(MODX2._pw_ok("anything") is False, "格式完全不对 → 拒绝一切口令（fail-closed）")
os.environ["NAS_ADMIN_PW_HASH"] = "sha256:" + SALT + "." + "zz" * 32
MODX3 = load("nasadmin_bad3")
ck(MODX3._pw_ok("hashtestpw") is False, "dk 非法十六进制 → 拒绝（不炸不放行）")
os.environ.pop("NAS_ADMIN_PW_HASH", None)

print("== 18. 哈希可复现 ==")
a = MOD.hash_password("pw1")
b = MOD.hash_password("pw1")
ck(a != b, "同一口令两次生成哈希不同（随机盐）")
ck(a.startswith("sha256:") and a.count(".") == 1, "哈希格式 sha256:<salt>.<dk>")
# 从哈希里取出盐重算，必须得到同一个 dk —— 证明比对路径可复现（不是碰运气）
_salt = a.split(":", 1)[1].split(".", 1)[0]
ck(a.endswith("." + MOD._pbkdf2("pw1", _salt).hex()),
   "取盐重算得到同一个 dk（比对可复现）")
ck(MOD._pbkdf2("pw1", _salt) != MOD._pbkdf2("pw2", _salt), "不同口令 dk 不同")
os.environ["NAS_ADMIN_PW"] = "testpw"   # 还原给后面的用例

print()
print("结果：%d 通过 / %d 失败" % (PASS, FAIL))
try:
    shutil.rmtree(TMP, ignore_errors=True)
except Exception:
    pass
sys.exit(1 if FAIL else 0)
