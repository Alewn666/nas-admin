#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
nas-admin 口令哈希模式 · 真机登录验收（NAS / 云机通用）

为什么单独写这个脚本：
  验收要「用明文口令去敲一次登录」，但明文**绝不能出现在命令行参数里**
  （会进 ps / shell 历史）。所以明文从一个 600 的临时文件读，命令行只给文件路径。

用法：
  python3 verify_hash_login.py http://127.0.0.1:8091 /tmp/xxx_pwfile

断言（任一失败 → 退出码 1）：
  1. /api/health   configured=true
  2. 明文口令登录 → 200
  3. 明文口令 + 一个错字符 → 401（证明比的是哈希，不是「有值就放行」）
  4. 大小写错 → 401
  5. 响应体里不出现 sha256:（不泄露哈希）
  6. 未登录直接打 /api/overview → 401
  7. env 文件里不再有 NAS_ADMIN_PW 明文行（可选：--env-file 指定时）
"""
import json
import os
import re
import sys
import urllib.error
import urllib.request


def post_json(url, payload, timeout=15):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def get(url, timeout=15):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    base = sys.argv[1].rstrip("/")
    pwfile = sys.argv[2]
    envfile = None
    if "--env-file" in sys.argv:
        envfile = sys.argv[sys.argv.index("--env-file") + 1]

    try:
        pw = open(pwfile, "r", encoding="utf-8").read().strip()
    except OSError as e:
        print("❌ 读不到明文口令文件 %s：%s" % (pwfile, e))
        return 2
    if not pw:
        print("❌ 明文口令文件是空的")
        return 2

    bad = 0

    def ck(cond, name, extra=""):
        nonlocal bad
        if cond:
            print("  ✔ %s" % name)
        else:
            bad += 1
            print("  ✘ %s %s" % (name, extra))

    print("== 口令哈希模式真机验收： %s ==" % base)

    st, body = get(base + "/api/health")
    ck(st == 200 and '"configured":true' in body.replace(" ", ""),
       "健康检查 configured=true", (st, body[:80]))

    st, body = post_json(base + "/api/login", {"password": pw})
    ck(st == 200, "明文口令登录 → 200", (st, body[:120]))
    ck("sha256:" not in body, "登录响应不泄露哈希", body[:120])

    st, body = post_json(base + "/api/login", {"password": pw + "x"})
    ck(st == 401, "错一个字符 → 401（不是「有值就放行」）", (st, body[:80]))

    st, body = post_json(base + "/api/login", {"password": pw.upper()})
    ck(st == 401, "大小写错 → 401", (st, body[:80]))

    # 未登录的受保护接口：开新连接（不带 cookie）
    st, body = get(base + "/api/overview")
    ck(st == 401, "未登录 /api/overview → 401", (st, body[:80]))

    if envfile:
        try:
            txt = open(envfile, "r", encoding="utf-8").read()
        except OSError as e:
            print("  ⚠ 读不到 env 文件 %s：%s" % (envfile, e))
            txt = ""
        if txt:
            ck(not re.search(r"^NAS_ADMIN_PW=", txt, re.M),
               "env 文件里没有 NAS_ADMIN_PW 明文行", "")
            ck(bool(re.search(r"^NAS_ADMIN_PW_HASH=sha256:[0-9a-f]+\.[0-9a-f]+$", txt, re.M)),
               "env 文件里有规范的 NAS_ADMIN_PW_HASH 行", "")
            ck("NAS_ADMIN_PW=" not in txt.replace("NAS_ADMIN_PW_HASH=", ""),
               "env 里不含任何裸明文口令（排除 HASH 前缀后）", "")

    print()
    print("结果：%d 项失败" % bad)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
