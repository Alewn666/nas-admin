#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""nas-admin 真机截图：登录 → 总览 → 核对 DeepSeek 余额格显示真值。只读，不改任何数据。"""
import os
import sys

from playwright.sync_api import sync_playwright

BASE = os.environ.get("BASE", "http://127.0.0.1:8091")
PW = os.environ.get("NAS_ADMIN_PW", "")
if not PW:
    print("❌ 需要 NAS_ADMIN_PW 环境变量")
    sys.exit(1)
OUT = os.environ.get("SHOT", "/tmp/nasadmin_bal.png")

fails = []


def ck(cond, name, extra=""):
    print(("  ✔ " if cond else "  ✘ ") + name + (("  | " + str(extra)[:160]) if (extra and not cond) else ""))
    if not cond:
        fails.append(name)


with sync_playwright() as p:
    b = p.chromium.launch()
    pg = b.new_page(viewport={"width": 1280, "height": 800})
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.goto(BASE, wait_until="domcontentloaded")
    pg.fill("#pwInput", PW)
    pg.click("#btnPwOk")
    pg.wait_for_selector("#balStat .v", timeout=10000)
    pg.wait_for_timeout(1200)          # 等余额接口回来渲染
    v = pg.inner_text("#balStat .v")
    ck("¥" in v, "余额格显示金额真值", v)
    ck("-0.02" in v, "金额与 /api/balance 一致（-0.02）", v)
    ck("已欠费" in v, "is_available=false → 显示「已欠费」提示", v)
    ck(pg.is_visible("#balStat [data-bal-refresh]"), "↻ 按钮可见")
    # 点 ↻ 强刷：应仍显示同一数值（余额没变）
    pg.click("#balStat [data-bal-refresh]")
    pg.wait_for_timeout(900)
    v2 = pg.inner_text("#balStat .v")
    ck("-0.02" in v2, "点 ↻ 强刷后数值仍正确", v2)
    ck(not errs, "零 JS 报错", errs[:2])
    pg.screenshot(path=OUT, full_page=False)
    print("截图：" + OUT)
    b.close()

print()
print("结果：%d 项失败" % len(fails))
sys.exit(1 if fails else 0)
