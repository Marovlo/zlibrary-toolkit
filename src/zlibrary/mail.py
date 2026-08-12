"""通过 IMAP 读取 Catch-all 转发的注册验证码邮件。"""
from __future__ import annotations

import email
import imaplib
import os
import re
import ssl
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.header import decode_header, make_header
from email.utils import getaddresses, parsedate_to_datetime
from pathlib import Path

import yaml
from bs4 import BeautifulSoup

from .config import project_root


class MailError(RuntimeError):
    """邮箱配置、连接或邮件解析失败。"""


class MailConfigError(MailError):
    pass


class MailTimeout(MailError):
    pass


@dataclass(frozen=True)
class MailConfig:
    host: str = "imap.qq.com"
    port: int = 993
    username: str = ""
    password: str = field(repr=False, default="")
    mailbox: str = "INBOX"
    connect_timeout: float = 10.0
    poll_interval: float = 3.0
    wait_timeout: float = 120.0

    @classmethod
    def load(cls, path: str | Path | None = None) -> "MailConfig":
        p = Path(path) if path else project_root() / "mail.yaml"
        if not p.exists():
            raise MailConfigError(f"邮箱配置不存在: {p}，请复制 mail-example.yaml 后填写")
        try:
            data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as e:
            raise MailConfigError(f"无法读取邮箱配置 {p}: {e}") from e
        if not isinstance(data, dict) or not isinstance(data.get("imap"), dict):
            raise MailConfigError(f"邮箱配置格式错误: {p}，需要 imap 配置段")
        values = data["imap"]
        host = str(values.get("host") or "imap.qq.com").strip()
        username = str(values.get("username") or "").strip()
        password = str(values.get("password") or "")
        mailbox = str(values.get("mailbox") or "INBOX").strip()
        if not host or not username or not password or not mailbox:
            raise MailConfigError("mail.yaml 的 imap.host/username/password/mailbox 不能为空")
        try:
            port = int(values.get("port", 993))
            connect_timeout = float(values.get("connect_timeout", 10))
            poll_interval = float(values.get("poll_interval", 3))
            wait_timeout = float(values.get("wait_timeout", 120))
        except (TypeError, ValueError) as e:
            raise MailConfigError("mail.yaml 的端口和超时配置必须是数字") from e
        if not 1 <= port <= 65535:
            raise MailConfigError("mail.yaml 的 imap.port 不在有效范围内")
        if connect_timeout <= 0 or poll_interval <= 0 or wait_timeout <= 0:
            raise MailConfigError("mail.yaml 的超时配置必须大于 0")
        if os.name == "posix" and p.stat().st_mode & 0o077:
            raise MailConfigError(f"邮箱配置权限过宽: {p}，请执行 chmod 600 {p}")
        return cls(
            host=host,
            port=port,
            username=username,
            password=password,
            mailbox=mailbox,
            connect_timeout=connect_timeout,
            poll_interval=poll_interval,
            wait_timeout=wait_timeout,
        )


