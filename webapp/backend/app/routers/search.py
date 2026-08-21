from __future__ import annotations

import httpx
from fastapi import APIRouter, HTTPException

from zlibrary.client import CloudflareError, SearchServiceUnavailable, filter_search_results

from .. import search_cache
from ..access import access_state
from ..errors import friendly_error
from ..health import health_monitor
from ..schemas import BookOut, SearchRequest
from ..sessions import get_logged_in_client

router = APIRouter(prefix="/api/search", tags=["search"])


@router.post("", response_model=list[BookOut])
def search(req: SearchRequest) -> list[BookOut]:
    health_monitor.record_activity()  # 真实搜索操作，用于健康监控判断"是否空闲"

    cache_site = access_state.current_site() or ""
    if not req.force_refresh:
        cached = search_cache.get(req.query, req.page, cache_site)
        if cached is not None:
            return cached

    def search_once():
        try:
            client, _acc = get_logged_in_client(req.account_email)
        except ValueError as e:
            raise HTTPException(400, str(e))
        try:
            return client.search(req.query, page=req.page)
        finally:
            client.close()

    try:
        results = search_once()
    except (CloudflareError, SearchServiceUnavailable, httpx.TransportError):
        # 客户端已经完成当前节点内重试；此处才触发一次新的「节点优先、站点兜底」选择。
        try:
            access_state.recover()
            results = search_once()
        except HTTPException:
            raise
        except Exception as e:  # noqa: BLE001
            raise friendly_error(e)
    except Exception as e:  # noqa: BLE001
        raise friendly_error(e)

    results = filter_search_results(results, req.query)
    out = [
        BookOut(
            title=b.title, author=b.author, year=b.year, language=b.language,
            format=b.format, size=b.size, rating=b.rating, book_id=b.book_id,
            hash=b.hash, detail_url=b.detail_url, download_url=b.download_url,
            isbn=b.isbn, publisher=b.publisher,
            match_score=b.match_score(req.query),
        )
        for b in results
    ]
    search_cache.set(req.query, req.page, out, access_state.current_site() or "")
    return out
