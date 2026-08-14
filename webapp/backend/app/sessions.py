"""每个账号独立保存登录态 cookie（区别于 CLI 全局唯一的 data/session.json），
支持面板里在多个账号之间切换时都能"免登录复用"，不用每次都重新走登录。

统一在这里把「网络暂不可用/账号不存在/额度用尽/登录失败」等问题转成简单的
ValueError 消息，调用方直接 `except ValueError`拿到可以展示给用户的文案即可，
不需要关心底层是代理问题还是站点问题。
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from zlibrary.client import ZLibraryClient

from . import archive
from .access import access_state
from .accounts_store import get_account_store


def _sessions_dir() -> Path:
    d = archive.data_dir() / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _session_path(email: str) -> Path:
    key = hashlib.sha256(email.lower().encode("utf-8")).hexdigest()[:16]
    return _sessions_dir() / f"{key}.json"


def get_logged_in_client(account_email: str) -> tuple[ZLibraryClient, object | None]:
    """返回(已就绪的 client, 账号对象或 None)。

    `account_email` 为空字符串或 "anonymous" 时走匿名模式，返回 (client, None)。
    """
    try:
        client = access_state.make_client()
    except Exception as e:  # noqa: BLE001
        raise ValueError("当前网络暂不可用，请稍后重试") from e

    if not account_email or account_email == "anonymous":
        return client, None

    store = get_account_store()
    acc = store.by_email(account_email)
    if not acc:
        client.close()
        raise ValueError(f"账号不存在: {account_email}")
    if not acc.available(store.limit):
        client.close()
        raise ValueError(f"账号 {account_email} 今日下载额度已用尽")

    session_path = _session_path(acc.email)
    saved_email = client.load_session(session_path)
    if saved_email == acc.email:
        state = client.check_logged_in()
        if state:
            return client, acc

    try:
        res = client.login(acc.email, acc.password)
    except Exception:
        client.close()
        try:
            access_state.recover()
            client = access_state.make_client()
            res = client.login(acc.email, acc.password)
        except Exception as e:  # noqa: BLE001
            client.close()
            raise ValueError("登录暂时失败，请稍后重试") from e
    if not res.ok:
        client.close()
        raise ValueError(f"账号 {acc.email} 登录失败: {res.error}")
    client.save_session(session_path, acc.email)
    if res.remaining is not None:
        store.set_remaining(acc, res.remaining)
    return client, acc
