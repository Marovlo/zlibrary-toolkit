from __future__ import annotations

from fastapi import APIRouter, HTTPException

from .. import jobs
from ..health import health_monitor
from ..schemas import DownloadRequest, JobStatus

router = APIRouter(prefix="/api/download", tags=["download"])


def _to_status(job: jobs.Job) -> JobStatus:
    return JobStatus(
        id=job.id, status=job.status, progress=job.progress,
        message=job.message, error=job.error, archived_id=job.archived_id,
    )


@router.post("", response_model=JobStatus)
def start_download(req: DownloadRequest) -> JobStatus:
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
