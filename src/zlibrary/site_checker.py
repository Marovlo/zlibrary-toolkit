"""站点连通性检测：直连测试 Z-Library 是否可达。"""
from __future__ import annotations

import logging

import httpx

log = logging.getLogger(__name__)


def check_direct(site_url: str, timeout: int = 15) -> bool:
    """直连测试 site_url 是否真的是可用的 Z-Library。

    必须校验响应内容，不能只看状态码：本机（及多数国内网络）对 `z-library.sk` 的 DNS
    是被投毒的（实测解析到 Facebook 的 31.13.x.x / 2a03:2880::face:b00c）。若只用
    `status_code < 500` 判定，很可能连到假地址却拿到某个 4xx 就判成"直连可用"，
    于是整条链路都不走代理、全部请求打向错误的服务器。
    """
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True, verify=False) as c:
            r = c.get(site_url)
    except Exception as e:  # noqa: BLE001
        log.info("直连 %s 失败: %s", site_url, e)
        return False
    if not _is_zlibrary(r):
        log.info("直连 %s 返回 HTTP %s，但内容不是 Z-Library（很可能 DNS 被投毒），判为不可用",
                 site_url, r.status_code)
        return False
    log.info("直连 %s -> HTTP %s", site_url, r.status_code)
    return True


def _is_zlibrary(r: httpx.Response) -> bool:
    """判断响应确实来自 Z-Library：认站点自有特征，而非任意能连上的服务器。"""
    if r.headers.get("x-zbackend") or r.headers.get("x-zproxy"):
        return True
    try:
        low = r.text[:20000].lower()
    except Exception:  # noqa: BLE001
        return False
    #挑战页也算（说明确实是 z-library 的前置代理）
    return any(m in low for m in ("z-library", "zlibrary", "z-lib", "checking your browser", "c_token="))


def check_via_proxy(proxy_url: str, site_url: str, timeout: int = 20) -> bool:
    """通过指定代理测试 site_url 是否可达。

    503 也算可达：z-library 在没有有效 `c_token` 时对任何请求都先回 503 浏览器校验页，
    这恰恰证明这条线路能打到站点（校验由`challenge` 模块自动解掉）。判定标准是
    "响应确实来自 z-library"，而不是状态码。
    """
    try:
        with httpx.Client(timeout=httpx.Timeout(timeout, connect=8), proxy=proxy_url,
                          follow_redirects=True, verify=False) as c:
            r = c.get(site_url)
    except Exception as e:  # noqa: BLE001
        log.debug("代理访问 %s 失败: %s", site_url, e)
        return False
    ok = _is_zlibrary(r)
    log.debug("代理 -> %s HTTP %s (是z-library: %s)", site_url, r.status_code, ok)
    return ok
