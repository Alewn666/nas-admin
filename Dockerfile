# nas-admin 镜像：管理 NAS 的高权限网页（**容器宿主模式**）
#
# 容器必须这样起（见 deploy.sh）：
#   --privileged --pid=host   ← 让 nsenter -t 1 能进宿主机命名空间
#   -v /:/host                ← 文件管理的根（NAS_ADMIN_FS_ROOT=/host）
#   --env-file env.nasadmin   ← 口令只走 env 文件，不进镜像、不进命令行
# ⚠️ 端口只绑局域网 IP，绝不映射公网。
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -i https://mirrors.aliyun.com/pypi/simple/ \
    -r requirements.txt
COPY app.py .
COPY templates ./templates
COPY static ./static
ENV NAS_ADMIN_BIND=0.0.0.0 \
    NAS_ADMIN_PORT=8091 \
    NAS_ADMIN_NSENTER=1 \
    NAS_ADMIN_FS_ROOT=/host \
    NAS_ADMIN_DATA=/app/data
EXPOSE 8091
# 镜像里没有 curl，用 python 探活（与作文批改同款思路：interval 大、timeout 宽，真死才报）
HEALTHCHECK --interval=60s --timeout=9s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8091/api/health',timeout=8).status==200 else 1)"
CMD ["python", "app.py"]
