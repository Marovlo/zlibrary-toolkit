"""主站/备用站和节点优先策略的离线自测。"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from zlibrary.config import AccessConfig, Config, MihomoConfig, SiteFinderConfig, BaidupcsConfig
from zlibrary import cli


def test_configured_site_order() -> None:
    config = Config(
        subscription_url="",
        default_site="https://zh.z-library.sk",
        fallback_sites=["https://zlib.bz/", "https://z-lib.sk", "https://zlib.bz"],
        download_dir=".",
        format_preference=[], mihomo=MihomoConfig("", "", "", "", 1),
        site_finder=SiteFinderConfig(False, "", ""), access=AccessConfig("", 10, 10, 1),
        baidupcs=BaidupcsConfig("", ""),
    )
    assert config.sites() == ["https://zh.z-library.sk", "https://zlib.bz", "https://z-lib.sk"]


def _config() -> Config:
    return Config(
        subscription_url="", default_site="https://zh.z-library.sk",
        fallback_sites=["https://zlib.bz", "https://z-lib.sk"], download_dir=".",
        format_preference=[], mihomo=MihomoConfig("", "", "", "", 1),
        site_finder=SiteFinderConfig(False, "", ""), access=AccessConfig("", 10, 10, 1),
        baidupcs=BaidupcsConfig("", ""),
    )


def test_direct_uses_primary_before_fallback() -> None:
    config = _config()
    checked = []
    with patch.object(cli, "check_direct", side_effect=lambda site, timeout: checked.append(site) or site.endswith("zlib.bz")):
        site, proxy, pm = cli._ensure_access(config, preferred_site="https://zh.z-library.sk")
    assert site == "https://zlib.bz"
    assert checked == ["https://zh.z-library.sk", "https://zlib.bz"]
    assert proxy is None and pm is None


def test_proxy_tries_site_fallback_after_near_nodes() -> None:
    class Best:
        name = "香港-优化"
        delay_ms = 1

    class FakeProxy:
        def __init__(self, config):
            self.calls = []

        def _load_state(self):
            return {}

        def setup_and_select_best(self):
            return Best()

        def proxy_url(self):
            return "http://proxy"

        def current_node(self):
            return "香港-优化"

        def reset_rotation_cycle(self):
            self.calls.append("reset")

        def rotate_node(self, near_only=False, far_only=False):
            self.calls.append((near_only, far_only))
            return None

        def _save_state(self, patch):
            self.calls.append(patch)

    checked = []
    with patch("zlibrary.proxy_manager.ProxyManager", FakeProxy), \
         patch.object(cli, "check_direct", return_value=False), \
         patch.object(cli, "check_via_proxy", side_effect=lambda proxy, site, **kwargs: checked.append(site) or site.endswith("zlib.bz")):
        site, proxy, pm = cli._ensure_access(_config())
    assert site == "https://zlib.bz"
    assert checked[:2] == ["https://zh.z-library.sk", "https://zlib.bz"]
    assert proxy == "http://proxy"


if __name__ == "__main__":
    test_configured_site_order()
    test_direct_uses_primary_before_fallback()
    test_proxy_tries_site_fallback_after_near_nodes()
    print("access failover 自测通过")
    test_configured_site_order()
    test_direct_uses_primary_before_fallback()
    print("access failover 自测通过")
