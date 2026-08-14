"""复用 CLI（`zlibrary.cli`）的接入逻辑：直连检测 -> 不通则起/复用 mihomo 代理 -> 选优->
构造 ZLibraryClient。直接 import 现有函数，不重复实现、不修改 CLI 任何代码。

进程内缓存一次接入结果（site/proxy_url/pm），避免每个请求都重新走直连探测 +
全量测速；失败时记录错误，`ensure(force=True)` 可强制重试。

出口节点抖动容忍（"单次超时不要立刻换节点"）现在已经下沉到 `zlibrary.client`
核心逻辑本身（`_handle_transport_error` / `SAME_NODE_RETRIES`），这里不需要
再额外包一层——真实搜索/下载请求和 CLI 用的是同一套重试逻辑。
"""
from __future__ import annotations

import logging
import threading

import click

from zlibrary.cli import _ensure_access, _make_client
from zlibrary.config import Config

log = logging.getLogger("webapp.access")


class AccessState:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.cfg: Config | None = None
        self.site: str | None = None
        self.proxy_url: str | None = None
        self.pm = None
        self.error: str | None = None

    def load_config(self, force: bool = False) -> Config:
        if self.cfg is None or force:
            self.cfg = Config.load()
        return self.cfg

    def ensure(self, force: bool = False):
        """返回 (cfg, site, proxy_url, pm)，成功后缓存当前站点组合。"""
        with self._lock:
            previous_site = self.site
            if self.site and not force:
                return self.cfg, self.site, self.proxy_url, self.pm
            if force:
                # 强制恢复失败后不能继续把旧站点当作健康路由返回。
                self.site = self.proxy_url = self.pm = None
            cfg = self.load_config(force=force)
            try:
                site, proxy_url, pm = _ensure_access(cfg, preferred_site=previous_site)
            except Exception as e:  # noqa: BLE001
                self.error = str(e)
                log.warning("接入失败: %s", e)
                raise
            self.site, self.proxy_url, self.pm, self.error = site, proxy_url, pm, None
            return cfg, self.site, self.proxy_url, self.pm

    def recover(self):
        """强制重新选择站点和节点，供请求/健康检查失败时调用。"""
        return self.ensure(force=True)

    def current_site(self) -> str | None:
        with self._lock:
            return self.site

    def make_client(self):
        cfg, site, proxy_url, pm = self.ensure()
        return _make_client(cfg, site, proxy_url, pm)


# 进程内单例：整个 web 服务共用同一份接入状态（同一个后台常驻 mihomo 实例）。
access_state = AccessState()
