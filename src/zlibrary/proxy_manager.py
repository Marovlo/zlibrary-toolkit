"""mihomo 代理管理器。

职责：
1. 下载 mihomo 二进制（如未安装）
2. 从订阅生成 mihomo 配置（非 TUN，只开本地端口，不影响系统）
3. 启动 mihomo 进程
4. 通过 RESTful API 逐节点测到 Z-Library 的延迟
5. 选最优节点并切换 selector

关键：不开 TUN，只开本地 HTTP/SOCKS 端口，只有显式走 127.0.0.1:port 的请求才被代理。
"""
from __future__ import annotations

import gzip
import logging
import os
import platform
import re
import shutil
import signal
import socket
import stat
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import httpx
import yaml

from .config import Config, project_root
from .subscription import ProxyNode, fetch_subscription, load_subscription, parse_subscription

log = logging.getLogger(__name__)

# mihomo 下载。用 GitHub API 取最新版本，失败回退固定版本。
MIHOMO_REPO = "MetaCubeX/mihomo"
MIHOMO_FALLBACK_VERSION = "v1.18.10"
MIHOMO_API_BASE = "http://127.0.0.1:{port}"


def _mihomo_arch() -> str:
    """当前系统架构 -> mihomo release资产名里用的架构标识。"""
    m = platform.machine().lower()
    if m in ("x86_64", "amd64"):
        return "amd64"
    if m in ("aarch64", "arm64"):
        return "arm64"
    if m.startswith("armv7") or m == "armv7l":
        return "armv7"
    return m


# 本地端口候选（写死，故意用不常见的高位端口）。别人机器上很可能已经跑着另一个
# mihomo/clash 占用了默认端口 7890/7891/9090，这里全部避开默认值，四类端口互不重叠，
# 每类给5 个候选逐个探测空闲，几乎不可能全部撞车。用户仍可在 config.yaml 里显式指定
# 端口（会作为第一候选优先尝试），留空则完全走这套自动挑选。
_HTTP_PORT_CANDIDATES = [17890, 27890, 37890, 47890, 57890]
_SOCKS_PORT_CANDIDATES = [17891, 27891, 37891, 47891, 57891]
_API_PORT_CANDIDATES = [17892, 27892, 37892, 47892, 57892]
_DNS_PORT_CANDIDATES = [17893, 27893, 37893, 47893, 57893]


