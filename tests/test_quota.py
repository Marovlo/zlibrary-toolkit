"""额度摘要的离线自测。"""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from zlibrary.accounts import Account, AccountStore


def test_each_account_is_capped_at_ten() -> None:
    a = Account("a@example.com", "p", downloads_today=0, remaining=None)
    assert a.effective_remaining(10) == 10
    a.downloads_today = 8
    assert a.effective_remaining(10) == 2
    a.remaining = 20
    assert a.effective_remaining(10) == 10


def test_total_remaining_is_not_unlimited() -> None:
    today = dt.date.today().isoformat()
    store = AccountStore(
        path=Path("/tmp/unused-accounts.yaml"),
        accounts=[
            Account("a@example.com", "p", downloads_today=9, last_reset_date=today, remaining=None),
            Account("b@example.com", "p", downloads_today=10, last_reset_date=today, remaining=None),
        ],
    )
    total = sum(a.effective_remaining(store.limit) for a in store.accounts)
    assert total == 1


if __name__ == "__main__":
    test_each_account_is_capped_at_ten()
    test_total_remaining_is_not_unlimited()
    print("quota 自测通过")
