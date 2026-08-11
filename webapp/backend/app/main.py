"""Web 私人面板入口：FastAPI app，托管 API + 前端构建产物。

跟 CLI（`src/zlibrary`）完全解耦：只 import 现有包做搜索/下载/账号/代理复用，
不修改 CLI 任何代码。本进程（uvicorn）监听的端口是独立的 TCP 监听 socket，
跟 mihomo 无关——mihomo 只在这里的代码里作为「出站代理」被显式传给 httpx
client（见 `access.py` -> `zlibrary.cli._make_client`），从未设置任何全局代理
环境变量，因此这个 web 服务本身的对外访问能力不受 mihomo 运行状态影响。
"""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from .health import health_monitor
from .routers import accounts, archive, baidu, download, search, status

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
for _noisy in ("httpx", "httpcore", "playwright", "urllib3"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

app = FastAPI(title="ZLib 私人面板")


@app.on_event("startup")
def _start_health_monitor() -> None:
    # 进程启动即开始探测（直连检测/起代理/测速选优/校验可达），不等第一个
    # HTTP 请求打进来才同步阻塞着做，避免打开网页第一次查询卡住很久。
    health_monitor.start()


app.include_router(accounts.router)
app.include_router(search.router)
app.include_router(download.router)
app.include_router(archive.router)
app.include_router(baidu.router)
app.include_router(status.router)

_FRONTEND_DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"
if _FRONTEND_DIST.exists():
    app.mount("/", StaticFiles(directory=str(_FRONTEND_DIST), html=True), name="frontend")
else:
    @app.get("/")
    def _no_frontend() -> dict:
        return {"message": "前端尚未构建：cd webapp/frontend && npm install && npm run build"}