def _port_free(port: int, also_udp: bool = False) -> bool:
    """探测本机127.0.0.1:port 当前是否空闲（TCP，dns 端口再多探一次 UDP）。

    用bind() 而非 connect()：connect 失败只能证明"没有服务在监听"，但 bind 才能
    证明"我们自己能占住这个端口"——两者的差别在多用户/权限受限环境下会体现出来。
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("127.0.0.1", port))
    except OSError:
        return False
    if also_udp:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


@dataclass
class NodeTestResult:
    name: str
    delay_ms: int | None  # None = 不可达
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.delay_ms is not None


class ProxyManager:
    """代理全生命周期管理。"""

    def __init__(self, config: Config) -> None:
        self.cfg = config
        self.mcfg = config.mihomo
        self.binary = self.mcfg.binary_abs()
        self.work_dir = self.mcfg.work_dir_abs()
        self.run_config = self.work_dir / "config.yaml"
        self.sub_path = self.work_dir / "sub.yaml"
        self.pid_file = self.work_dir / "mihomo.pid"
        self._proc: subprocess.Popen | None = None
        self.nodes: list[ProxyNode] = []
        self._rotated: set[str] = set()  # 本次会话已轮换过的节点，避免来回换同一批
        self._load_persisted_ports()  # 让status/stop 等不经过 start() 的命令也能用对端口

    # ---------- 0. 端口选择（避免跟用户机器上已有的 mihomo/clash 冲突） ----------

    def _load_persisted_ports(self) -> None:
        """把上次持久化选中的端口读回 mcfg（只读取，不做空闲探测）。

        这样 `zlib status` / `zlib stop` 等不经过 `start()` 的命令，也能知道去哪个
        端口找已经在跑的 mihomo 实例，而不是每次都用 config.yaml 里的默认值（现在
        默认是 None）去问一个根本不对的端口，误判成"未运行"。真正的空闲探测/重新
        挑选只在 `start()` 内调用 `ensure_ports()` 时发生。
        """
        ports = self._load_state().get("ports") or {}
        for key in ("http", "socks", "api", "dns"):
            val = ports.get(key)
            if val:
                setattr(self.mcfg, f"{key}_port", val)

    def ensure_ports(self) -> None:
        """解析4类本地端口（http/socks/api/dns），写回 mcfg 并持久化。

        每类端口的候选顺序：用户在 config.yaml 里显式配置的值（若有）→ 上次持久化选中
        的值（若还空闲，尽量保持跨重启端口不变）→ 内置的一组不常见端口逐个探测。
        只在确认不再运行（`start()` 里 `is_running()` 为 False 之后）才调用，避免把
        我们自己已经占用的端口误判成"被占用需要换"。
        """
        self.mcfg.http_port = self._resolve_port("http", self.mcfg.http_port, _HTTP_PORT_CANDIDATES)
        self.mcfg.socks_port = self._resolve_port("socks", self.mcfg.socks_port, _SOCKS_PORT_CANDIDATES)
        self.mcfg.api_port = self._resolve_port("api", self.mcfg.api_port, _API_PORT_CANDIDATES)
        self.mcfg.dns_port = self._resolve_port("dns", self.mcfg.dns_port, _DNS_PORT_CANDIDATES, also_udp=True)
        log.info("本地端口: http=%s socks=%s api=%s dns=%s",
                 self.mcfg.http_port, self.mcfg.socks_port, self.mcfg.api_port, self.mcfg.dns_port)
        self._save_state({"ports": {
            "http": self.mcfg.http_port, "socks": self.mcfg.socks_port,
            "api": self.mcfg.api_port, "dns": self.mcfg.dns_port,
        }})

    @staticmethod
    def _resolve_port(kind: str, current: int | None, candidates: list[int], also_udp: bool = False) -> int:
        tried: list[int] = []
        ordered = ([current] if current else []) + [c for c in candidates if c != current]
        for port in ordered:
            tried.append(port)
            if _port_free(port, also_udp=also_udp):
                return port
        raise RuntimeError(
            f"没有可用的 {kind} 端口：尝试过 {tried} 均被占用。"
            f"请检查是否有其它程序占用了这些端口，或在 config.yaml 的 mihomo.{kind}_port 手动指定一个空闲端口。"
        )

    # ---------- 1. 下载二进制 ----------

    def ensure_binary(self) -> Path:
        """确保本地有可执行的 mihomo。

        「先鸡先蛋」问题：国内大部分网络本身就是因为连不了 GitHub 才需要这个代理工具，
        但装好代理之前又恰恰连不上 GitHub 去下载 mihomo。解法是随包自带一份二进制
        （`vendor/mihomo-linux-{arch}.gz`），首次启动直接本地解压，完全不碰网络；
        只有随包版本缺失当前架构时才尝试联网下载（走原先的 GitHub +镜像多源兜底）。
        之后如果想升级到最新版，用 `zlib upgrade-mihomo`——那时代理已经跑起来了，
        可以经这条已经打通的线路去连 GitHub，不再依赖用户网络能直连。
        """
        if self.binary.exists() and os.access(self.binary, os.X_OK):
            log.debug("mihomo 二进制已存在: %s", self.binary)
            return self.binary
        self.binary.parent.mkdir(parents=True, exist_ok=True)

        vendored = self._vendored_asset()
        if vendored:
            log.info("使用随包自带的 mihomo (%s)，无需联网下载", vendored.name)
            self._extract_gz(vendored, self.binary)
            log.info("mihomo 就绪: %s", self.binary)
            return self.binary

        log.warning("未找到随包自带的 mihomo（当前架构 %s 无预置包），尝试联网下载", _mihomo_arch())
        version = self._latest_version()
        self._download_and_extract(version, self.binary, proxy_url=None)
        log.info("mihomo 就绪: %s", self.binary)
        return self.binary

    def _vendored_asset(self) -> Path | None:
        p = project_root() / "vendor" / f"mihomo-linux-{_mihomo_arch()}.gz"
        return p if p.exists() else None

    @staticmethod
    def _extract_gz(gz_path: Path, dest: Path) -> None:
        with gzip.open(gz_path, "rb") as gz, open(dest, "wb") as out:
            shutil.copyfileobj(gz, out)
        dest.chmod(dest.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    def _download_and_extract(self, version: str, dest: Path, proxy_url: str | None) -> None:
        """下载指定版本并解压到 dest。`proxy_url` 非空时经该代理下载
        （用于「代理已跑起来后再升级」的场景，此时用户网络本身未必能直连 GitHub）。"""
        asset = f"mihomo-linux-{_mihomo_arch()}-{version}.gz"
        urls = [
            f"https://github.com/{MIHOMO_REPO}/releases/download/{version}/{asset}",
            f"https://ghproxy.net/https://github.com/{MIHOMO_REPO}/releases/download/{version}/{asset}",
            f"https://mirror.ghproxy.com/https://github.com/{MIHOMO_REPO}/releases/download/{version}/{asset}",
        ]
        tmp_gz = dest.with_suffix(".gz")
        last_err: Exception | None = None
        client_kwargs: dict = {"timeout": 120, "follow_redirects": True}
        if proxy_url:
            client_kwargs["proxy"] = proxy_url
        for url in urls:
            try:
                log.info("下载 mihomo %s: %s%s", version, url, "（经本地代理）" if proxy_url else "")
                with httpx.Client(**client_kwargs) as c:
                    with c.stream("GET", url) as r:
                        r.raise_for_status()
                        with open(tmp_gz, "wb") as f:
                            for chunk in r.iter_bytes():
                                f.write(chunk)
                break
            except Exception as e:  # noqa: BLE001
                log.warning("下载失败 (%s): %s", url, e)
                last_err = e
        else:
            raise RuntimeError(f"mihomo 二进制下载失败（所有源）：{last_err}")
        self._extract_gz(tmp_gz, dest)
        tmp_gz.unlink(missing_ok=True)

    def _latest_version(self, proxy_url: str | None = None) -> str:
        """取最新版本，超时/失败快速回退到固定版本。"""
        try:
            kwargs: dict = {"timeout": 8}
            if proxy_url:
                kwargs["proxy"] = proxy_url
            with httpx.Client(**kwargs) as c:
                r = c.get(f"https://api.github.com/repos/{MIHOMO_REPO}/releases/latest")
                r.raise_for_status()
                tag = r.json().get("tag_name")
                if tag:
                    return tag
        except Exception as e:  # noqa: BLE001
            log.warning("取 mihomo 最新版本失败，回退 %s: %s", MIHOMO_FALLBACK_VERSION, e)
        return MIHOMO_FALLBACK_VERSION

    def binary_version(self) -> str:
        """当前 mihomo 二进制的版本号（如 `v1.19.29`），供status/upgrade 展示。"""
        if not (self.binary.exists() and os.access(self.binary, os.X_OK)):
            return "未安装"
        try:
            out = subprocess.run([str(self.binary), "-v"], capture_output=True,
                                  text=True, timeout=5).stdout
            m = re.search(r"v[\d.]+", out)
            return m.group(0) if m else out.strip()[:40] or "未知"
        except Exception:  # noqa: BLE001
            return "未知"

    def upgrade_binary(self, proxy_url: str | None) -> tuple[str, str]:
        """经当前代理线路检查并升级 mihomo 到 GitHub 最新版本。

        「先鸡先蛋」的第二步：随包版本可能不是最新的，但那已经不影响首次启动
        （能用随包版本先把订阅节点跑起来）。之后想升级时，直连GitHub 未必通，
        但这条已经验证过能访问Z-Library 的代理线路大概率也能到 GitHub，
        所以升级请求走 `proxy_url`，不依赖用户网络本身。
        """
        old_version = self.binary_version()
        new_version = self._latest_version(proxy_url=proxy_url)
        if new_version == old_version:
            return old_version, new_version
        was_running = self.is_running()
        if was_running:
            self.stop()
        tmp_new = self.binary.with_suffix(".new")
        try:
            self._download_and_extract(new_version, tmp_new, proxy_url=proxy_url)
            os.replace(tmp_new, self.binary)
            self.binary.chmod(self.binary.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        finally:
            tmp_new.unlink(missing_ok=True)
            if was_running:
                self.start()
        return old_version, new_version

    # ---------- 2. 生成配置 ----------

    def prepare_subscription(self, force: bool = False) -> list[ProxyNode]:
        """拉取并解析订阅。默认有效期内（`subscription_cache_hours`）直接复用本地缓存，
        避免每次运行都请求订阅接口；过期或强制刷新时才重新拉取，拉取失败回退本地
        `clash-config.yaml`（若也没有则回退用旧缓存）。"""
        cache_hours = self.mcfg.subscription_cache_hours
        if not force and cache_hours > 0 and self.sub_path.exists():
            age_h = (time.time() - self.sub_path.stat().st_mtime) / 3600
            if age_h < cache_hours:
                nodes = parse_subscription(self.sub_path)
                if nodes:
                    log.info("订阅缓存未过期（%.1fh前拉取），复用本地文件，跳过拉取", age_h)
                    self.nodes = nodes
                    return self.nodes
        try:
            fetch_subscription(self.cfg.subscription_url, self.sub_path)
        except Exception as e:  # noqa: BLE001
            local_fallback = project_root() / "clash-config.yaml"
            if local_fallback.exists():
                log.warning("拉取订阅失败，回退本地配置文件 %s: %s", local_fallback, e)
                self.sub_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy(local_fallback, self.sub_path)
            elif self.sub_path.exists():
                log.warning("拉取订阅失败且无本地兜底文件，继续使用旧缓存: %s", e)
            else:
                raise
        self.nodes = parse_subscription(self.sub_path)
        if not self.nodes:
            raise RuntimeError("订阅无可用节点")
        return self.nodes

    def generate_config(self) -> Path:
        """生成 mihomo 配置：本地端口 + selector 组 + 全走 selector +防污染 DNS。

        DNS 段必须显式配置，不能省。本机（以及多数国内环境）系统DNS 对
        `z-library.sk` 是被投毒的（实测解析到 Facebook 的 31.13.x.x / 2a03:2880::face:b00c，
        且每次查询返回的假地址还不一样）。mihomo 作为 HTTP 代理收到
        `CONNECT zh.z-library.sk:443` 后要自己解析域名，若沿用系统 resolver 就会把流量
        送到假IP，表现为「所有节点测速全部超时 / TLS handshake timeout」——看起来像
        “节点全挂了”，实际节点是好的（同一节点访问 gstatic 正常）。

        解决办法是 fake-ip 模式：mihomo 不在本地做真实解析，直接把域名透传给远端节点由
        出口侧解析，从根上免疫本地投毒。同时保留订阅自带的 `nameserver-policy`
        （订阅里给节点自己的域名指定了专用 DoH，缺了它连节点域名都可能解析错）。
        """
        self.work_dir.mkdir(parents=True, exist_ok=True)
        sub = load_subscription(self.sub_path)
        proxies = [p for p in sub.get("proxies", []) if isinstance(p, dict) and p.get("name")]
        proxies = [p for p in proxies if not _is_placeholder(p["name"])]
        node_names = [p["name"] for p in proxies]

        cfg = {
            "mixed-port": self.mcfg.http_port,  # HTTP+SOCKS 同端口
            "socks-port": self.mcfg.socks_port,
            "allow-lan": False,
            "mode": "rule",
            "log-level": "warning",
            "external-controller": f"127.0.0.1:{self.mcfg.api_port}",
            "secret": self.mcfg.api_secret,
            "dns": self._dns_config(sub),
            "proxies": proxies,
            "proxy-groups": [
                {
                    "name": "ZLIB-SELECT",
                    "type": "select",
                    "proxies": node_names,
                },
            ],
            "rules": ["MATCH,ZLIB-SELECT"],
        }
        with open(self.run_config, "w", encoding="utf-8") as f:
            yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False)
        log.info("mihomo 配置已生成: %s（%d 节点）", self.run_config, len(node_names))
        return self.run_config

    def _dns_config(self, sub: dict) -> dict:
        """构造防投毒 DNS 配置。

        - `enhanced-mode: fake-ip`：域名不在本地做真实解析，域名本身透传给出口节点，
          本地 DNS 投毒对目标站点完全无效。
        - `proxy-server-nameserver`：解析「节点服务器自己的域名」用的解析器。这一步只能走
          直连（还没建立代理），所以用国内可信DoH。
        - `nameserver-policy`：沿用订阅自带的策略（订阅会给自己的节点域名指定专用 DoH，
          丢掉会导致节点域名解析到错误地址）。
        """
        sub_dns = sub.get("dns") or {}
        policy = dict(sub_dns.get("nameserver-policy") or {})
        trusted_cn = ["https://223.5.5.5/dns-query", "https://doh.pub/dns-query"]
        return {
            "enable": True,
            "listen": f"127.0.0.1:{self.mcfg.dns_port}",
            "ipv6": False,
            "enhanced-mode": "fake-ip",
            "fake-ip-range": "198.18.0.1/16",
            "default-nameserver": ["223.5.5.5", "119.29.29.29"],
            "proxy-server-nameserver": trusted_cn,
            "nameserver": trusted_cn,
            "nameserver-policy": policy,
        }

    # ---------- 3. 启动 ----------

    def start(self) -> None:
        if self.is_running():
            log.debug("mihomo 已在运行 (http=%s socks=%s api=%s)",
                      self.mcfg.http_port, self.mcfg.socks_port, self.mcfg.api_port)
            return
        self.ensure_ports()
        self.ensure_binary()
        # 用刚解析出的端口重新生成配置：不能只在文件不存在时才生成，否则端口从上次
        # 运行后发生变化（比如被别的程序占用改选了新端口）时，会启动一个绑定着
        # 陈旧端口配置的 mihomo，跟 mcfg 里的新端口不一致。
        self.generate_config()
        log.info("启动 mihomo ...")
        log_file = open(self.work_dir / "mihomo.log", "ab")
        self._proc = subprocess.Popen(
            [str(self.binary), "-d", str(self.work_dir), "-f", str(self.run_config)],
            stdout=subprocess.DEVNULL,
            stderr=log_file,
            start_new_session=True,  # 脱离当前终端会话，父进程退出后不会被 SIGHUP/SIGPIPE 杀掉
        )
        log_file.close()
        self.pid_file.write_text(str(self._proc.pid))
        # 等 API 就绪
        for _ in range(30):
            if self._api_health():
                log.info("mihomo 已启动 (pid=%s, api=%s)，后台常驻", self._proc.pid, self.mcfg.api_port)
                return
            time.sleep(0.5)
        # 启动失败
        err = ""
        log_path = self.work_dir / "mihomo.log"
        if log_path.exists():
            err = log_path.read_text(encoding="utf-8", errors="replace")[-2000:]
        raise RuntimeError(f"mihomo 启动失败: {err}")

    def is_running(self) -> bool:
        if self._proc and self._proc.poll() is None:
            return True
        return self._api_health()

    def _api_health(self) -> bool:
        try:
            r = self._api_get("/version")
            return r.status_code == 200
        except Exception:  # noqa: BLE001
            return False

    def stop(self) -> None:
        """停止 mihomo。既支持停当前进程持有的 self._proc，也支持通过 pid 文件
        停另一个 CLI 进程启动的后台常驻实例（跨进程管理）。"""
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
            log.info("mihomo 已停止")
        elif self.pid_file.exists():
            try:
                pid = int(self.pid_file.read_text().strip())
                os.kill(pid, signal.SIGTERM)
                log.info("mihomo 已停止 (pid=%s)", pid)
            except (ValueError, ProcessLookupError, PermissionError) as e:
                log.debug("停止 mihomo 失败（可能已不在运行）: %s", e)
        self._proc = None
        self.pid_file.unlink(missing_ok=True)

    # ---------- 4. API 封装 ----------

    def _api_url(self, path: str) -> str:
        return f"{MIHOMO_API_BASE.format(port=self.mcfg.api_port)}{path}"

    def _api_get(self, path: str, params: dict | None = None) -> httpx.Response:
        with httpx.Client(timeout=10) as c:
            return c.get(self._api_url(path), params=params, headers={"Authorization": f"Bearer {self.mcfg.api_secret}"})

    def _api_put(self, path: str, json: dict) -> httpx.Response:
        with httpx.Client(timeout=10) as c:
            return c.put(self._api_url(path), json=json, headers={"Authorization": f"Bearer {self.mcfg.api_secret}"})

    def list_nodes(self) -> list[str]:
        r = self._api_get("/proxies/ZLIB-SELECT")
        r.raise_for_status()
        data = r.json()
        return data.get("all", [])

    def current_node(self) -> str | None:
        r = self._api_get("/proxies/ZLIB-SELECT")
        r.raise_for_status()
        return r.json().get("now")

    def switch_node(self, name: str) -> None:
        r = self._api_put("/proxies/ZLIB-SELECT", {"name": name})
        r.raise_for_status()
        log.info("已切换节点 -> %s", name)

    # ---------- 5. 测速选优 ----------

    def _test_one(self, name: str, test_url: str | None = None) -> NodeTestResult:
        """测单个节点的延迟，供全量测速和「复用上次节点」快速校验共用。"""
        test_url = test_url or self.mcfg.test_url
        try:
            r = self._api_get(
                f"/proxies/{_enc(name)}/delay",
                params={"url": test_url, "timeout": self.mcfg.test_timeout_ms},
            )
            if r.status_code == 200:
                delay = r.json().get("delay")
                return NodeTestResult(name=name, delay_ms=delay)
            msg = r.json().get("message", r.text[:100]) if r.headers.get("content-type", "").startswith("application/json") else r.text[:100]
            return NodeTestResult(name=name, delay_ms=None, error=str(msg))
        except Exception as e:  # noqa: BLE001
            return NodeTestResult(name=name, delay_ms=None, error=str(e))

    def test_all_nodes(self, test_url: str | None = None, max_workers: int = 8) -> list[NodeTestResult]:
        """并发测所有节点到 test_url 的延迟。"""
        test_url = test_url or self.mcfg.test_url
        nodes = self.list_nodes()
        log.info("测速 %d 个节点 -> %s", len(nodes), test_url)
        results: list[NodeTestResult] = [NodeTestResult(name=n, delay_ms=None) for n in nodes]
        import concurrent.futures

        def _test(idx: int, name: str) -> tuple[int, NodeTestResult]:
            res = self._test_one(name, test_url)
            return idx, res

        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = [pool.submit(_test, i, n) for i, n in enumerate(nodes)]
            for fut in concurrent.futures.as_completed(futures):
                idx, res = fut.result()
                results[idx] = res
                flag = f"{res.delay_ms}ms" if res.ok else "FAIL"
                log.debug("  节点 %s: %s %s", res.name, flag, res.error or "")

        ok = [r for r in results if r.ok]
        ok.sort(key=lambda r: r.delay_ms)
        log.info("测速完成: %d/%d 可达，最快 %s",
                 len(ok), len(results),
                 f"{ok[0].name}({ok[0].delay_ms}ms)" if ok else "无")
        return results

    def select_best(self, results: list[NodeTestResult] | None = None) -> NodeTestResult | None:
        if results is None:
            results = self.test_all_nodes()
        ok = [r for r in results if r.ok]
        if not ok:
            return None
        ok.sort(key=lambda r: r.delay_ms)
        best = ok[0]
        self.switch_node(best.name)
        return best

    def proxy_url(self) -> str:
        """返回本地代理 URL，供 httpx/playwright 使用。"""
        return f"http://127.0.0.1:{self.mcfg.http_port}"

    def rotate_node(self) -> str | None:
        """换到下一个「当前实测可用」的节点，返回新节点名；无可换节点返回 None。

        节点抖动很常见（同一节点几分钟内可能从可用变成 TLS 握手超时），而站点又按出口IP
        把请求路由到不同后端、不同后端对同一本书的态度还不一样，所以「换出口重试」
        既能绕开线路抖动，也能绕开拒绝该资源的后端。

        为避免每次轮换都全量测速，这里按节点列表顺序往后找，逐个快速验活，
        第一个通的就用；轮换过的节点记在 `_rotated` 里不再重复。
        """
        try:
            nodes = self.list_nodes()
            current = self.current_node()
        except Exception as e:  # noqa: BLE001
            log.warning("无法获取节点列表，跳过轮换: %s", e)
            return None
        if not nodes:
            return None
        self._rotated.add(current or "")
        start = nodes.index(current) + 1 if current in nodes else 0
        order = nodes[start:] + nodes[:start]
        for name in order:
            if name in self._rotated:
                continue
            self._rotated.add(name)
            if self._test_one(name).ok:
                self.switch_node(name)
                self._save_state({"node": name})
                return name
        # 全部轮换过一遍仍没找到可用的：清空记录再给一次机会。
        # 线路抖动是常态，几十秒前不通的节点现在可能已经恢复，直接放弃太早。
        if self._rotated:
            log.info("全部 %d 个节点轮换过一遍均不可用，清空记录重新尝试一轮", len(nodes))
            self._rotated.clear()
            for name in order:
                if self._test_one(name).ok:
                    self.switch_node(name)
                    self._save_state({"node": name})
                    return name
        log.warning("当前没有任何可用节点")
        return None

    # ---------- 节点选择状态持久化（跨进程复用，避免每次都全量测速） ----------

    def _state_file(self) -> Path:
        return self.work_dir / "state.json"

    def _load_state(self) -> dict:
        f = self._state_file()
        if not f.exists():
            return {}
        try:
            import json

            return json.loads(f.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return {}

    def _save_state(self, patch: dict) -> None:
        """把 `patch` 合并进 state.json 并整体写回（浅合并，同 key 覆盖）。

        必须是合并而非整体覆盖：`ensure_ports()` 存的`ports` 字段和
        `rotate_node()`/`select_best()` 存的 `node` 字段是两个独立调用方各自维护的，
        谁后调用都不该把对方刚存的字段抹掉。
        """
        import json

        state = self._load_state()
        state.update(patch)
        self._state_file().write_text(json.dumps(state), encoding="utf-8")

    # ---------- 一键编排 ----------

    def setup_and_select_best(self) -> NodeTestResult | None:
        """完整流程：准备订阅→启动（内部会解析端口+生成配置）→（优先复用上次节点，不通才全量测速选优）。"""
        self.prepare_subscription()
        self.start()

        last_name = self._load_state().get("node")
        if last_name and last_name in self.list_nodes():
            quick = self._test_one(last_name)
            if quick.ok:
                self.switch_node(last_name)
                log.info("复用上次节点 %s（%dms），跳过全量测速", last_name, quick.delay_ms)
                return quick
            log.info("上次节点 %s 已不可达（%s），重新测速选优", last_name, quick.error or "超时")

        results = self.test_all_nodes()
        best = self.select_best(results)
        if best is None:
            log.warning("所有节点均不可达，强制刷新订阅后重试一次")
            self.prepare_subscription(force=True)
            self.stop()
            self.start()  # 内部会用刷新后的订阅重新生成配置
            results = self.test_all_nodes()
            best = self.select_best(results)
        if best:
            self._save_state({"node": best.name})
        return best


def _enc(name: str) -> str:
    """节点名 URL 编码（含特殊字符）。"""
    import urllib.parse

    return urllib.parse.quote(name, safe="")


# 订阅里混入的「伪节点」：机场用节点名当公告牌展示流量/到期/线路数等信息，
# 它们指向真实服务器所以测速也会通，但选中它们没有意义（且会占用测速时间）。
_PLACEHOLDER_PAT = re.compile(
    r"剩余流量|套餐到期|过滤掉|距离下次|官网|订阅|重置|到期时间|流量：|expire|traffic",
    re.IGNORECASE,
)


def _is_placeholder(name: str) -> bool:
    return bool(_PLACEHOLDER_PAT.search(name))
