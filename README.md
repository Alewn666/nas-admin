# 🛡️ NAS 管理网页

![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)
![Python](https://img.shields.io/badge/Python-3.11-3776AB?logo=python&logoColor=white)
![Release](https://img.shields.io/github/v/release/Alewn666/nas-admin?label=release)
![LAN only](https://img.shields.io/badge/%E9%83%A8%E7%BD%B2-%E4%BB%85%E5%B1%80%E5%9F%9F%E7%BD%91-critical)

> 一个给自家 NAS 用的高权限管理页（8091）：看资源曲线、管文件、管容器、执行命令、重启关机。**因为它权限很高，所以安全设计是第一优先级**——口令只走环境变量（支持 PBKDF2 哈希，磁盘不落明文）、错 5 次锁 30 秒、危险操作二次确认、所有动作写审计日志，并且**设计上绝不映射公网**。

常见的 NAS 管理页要么功能太浅，要么把 root 级能力随随便便暴露在网页上。这个项目的做法是把「能做什么」和「怎么防止做错」放在同等位置：文件管理、容器启停、命令执行都做，但每一类危险动作都有对应的护栏，而且口令体系经过专门一轮重构（见第 43 轮修复记录）。

## 功能

| 分类 | API / 能力 |
| --- | --- |
| 总览 | `/api/overview` 主机信息、`/api/charts` 历史曲线（TTL 缓存）、`/api/balance` 负载均衡视图 |
| 文件 | `/api/fs` 列目录、读、保存、建目录、上传、下载、**整目录打包下载**、删除 |
| 容器 | `/api/containers` 列表、`/api/container` 启停、`/api/container/logs` 日志 |
| 执行 | `/api/exec` 在宿主机执行命令 |
| 系统 | `/api/system` 重启 / 关机 |
| 审计 | `/api/audit` 全量动作日志 |
| 健康检查 | `/api/health`（Dockerfile 内用 Python 探活，不依赖镜像里的 curl） |

## 安全设计

| 机制 | 具体实现 |
| --- | --- |
| 口令不落盘 | 首次部署给 `NAS_ADMIN_PW` 或 `NAS_ADMIN_PW_HASH`，写入 `env.nasadmin`（权限 **600**），只建一次；推荐直接给 **PBKDF2 哈希**，明文不进磁盘也不进命令行 |
| 会话 | 服务端 session，`NAS_ADMIN_SECRET` 自动生成并补进 env 文件，可配 `NAS_ADMIN_SESSION_DAYS` |
| 防爆破 | 连续错 5 次锁定 30 秒 |
| 防误操作 | 删目录、重启、关机等动作一律**二次确认** |
| 可追溯 | 每个动作落到审计日志 |
| 网络边界 | 端口只绑局域网 IP，**部署脚本明确不映射公网** |

## 容器为什么这么起

管理宿主机需要进它的命名空间，所以容器用 `--privileged --pid=host`，并挂 `/:/host`，通过 `nsenter -t 1` 操作宿主；文件管理的根由 `NAS_ADMIN_FS_ROOT=/host` 指定。这不是疏忽，是刻意的高权限设计——因此前面那些护栏和网络边界才不能省。

```bash
NAS_ADMIN_PW_HASH='sha256:<salt>.<dk>' sh deploy.sh
```

`deploy.sh` 会同步代码 → 首次生成 env 文件 → 建镜像 → 重建容器，并给容器配健康检查。它还有一条自毁护栏：如果当前目录就是部署目标目录，直接拒绝运行。

## 快速开始

```bash
# ⚠️ 仅限可信局域网，不要把 8091 映射到公网或做端口转发
NAS_ADMIN_PW='你的口令' sh deploy.sh      # 首次部署
sh deploy.sh                              # 之后更新代码不再需要给口令
```

本地开发（不带宿主权限，功能会受限）：

```bash
pip install -r requirements.txt
NAS_ADMIN_PW='你的口令' NAS_ADMIN_PORT=8091 python3 app.py
```

## 配置

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `NAS_ADMIN_PW` / `NAS_ADMIN_PW_HASH` | — | 登录口令 / 推荐使用的 PBKDF2 哈希 |
| `NAS_ADMIN_SECRET` | 自动生成 | session 签名密钥 |
| `NAS_ADMIN_PORT` / `NAS_ADMIN_BIND` | `8091` / `0.0.0.0` | 监听地址与端口 |
| `NAS_ADMIN_FS_ROOT` | `/host` | 文件管理可见的根 |
| `NAS_ADMIN_NSENTER` | `1` | 通过 `nsenter -t 1` 进宿主命名空间 |
| `NAS_ADMIN_DATA` | `/app/data` | 历史数据与审计日志目录 |
| `NAS_ADMIN_PBKDF2_ROUNDS` | — | 口令派生轮数 |
| `NAS_ADMIN_SESSION_DAYS` | — | session 有效期 |
| `NAS_ADMIN_HIST_INTERVAL` / `NAS_ADMIN_HIST_MAX` | — | 历史曲线采样间隔与保留点数 |
| `NAS_ADMIN_BAL_TTL` | — | 负载均衡视图缓存时间 |

## 目录结构

```
nas-admin/
├── app.py                    # Flask 服务（约 1150 行，含全部 API 与护栏）
├── deploy.sh                 # NAS Docker 部署（同步 / env / 建镜像 / 重建）
├── Dockerfile                # 宿主模式镜像，含 HEALTHCHECK
├── templates/ static/        # 单页管理界面
├── test_admin.py             # ~31 KB 的功能与安全回归
├── verify_hash_login.py      # 哈希口令登录专项验证
├── verify_live.py            # 对运行中服务做活体校验
├── ui_admin.py / shot_balance.py  # 界面回归与截图
└── docs/
    ├── 检查与修复记录.md
    ├── 检查与修复记录_第四十二轮_nas-admin部署云机.md
    └── 检查与修复记录_第四十三轮_口令哈希.md
```

## 测试

```bash
python3 test_admin.py
python3 verify_hash_login.py
python3 verify_live.py
```

`docs/` 里保留了第 42、43 轮的修复记录（部署云机、口令哈希重构），能看出哪些护栏是被真实问题倒逼出来的，而不是一开始就写好的口号。

## 说在前面

- **这是高权限工具，只适合自己可控的 NAS 和可信局域网**。任何把它暴露到公网的想法都请先打住。
- `--privileged --pid=host` + 挂宿主根目录意味着容器几乎等同于宿主 root，请自行评估风险。
- `env.nasadmin`、`data/`、审计日志、私钥与真实口令均未纳入公开仓库；文档中的 IP 与口令是示例值。

## 许可证

MIT — 见 [LICENSE](LICENSE)。本项目由 NAS 归档包整理后公开发布。
