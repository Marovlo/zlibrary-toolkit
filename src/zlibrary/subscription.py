"""订阅解析：拉取 + 解析 clash 订阅 yaml，提取代理节点。"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import yaml

log = logging.getLogger(__name__)

# 订阅 API 通常要求 clash 类 UA 才返回 yaml 格式
SUBSCRIBE_UA = "clash-verge/v1.5.0"


@dataclass
class ProxyNode:
    name: str
    type: str            # ss / vmess / trojan / hysteria2 ...
    raw: dict[str, Any]  # 原始 proxy dict，直接喂给 mihomo config


def fetch_subscription(url: str, dest: Path, retries: int = 3) -> Path:
    """拉取订阅到本地文件。失败抛异常。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    last_err: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            log.info("拉取订阅 (尝试 %d/%d): %s", attempt, retries, url)
            with httpx.Client(timeout=30, follow_redirects=True, headers={"User-Agent": SUBSCRIBE_UA}) as c:
                r = c.get(url)
                r.raise_for_status()
                text = r.text
            # 校验是 yaml
            data = yaml.safe_load(text)
            if not isinstance(data, dict) or "proxies" not in data:
                raise ValueError("订阅内容不含 proxies 字段，可能不是 clash 格式")
            dest.write_text(text, encoding="utf-8")
            log.info("订阅已保存到 %s，共 %d 个节点", dest, len(data.get("proxies", [])))
            return dest
        except Exception as e:  # noqa: BLE001
            last_err = e
            log.warning("拉取订阅失败: %s", e)
            if attempt < retries:
                time.sleep(2 * attempt)
    raise RuntimeError(f"拉取订阅失败（{retries} 次）: {last_err}")


def parse_subscription(path: Path) -> list[ProxyNode]:
    """解析本地订阅 yaml，返回节点列表。"""
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    proxies = data.get("proxies", [])
    nodes: list[ProxyNode] = []
    for p in proxies:
        if not isinstance(p, dict):
            continue
        name = p.get("name", "")
        ptype = p.get("type", "")
        if name and ptype:
            nodes.append(ProxyNode(name=name, type=ptype, raw=p))
    log.info("解析出 %d 个代理节点", len(nodes))
    return nodes


def load_subscription(path: Path) -> dict[str, Any]:
    """加载完整订阅 dict（含 proxies/proxy-groups 等）。"""
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}
