#!/usr/bin/env bash
# 启动 zlib 私人 web 面板（前后端同一端口，FastAPI 托管前端构建产物）。
#
# 可选环境变量：
#   ZLIB_WEB_HOST   监听地址，默认 0.0.0.0（局域网/公网可访问；仅本机访问用 127.0.0.1）
#   ZLIB_WEB_PORT   监听端口，默认 8765
set -euo pipefail
cd "$(dirname "$0")/.."   # 回到仓库根目录

if [ ! -d .venv ]; then
    echo "未找到 .venv，请先运行 ./install.sh 和 ./webapp/install_web.sh" >&2
    exit 1
fi

PORT="${ZLIB_WEB_PORT:-8765}"
HOST="${ZLIB_WEB_HOST:-0.0.0.0}"

exec .venv/bin/python -m uvicorn app.main:app \
    --app-dir webapp/backend \
    --host "$HOST" --port "$PORT" \
    --log-config webapp/backend/logging_config.yaml
