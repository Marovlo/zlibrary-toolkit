"""官网查找器（低优）。

通过搜索引擎查找 Z-Library 官网。结果缓存到 data/known_sites.json。
当前默认 zh.z-library.sk 已知，查找失败不影响主流程。
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote

import httpx

log = logging.getLogger(__name__)

# 已知的 Z-Library 域名特征
ZLIB_DOMAIN_HINTS = ["z-library.sk", "zlib.bz", "z-lib.sk", "z-lib.io", "z-lib.org", "1lib.sk", "zlibrary"]
SEARCH_QUERIES = ["z-library official site", "zlibrary official domain"]


@dataclass
class SiteResult:
    url: str
    source: str
    found_at: float


def find_official_site(cache_file: Path | None = None, force: bool = False) -> list[str]:
    """返回候选官网 URL 列表（缓存优先）。"""
    if cache_file and cache_file.exists() and not force:
        try:
            data = json.loads(cache_file.read_text(encoding="utf-8"))
            age = time.time() - data.get("updated_at", 0)
            if age < 7 * 86400 and data.get("sites"):  # 7 天缓存
                log.info("使用缓存的官网列表: %s", data["sites"])
                return data["sites"]
        except Exception:  # noqa: BLE001
            pass

    sites: list[str] = []
    for q in SEARCH_QUERIES:
        try:
            found = _search_bing(q)
            sites.extend(found)
        except Exception as e:  # noqa: BLE001
            log.debug("搜索 '%s' 失败: %s", q, e)

    # 去重保序
    seen: set[str] = set()
    dedup: list[str] = []
    for s in sites:
        if s not in seen:
            seen.add(s)
            dedup.append(s)
    if dedup and cache_file:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(
            json.dumps({"updated_at": time.time(), "sites": dedup}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    return dedup


def _search_bing(query: str) -> list[str]:
    """用 Bing HTML 版搜索，提取结果 URL。"""
    url = "https://www.bing.com/search"
    headers = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/126.0.0.0 Safari/537.36"}
    with httpx.Client(timeout=20, follow_redirects=True, headers=headers) as c:
        r = c.get(url, params={"q": query, "setlang": "en"})
        r.raise_for_status()
    html = r.text
    # Bing 结果链接在 <a href="https://..."> 中，也可能重定向
    links = re.findall(r'<a[^>]+href="(https?://[^"]+)"', html)
    out: list[str] = []
    for lk in links:
        lk = unquote(lk)
        if any(h in lk for h in ZLIB_DOMAIN_HINTS):
            # 取根
            from urllib.parse import urlparse

            p = urlparse(lk)
            site = f"{p.scheme}://{p.netloc}"
            if site not in out:
                out.append(site)
    log.debug("Bing '%s' -> %s", query, out)
    return out
