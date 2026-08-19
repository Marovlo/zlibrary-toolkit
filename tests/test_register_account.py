"""register-account 的离线自测，不访问真实 QQ 或 Z-Library。"""
from __future__ import annotations

import email
import imaplib
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from zlibrary.client import ZLibraryClient
from zlibrary.cli import _REGISTER_LOCAL_RE, _registration_email
from zlibrary.mail import MailConfig, MailError, VerificationMailbox


RAW_CODE_MAIL = """From: Library <no-reply@z-lib.hn>
To: test-001@marovlo.cloud
Subject: =?utf-8?b?5oKo55qE56Gu6K6k56CB77ya?=
Date: Wed, 12 Aug 2026 10:07:39 +0300
Content-Type: text/html; charset=utf-8

<html><body><p>您的确认码：</p><h1>9364</h1></body></html>
""".encode()


class FakeMailbox:
    def __init__(self, messages: list[bytes]):
        self.messages = messages
        self.selected = False
        self.logged_out = False

    def login(self, username: str, password: str):
        assert username == "test@qq.com"
        assert password == "secret"
        return "OK", [b"Success"]

    def select(self, mailbox: str, readonly: bool = False):
        assert mailbox == "INBOX"
        assert readonly is True
        self.selected = True
        return "OK", [str(len(self.messages)).encode()]

    def uid(self, command: str, *args):
        if command == "search":
            return "OK", [b"1"]
        if command == "fetch":
            return "OK", [(b"header", self.messages[0])]
        raise AssertionError(command)

    def logout(self):
        self.logged_out = True
        return "OK", [b"logout"]


def test_extract_code_from_forwarded_html() -> None:
    cfg = MailConfig(username="test@qq.com", password="secret")
    mailbox = VerificationMailbox(cfg)
    fake = FakeMailbox([RAW_CODE_MAIL])
    with patch.object(imaplib, "IMAP4_SSL", return_value=fake):
        assert mailbox.wait_for_code(
            "test-001@marovlo.cloud",
            seen_uids=set(),
            not_before=datetime(2026, 8, 12, 0, tzinfo=timezone.utc),
            timeout=1,
        ) == "9364"
    assert fake.logged_out


def test_old_uid_is_ignored() -> None:
    cfg = MailConfig(username="test@qq.com", password="secret")
    mailbox = VerificationMailbox(cfg)
    fake = FakeMailbox([RAW_CODE_MAIL])
    with patch.object(imaplib, "IMAP4_SSL", return_value=fake):
        with patch.object(mailbox, "_search", return_value=[b"1"]):
            try:
                mailbox.wait_for_code("test-001@marovlo.cloud", {"1"}, timeout=0.01)
            except MailError as exc:
                assert "超时" in str(exc)
            else:
                raise AssertionError("旧邮件不应被作为验证码返回")


def test_registration_form_data_is_dynamic() -> None:
    client = ZLibraryClient("https://example.test", None, "test-agent")
    registration_html = """
    <form action="/registration" method="post">
      <input type="hidden" name="csrf_token" value="abc">
      <input type="email" name="email">
      <input type="password" name="password">
      <input type="text" name="name">
    </form>
    """
    class Response:
        def __init__(self, text: str, url: str):
            self.text = text
            self.url = url

        def json(self):
            return {"success": 1}

    with patch.object(client, "_get", return_value=Response(registration_html, "https://example.test/registration")), \
         patch.object(client, "_post_multipart", return_value=Response("{}", "https://example.test/papi/user/verification/send-code")) as post:
        session = client.begin_registration("test@example.com", "password")
    assert session.code_field == "verifyCode"
    assert session.action == "/registration"
    assert session.fields == {
        "csrf_token": "abc",
        "email": "test@example.com",
        "password": "password",
        "name": "test",
    }
    assert post.call_args.kwargs["data"] == session.fields


def test_registration_email_local_part_is_anchored() -> None:
    import click

    assert _REGISTER_LOCAL_RE.fullmatch("test-001")
    assert _REGISTER_LOCAL_RE.fullmatch("a")
    assert _registration_email("test-001", set()) == "test-001@marovlo.cloud"
    assert _registration_email("User.Name+1@marovlo.cloud", set()) == "User.Name+1@marovlo.cloud"
    try:
        _registration_email("bad email", set())
    except click.ClickException:
        pass
    else:
        raise AssertionError("非法本地部分应被拒绝")
    try:
        _registration_email("test-001@other.example", set())
    except click.ClickException:
        pass
    else:
        raise AssertionError("非指定域名应被拒绝")


def test_blank_registration_form_response_is_pending_success() -> None:
    client = ZLibraryClient("https://example.test", None, "test-agent")
    class Response:
        text = '<form id="registrationForm" class="require-verification"></form>'
        url = "https://example.test/registration"
    from zlibrary.client import RegistrationSession
    session = RegistrationSession("/registration", {}, "verifyCode")
    with patch.object(client, "_post", return_value=Response()):
        result = client.finish_registration(session, "9364")
    assert result.ok


if __name__ == "__main__":
    test_extract_code_from_forwarded_html()
    test_old_uid_is_ignored()
    test_registration_form_data_is_dynamic()
    test_blank_registration_form_response_is_pending_success()
    test_registration_email_local_part_is_anchored()
    print("register-account 离线自测通过")
