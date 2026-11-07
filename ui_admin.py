#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""nas-admin 真浏览器 UI 测试。

安全边界（这份测试跑在**真机**上，绝不能有副作用）：
  · 后端用环境变量跑在**随机空闲端口**，FS_ROOT 指到临时目录（文件操作只能打到测试目录）
  · 容器页**只读**：只断言列表渲染，绝不点 启动/停止/重启/删除 真容器
  · 危险区只输**错的**确认词（服务端 400），绝不触发真实 reboot/shutdown/prune
  · 命令页只跑 `echo hi` 这类无害命令

跑法：python3 ui_admin.py          （需要 Playwright + chromium）
"""
import atexit
import json
import os
import random
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp(prefix="nasadmin_ui_")
FS = os.path.join(TMP, "fs")
DATA = os.path.join(TMP, "data")
os.makedirs(FS, exist_ok=True)
os.makedirs(DATA, exist_ok=True)
with open(os.path.join(FS, "hello.txt"), "w", encoding="utf-8") as f:
    f.write("你好 nas-admin\n")
# 一个子目录：给「文件夹打包下载」用
os.makedirs(os.path.join(FS, "子目录"), exist_ok=True)
with open(os.path.join(FS, "子目录", "inner.txt"), "w", encoding="utf-8") as f:
    f.write("inner\n")
LONG_NAME = "这是一个非常非常长的文件名用来测试手机端不会把名字截断.txt"
with open(os.path.join(FS, LONG_NAME), "w", encoding="utf-8") as f:
    f.write("long name\n")

MARKER = os.path.join(tempfile.gettempdir(), "nasadmin_ui.marker")
PW = "uitestpw"

PASS = FAIL = 0


def ck(cond, name, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ✔ " + name)
    else:
        FAIL += 1
        print("  ✘ " + name + ("　" + str(extra)[:200] if extra else ""))


# ---- 起服务前先清孤儿（顺序别反：放 Popen 之后会把自己刚起的服务杀掉）----
# ⚠️ 踩过（2026-10-17）：按 cmdline 子串匹配会把**启动本测试的父 shell** 一起杀掉
#    （shell 的 cmdline 里就有 "ui_admin.py" 字样）→ 整条命令 -9 自杀。三条限定：
#    ① 只看 comm 以 python 开头的进程 ② argv 必须**整段等于**目标脚本路径
#    ③ 祖先链（父 shell、工具执行器）永不杀。
def _ppid(pid):
    try:
        s = open("/proc/%d/stat" % pid, encoding="utf-8", errors="replace").read()
        return int(s[s.rfind(")") + 1:].split()[1])
    except Exception:
        return None


def kill_orphans():
    if not os.path.exists(MARKER):
        return
    ancestors = set()
    p = os.getppid()
    while p and p not in ancestors:
        ancestors.add(p)
        p = _ppid(p) or 0
    SCRIPT = os.path.join(HERE, "app.py")
    for pid in os.listdir("/proc"):
        if not pid.isdigit() or int(pid) == os.getpid() or int(pid) in ancestors:
            continue        # 自己 + 祖先链永不杀（自己的 argv 也以 ui_admin.py 结尾，踩过：自杀）
        try:
            comm = open("/proc/%s/comm" % pid, encoding="utf-8",
                        errors="replace").read().strip()
        except Exception:
            continue
        if not comm.startswith("python"):
            continue
        try:
            argv = [a.decode("utf-8", "replace") for a in
                    open("/proc/%s/cmdline" % pid, "rb").read().split(b"\0") if a]
        except Exception:
            continue
        if SCRIPT in argv or any(a.endswith("ui_admin.py") for a in argv):
            try:
                os.kill(int(pid), signal.SIGKILL)
                print("  （清掉孤儿 python %s）" % pid)
            except Exception:
                pass
    try:
        os.remove(MARKER)
    except Exception:
        pass


kill_orphans()


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


PORT = free_port()
env = dict(os.environ)
env.update({
    "NAS_ADMIN_PW": PW,
    "NAS_ADMIN_DATA": DATA,
    "NAS_ADMIN_FS_ROOT": FS,
    "NAS_ADMIN_PORT": str(PORT),
    "NAS_ADMIN_BIND": "127.0.0.1",
    "NAS_ADMIN_SECRET": "ui-test-secret",
    "NAS_ADMIN_HIST_INTERVAL": "1",     # 测试里 1 秒一个采样点，几秒就能画出真曲线
})
with open(MARKER, "w") as f:
    f.write(str(os.getpid()))
PROC = subprocess.Popen([sys.executable, os.path.join(HERE, "app.py")], env=env,
                        stdout=open(os.path.join(TMP, "srv.log"), "wb"),
                        stderr=subprocess.STDOUT)
BASE = "http://127.0.0.1:%d" % PORT

# 崩溃兜底：任何路径退出都收尸服务 + 摘 marker（否则下次启动会带着陈旧 marker 扫孤儿）
def _atexit_cleanup():
    if PROC.poll() is None:
        try:
            PROC.send_signal(signal.SIGTERM)
            PROC.wait(timeout=3)
        except Exception:
            try:
                PROC.kill()
            except Exception:
                pass
    try:
        os.remove(MARKER)
    except Exception:
        pass
atexit.register(_atexit_cleanup)

import urllib.request
for i in range(50):
    try:
        with urllib.request.urlopen(BASE + "/api/health", timeout=1) as r:
            if r.status == 200:
                break
    except Exception:
        time.sleep(0.2)
else:
    print(open(os.path.join(TMP, "srv.log")).read())
    print("服务起不来")
    sys.exit(1)

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("没有 Playwright，跳过 UI 测试")
    sys.exit(1)

print("== 服务起来了 %s ==" % BASE)
JS_ERRS = []

with sync_playwright() as p:
    br = p.chromium.launch(args=["--no-sandbox"])
    pg = br.new_page(viewport={"width": 390, "height": 844})
    # 诊断探针：谁在 fetch(null/undefined) → 404 /null（历史上摸不到来源，留个栈）
    pg.add_init_script("""
      (() => {
        const of = window.fetch;
        window.fetch = function (u) {
          let s = '';
          try { s = (u && u.url) ? u.url : String(u); } catch (e) { s = '?'; }
          if (u == null || s === 'null' || s === 'undefined') {
            window.__badFetchArg = s;
            window.__badFetchStack = (new Error()).stack || 'no-stack';
          }
          return of.apply(this, arguments);
        };
      })();
    """)
    pg.on("pageerror", lambda e: JS_ERRS.append("JS: " + str(e)[:200]))
    pg.on("console", lambda m: JS_ERRS.append("CONSOLE: " + m.text[:200])
          if m.type == "error" else None)
    # 收集所有 4xx/5xx 响应：最后断言它们**全部落在预期名单**里
    # （首屏 /api/overview 401、错口令 /api/login 401、危险区错词 400、/etc 越界 400）
    BAD_RESP = []
    pg.on("response", lambda r: BAD_RESP.append("%d %s [%s]" % (r.status, r.url, r.request.resource_type))
          if r.status >= 400 else None)
    # confirm/alert/prompt 统一接住：confirm→接受；alert→关掉；prompt→给**错的**答案
    PG_PROMPT = {"val": None}
    def on_dialog(d):
        if d.type == "confirm":
            d.accept()
        elif d.type == "alert":
            d.accept()
        else:
            if PG_PROMPT["val"] is None:
                d.dismiss()
            else:
                d.accept(PG_PROMPT["val"])
    pg.on("dialog", on_dialog)

    print("== 1. 门禁 ==")
    pg.goto(BASE, wait_until="domcontentloaded")
    pg.wait_for_timeout(700)
    ck(pg.is_visible("#pwOverlay"), "未登录 → 口令遮罩自动弹出")
    ck(pg.is_visible("#cfgBanner") is False, "已配口令 → 无告警横幅")
    pg.fill("#pwInput", "bad")
    pg.click("#btnPwOk")
    pg.wait_for_timeout(500)
    ck(pg.is_visible("#pwErr"), "错口令 → 遮罩里报错", pg.inner_text("#pwErr") if pg.is_visible("#pwErr") else "")
    pg.fill("#pwInput", PW)
    pg.click("#btnPwOk")
    pg.wait_for_timeout(800)
    ck(not pg.is_visible("#pwOverlay"), "对口令 → 遮罩收起")
    # 用户要求「不要每次打开都输口令」：cookie 必须带到期时间（持久 cookie），
    # 否则关掉浏览器就丢 —— 这是浏览器侧唯一可验证的点
    cookies = pg.context.cookies()
    sess_c = [c for c in cookies if c["name"] == "session"]
    ck(bool(sess_c) and sess_c[0].get("expires", -1) > 0, "登录 cookie 有到期时间（持久，不是会话级）",
       sess_c[:1])

    print("== 2. 总览 ==")
    pg.wait_for_selector("#ovStats .stat", timeout=8000)
    txt = pg.inner_text("#ovStats")
    ck("CPU" in txt and "内存" in txt and "负载" in txt, "CPU/内存/负载三张格子有数", txt[:120])
    ck("运行时间" in txt, "运行时间格子")
    # 2026-10-18 重排：swap 升格成统计格（原来藏在「系统信息」卡里看不见）
    ck("Swap" in txt, "Swap 统计格（升格自系统信息卡）", txt[:160])
    # 排列顺序：动态健康指标在前，运行时间在容器/Swap 之后
    ck(txt.index("CPU 占用") < txt.index("运行时间"), "CPU 排最前")
    # 2026-10-18 新增：DeepSeek 余额格子（测试环境没配 key → 优雅显示"查不到"而不是崩）
    ck("DeepSeek 余额" in txt, "DeepSeek 余额统计格存在", txt[:200])
    pg.wait_for_selector("#balStat .v", timeout=5000)
    balv = pg.inner_text("#balStat .v")
    ck("查不到" in balv or "¥" in balv, "余额格渲染出结果（查不到/金额二选一）", balv[:80])
    ck(pg.is_visible("#balStat [data-bal-refresh]"), "余额格有 ↻ 手动刷新按钮")
    disk = pg.inner_text("#ovDisks")
    ck("/" in disk or "vol1" in disk, "磁盘表有真实挂载点", disk[:120])
    ck(pg.is_visible(".bar"), "磁盘有占用进度条")
    misc = pg.inner_text("#ovMisc")
    ck("内核" in misc, "内核/温度信息", misc[:100])

    print("== 3. 容器页（只读） ==")
    pg.click('.tab[data-v="containers"]')
    pg.wait_for_timeout(1200)
    ck(pg.is_visible("#ctList table"), "容器表格渲染出来")
    body = pg.inner_text("#ctList")
    ck("日志" in body and "删除" in body, "每行有 日志/删除 按钮")
    # 真机上至少有 blog/essay-revise/recite 里的一个
    ck(any(n in body for n in ("blog", "essay-revise", "recite")), "看到真实容器", body[:150])
    pg.click("#btnCtLogClose") if pg.is_visible("#btnCtLogClose") else None

    print("== 4. 文件页（FS_ROOT=测试目录） ==")
    pg.click('.tab[data-v="files"]')
    pg.wait_for_selector("#fsList .fs-row", timeout=8000)
    fsbody = pg.inner_text("#fsList")
    ck("hello.txt" in fsbody, "列到测试文件 hello.txt", fsbody[:150])
    # 每行都有下载按钮：文件标「⬇️」，目录标「⬇️打包」
    ck(pg.locator("#fsList .fs-row [data-dl]").count() >= 2, "每个文件/文件夹都有下载按钮",
       "按钮数 %d" % pg.locator("#fsList .fs-row [data-dl]").count())
    ck(pg.locator('#fsList .fs-row.dir [data-dl][data-isdir="1"]').count() >= 1,
       "文件夹的按钮是「打包」下载")
    # 真点文件下载：浏览器必须真的收到一个附件响应
    with pg.expect_download(timeout=15000) as dlinfo:
        pg.click('#fsList .fs-row:not(.dir) [data-dl]')
    dl = dlinfo.value
    ck(dl.suggested_filename == "hello.txt", "点⬇️ 真的下载到 hello.txt", dl.suggested_filename)
    # 真点文件夹打包：拿到 .zip
    with pg.expect_download(timeout=25000) as dlinfo2:
        pg.click('#fsList .fs-row.dir [data-dl][data-isdir="1"] >> nth=0')
    dl2 = dlinfo2.value
    ck(dl2.suggested_filename.endswith(".zip"), "点「打包」真的下载到 zip", dl2.suggested_filename)
    # 点下载按钮不该顺带进入目录（冒泡没拦住的话会跳走）
    pg.wait_for_timeout(400)
    ck(pg.is_visible("#fsList"), "点下载没有误触目录跳转")
    pg.click('#fsList .fs-row:not(.dir)')          # 打开 hello.txt 编辑
    pg.wait_for_selector("#fsEditCard:not(.hidden)", timeout=5000)
    ck("你好 nas-admin" in pg.input_value("#fsEdit"), "文件内容读进编辑框")
    pg.fill("#fsEdit", "改过了\n第二行")
    pg.click("#btnFsSave")
    pg.wait_for_timeout(600)
    ck(open(os.path.join(FS, "hello.txt"), encoding="utf-8").read() == "改过了\n第二行",
       "保存真的落盘")
    # 导航到测试根目录（输入框路径），确认面包屑与返回上级
    pg.fill("#fsPath", FS)
    pg.press("#fsPath", "Enter")
    pg.wait_for_timeout(600)
    ck(pg.is_visible("#fsCrumbs a"), "面包屑渲染")
    # 越界路径：后端 400，前端显示提示（FS_ROOT 是临时目录，/etc 必然越界）
    pg.fill("#fsPath", "/etc")
    pg.press("#fsPath", "Enter")
    pg.wait_for_timeout(600)
    ck("越界" in pg.inner_text("#fsList") or "读不了" in pg.inner_text("#fsList"),
       "越界路径前端报错（服务端挡）", pg.inner_text("#fsList")[:100])

    print("== 5. 命令页 ==")
    pg.click('.tab[data-v="cmd"]')
    pg.fill("#cmdIn", "echo hi && echo 两个字")
    pg.click("#btnCmdRun")
    # ⚠️ 别用 "#cmdOut:not(.hidden)" 等 —— 点下按钮它就显示"执行中"占位了，
    #    立刻 inner_text 读到的是占位文本（里面也有命令回显的两个字，会假通过前半句）
    pg.wait_for_function(
        "() => { const e = document.querySelector('#cmdOut');"
        " return e && !e.classList.contains('hidden') && e.textContent.indexOf('exit=') >= 0; }",
        timeout=8000)
    out = pg.inner_text("#cmdOut")
    ck("两个字" in out, "命令输出回显", out[:120])
    ck("exit=0" in out, "退出码显示", out[:120])
    pg.wait_for_timeout(800)
    ck("shell" in pg.inner_text("#cmdHistory"), "审计列表里出现这条执行记录")

    print("== 6. 危险区（只输错词，服务端必须拒绝） ==")
    pg.click('.tab[data-v="danger"]')
    pg.wait_for_timeout(400)
    ck(pg.is_visible("#btnReboot") and pg.is_visible("#btnShutdown") and pg.is_visible("#btnPrune"),
       "三个危险按钮都在")
    pg.fill("#cfReboot", "reboot")          # 故意写英文 → 服务端要「重启」
    pg.click("#btnReboot")                  # confirm 对话框被自动接受 → 打到服务端 400
    pg.wait_for_timeout(700)
    ck("need_confirm" not in pg.inner_text("#toast").lower() and
       ("输入" in pg.inner_text("#toast") or "确认" in pg.inner_text("#toast")),
       "错确认词 → 服务端 400，前端提示要确认", pg.inner_text("#toast")[:80])
    time.sleep(1)
    # 真机上绝不能出现"正在重启"的日志：审计里这条 action=reboot 的 rc 应为 400 被拒
    ck(pg.is_visible("#auditList"), "审计日志卡片存在")

    print("== 7. 自动刷新（强制开启，只可选间隔） ==")
    pg.click('.tab[data-v="overview"]')
    pg.wait_for_timeout(600)
    ck(pg.query_selector("#ovAuto") is None, "没有自动刷新开关了（强制启用）")
    ck(pg.is_visible("#ovAutoSec"), "仍可调刷新间隔")
    ck("自动刷新" in pg.inner_text("#view-overview"), "页面上标明了自动刷新状态")
    # 钩 fetch 数命中次数
    pg.evaluate("""(() => {
      if (!window.__hooked) {
        const of = window.fetch; window.__hits = { ov: 0, ct: 0 };
        window.fetch = function (u) {
          const s = String(u);
          if (s.indexOf('/api/overview') === 0) window.__hits.ov++;
          if (s.indexOf('/api/containers') === 0) window.__hits.ct++;
          return of.apply(this, arguments);
        };
        window.__hooked = 1;
      }
      window.__hits.ov = 0; window.__hits.ct = 0; })()""")
    pg.select_option("#ovAutoSec", "3")
    pg.wait_for_timeout(7200)                       # 约 2 个周期
    ov_hits = pg.evaluate("window.__hits.ov")
    ck(ov_hits >= 2, "进页面就自动刷新（3 秒，无需任何操作）", "命中 %d 次/7.2s" % ov_hits)
    ck("更新于" in pg.inner_text("#ovAge"), "显示最后更新时间", pg.inner_text("#ovAge"))
    # 切走页签必须停（否则在文件/命令页也一直烧 NAS）
    pg.click('.tab[data-v="files"]')
    pg.wait_for_timeout(300)
    before = pg.evaluate("window.__hits.ov")
    pg.wait_for_timeout(4000)
    ck(pg.evaluate("window.__hits.ov") == before, "切到别的页签后总览不再请求（只刷可见页签）",
       "切走后 4s 内新增 %d" % (pg.evaluate("window.__hits.ov") - before))
    # 切回来要自动恢复，不需要任何操作
    pg.click('.tab[data-v="overview"]')
    pg.wait_for_timeout(4500)
    ck(pg.evaluate("window.__hits.ov") > before, "切回总览自动恢复刷新（不用手动开）",
       "新增 %d" % (pg.evaluate("window.__hits.ov") - before))
    # 间隔要记住
    pg.select_option("#ovAutoSec", "10")
    pg.wait_for_timeout(300)
    pg.reload(wait_until="domcontentloaded")
    pg.wait_for_timeout(900)
    ck(pg.eval_on_selector("#ovAutoSec", "el => el.value") == "10", "刷新页面后记住间隔选择")
    # 容器页：资源占用模式下必须暂停（docker stats 一次约 2 秒）
    pg.click('.tab[data-v="containers"]')
    pg.wait_for_timeout(900)
    ck(pg.eval_on_selector("#ctAutoSec", "el => !el.disabled"), "容器列表模式间隔可选")
    pg.click("#btnCtStats")
    pg.wait_for_timeout(400)
    ck(pg.eval_on_selector("#ctAutoSec", "el => el.disabled"), "资源占用模式下自动刷新暂停（间隔禁用）")

    print("== 8. 图表（CPU / 内存） ==")
    pg.click('.tab[data-v="overview"]')
    pg.wait_for_timeout(800)
    ck(pg.is_visible("#chartCard") and pg.is_visible("#chCpu") and pg.is_visible("#chMem"),
       "总览有 CPU / 内存两张图")
    pg.wait_for_function(
        "() => { const c = document.querySelector('#chCpu');"
        " if (!c || !c.width) return false;"
        " const d = c.getContext('2d').getImageData(0, 0, c.width, c.height).data;"
        " let n = 0; for (let i = 3; i < d.length; i += 4) if (d[i] > 8) n++;"
        " return n > 400; }", timeout=20000)
    ck(True, "CPU 图画布真的画出了内容（不是空 canvas）")
    px = pg.evaluate("""() => {
      const c = document.querySelector('#chMem');
      if (!c || !c.width) return 0;
      const d = c.getContext('2d').getImageData(0, 0, c.width, c.height).data;
      let n = 0; for (let i = 3; i < d.length; i += 4) if (d[i] > 8) n++;
      return n; }""")
    ck(px > 400, "内存图画布有内容", "像素 %d" % px)
    ck("%" in pg.inner_text("#chCpuNow"), "图上显示当前 CPU 值", pg.inner_text("#chCpuNow"))
    ck("MB" in pg.inner_text("#chMemNow"), "图上显示当前内存值", pg.inner_text("#chMemNow"))
    # ⚠️ 必须包 IIFE：字符串表达式如果以「函数赋值」结尾，Playwright 会把返回值当函数
    #    用 null 调一次 → 里面 of.apply(this, arguments) 变成 fetch(null) → 404 /null
    #    （留痕记一笔：这个 404 是测试自伤，不是产品问题）
    pg.evaluate("(() => { window.__chHits = 0; const of = window.fetch;"
                " window.fetch = function (u) {"
                " if (String(u).indexOf('/api/charts') === 0) window.__chHits++;"
                " return of.apply(this, arguments); }; })()")
    pg.select_option("#chRange", "5")
    pg.wait_for_timeout(1200)
    ck(pg.evaluate("window.__chHits") >= 1, "切换时间范围真的重新拉图表数据",
       "命中 %d" % pg.evaluate("window.__chHits"))
    pg.select_option("#ovAutoSec", "3")
    # ⚠️ #ovAuto 开关 2026-10-18 已删（强制自动刷新，见第 336 行断言）——
    #    这里不再勾选任何开关，选好间隔直接等一轮即可
    pg.wait_for_timeout(7000)
    ck(pg.evaluate("window.__chHits") >= 2, "自动刷新时图表跟着刷新",
       "命中 %d" % pg.evaluate("window.__chHits"))
    # 图表值要和顶部卡片同源（不能一个 99% 一个 7% —— 之前 CPU 口径 bug 就是这么发现的）
    ck(pg.inner_text("#ovStats").count("CPU 占用") == 1, "顶部 CPU 卡片仍在")

    print("== 8b. 总览排列（2026-10-18 重排：桌面双栏） ==")
    pg.set_viewport_size({"width": 1280, "height": 800})
    pg.wait_for_timeout(400)
    cols = pg.evaluate("getComputedStyle(document.querySelector('.ov-grid')).gridTemplateColumns.split(' ').length")
    ck(cols == 2, "电脑上总览下半区双栏（磁盘 | 容器+系统信息）", "列数 %s" % cols)
    ck(pg.is_visible("#diskCard") and pg.is_visible("#ovMisc"), "两栏里的卡都可见")
    pg.set_viewport_size({"width": 390, "height": 844})
    pg.wait_for_timeout(400)
    cols1 = pg.evaluate("getComputedStyle(document.querySelector('.ov-grid')).gridTemplateColumns.split(' ').length")
    ck(cols1 == 1, "手机上总览下半区仍是单栏", "列数 %s" % cols1)

    print("== 9. 手机适配（390×844） ==")
    pg.set_viewport_size({"width": 390, "height": 844})
    pg.click('.tab[data-v="containers"]')
    pg.wait_for_timeout(1000)
    ck(pg.evaluate("getComputedStyle(document.querySelector('#ctList table tr')).display") == "block",
       "容器表格在手机上变成卡片（不再是竖排单字）")
    ck(pg.evaluate("getComputedStyle(document.querySelector('#ctList thead')).display") == "none",
       "表头隐藏（列名改用 data-label）")
    ck(pg.evaluate("document.querySelector('#ctList').scrollWidth <= document.querySelector('#ctList').clientWidth + 1"),
       "容器卡片不横向溢出")
    row_h = pg.evaluate("document.querySelector('#ctList table tr').getBoundingClientRect().height")
    ck(row_h < 260, "单个容器卡片高度合理（之前被挤成一屏一张）", "%.0f px" % row_h)
    pg.click('.tab[data-v="files"]')
    pg.wait_for_timeout(900)
    ck(pg.evaluate("""() => { const m=document.querySelector('#fsList .fs-row .mt2.mono');
          return !m || getComputedStyle(m).display === 'none'; }"""),
       "文件行在手机上不显示权限位（省地方）")
    lr = pg.evaluate("""() => {
      const r=[...document.querySelectorAll('#fsList .fs-row')].find(
        x => x.textContent.indexOf('非常非常长') >= 0);
      if (!r) return {found:false};
      const nm = r.querySelector('.nm');
      return {found:true, clipped: nm.scrollWidth > nm.clientWidth + 2,
              h: Math.round(r.getBoundingClientRect().height)};
    }""")
    ck(lr.get("found") and not lr.get("clipped"), "长文件名换行显示、不再被省略号截断", lr)
    pg.click('.tab[data-v="cmd"]')
    pg.wait_for_timeout(1000)
    ck(pg.evaluate("""() => !!document.querySelector('#cmdHistory td[data-label="详情"]')"""),
       "审计日志在手机上保留「详情」列")
    det_h = pg.evaluate("""() => { const e=document.querySelector('#cmdHistory td[data-label="详情"]');
        return e ? e.getBoundingClientRect().height : 0; }""")
    ck(det_h > 0, "详情内容真的可见（不是被裁掉）", "%.0f px" % det_h)
    tabs_h = pg.evaluate("document.querySelector('#tabs').getBoundingClientRect().height")
    ck(tabs_h < 70, "五个页签在手机上一行放下（可横滑）", "%.0f px" % tabs_h)
    # 可点区域别太小（拇指友好）：下载按钮高度
    pg.click('.tab[data-v="files"]')
    pg.wait_for_timeout(600)
    btn_h = pg.evaluate("document.querySelector('#fsList .fs-row [data-dl]').getBoundingClientRect().height")
    ck(btn_h >= 22, "下载按钮可点高度 ≥22px", "%.0f px" % btn_h)

    print("== 10. 响应式 + 零 JS 报错 ==")
    for w, h, tag in ((390, 844, "手机"), (768, 1024, "平板"), (1280, 800, "电脑")):
        pg.set_viewport_size({"width": w, "height": h})
        pg.wait_for_timeout(350)
        over = pg.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")
        ck(over <= 0, "%d×%d 无横向溢出" % (w, h), "溢出 %s px" % over)
    # 4xx 白名单：只允许出现在这几处（出现别处 = 真回归）
    def _allowed(s):
        return ((s.startswith("401") and ("/api/overview" in s or "/api/login" in s)) or
                (s.startswith("400") and ("/api/system" in s or "/api/fs?path=" in s)))
    disallowed = [s for s in BAD_RESP if not _allowed(s)]
    if disallowed:
        print("   [诊断] bad fetch arg=%r" % pg.evaluate("window.__badFetchArg || ''"))
        print("   [诊断] stack:\n" + (pg.evaluate("window.__badFetchStack || '(无栈)'") or '')[:1200])
    ck(not disallowed, "全部 4xx 都在预期名单里（首屏401/错口令401/危险区400/越界400）",
       disallowed[:5])
    # 浏览器把"资源加载失败"也记成 console error —— 那些就是上面 4xx 的孪生记录，剔掉；
    # 真正的 JS 异常（pageerror / 其它 console error）一条都不许有
    real_errs = [e for e in JS_ERRS if not e.startswith("CONSOLE: Failed to load resource")]
    ck(not real_errs, "零 JS 报错", real_errs[:3])

    br.close()

print()
print("结果：%d 通过 / %d 失败" % (PASS, FAIL))
try:
    PROC.send_signal(signal.SIGTERM)
    PROC.wait(timeout=5)
except Exception:
    try:
        PROC.kill()
    except Exception:
        pass
for f in (MARKER,):
    try:
        os.remove(f)
    except Exception:
        pass
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if FAIL else 0)
