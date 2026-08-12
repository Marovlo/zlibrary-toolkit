"""账号池单例：复用 `zlibrary.accounts.AccountStore`，跨请求共享同一份内存状态。

CLI 和 Web 共用仓库根目录的 accounts.yaml；每次获取单例时检查文件签名，
因此 CLI 新增/更新账号后 Web 无需重启即可看到。
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path

import yaml

from zlibrary.accounts import DEFAULT_DAILY_LIMIT, AccountStore
from zlibrary.config import project_root

log = logging.getLogger("webapp.accounts_store")

_lock = threading.Lock()
_store: AccountStore | None = None
_signature: tuple[int, int] | None = None


def _file_signature(path: Path) -> tuple[int, int] | None:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    return stat.st_mtime_ns, stat.st_size


def get_account_store() -> AccountStore:
    global _store, _signature
    path = project_root() / "accounts.yaml"
    with _lock:
        current_signature = _file_signature(path)
        if _store is None:
            _store = AccountStore.load(path, limit=DEFAULT_DAILY_LIMIT)
            _signature = _file_signature(path)
        elif current_signature != _signature:
            try:
                _store.refresh_from_disk()
            except (OSError, ValueError, yaml.YAMLError) as e:
                log.warning("重新加载账号文件失败，继续使用上次有效状态: %s", e)
            else:
                _signature = _file_signature(path)
        return _store
