"""搜索结果缓存：书本的标题/作者/年份/格式/大小/评分/book_id/hash/detail_url
等元数据一旦生成基本不会变（同一条记录是否被站点判定失效是记录本身的固定属性，
不会来回翻，见 DEV.md 四.1）；真正会变的是"下载链接"，但那本身就不从搜索结果
里直接复用（见 `client.get_download_url()`：每次下载都重新访问详情页解析，
`/dl/` 短码随会话/后端变化，本来就无法预先缓存）。所以这里只缓存"搜索结果本身"，
省下的是"重新发一次搜索请求"的开销（PoW 解题 + 网络往返 + HTML 解析），跟下载
链接的新鲜度完全无关，不影响下载本身。

缓存粒度：(归一化后的查询词, 页码, 当前站点) -> 结果列表。不区分 account_email，因为搜索
结果内容跟用哪个账号无关（账号只影响下载额度，不影响能搜到什么）；切换站点后不复用旧站点结果。

TTL 默认 12 小时，进程内内存缓存，服务重启即清空（简单优先，不做持久化——
私人面板量级下，重启不频繁，没必要为了跨重启保留缓存增加复杂度）。
"""
from __future__ import annotations

import threading
import time

TTL_SECONDS = 12 * 3600
MAX_ENTRIES = 500  # 简单的容量上限，超过后清掉最老的一半，避免无限增长

_lock = threading.Lock()
_cache: dict[tuple[str, int, str], tuple[float, list]] = {}


def _key(query: str, page: int, site: str = "") -> tuple[str, int, str]:
    return (query.strip().lower(), page, site.strip().rstrip("/"))


def get(query: str, page: int, site: str = "") -> list | None:
    k = _key(query, page, site)
    with _lock:
        entry = _cache.get(k)
        if not entry:
            return None
        ts, data = entry
        if time.time() - ts > TTL_SECONDS:
            del _cache[k]
            return None
        return data


def set(query: str, page: int, data: list, site: str = "") -> None:
    k = _key(query, page, site)
    with _lock:
        if len(_cache) >= MAX_ENTRIES:
            # 超过容量上限：简单粗暴地清掉最老的一半，不做精细 LRU
            # （私人面板量级下够用，没必要为此引入额外依赖）。
            oldest = sorted(_cache.items(), key=lambda kv: kv[1][0])[: len(_cache) // 2]
            for old_k, _ in oldest:
                _cache.pop(old_k, None)
        _cache[k] = (time.time(), data)
