"""账号池单例：复用 `zlibrary.accounts.AccountStore`，跨请求共享同一份内存状态，
写操作即时持久化到仓库根目录的 accounts.yaml（跟 CLI 共用同一份账号池文件）。
"""
from __future__ import annotations

import threading

from zlibrary.accounts import DEFAULT_DAILY_LIMIT, AccountStore
from zlibrary.config import project_root

_lock = threading.Lock()
_store: AccountStore | None = None


def get_account_store() -> AccountStore:
    global _store
    with _lock:
        if _store is None:
            path = project_root() / "accounts.yaml"
            _store = AccountStore.load(path, limit=DEFAULT_DAILY_LIMIT)
        return _store
