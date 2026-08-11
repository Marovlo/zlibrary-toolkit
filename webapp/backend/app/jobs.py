"""下载任务：后台线程执行 + 前端轮询状态。

规模是私人面板（个位数用户、非高并发），用内存字典 + 线程池即可满足，不引入
Celery/Redis 等额外基础设施（简单优先）。
"""
from __future__ import annotations

import logging
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from . import archive
from .accounts_store import get_account_store
from .sessions import get_logged_in_client

log = logging.getLogger("webapp.jobs")

_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="download")
_jobs: dict[str, "Job"] = {}
_jobs_lock = threading.Lock()


@dataclass
class Job:
    id: str
    status: str = "pending"  # pending|running|success|failed
    progress: float = 0.0
    message: str = ""
    error: str = ""
    archived_id: int | None = None
    created_at: float = field(default_factory=time.time)


def _set(job: Job, **kw) -> None:
    for k, v in kw.items():
        setattr(job, k, v)


def get_job(job_id: str) -> Job | None:
    with _jobs_lock:
        return _jobs.get(job_id)


def start_download(payload: dict) -> str:
    job = Job(id=str(uuid.uuid4()))
    with _jobs_lock:
        _jobs[job.id] = job
    _executor.submit(_run, job, payload)
    return job.id


def _parse_size_bytes(size_text: str) -> int:
    m = re.match(r"([\d.]+)\s*(KB|MB|GB)", size_text or "", re.IGNORECASE)
    if not m:
        return 0
    val, unit = float(m.group(1)), m.group(2).upper()
    mult = {"KB": 1024, "MB": 1024 ** 2, "GB": 1024 ** 3}[unit]
    return int(val * mult)


def _guess_dest_name(book) -> str:
    """跟 client.py `download()` 的落盘命名逻辑保持一致，仅用于估算下载进度
    （轮询磁盘上这个文件当前大小 / 预期大小），不影响真正落盘路径。"""
    safe_title = re.sub(r"[^\w\s\-]+", "", book.title).strip().replace(" ", "_") or book.book_id
    safe_author = re.sub(r"[^\w\s\-]+", "", book.author).strip().replace(" ", "_") if book.author else ""
    ext = book.format or "epub"
    return f"{safe_title}" + (f" - {safe_author}" if safe_author else "") + f".{ext}"


def _friendly_download_error(e: Exception) -> str:
    from zlibrary.client import IpQuotaExceeded, SiteRejected

    if isinstance(e, SiteRejected):
        return "该记录的文件在站点侧已失效，请换一个候选版本重试"
    if isinstance(e, IpQuotaExceeded):
        return "当前出口的匿名下载额度已用完，请切换一个账号后重试"
    return "下载失败，请稍后重试或换一个候选版本"


def _run(job: Job, payload: dict) -> None:
    from zlibrary.client import BookResult

    book = BookResult(
        title=payload["title"], author=payload.get("author", ""),
        year=payload.get("year", ""), language=payload.get("language", ""),
        format=payload.get("format", ""), size=payload.get("size", ""),
        rating=float(payload.get("rating") or 0), book_id=payload["book_id"],
        hash=payload["hash"], detail_url=payload.get("detail_url", ""),
        download_url=payload.get("download_url", ""),
    )

    # 已存档过，直接本地命中，不再访问 Z-Library
    existing = archive.find_by_book(book.book_id, book.hash)
    if existing:
        _set(job, status="success", progress=1.0, message="已存档，直接使用本地文件",
             archived_id=existing.id)
        return

    _set(job, status="running", message="正在连接节点/账号...")
    account_email = payload.get("account_email") or ""
    try:
        client, acc = get_logged_in_client(account_email)
    except ValueError as e:
        _set(job, status="failed", error=str(e))
        return

    _set(job, message="正在获取下载链接...")
    dest_dir = archive.library_dir()
    expected_bytes = _parse_size_bytes(book.size)
    guess_dest = dest_dir / _guess_dest_name(book)
    stop_flag = threading.Event()

    def _watch_progress() -> None:
        # 通过轮询磁盘上目标文件的大小估算下载进度（client.download 内部逐块
        # 落盘，这里不侵入 CLI 核心代码，只是外部观察文件增长情况）。
        started = False
        while not stop_flag.is_set():
            try:
                if guess_dest.exists():
                    size = guess_dest.stat().st_size
                    if size > 0:
                        started = True
                        if expected_bytes:
                            pct = min(95, int(size / expected_bytes * 100))
                            job.progress = pct / 100
                            job.message = f"下载中 {pct}%（{size / 1048576:.1f}/{expected_bytes / 1048576:.1f} MB）"
                        else:
                            job.message = f"下载中（已收到 {size / 1048576:.1f} MB）"
            except OSError:
                pass
            if not started:
                job.message = "正在连接下载线路..."
            time.sleep(2)

    watcher = threading.Thread(target=_watch_progress, daemon=True)
    watcher.start()
    try:
        path = client.download(book, dest_dir, max_rounds=3)
    except Exception as e:  # noqa: BLE001
        stop_flag.set()
        client.close()
        log.warning("下载失败: %s: %s", type(e).__name__, e)
        _set(job, status="failed", error=_friendly_download_error(e))
        return
    stop_flag.set()

    _set(job, message="正在写入本地书库...", progress=0.98)
    if acc:
        get_account_store().mark_used(acc)
    client.close()

    archived = archive.add(
        book_id=book.book_id, hash_=book.hash, title=book.title, author=book.author,
        year=book.year, language=book.language, fmt=book.format, file_path=path,
        rating=book.rating,
    )
    _set(job, status="success", progress=1.0, message="下载完成，可保存到本地", archived_id=archived.id)
