# NAS 管理网页

局域网内的高权限 NAS 管理页(Flask 8091)：口令只从环境变量读(支持 PBKDF2 哈希)、服务端 session、错 5 次锁 30 秒、删目录/重启/关机等危险操作二次确认、全部动作落审计日志。设计上绝不映射公网。

> 本仓库由 NAS 归档包整理发布。已剥离运行时数据、密钥、审计日志与大型二进制镜像；
> 文档中的真实口令与内网 IP 已脱敏。镜像请用仓库内构建脚本生成，或从 Releases 获取。

## 目录结构

```
├── Dockerfile
├── app.py
├── deploy.sh
├── docs/
├── requirements.txt
├── shot_balance.py
├── static/
├── templates/
├── docs/检查与修复记录.md
├── docs/检查与修复记录_第四十三轮_口令哈希.md
├── docs/检查与修复记录_第四十二轮_nas-admin部署云机.md
├── static/css/
├── static/js/
├── templates/index.html
```

## 使用

请参考仓库内的说明文档与构建/部署脚本；首次运行前把示例地址、口令改成自己的值。

## 安全约定

- `data/`、`.env`、`env.*`、`*.pem`、`*.keystore`、`config.json`、`*.db`、`*.log` 均已在 `.gitignore` 中排除，切勿强制提交。
- 文档里的 `192.168.1.100` 与 `203.0.113.x` 是示例地址，`<REDACTED_PASSWORD>` 是脱敏占位符。
- 涉及管理后台的服务请只在可信局域网内使用。

## 许可证

MIT
