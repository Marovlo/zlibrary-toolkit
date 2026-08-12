"""账密管理器：多账号轮换、每日下载次数跟踪。

- 从 accounts.yaml 加载（明文，权限 0600）
- 每日 0 点按本地日期重置 downloads_today
- 选号策略：优先 downloads_today < limit 的
- 下载成功后 inc()，达 limit 标记不可用，自动切下一个
- 登录后可用 set_remaining() 用真实剩余次数校正
"""
from __future__ import annotations

import datetime as _dt
import fcntl
import logging
import os
import tempfile
import threading
from contextlib import contextmanager
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
    # webapp 场景下多个下载任务线程可能几乎同时对同一个账号调用 mark_used()，
    # 不加锁的 `+= 1` 不是原子操作，并发下会漏计数；CLI 单线程不受影响，加锁
    # 开销可忽略。不参与 dataclass 的 repr/相等比较。
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    @staticmethod
    def _read_accounts(path: Path) -> list[Account]:
        if not path.exists():
            return []
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        if not isinstance(data, dict):
            raise ValueError("账号文件格式错误：顶层必须是对象")
        accounts = []
        for item in data.get("accounts", []):
            if not isinstance(item, dict) or not item.get("email"):
                continue
            accounts.append(Account(
                email=item["email"],
                password=item.get("password", ""),
                downloads_today=item.get("downloads_today", 0),
                last_reset_date=item.get("last_reset_date", ""),
                remaining=item.get("remaining"),
            ))
        for account in accounts:
            account.maybe_reset()
        return accounts

    @staticmethod
    def _account_data(accounts: list[Account]) -> dict:
        return {
            "accounts": [
                {
                    "email": account.email,
                    "password": account.password,
                    "downloads_today": account.downloads_today,
                    "last_reset_date": account.last_reset_date,
                    "remaining": account.remaining,
                }
                for account in accounts
            ]
        }

    @contextmanager
    def _file_lock(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.path.with_name(self.path.name + ".lock")
        with open(lock_path, "a+") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def _write_accounts(self, accounts: list[Account]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                yaml.safe_dump(self._account_data(accounts), f, allow_unicode=True, sort_keys=False)
            os.replace(tmp_name, self.path)
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass
        finally:
            try:
                os.unlink(tmp_name)
            except FileNotFoundError:
                pass

    def _sync_accounts(self, fresh: list[Account]) -> None:
        old = {account.email: account for account in self.accounts}
        synced = []
        for incoming in fresh:
            account = old.get(incoming.email)
            if account is None:
                account = incoming
            else:
                account.password = incoming.password
                account.downloads_today = incoming.downloads_today
                account.last_reset_date = incoming.last_reset_date
                account.remaining = incoming.remaining
            synced.append(account)
        self.accounts[:] = synced

    @classmethod
    def load(cls, path: str | Path, limit: int = DEFAULT_DAILY_LIMIT) -> "AccountStore":
        p = Path(path)
        if not p.exists():
            log.warning("账号文件不存在: %s", p)
        return cls(path=p, accounts=cls._read_accounts(p), limit=limit)

    def refresh_from_disk(self) -> None:
        """原地读取外部更新，保持已有 Account 引用有效。"""
        with self._lock:
            self._sync_accounts(self._read_accounts(self.path))

    def save(self) -> None:
        """保存当前内存状态；业务更新应优先使用 add/mark/set 方法。"""
        with self._lock, self._file_lock():
            self._write_accounts(self.accounts)

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
        """添加或更新账号并立即持久化，合并其他进程刚写入的账号。"""
        with self._lock, self._file_lock():
            fresh = self._read_accounts(self.path)
            by_email = {account.email: account for account in fresh}
            account = by_email.get(email)
            if account is None:
                account = Account(
                    email=email, password=password, downloads_today=0,
                    last_reset_date=_dt.date.today().isoformat(), remaining=remaining,
                )
                fresh.append(account)
            else:
                account.password = password
                if remaining is not None:
                    account.remaining = remaining
            self._write_accounts(fresh)
            self._sync_accounts(fresh)
            return self.by_email(email)

    def mark_used(self, acc: Account) -> None:
        with self._lock, self._file_lock():
            fresh = self._read_accounts(self.path)
            current = next((item for item in fresh if item.email == acc.email), None)
            if current is None:
                raise ValueError(f"账号已从账号池移除: {acc.email}")
            current.downloads_today += 1
            if current.remaining is not None:
                current.remaining = max(0, current.remaining - 1)
            self._write_accounts(fresh)
            self._sync_accounts(fresh)
            log.info("账号 %s 今日下载 %d/%d", current.email, current.downloads_today, self.limit)

    def set_remaining(self, acc: Account, remaining: int) -> None:
        with self._lock, self._file_lock():
            fresh = self._read_accounts(self.path)
            current = next((item for item in fresh if item.email == acc.email), None)
            if current is None:
                raise ValueError(f"账号已从账号池移除: {acc.email}")
            current.remaining = remaining
            self._write_accounts(fresh)
            self._sync_accounts(fresh)
