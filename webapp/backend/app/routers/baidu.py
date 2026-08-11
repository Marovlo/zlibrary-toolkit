from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ..health import health_monitor
from ..schemas import AddBaiduRequest, BaiduStatus

router = APIRouter(prefix="/api/baidu", tags=["baidu"])


def _status() -> BaiduStatus:
    from zlibrary.baidupcs import BaiduPCSManager, load_cookies
    from zlibrary.config import Config

    cookies = load_cookies()
    mgr = BaiduPCSManager(Config.load())
    logged_in = mgr.is_logged_in() if cookies else False
    account = ""
    if logged_in:
        account = mgr._run("who").stdout.strip()
    return BaiduStatus(
        configured=cookies is not None,
        logged_in=logged_in,
        account=account,
        binary_version=mgr.binary_version(),
    )


@router.get("", response_model=BaiduStatus)
def get_baidu_status() -> BaiduStatus:
    return _status()


@router.post("", response_model=BaiduStatus)
def add_baidu_cookies(req: AddBaiduRequest) -> BaiduStatus:
    """添加百度网盘 cookies：先验证能否登录，成功才写入 baidu.yaml（跟 CLI
    的 `zlib add-baidu-cookies` 行为一致），失败不会写入。"""
    health_monitor.record_activity()
    from zlibrary.baidupcs import BaiduPCSManager, save_cookies
    from zlibrary.config import Config

    mgr = BaiduPCSManager(Config.load())
    mgr.ensure_binary()
    ok, msg = mgr.login(req.cookies)
    if not ok:
        raise HTTPException(400, f"cookies 验证失败: {msg}")
    save_cookies(req.cookies)
    return _status()
