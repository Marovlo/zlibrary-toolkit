from __future__ import annotations

from fastapi import APIRouter, HTTPException

from zlibrary.client import BookResult, is_echo_pseudo_result

from .. import jobs
from ..health import health_monitor
from ..schemas import DownloadRequest, JobStatus

router = APIRouter(prefix="/api/download", tags=["download"])


def _to_status(job: jobs.Job) -> JobStatus:
    return JobStatus(
        id=job.id, status=job.status, progress=job.progress,
        message=job.message, error=job.error, archived_id=job.archived_id,
        phase=job.phase, share_url=job.share_url or None,
    )


@router.post("", response_model=JobStatus)
def start_download(req: DownloadRequest) -> JobStatus:
    if not req.book_id.strip():
        raise HTTPException(400, "该搜索结果缺少有效书籍标识")
    book = BookResult(
        title=req.title, author=req.author, year=req.year, language=req.language,
        format=req.format, size=req.size, rating=req.rating, book_id=req.book_id,
        hash=req.hash, detail_url=req.detail_url, download_url=req.download_url,
        isbn=req.isbn, publisher=req.publisher,
    )
    if is_echo_pseudo_result(book, req.title):
        raise HTTPException(409, "该结果疑似站点回声伪卡，已禁止下载")
    if not req.hash.strip() and not (req.download_url.strip() or req.detail_url.strip()):
        raise HTTPException(400, "该搜索结果缺少有效下载标识")
    health_monitor.record_activity()  # 真实下载操作，用于健康监控判断"是否空闲"
    job_id = jobs.start_download(req.model_dump())
    job = jobs.get_job(job_id)
    return _to_status(job)


@router.get("/{job_id}", response_model=JobStatus)
def get_download(job_id: str) -> JobStatus:
    job = jobs.get_job(job_id)
    if not job:
        raise HTTPException(404, "任务不存在")
    return _to_status(job)