class VerificationMailbox:
    """从一个 IMAP 收件箱中读取指定收件人的最新验证码。"""

    _SUBJECT_MARKERS = ("确认码", "验证码", "confirmation code", "verification code")
    _CODE_RE = re.compile(
        r"(?:确认码|验证码|confirmation\s+code|verification\s+code)"
        r"[^\d]{0,30}(\d{4,8})(?!\d)",
        re.IGNORECASE,
    )

    def __init__(self, config: MailConfig):
        self.config = config

    def _connect(self):
        try:
            box = imaplib.IMAP4_SSL(
                self.config.host,
                self.config.port,
                ssl_context=ssl.create_default_context(),
                timeout=self.config.connect_timeout,
            )
            box.login(self.config.username, self.config.password)
            status, _ = box.select(self.config.mailbox, readonly=True)
            if status != "OK":
                raise MailError(f"无法打开邮箱文件夹: {self.config.mailbox}")
            return box
        except MailError:
            raise
        except imaplib.IMAP4.error as e:
            raise MailError("QQ 邮箱 IMAP 登录或选择收件箱失败，请检查授权码") from e
        except OSError as e:
            raise MailError(f"无法连接 IMAP 服务器 {self.config.host}:{self.config.port}") from e

    @staticmethod
    def _search(box, recipient: str) -> list[bytes]:
        status, data = box.uid("search", None, "TO", recipient)
        if status != "OK":
            raise MailError("IMAP 搜索邮件失败")
        return data[0].split() if data and data[0] else []

    def snapshot(self, recipient: str) -> set[str]:
        """记录注册前已经存在的匹配邮件 UID。"""
        box = self._connect()
        try:
            return {uid.decode("ascii", errors="ignore") for uid in self._search(box, recipient)}
        finally:
            try:
                box.logout()
            except Exception:
                pass

    def wait_for_code(
        self,
        recipient: str,
        seen_uids: set[str] | None = None,
        not_before: datetime | None = None,
        timeout: float | None = None,
    ) -> str:
        """等待并返回指定收件人的验证码，不修改邮件已读状态。"""
        seen = seen_uids or set()
        start = not_before or datetime.now(timezone.utc)
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        deadline = time.monotonic() + (timeout or self.config.wait_timeout)
        while time.monotonic() < deadline:
            box = self._connect()
            try:
                for uid in reversed(self._search(box, recipient)):
                    uid_text = uid.decode("ascii", errors="ignore")
                    if uid_text in seen:
                        continue
                    status, fetched = box.uid("fetch", uid, "(BODY.PEEK[])")
                    if status != "OK":
                        continue
                    raw = b"".join(item[1] for item in fetched if isinstance(item, tuple))
                    message = email.message_from_bytes(raw)
                    if not self._is_candidate(message, recipient, start):
                        continue
                    code = self._extract_code(message)
                    if code:
                        return code
            finally:
                try:
                    box.logout()
                except Exception:
                    pass
            time.sleep(self.config.poll_interval)
        raise MailTimeout(f"等待验证码超时（收件地址: {recipient}）")

    @classmethod
    def _is_candidate(cls, message: email.message.Message, recipient: str, not_before: datetime) -> bool:
        headers = [message.get(name, "") for name in ("To", "Delivered-To", "X-Original-To", "Envelope-To")]
        addresses = {addr.casefold() for _, addr in getaddresses(headers) if addr}
        # QQ 转发的裸 To 头有时会被 email.parser 当成空地址，补充标准邮箱地址匹配。
        for header in headers:
            addresses.update(match.casefold() for match in re.findall(
                r"[A-Z0-9._%+\-]+@[A-Z0-9.\-]+", str(header), re.IGNORECASE
            ))
        if recipient.casefold() not in addresses:
            return False
        subject = cls._decode_header(message.get("Subject", ""))
        if not any(marker.casefold() in subject.casefold() for marker in cls._SUBJECT_MARKERS):
            return False
        date_value = message.get("Date")
        if date_value:
            try:
                message_date = parsedate_to_datetime(date_value)
                if message_date.tzinfo is None:
                    message_date = message_date.replace(tzinfo=timezone.utc)
                if message_date < not_before:
                    return False
            except (TypeError, ValueError, OverflowError):
                pass
        return True

    @classmethod
    def _extract_code(cls, message: email.message.Message) -> str | None:
        text = cls._body_text(message)
        matches = list(cls._CODE_RE.finditer(text))
        codes = {match.group(1) for match in matches}
        if len(codes) > 1:
            raise MailError("验证码邮件中发现多个不同验证码，已停止猜测")
        return next(iter(codes), None)

    @staticmethod
    def _decode_header(value: str) -> str:
        return str(make_header(decode_header(value or "")))

    @staticmethod
    def _body_text(message: email.message.Message) -> str:
        parts = message.walk() if message.is_multipart() else [message]
        text_parts: list[str] = []
        for part in parts:
            if part.get_content_type() not in ("text/plain", "text/html"):
                continue
            if "attachment" in str(part.get("Content-Disposition", "")).lower():
                continue
            payload = part.get_payload(decode=True)
            if not payload:
                continue
            charset = part.get_content_charset() or "utf-8"
            decoded = payload.decode(charset, errors="replace")
            if part.get_content_type() == "text/html":
                decoded = BeautifulSoup(decoded, "html.parser").get_text(" ", strip=True)
            text_parts.append(decoded)
        return "\n".join(text_parts)
