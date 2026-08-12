"""账号文件热加载与并发写入离线自测。"""
from __future__ import annotations

import multiprocessing
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from zlibrary.accounts import AccountStore


def _add(path: str, email: str) -> None:
    AccountStore.load(path).add_account(email, "password")


def test_refresh_preserves_account_identity() -> None:
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "accounts.yaml"
        store = AccountStore.load(path)
        first = store.add_account("a@example.com", "old")
        other = AccountStore.load(path)
        other.add_account("b@example.com", "b-pass")
        store.refresh_from_disk()
        assert store.by_email("a@example.com") is first
        assert store.by_email("a@example.com").password == "old"
        assert store.by_email("b@example.com") is not None


def test_concurrent_adds_are_merged() -> None:
    with tempfile.TemporaryDirectory() as d:
        path = str(Path(d) / "accounts.yaml")
        processes = [
            multiprocessing.Process(target=_add, args=(path, "a@example.com")),
            multiprocessing.Process(target=_add, args=(path, "b@example.com")),
        ]
        for process in processes:
            process.start()
        for process in processes:
            process.join()
            assert process.exitcode == 0
        store = AccountStore.load(path)
        assert {a.email for a in store.accounts} == {"a@example.com", "b@example.com"}


def test_mark_used_merges_external_account() -> None:
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "accounts.yaml"
        store = AccountStore.load(path)
        account = store.add_account("a@example.com", "a-pass")
        external = AccountStore.load(path)
        external.add_account("b@example.com", "b-pass")
        store.mark_used(account)
        latest = AccountStore.load(path)
        assert latest.by_email("a@example.com").downloads_today == 1
        assert latest.by_email("b@example.com") is not None


def test_invalid_refresh_keeps_state() -> None:
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "accounts.yaml"
        store = AccountStore.load(path)
        account = store.add_account("a@example.com", "a-pass")
        path.write_text("accounts: [", encoding="utf-8")
        try:
            store.refresh_from_disk()
        except Exception:
            pass
        assert store.by_email("a@example.com") is account
        assert account.password == "a-pass"


if __name__ == "__main__":
    test_refresh_preserves_account_identity()
    test_concurrent_adds_are_merged()
    test_mark_used_merges_external_account()
    test_invalid_refresh_keeps_state()
    print("accounts store 自测通过")
