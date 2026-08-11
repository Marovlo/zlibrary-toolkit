#!/usr/bin/env bash
# zlib 私人 web 面板 安装脚本。
#
# 前提：先执行过仓库根目录的 ./install.sh（已有 .venv）。这里只额外装web 专用
# 依赖（FastAPI/uvicorn，属于 pyproject.toml 的 [web] extras，不影响 CLI 核心
# 依赖），并构建前端。
set -euo pipefail

cd "$(dirname "$0")/.."   # 回到仓库根目录

info()  { printf '\033[36m[install-web]\033[0m %s\n' "$1"; }
error() { printf '\033[31m[install-web]\033[0m %s\n' "$1" >&2; }

if [ ! -d .venv ]; then
    error "未找到 .venv，请先运行仓库根目录的 ./install.sh"
    exit 1
fi

info "安装 web 后端依赖（fastapi/uvicorn，不影响 CLI 核心依赖）..."
.venv/bin/pip install --disable-pip-version-check -q -e ".[web]"

if ! command -v npm >/dev/null 2>&1; then
    error "未找到 npm，请先安装 Node.js（建议 18+）后重新运行本脚本"
    exit 1
fi

info "安装并构建前端..."
(cd webapp/frontend && npm install && npm run build)

echo
info "安装完成！启动面板："
echo "  ./webapp/run.sh"
echo "然后浏览器访问 http://<服务器IP>:8765"
