#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""nas-admin 线上验收（部署到 NAS 后跑）。

安全边界：
  · 口令从环境变量 `NAS_ADMIN_PW` 读（**不写进脚本、不回显**）
  · 命令只跑无害的 `echo`
  · 文件只在 `/tmp/nasadmin_live_*` 自建自删
  · 危险区只喂**错的**确认词（服务端 400），绝不触发真重启/关机/清垃圾

跑法：NAS_ADMIN_PW=... python3 verify_live.py [http://<NAS>:8091]
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8091"
PW = os.environ.get("NAS_ADMIN_PW", "")
PASS = FAIL = 0


def ck(cond, name, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ✔ " + name)
    else:
        FAIL += 1
        print("  ✘ " + name + ("　" + str(extra)[:200] if extra else ""))


def call(path, method="GET", body=None, cookie=None, raw=None):
    url = BASE + path
    headers = {}
    if cookie:
        headers["Cookie"] = cookie
    data = None
    if raw is not None:
        data = raw
        headers["Content-Type"] = "application/json"
    elif body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=45) as r:
            sc, hdr, content = r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        sc, hdr, content = e.code, dict(e.headers), e.read()
    except Exception as e:
        return 0, {}, str(e).encode()
    ct = hdr.get("Content-Type", "")
    j = None
    if "application/json" in ct:
        try:
            j = json.loads(content.decode("utf-8", "replace"))
        except Exception:
            j = None
    return sc, {"cookie": hdr.get("Set-Cookie", ""), "raw": hdr}, (j if j is not None else content)


if not PW:
    print("必须给 NAS_ADMIN_PW 环境变量（口令不进脚本）")
    sys.exit(1)

print("== nas-admin 线上验收 %s ==" % BASE)
sc, _, j = call("/api/health")
ck(sc == 200 and j and j.get("ok") and j.get("configured") is True, "健康检查 configured=true", j)

sc, _, page = call("/")
ck(sc == 200 and "NAS 管理".encode() in page, "页面 200 有标题")
ck(b'login-overlay' in page and b'pwOverlay' in page, "页面带口令遮罩")

sc, _, j = call("/api/overview")
ck(sc == 401 and j and j.get("error") == "need_login", "未登录 → 401")

sc, hdr, j = call("/api/login", method="POST", body={"password": PW})
ck(sc == 200 and j and j.get("ok"), "登录成功", j)
if sc != 200:
    # 口令与容器 env.nasadmin 不一致时**干净退出**，别让后面一堆 KeyError/TypeError
    # 把真正的根因（登录失败）糊掉（踩过：ov['unauthorized'] 字典 → ov["containers"] KeyError）
    print("❌ 登录失败：传入的 NAS_ADMIN_PW 与容器里的 env.nasadmin 不一致，后面全部跳过")
    print("结果：%d 通过 / %d 失败" % (PASS, FAIL))
    sys.exit(1)
COOKIE = hdr.get("cookie", "").split(";")[0]
ck(bool(COOKIE), "拿到会话 cookie")

sc, _, j = call("/api/login", method="POST", body={"password": "definitely-wrong"})
ck(sc == 401, "错口令 401")

sc, _, ov = call("/api/overview", cookie=COOKIE)
ck(sc == 200 and ov and len(ov.get("disks", [])) >= 1, "总览：磁盘",
   ov.get("disks")[:1] if isinstance(ov, dict) else ov)
ck(isinstance(ov, dict) and ov["containers"]["error"] == "" and ov["containers"]["total"] >= 4,
   "总览：docker 可用且容器数正常", ov.get("containers"))
ck(isinstance(ov, dict) and ov["cpu"]["total"] is not None and ov["mem"]["total_mb"] > 1000,
   "总览：CPU/内存是 NAS 真值", (ov.get("cpu"), ov.get("mem")))
ck(isinstance(ov, dict) and any(t["c"] > 20 for t in ov["temps"]), "总览：温度真读到",
   ov.get("temps"))

sc, _, ct = call("/api/containers", cookie=COOKIE)
names = [c["name"] for c in ct.get("containers", [])] if sc == 200 else []
ck(sc == 200 and any(n in names for n in ("blog", "essay-revise", "recite", "nas-admin")
                      or ("blog" in names)), "容器列表（含真实容器）", names)
ck("blog" in names and "essay-revise" in names and "recite" in names, "三件套都在", names)
ck(all(isinstance(c.get("up"), bool) for c in ct.get("containers", [])), "运行状态字段齐")

sc, _, lg = call("/api/container/logs?name=blog&tail=5", cookie=COOKIE)
ck(sc == 200 and lg.get("text"), "读 blog 日志（只读）", str(lg)[:80])

sc, _, ex = call("/api/exec", method="POST", body={"cmd": "echo nas-admin-live-ok; hostname"},
                 cookie=COOKIE)
ck(sc == 200 and ex.get("rc") == 0 and "nas-admin-live-ok" in ex.get("out", ""), "命令执行 echo",
   ex.get("out")[:80] if isinstance(ex, dict) else ex)

# ⚠️ 所有文件操作路径按 FS_ROOT 加前缀：容器宿主模式 FS_ROOT=/host（-v /:/host 挂载），
#    传裸 /tmp 会被 _safe()「越界」**正确**拒绝（第一版就这么挂了 7 项）。宿主机模式 FS="" 原样。
FS = os.environ.get("NAS_ADMIN_FS_ROOT", "/").rstrip("/")


def fp(p):
    return (FS + p) if FS else p


tmpdir = fp("/tmp/nasadmin_live_%d" % int(time.time()))
sc, _, r = call("/api/fs/mkdir", method="POST", body={"path": tmpdir}, cookie=COOKIE)
ck(sc == 200, "建临时目录 %s" % tmpdir, r)
sc, _, r = call("/api/fs/save", method="POST",
                body={"path": tmpdir + "/t.txt", "content": "线上验收写入"}, cookie=COOKIE)
ck(sc == 200, "写临时文件")
sc, _, r = call("/api/fs/read?path=" + tmpdir + "/t.txt", cookie=COOKIE)
ck(sc == 200 and isinstance(r, dict) and r.get("content") == "线上验收写入", "读回临时文件", r)
sc, _, r = call("/api/fs/download?path=" + tmpdir + "/t.txt", cookie=COOKIE)
ck(sc == 200 and r == "线上验收写入".encode(), "下载临时文件")
sc, _, r = call("/api/fs/delete", method="POST",
                body={"path": tmpdir + "/t.txt", "confirm": "t.txt"}, cookie=COOKIE)
ck(sc == 200, "删临时文件（文件名确认）", r)
sc, _, r = call("/api/fs/delete", method="POST",
                body={"path": tmpdir, "confirm": tmpdir}, cookie=COOKIE)
ck(sc == 200, "删临时目录（完整路径确认）", r)
sc, _, r = call("/api/fs?path=" + fp("/etc"), cookie=COOKIE)   # ⚠️ 列目录接口要给**目录**（给 /etc/passwd 文件是 not_dir，历史 bug）
ck(sc == 200 and isinstance(r, dict) and "entries" in r,
   "能读 /etc 目录（这就是高权限）—— 只读不写", str(r)[:60])

# 危险区：只喂错词，服务端必须拒绝
for act, bad in (("reboot", "reboot"), ("shutdown", "poweroff"), ("docker_prune", "yes")):
    sc, _, r = call("/api/system", method="POST",
                    body={"action": act, "confirm": bad}, cookie=COOKIE)
    ck(sc == 400 and isinstance(r, dict) and r.get("error") == "need_confirm",
       "危险区 %s 错确认词被拒" % act, r)
sc, _, r = call("/api/system", method="POST",
                body={"action": "rm_rf", "confirm": "重启"}, cookie=COOKIE)
ck(sc == 400 and isinstance(r, dict) and r.get("error") == "bad_action", "未注册动作 400")

sc, _, au = call("/api/audit?tail=50", cookie=COOKIE)
rows = au.get("rows", []) if sc == 200 else []
ck(any(x.get("kind") == "exec" and "nas-admin-live-ok" in str(x.get("detail"))
       for x in rows), "命令进了审计日志")

sc, _, r = call("/api/logout", method="POST", cookie=COOKIE)
ck(sc == 200, "登出")
sc, _, r = call("/api/overview", cookie=COOKIE)
ck(sc == 401, "登出后再访问 → 401")

# 没带口令的健康检查永远可用
sc, _, j = call("/api/health")
ck(sc == 200 and j.get("configured") is True, "health 无需登录")

print()
print("结果：%d 通过 / %d 失败" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
