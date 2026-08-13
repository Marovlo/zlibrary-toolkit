"""后台执行一次人工触发的 Z-Library 注册任务。"""
from __future__ import annotations

import logging
import secrets
import string
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone

from zlibrary.accounts import AccountStore, DEFAULT_DAILY_LIMIT
from zlibrary.cli import _ensure_access, _make_client
from zlibrary.config import Config, project_root
from zlibrary.mail import MailConfig, VerificationMailbox

log = logging.getLogger("webapp.registration_jobs")
_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="register-account")
_lock = threading.Lock()
_jobs: dict[str, "RegistrationJob"] = {}
_current_job_id: str | None = None
_DOMAIN = "marovlo.cloud"


@dataclass
class RegistrationJob:
    id: str
    status: str = "pending"
    phase: str = "preparing"
    message: str = "准备注册账号..."
    email: str = ""
    error: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0

    def __post_init__(self) -> None:
        now = time.time()
        if not self.created_at:
            self.created_at = now
        if not self.updated_at:
            self.updated_at = now

    def update(self, **values) -> None:
        for key, value in values.items():
            setattr(self, key, value)
        self.updated_at = time.time()

    def public(self) -> dict:
        return {
            "id": self.id,
            "status": self.status,
            "phase": self.phase,
            "message": self.message,
            "email": self.email,
            "error": self.error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


def _new_email(store: AccountStore) -> str:
    existing = {account.email.casefold() for account in store.accounts}
    for _ in range(10):
        email = f"test-{secrets.token_hex(6)}@{_DOMAIN}"
        if email.casefold() not in existing:
            return email
    raise RuntimeError("无法生成不重复的测试邮箱地址")


def _new_password() -> str:
    alphabet = string.ascii_letters + string.digits + "-_!@#$%^&*"
    return "".join(secrets.choice(alphabet) for _ in range(24))


def _run(job: RegistrationJob) -> None:
    client = None
    try:
        job.update(status="running", phase="preparing", message="准备邮箱配置...")
        mail_config = MailConfig.load()
        store = AccountStore.load(project_root() / "accounts.yaml", limit=DEFAULT_DAILY_LIMIT)
        email = _new_email(store)
        password = _new_password()
        job.update(email=email, phase="connecting", message="正在连接 Z-Library...")
        cfg = Config.load()
        site, proxy_url, pm = _ensure_access(cfg)
        client = _make_client(cfg, site, proxy_url, pm)

        mailbox = VerificationMailbox(mail_config)
        seen = mailbox.snapshot(email)
        started = datetime.now(timezone.utc)
        job.update(phase="sending_code", message="注册请求已提交，等待邮箱验证码...")
        registration = client.begin_registration(email, password)
        job.update(phase="reading_mail", message="正在读取 QQ 邮箱验证码...")
        code = mailbox.wait_for_code(email, seen, started)
        job.update(phase="verifying", message="已收到验证码，正在完成注册...")
        result = client.finish_registration(registration, code)
        if not result.ok:
            raise RuntimeError(result.error)
        job.update(phase="logging_in", message="注册完成，正在验证新账号登录...")
        login = client.login(email, password)
        if not login.ok:
            raise RuntimeError(f"注册完成但登录验证失败: {login.error}")
        job.update(phase="saving", message="正在写入账号池...")
        store.add_account(email, password, remaining=login.remaining)
        job.update(status="success", phase="done", message="注册并登录验证成功，已加入账号池")
    except Exception as e:  # noqa: BLE001
        log.warning("后台注册任务失败: %s", e)
        job.update(status="failed", phase="failed", message="注册失败", error=str(e))
    finally:
        if client is not None:
            client.close()


def start() -> RegistrationJob:
    global _current_job_id
    with _lock:
        if _current_job_id:
            current = _jobs.get(_current_job_id)
            if current and current.status in ("pending", "running"):
                return current
        job = RegistrationJob(id=str(uuid.uuid4()))
        _jobs[job.id] = job
        _current_job_id = job.id
        _executor.submit(_run, job)
        return job


def get(job_id: str) -> RegistrationJob | None:
    with _lock:
        return _jobs.get(job_id)


def current() -> RegistrationJob | None:
    with _lock:
        return _jobs.get(_current_job_id) if _current_job_id else None
