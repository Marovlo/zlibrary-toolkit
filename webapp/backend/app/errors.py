"""把底层异常统一转成对用户友好、不暴露代理/节点/网络实现细节的提示。

完整异常信息仍会记到服务端日志，只是不透传给前端响应体（对应需求：其他网络相关
问题对用户屏蔽）。
"""
from __future__ import annotations

import logging

import click
import httpx
from fastapi import HTTPException

from zlibrary.client import (
    CloudflareError,
    IpQuotaExceeded,
    SearchServiceUnavailable,
    SiteRejected,
)

log = logging.getLogger("webapp.errors")


def friendly_error(e: Exception) -> HTTPException:
    log.warning("请求失败: %s: %s", type(e).__name__, e)
    if isinstance(e, SearchServiceUnavailable):
        return HTTPException(503, "Z-Library 搜索服务暂时不可用，请稍后重试（站点侧问题，与账号/网络无关）")
    if isinstance(e, IpQuotaExceeded):
        return HTTPException(429, "当前出口的匿名下载额度已用完，请切换一个账号后重试")
    if isinstance(e, SiteRejected):
        return HTTPException(409, "该记录的文件在站点侧已失效，请换一个候选版本重试")
    if isinstance(e, CloudflareError):
        return HTTPException(502, "暂时无法通过站点校验，请稍后重试")
    if isinstance(e, click.ClickException):
        return HTTPException(503, "当前网络暂不可用，请稍后重试")
    if isinstance(e, (httpx.TransportError, httpx.RemoteProtocolError, ConnectionError, TimeoutError)):
        return HTTPException(503, "网络暂时不稳定，请稍后重试")
    return HTTPException(500, "服务暂时出现问题，请稍后重试")
