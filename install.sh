#!/usr/bin/env bash
# zlib 一键安装脚本。
#
# 做的事：
#   1. 检查 python3（>=3.10）
#   2. 创建虚拟环境 .venv，安装依赖
#   3. 尝试安装 playwright 的 chromium（失败不影响主流程，仅httpx 的 playwright 回退用不了）
#   4. 从模板生成 config.yaml / accounts.yaml（若已存在则不覆盖）
#
# 没有在这里下载 mihomo：本仓库自带 vendor/mihomo-linux-{arch}.gz，
# 首次运行 `zlib` 时会自动从这个随包文件解压出来，完全不需要联网、
# 不需要能连GitHub（这就是"先鸡先蛋"问题的解法——见README/DEV.md）。
# 之后如果想升级 mihomo 到GitHub 最新版，用 `zlib upgrade-mihomo`
#（那时代理已经跑起来了，走这条代理线路去连 GitHub，不依赖本机网络能直连）。
set -euo pipefail

cd "$(dirname "$0")"

info()  { printf '\033[36m[install]\033[0m %s\n' "$1"; }
warn()  { printf '\033[33m[install]\033[0m %s\n' "$1"; }
error() { printf '\033[31m[install]\033[0m %s\n' "$1" >&2; }

#---------- 1. 检查 python3 ----------
if ! command -v python3 >/dev/null 2>&1; then
    error "未找到 python3，请先安装 Python 3.10 及以上版本"
    exit 1
fi

PY_VER=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
PY_MAJOR=$(python3 -c 'import sys; print(sys.version_info[0])')
PY_MINOR=$(python3 -c 'import sys; print(sys.version_info[1])')
if [ "$PY_MAJOR" -lt 3 ] || { [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 10 ]; }; then
    error "检测到 Python $PY_VER，需要 3.10 及以上版本"
    exit 1
fi
info "python3 版本: $PY_VER"

# ---------- 2. 虚拟环境 + 依赖 ----------
if [ ! -d .venv ]; then
    info "创建虚拟环境 .venv ..."
    python3 -m venv .venv
else
    info "虚拟环境 .venv 已存在，跳过创建"
fi

info "安装依赖（pip install -e .）..."
.venv/bin/pip install --disable-pip-version-check -q --upgrade pip
.venv/bin/pip install --disable-pip-version-check -q -e .

# ---------- 3. playwright chromium（可选，失败不阻塞） ----------
info "尝试安装 playwright chromium（用于httpx 失败时的浏览器回退，可选）..."
if .venv/bin/playwright install chromium 2>/tmp/zlib_playwright_install.log; then
    info "playwright chromium 安装成功"
else
    warn "playwright chromium 安装失败（可能是网络问题），不影响主流程，跳过"
    warn "详情见 /tmp/zlib_playwright_install.log；需要时可稍后手动执行:"
    warn "  .venv/bin/playwright install chromium"
fi

# ---------- 4. 配置文件模板 ----------
if [ ! -f config.yaml ]; then
    cp config.example.yaml config.yaml
    warn "已生成 config.yaml，请编辑其中的 subscription_url 为你自己的订阅链接！"
else
    info "config.yaml 已存在，跳过生成"
fi

if [ ! -f accounts.yaml ]; then
    cp accounts.example.yaml accounts.yaml
    info "已生成空的 accounts.yaml，稍后用 zlib add-account 添加账号"
else
    info "accounts.yaml 已存在，跳过生成"
fi

echo
info "安装完成！接下来："
echo "  0. source .venv/bin/activate   # 每次新开终端都需要先执行这一步"
echo "  1. 设置代理订阅（二选一）："
echo "     - zlib set-subscription \"<你的clash订阅链接>\"（会自动验证是否有效）"
echo "     - 或手动编辑 config.yaml 的 subscription_url"
echo "  2. zlib add-account <email> <password>   # 添加账号（可选，不加则匿名下载，受IP每日限额）"
echo "  3. zlib download \"书名\"                  # 搜索并选择下载"
echo
echo "常用命令：zlib status / zlib stop / zlib logout / zlib upgrade-mihomo"
echo "详细说明（账号策略、IP限额、候选排序规则等）：zlib help"
