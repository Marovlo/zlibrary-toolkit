from __future__ import annotations

from fastapi import APIRouter

from ..health import health_monitor
from ..schemas import StatusOut

router = APIRouter(prefix="/api/status", tags=["status"])


@router.get("", response_model=StatusOut)
def get_status(refresh: bool = False) -> StatusOut:
    """立即返回后台健康监控线程维护的最新状态（非阻塞）。

    `refresh=true` 只是提前唤醒后台线程立刻复检一次，接口本身仍立刻返回当前
    已知状态（复检结果会在下一次轮询时体现），不会让本次请求等复检跑完。
    """
    if refresh:
        health_monitor.request_refresh()
    return StatusOut(**health_monitor.snapshot())
