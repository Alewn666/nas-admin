# 检查与修复记录 — nas-admin 移植到老云机 8080（第四十二轮）

日期：2026-10-18　执行：qwenpaw（default）
触发：用户「可以把nas-admin部署到老云机上，端口用8080吧」「可以就用换端口那个方法，然后HTTPS这个你不用担心，
因为我的这个服务器没有经过ICP备案，只能用HTTPS」

---

## 一、结论（TL;DR）

**nas-admin 已在老云机 203.0.113.12:8080 上线，全 HTTPS，外网真浏览器验收 8/8 通过。**

- 入口：`https://203.0.113.12:8080/`（IP 证书，与 443 共用 `/etc/letsencrypt/live/203.0.113.12/`）
- 口令：22 位随机（`openssl rand -base64 27 | tr -d "/+=" | cut -c1-22`），只落在 `/opt/nas-admin/env.nasadmin`(600)
- 架构：nginx `0.0.0.0:8080` SSL → gunicorn `127.0.0.1:8099`（**gunicorn 永不直接对外**）
- 作文批改（443）**未受影响**，70 篇完好
- 云机无 docker → 容器页正确显示「docker 不可用」（预期行为，非故障）

## 二、为什么必须挪端口

8080 被**作文批改**的 gunicorn 占着。它只绑 `127.0.0.1`，所以外网 `:8080` 一直连不上——
这就是之前「8080 明明放行了却探不通」的根因。nginx 已经在反代它，所以从外部看 8080 是"没东西"。

挪法（两处，都可回滚）：
- `/etc/systemd/system/essay-revise.service`：`-b 127.0.0.1:8080` → `8083`
- `/etc/nginx/sites-enabled/essay`：两处 `proxy_pass http://127.0.0.1:8080;` → `8083`
- 备份：`/root/rollback_20261018_135254/{essay.nginx.bak, essay-revise.service.bak}`
- 回滚：还原这两个文件 + `systemctl daemon-reload && systemctl restart essay-revise && systemctl reload nginx`

**验证（挪完立刻做，未出现中断）**：本机 8083 → 200；nginx 443 → 200；NAS 打外网 443 → 200；
作文数 70。`ss` 确认 8080 已空闲。

## 三、部署内容

| 项 | 值 |
|---|---|
| 代码 | `/opt/nas-admin/`（tar 上传，md5 `81b8a1c4…` 本地=云端一致；app.py/app.js 逐文件 md5 一致） |
| venv | `/opt/nas-admin/venv`（Flask 3.1.3 + gunicorn 26.2.0） |
| 服务 | `nas-admin.service`，`Restart=always`，`enable` |
| 环境变量 | `/opt/nas-admin/env.nasadmin`(600)：PW / SECRET / FS_ROOT=/ / NSENTER=0 / DS_CFG |
| nginx | `/etc/nginx/sites-available/nasadmin`（软链进 sites-enabled），`listen 8080 ssl` |
| fail2ban | jail `nas-admin` + filter `nas-admin.conf`，盯 `/opt/nas-admin/data/audit.log` |

### 安全设计（四层）

1. **口令强**：22 位随机，非字典词；只走 env 文件（600），不出现在命令行/进程表/日志
2. **传输加密**：8080 只监听 SSL（无明文端口），复用现有 IP 证书
3. **进程收敛**：gunicorn 只绑回环；systemd 加 `NoNewPrivileges` / `PrivateTmp` / `ProtectSystem=full`
   / `ProtectHome` / `ReadWritePaths=/opt/nas-admin/data`
4. **暴力破解防护**：应用内 5 次锁 30 秒 + fail2ban 网络层封禁（maxretry 5/10min，bantime 1h 起倍增，上限 7 天）

### ⚠️ 一个关键安全坑（已堵，写下来备查）

`app.py:207` 取客户端 IP 的方式是：
```python
request.headers.get("X-Forwarded-For", request.remote_addr).split(",")[0]
```
**取 XFF 的第一个值**。如果 nginx 用业界最常见的 `proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;`，
客户端自带的伪造 XFF 会被**放在最前面** → app 看到的 IP 是攻击者随便填的 →
**fail2ban 封错 IP、按 IP 锁定形同虚设**。

修法（已写进 nginx 配置并加注释）：用 `$remote_addr` **覆盖**而不是追加
```nginx
proxy_set_header X-Forwarded-For $remote_addr;
```
单层反代场景下这是唯一正确写法。

## 四、排障记录（3 个，都是真机才暴露的）

### ① fail2ban jail 加载失败：`Have not found any log file for nas-admin jail`
原因：`/opt/nas-admin/data/audit.log` 当时不存在（面板刚起、还没人登录过 → 从没写过审计记录），
而 **fail2ban 启动时要求 logpath 必须存在**。
修法：`touch /opt/nas-admin/data/audit.log && chmod 644`，再 `systemctl restart fail2ban`。
**教训**：用应用自己的日志当 fail2ban 监控对象时，得先把这个日志文件建出来，否则部署时"看起来配好了"
其实一直没生效（`fail2ban-client status` 里压根没有这个 jail）。

**验证 filter 真能认**：构造 5 行日志跑 `fail2ban-regex` → 3 matched（2×login_fail + 1×lockout，
全攻击者 IP）、2 missed（登录成功、exec —— 这两行**本就不该封**）。确认没有误伤正常操作。

