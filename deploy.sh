#!/bin/sh
# nas-admin 部署（Docker 版）：同步代码 → 建镜像 → 重建容器（宿主模式）
#
# 用法：NAS_ADMIN_PW='口令' sh deploy.sh
# ⚠️ 口令**不在本脚本里**：首次部署需环境变量 NAS_ADMIN_PW（写进
#    <目标>/env.nasadmin，权限 600，只建一次，以后更新代码不再需要给）。
# ⚠️ 容器 = --privileged --pid=host + 挂 /:/host —— 这就是"很高管理权"的实现；
#    端口**只绑局域网 IP**，绝不映射公网。
set -e
SRC=$(cd "$(dirname "$0")" && pwd)
DST=/vol1/1000/项目/nas-admin

[ "$SRC" = "$DST" ] && { echo "SRC==DST，别在目标目录里跑本脚本"; exit 1; }

echo "== 1/5 同步代码（venv 已废弃不用了；data/env 保留）"
sudo -n install -d -m 755 "$DST"
sudo -n cp "$SRC/app.py" "$SRC/Dockerfile" "$SRC/requirements.txt" "$SRC/deploy.sh" \
      "$SRC/test_admin.py" "$SRC/ui_admin.py" "$SRC/verify_live.py" "$DST/"
sudo -n rm -rf "$DST/templates" "$DST/static"
sudo -n cp -r "$SRC/templates" "$SRC/static" "$DST/"
if [ -d "$SRC/docs" ]; then sudo -n cp -r "$SRC/docs" "$DST/"; fi
sudo -n find "$DST" -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true
sudo -n chown -R 1000:1001 "$DST/app.py" "$DST/Dockerfile" "$DST/requirements.txt" \
      "$DST/test_admin.py" "$DST/ui_admin.py" "$DST/verify_live.py" \
      "$DST/templates" "$DST/static" "$DST/docs" 2>/dev/null || true
sudo -n install -d -m 700 "$DST/data"

echo "== 2/5 口令 env 文件（只建一次；老文件缺 SECRET 会自动补）"
gen_secret() { head -c 32 /dev/urandom | md5sum | cut -d' ' -f1; }
if [ ! -f "$DST/env.nasadmin" ]; then
  # 两种给法二选一：明文 NAS_ADMIN_PW=... 或哈希 NAS_ADMIN_PW_HASH=...（推荐，磁盘不落明文）
  if [ -z "$NAS_ADMIN_PW" ] && [ -z "$NAS_ADMIN_PW_HASH" ]; then
    echo "首次部署必须给 NAS_ADMIN_PW=... 或 NAS_ADMIN_PW_HASH=... 环境变量"; exit 1
  fi
  # ⚠️ 值绝不能带引号写进 env 文件：docker run --env-file **不剥引号**（踩过：
  #    printf '"%s"' 写进去 → 容器里口令变 17 字符带双引号 → 登录全 bad_password）
  case "$NAS_ADMIN_PW$NAS_ADMIN_PW_HASH" in
    *\"*|*\'*) echo "口令值不能含引号（docker env-file 不剥引号、不认转义）"; exit 1 ;;
  esac
  umask 077
  if [ -n "$NAS_ADMIN_PW_HASH" ]; then
    printf 'NAS_ADMIN_PW_HASH=%s\nNAS_ADMIN_SECRET=%s\n' "$NAS_ADMIN_PW_HASH" "$(gen_secret)" \
      | sudo -n tee "$DST/env.nasadmin" >/dev/null
    echo "   已生成 $DST/env.nasadmin (600)，**只存哈希，无明文**"
  else
    printf 'NAS_ADMIN_PW=%s\nNAS_ADMIN_SECRET=%s\n' "$NAS_ADMIN_PW" "$(gen_secret)" \
      | sudo -n tee "$DST/env.nasadmin" >/dev/null
    echo "   已生成 $DST/env.nasadmin (600)"
  fi
  sudo -n chmod 600 "$DST/env.nasadmin"
else
  if ! sudo -n grep -q '^NAS_ADMIN_SECRET=' "$DST/env.nasadmin"; then
    # SECRET 是 cookie 签名密钥：不固定的话**每次重建容器所有登录都失效**（用户反馈"每次都要输口令"）
    printf 'NAS_ADMIN_SECRET=%s\n' "$(gen_secret)" | sudo -n tee -a "$DST/env.nasadmin" >/dev/null
    sudo -n chmod 600 "$DST/env.nasadmin"
    echo "   已补 NAS_ADMIN_SECRET（固定签名密钥，重启不掉登录）"
  fi
  echo "   已存在，不覆盖口令（要换口令先删掉它）"
fi

echo "== 3/5 构建镜像（⚠️ 不接管道：构建失败必须炸出来，别被 tail 吃掉退出码）"
cd "$DST"
sudo -n docker build -t nas-admin:latest .

echo "== 4/5 重建容器（端口只绑局域网 IP）"
LAN_IP=$(ip -4 -o addr show scope global 2>/dev/null | awk '{print $4}' | cut -d/ -f1 | head -1)
LAN_IP=${LAN_IP:-192.168.1.100}
echo "   局域网 IP = $LAN_IP"
sudo -n docker rm -f nas-admin 2>/dev/null || true
# 两路绑定：LAN IP 给局域网设备用；127.0.0.1 给本机工具/验收脚本用 ——
# ⚠️ NAS 老 hairpin 问题：宿主机打自己 192.168.1.100:<端口> 必超时（博客 80/作文 8083 同样），
#    nftables DNAT 后出 docker0 被拦；回环那路不走 hairpin，本机永远通。
sudo -n docker run -d --name nas-admin --restart unless-stopped \
  --privileged --pid=host \
  -v /:/host:rw \
  -v "$DST/data:/app/data" \
  -p "127.0.0.1:8091:8091" \
  -p "$LAN_IP:8091:8091" \
  --env-file "$DST/env.nasadmin" \
  nas-admin:latest

echo "== 5/5 健康检查"
# ⚠️ 探活只打 127.0.0.1 —— 宿主机打自己 LAN IP 是 NAS 老 hairpin 问题（必超时，非本项目 bug）。
for i in 1 2 3 4 5 6 7 8; do
  sleep 1
  OUT=$(curl -s -m 4 "http://127.0.0.1:8091/api/health" || true)
  case "$OUT" in
    *'"configured":true'*)   # ⚠️ app.py 的 json.dumps 是紧凑分隔符（无空格）——带空格的模式必 NOMATCH（踩过）
      echo "   本机(回环): $OUT"
      LAN_OUT=$(curl -s -m 3 "http://$LAN_IP:8091/api/health" || true)
      if [ -n "$LAN_OUT" ]; then echo "   局域网IP: $LAN_OUT"
      else echo "   局域网IP: 本机自打自超时（NAS hairpin 老问题，博客 80/作文 8083 同样）——局域网设备不受影响"; fi
      echo "✅ 部署完成 → http://$LAN_IP:8091 （本机回环 http://127.0.0.1:8091）"
      exit 0 ;;
  esac
done
echo "❌ 健康检查没过，看容器日志："
sudo -n docker logs --tail 30 nas-admin
exit 1
