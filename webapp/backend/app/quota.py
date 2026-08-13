"""账号池额度摘要与低额度后台检查。"""
from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from .accounts_store import get_account_store

log = logging.getLogger("webapp.quota")
LOW_BALANCE_THRESHOLD = 2
_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="quota-check")
_lock = threading.Lock()


@dataclass(frozen=True)
class QuotaSnapshot:
    total_remaining: int
    available_accounts: int
    threshold: int = LOW_BALANCE_THRESHOLD
    low_balance: bool = False


def snapshot() -> QuotaSnapshot:
    store = get_account_store()
    remaining = [account.effective_remaining(store.limit) for account in store.accounts]
    total = sum(remaining)
    return QuotaSnapshot(
        total_remaining=total,
        available_accounts=sum(value > 0 for value in remaining),
        low_balance=total <= LOW_BALANCE_THRESHOLD,
    )


def check_in_background() -> None:
    """异步读取账号池，检查失败不影响搜索/下载主流程。"""
    def run() -> None:
        try:
            snapshot()
        except Exception as e:  # noqa: BLE001
            log.warning("后台额度检查失败: %s", e)

    with _lock:
        _executor.submit(run)