### ② gunicorn 启动报错 `Control server error: [Errno 30] Read-only file system: '/root/.gunicorn'`
原因：**我自己加的** `ProtectSystem=full` 把 `/root` 变只读，而 gunicorn 要写控制 socket 目录。
不致命（worker 正常起来），但日志持续报错。
修法：`Environment=HOME=/opt/nas-admin/data`（指向 `ReadWritePaths` 里那个可写目录）。
重启后日志干净：`Control socket listening at /opt/nas-admin/data/.gunicorn/gunicorn.ctl`。
**教训**：给 systemd 加文件系统加固时，得同时想清楚被管进程的 `HOME` 落在哪。

### ③ 总览「DeepSeek 余额」卡显示「查不到：没找到 DeepSeek key」
原因：`app.py:565` 的默认路径写死成 NAS 布局 `/vol1/1000/项目/作文批改/data/config.json`，
云机上实际是 `/opt/essay-revise/data/config.json`。
修法：**零改码**——`_ds_key()` 早就支持 `NAS_ADMIN_DS_CFG` 环境变量覆盖，云机 env 文件里加上即可。
复验：真打 DeepSeek API 返回 `total=-0.02 CNY / is_available=false`（与 10-18 早前查询一致），
前端卡片显示 `¥-0.02`。
**教训**：代码里"有环境变量可覆盖"的口子要记得用；跨机器移植时，任何写死的绝对路径都是雷。

## 五、验证证据

### 外网端到端（用户在阿里云放行 8080 之后）
```
https://203.0.113.12:8080/api/health → 200（0.119s）
{"configured":true,"ok":true,"service":"nas-admin"}
<title>NAS 管理</title>          ← 确认不是作文批改
https://203.0.113.12/ → 200    ← 作文批改未受影响
作文数: 70
```

### 真浏览器（Playwright，直接打公网，非隧道）
```
[OK] 外网可达 + 标题 NAS 管理
[OK] 登录成功进总览
[OK] 是云机(主机名 iZwz)          ← 不是 NAS 的数据
[OK] 磁盘20G
[OK] 页签[容器] / [文件] / [命令]
[OK] 零 JS 报错 []
外网真浏览器：8 通过 / 0 失败
```
截图内容核对：CPU 0.5% / 内存 271·407MB / 容器 0 运行 0 总数（docker 不存在，正确）/
Swap 0·1024MB / 磁盘 `/data` 1.3G·19.5G、`/` 4.6G·19.3G / 内核 6.12.111+deb13-amd64 /
主机名 iZwz9hxt4nywgz82xbz0ctZ —— **全是云机特征，确认没连错机器**。

### 门禁
```
GET  /api/overview 未登录 → 401
POST /api/login 错口令     → 401
POST /api/login 真口令     → 200
```

### 监听面（gunicorn 必须只在回环）
```
127.0.0.1:8099  users:(("gunicorn",...))     ← 后端
0.0.0.0:8080    users:(("nginx",...))        ← 唯一对外入口
```

### 审计日志真实记录（fail2ban 盯的就是它）
```json
{"ts":"2026-10-18 14:11:03","ip":"127.0.0.1","kind":"auth","action":"login_fail","detail":""}
{"ts":"2026-10-18 14:12:36","ip":"127.0.0.1","kind":"auth","action":"login","detail":"登录成功"}
```

## 六、遗留与建议（等用户拍板，未动）

### 🔴 SSH root 密码登录仍开着 —— 现在最大的暴露面
```
permitrootlogin     yes
passwordauthentication yes
maxauthtries        3
```
密码与 NAS 是**同一串**。这台机现在承载 70 篇真实学生作文 + DeepSeek/mimo 两把 API key，
一旦密码被爆破就是全失。密钥登录已配好并实测可用
（`scripts/cloud_ed25519`，服务器侧注释 `nas-qwenpaw-20260927`）。

建议（**需用户点头，不可逆操作**）：
```bash
sed -i 's/^#\?PermitRootLogin.*/PermitRootLogin prohibit-password/' /etc/ssh/sshd_config
sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config
sshd -t && systemctl reload ssh    # 先测语法再 reload，别把自己关在外面
```
⚠️ 改之前请确认：①密钥能登录（我已实测）②阿里云控制台的 VNC/远程连接作为兜底可用。

### 外网实际暴露面（已实测）
只有 **22 / 80 / 443 / 8080** 四个口。3306/6379/27017/9090 等均"无服务在听"（不是被拦，是压根没装）。
主机层 `nft list ruleset` 为**空**（无防火墙规则），所以此前拦 8080 的是**阿里云安全组**，已由用户放行。

### 其他小项
- 容器页在云机上无意义（无 docker）——是否要按 `NAS_ADMIN_NSENTER`/无 docker 自动隐藏该页签，待定
- 云机 407MB 内存，nas-admin gunicorn worker RSS 约 34MB，压力可忽略
- `ds_balance_watch.py` 的 cron 仍只在云机本地跑（DeepSeek 余额已 -0.02 欠费），与本面板余额卡数据源相同

## 七、回滚点

| 东西 | 位置 |
|---|---|
| 挪端口前的 nginx + systemd | 云机 `/root/rollback_20261018_135254/` |
| nas-admin 代码包 | 云机 `/tmp/nasadmin_cloud.tgz`（md5 `81b8a1c4d78516cb8d4d3ced729165bb`） |
| 停服务 | `systemctl disable --now nas-admin` |
| 摘 nginx 入口 | `rm /etc/nginx/sites-enabled/nasadmin && systemctl reload nginx` |
| 彻底还原 8080 给作文批改 | 还原 rollback 里两个文件 + daemon-reload + restart + reload |

完全停掉 nas-admin **不影响**作文批改（两者只共享 443 证书和 nginx 主进程）。
