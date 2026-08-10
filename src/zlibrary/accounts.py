"""账密管理器：多账号轮换、每日下载次数跟踪。

- 从 accounts.yaml 加载（明文，权限 0600）
- 每日 0 点按本地日期重置 downloads_today
- 选号策略：优先 downloads_today < limit 的
- 下载成功后 inc()，达 limit 标记不可用，自动切下一个
- 登录后可用 set_remaining() 用真实剩余次数校正
"""
from __future__ import annotations

import datetime as _dt
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

log = logging.getLogger(__name__)

DEFAULT_DAILY_LIMIT = 10


@dataclass
class Account:
    email: str
    password: str
    downloads_today: int = 0
    last_reset_date: str = ""
    remaining: int | None = None  # 登录后从账户页读取的真实剩余，用于校正

    def today(self) -> str:
        return _dt.date.today().isoformat()

    def maybe_reset(self) -> bool:
        """跨日重置，返回是否发生了重置。"""
        today = self.today()
        if self.last_reset_date != today:
            self.downloads_today = 0
            self.remaining = None
            self.last_reset_date = today
            return True
        return False

    def available(self, limit: int = DEFAULT_DAILY_LIMIT) -> bool:
        self.maybe_reset()
        if self.remaining is not None:
            return self.remaining > 0
        return self.downloads_today < limit


@dataclass
class AccountStore:
    path: Path
    accounts: list[Account] = field(default_factory=list)
    limit: int = DEFAULT_DAILY_LIMIT

    @classmethod
    def load(cls, path: str | Path, limit: int = DEFAULT_DAILY_LIMIT) -> "AccountStore":
        p = Path(path)
        if not p.exists():
            log.warning("账号文件不存在: %s", p)
            return cls(path=p, limit=limit)
        with open(p, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        accs = [
            Account(
                email=a["email"],
                password=a.get("password", ""),
                downloads_today=a.get("downloads_today", 0),
                last_reset_date=a.get("last_reset_date", ""),
                remaining=a.get("remaining"),
            )
            for a in data.get("accounts", [])
            if a.get("email")
        ]
        store = cls(path=p, accounts=accs, limit=limit)
        for a in accs:
            a.maybe_reset()
        return store

    def save(self) -> None:
        data = {
            "accounts": [
                {
                    "email": a.email,
                    "password": a.password,
                    "downloads_today": a.downloads_today,
                    "last_reset_date": a.last_reset_date,
                    "remaining": a.remaining,
                }
                for a in self.accounts
            ]
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)
        os.replace(tmp, self.path)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def next_available(self, exclude: set[str] | None = None) -> Account | None:
        """选下一个可用账号（排除本次会话已尝试失败的）。"""
        for a in self.accounts:
            if exclude and a.email in exclude:
                continue
            if a.available(self.limit):
                log.info("选用账号 %s（今日已下 %d/%d，真实剩余 %s）",
                         a.email, a.downloads_today, self.limit, a.remaining)
                return a
        return None

    def by_email(self, email: str) -> Account | None:
        """按邮箱查找账号，用于复用已保存的登录态。"""
        for a in self.accounts:
            if a.email == email:
                return a
        return None

    def add_account(self, email: str, password: str, remaining: int | None = None) -> Account:
        """添加或更新账号并立即持久化。已存在同邮箱则更新密码和剩余次数。"""
        existing = self.by_email(email)
        if existing:
            existing.password = password
            if remaining is not None:
                existing.remaining = remaining
            self.save()
            return existing
        acc = Account(
            email=email, password=password, downloads_today=0,
            last_reset_date=_dt.date.today().isoformat(), remaining=remaining,
        )
        self.accounts.append(acc)
        self.save()
        return acc

    def mark_used(self, acc: Account) -> None:
        acc.downloads_today += 1
        if acc.remaining is not None:
            acc.remaining = max(0, acc.remaining - 1)
        self.save()
        log.info("账号 %s 今日下载 %d/%d", acc.email, acc.downloads_today, self.limit)

    def set_remaining(self, acc: Account, remaining: int) -> None:
        acc.remaining = remaining
        self.save()
