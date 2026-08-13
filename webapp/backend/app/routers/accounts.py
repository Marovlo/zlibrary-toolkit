from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ..access import access_state
from ..accounts_store import get_account_store
from ..errors import friendly_error
from .. import quota, registration_jobs
from ..schemas import AccountInfo, AccountSummary, AddAccountRequest, RegistrationJobStatus

router = APIRouter(prefix="/api/accounts", tags=["accounts"])


def _to_info(a, limit: int) -> AccountInfo:
    a.maybe_reset()
    return AccountInfo(
        email=a.email, downloads_today=a.downloads_today, limit=limit,
        remaining=a.remaining, effective_remaining=a.effective_remaining(limit),
        available=a.available(limit),
    )


@router.get("", response_model=list[AccountInfo])
def list_accounts() -> list[AccountInfo]:
    store = get_account_store()
    return [_to_info(a, store.limit) for a in store.accounts]


@router.get("/summary", response_model=AccountSummary)
def account_summary() -> AccountSummary:
    current = registration_jobs.current()
    summary = quota.snapshot()
    return AccountSummary(
        total_remaining=summary.total_remaining,
        available_accounts=summary.available_accounts,
        threshold=summary.threshold,
        low_balance=summary.low_balance,
        registration_job_id=current.id if current else None,
    )


@router.post("/register", response_model=RegistrationJobStatus)
def start_registration() -> RegistrationJobStatus:
    return RegistrationJobStatus(**registration_jobs.start().public())


@router.get("/register/{job_id}", response_model=RegistrationJobStatus)
def registration_status(job_id: str) -> RegistrationJobStatus:
    job = registration_jobs.get(job_id)
    if not job:
        raise HTTPException(404, "注册任务不存在")
    return RegistrationJobStatus(**job.public())


@router.post("", response_model=AccountInfo)
def add_account(req: AddAccountRequest) -> AccountInfo:
    """添加账号：先做一次真实登录测试，成功才持久化到 accounts.yaml（跟 CLI
    的 `zlib add-account` 行为一致），失败不会写入错误密码。"""
    try:
        client = access_state.make_client()
    except Exception as e:  # noqa: BLE001
        raise friendly_error(e)
    try:
        res = client.login(req.email, req.password)
    except Exception as e:  # noqa: BLE001
        raise friendly_error(e)
    finally:
        client.close()
    if not res.ok:
        raise HTTPException(400, f"登录测试失败: {res.error}")

    store = get_account_store()
    store.add_account(req.email, req.password, remaining=res.remaining)
    acc = store.by_email(req.email)
    return _to_info(acc, store.limit)
