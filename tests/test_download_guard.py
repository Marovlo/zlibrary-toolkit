"""Web 下载入口防伪卡离线自测。"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "webapp" / "backend"))

from fastapi import HTTPException

from app.routers.download import start_download
from app.schemas import DownloadRequest


def _request(**changes) -> DownloadRequest:
    values = {
        "book_id": "420948",
        "hash": "",
        "title": "低空技术与工程导论",
        "author": "低空技术与工程导论",
        "detail_url": "https://zlib.bz/book/a/title.html",
        "download_url": "https://zlib.bz/dl/a",
        "isbn": "",
        "publisher": "低空技术与工程导论",
    }
    values.update(changes)
    return DownloadRequest(**values)


def test_echo_card_is_rejected_before_job_creation() -> None:
    with patch("app.routers.download.jobs.start_download") as start:
        try:
            start_download(_request())
        except HTTPException as exc:
            assert exc.status_code == 409
        else:
            raise AssertionError("回声伪卡不应创建下载任务")
        start.assert_not_called()


def test_empty_book_id_is_rejected() -> None:
    try:
        start_download(_request(book_id=""))
    except HTTPException as exc:
        assert exc.status_code == 400
    else:
        raise AssertionError("空 book_id 不应创建下载任务")


if __name__ == "__main__":
    test_echo_card_is_rejected_before_job_creation()
    test_empty_book_id_is_rejected()
    print("download guard 自测通过")
