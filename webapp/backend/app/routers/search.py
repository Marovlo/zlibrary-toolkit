from __future__ import annotations

from fastapi import APIRouter, HTTPException

from .. import search_cache
from ..errors import friendly_error
from ..health import health_monitor
from ..schemas import BookOut, SearchRequest
from ..sessions import get_logged_in_client

router = APIRouter(prefix="/api/search", tags=["search"])


@router.post("", response_model=list[BookOut])
def search(req: SearchRequest) -> list[BookOut]:
    health_monitor.record_activity()  # 真实搜索操作，用于健康监控判断"是否空闲"

    if not req.force_refresh:
        cached = search_cache.get(req.query, req.page)
        if cached is not None:
            return cached

    try:
        client, _acc = get_logged_in_client(req.account_email)
    except ValueError as e:
        raise HTTPException(400, str(e))

    try:
        results = client.search(req.query, page=req.page)
    except Exception as e:  # noqa: BLE001
        raise friendly_error(e)
    finally:
        client.close()

    out = [
        BookOut(
            title=b.title, author=b.author, year=b.year, language=b.language,
            format=b.format, size=b.size, rating=b.rating, book_id=b.book_id,
            hash=b.hash, detail_url=b.detail_url, download_url=b.download_url,
            match_score=b.match_score(req.query),
        )
        for b in results
    ]
    search_cache.set(req.query, req.page, out)
    return out
