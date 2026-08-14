"""配置加载。从 config.yaml 读取，路径相对于项目根解析。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


def project_root() -> Path:
    # src/zlib/config.py -> 上溯两级到项目根
    return Path(__file__).resolve().parents[2]


@dataclass
class MihomoConfig:
    binary_path: str
    api_secret: str
    work_dir: str
    test_url: str
    test_timeout_ms: int
    # 端口默认留空，由 ProxyManager 自动从内置的一组不常见端口里挑选当前空闲的
    # （避免跟用户机器上可能已经在跑的另一个 mihomo/clash 抢占默认端口 7890/7891/9090）。
    # 只有用户在 config.yaml 里显式指定时才会优先尝试这个值。
    http_port: int | None = None
    socks_port: int | None = None
    api_port: int | None = None
    dns_port: int | None = None  # mihomo 内置 DNS 监听端口（仅本机，用于 fake-ip 防投毒）
    subscription_cache_hours: float = 24

    def binary_abs(self) -> Path:
        p = Path(self.binary_path)
        return p if p.is_absolute() else project_root() / p

    def work_dir_abs(self) -> Path:
        p = Path(self.work_dir)
        return p if p.is_absolute() else project_root() / p


@dataclass
class SiteFinderConfig:
    enabled: bool
    cache_file: str
    search_engine: str


@dataclass
class AccessConfig:
    user_agent: str
    httpx_timeout: int
    playwright_timeout: int
    max_retries: int


@dataclass
class BaidupcsConfig:
    binary_path: str
    pan_dir: str
    share_period: int = 0  # 0=永久

    def binary_abs(self) -> Path:
        p = Path(self.binary_path)
        return p if p.is_absolute() else project_root() / p


@dataclass
class Config:
    subscription_url: str
    default_site: str
    download_dir: str
    format_preference: list[str]
    mihomo: MihomoConfig
    site_finder: SiteFinderConfig
    access: AccessConfig
    baidupcs: BaidupcsConfig
    fallback_sites: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    def download_dir_abs(self) -> Path:
        d = Path(self.download_dir).expanduser()
        return d

    def sites(self) -> list[str]:
        """按主站优先顺序返回去重后的站点列表。"""
        result: list[str] = []
        for site in [self.default_site, *self.fallback_sites]:
            normalized = site.strip().rstrip("/")
            if normalized and normalized not in result:
                result.append(normalized)
        return result

    @classmethod
    def load(cls, path: str | Path | None = None) -> "Config":
        p = Path(path) if path else project_root() / "config.yaml"
        with open(p, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        m = data["mihomo"]
        return cls(
            subscription_url=data["subscription_url"],
            default_site=data["default_site"],
            fallback_sites=[str(site) for site in data.get("fallback_sites", [])],
            download_dir=data["download_dir"],
            format_preference=data.get("format_preference", ["epub", "pdf"]),
            mihomo=MihomoConfig(
                binary_path=m["binary_path"],
                api_secret=m["api_secret"],
                work_dir=m["work_dir"],
                test_url=m["test_url"],
                test_timeout_ms=m["test_timeout_ms"],
                http_port=m.get("http_port"),
                socks_port=m.get("socks_port"),
                api_port=m.get("api_port"),
                dns_port=m.get("dns_port"),
                subscription_cache_hours=m.get("subscription_cache_hours", 24),
            ),
            site_finder=SiteFinderConfig(
                enabled=data["site_finder"]["enabled"],
                cache_file=data["site_finder"]["cache_file"],
                search_engine=data["site_finder"]["search_engine"],
            ),
            access=AccessConfig(
                user_agent=data["access"]["user_agent"],
                httpx_timeout=data["access"]["httpx_timeout"],
                playwright_timeout=data["access"]["playwright_timeout"],
                max_retries=data["access"]["max_retries"],
            ),
            baidupcs=BaidupcsConfig(
                binary_path=data.get("baidupcs", {}).get("binary_path", "data/baidupcs"),
                pan_dir=data.get("baidupcs", {}).get("pan_dir", "/zlibrary"),
                share_period=data.get("baidupcs", {}).get("share_period", 0),
            ),
            raw=data,
        )
