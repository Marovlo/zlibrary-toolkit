"""后台健康监控：启动即探测出最优节点，之后尽量长时间复用，不频繁折腾。

设计原则（对应"手动用 VPN 的体感"）：找到一个能用的节点后，只要没人报告它出
问题，就应该一直用下去，不该没事就去 ping 一下、稍有抖动就切换。

三条关键规则：
1. **启动时**：走 CLI 既有的 `_ensure_access()`（测速选优 + 真实校验首页可达），
   选出一个当前最优节点；再额外做一次**真实搜索**（不是简单 GET ping）作为最终
   确认——万一节点能连上首页但搜索接口本身有问题，也能在启动时就发现，而不是
   等用户真的搜索时才踩坑。
2. **稳定态下**（`direct`/`proxy_ok`）：**不做周期性探测**。只有同时满足下面两个
   条件才会真的去复检一次：
   - 空闲：超过 `IDLE_RECHECK_INTERVAL`（默认 1 小时）没有任何真实搜索/下载操作
     （`record_activity()` 由 search/download 接口调用打点；简单的状态轮询
     `GET /api/status` 不算"操作"，不会推迟这个判断）；
   - 距上次检查也超过同样时长（避免刚查过又立刻查）。
   复检本身走**真实搜索流程**（`_probe_via_search`：调用 `client.search()`，
   包含 PoW 挑战求解、真实解析结果），而不是简单发一次 GET 探活——这样才能
   验证"真的能正常搜索"而不只是"服务器还有响应"。
3. **异常态下**（`connecting`/`switching`/`unavailable`）：不受空闲限制，尽快
   重试恢复（`UNHEALTHY_RETRY_INTERVAL`），且单次探测失败不会立刻判定节点坏
   （抖动容忍：原地对同一节点重试几次，仍然全部失败才真的换节点）——避免因
   一次瞬时抖动就来回切换，找到候选后同样用真实搜索流程最终确认才声明"健康"。

真实搜索/下载请求本身遇到传输层错误时的"原地重试再换节点"容忍已经下沉到
`zlibrary.client` 核心逻辑（`_handle_transport_error`），跟这里的健康监控是
两套独立机制：一个负责后台状态展示与"选出最优节点"，一个负责单次真实请求的
自愈；两者都不会因为一次抖动就立刻切换。
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable

from zlibrary.site_checker import check_direct, check_via_proxy

from .access import access_state

log = logging.getLogger("webapp.health")

IDLE_RECHECK_INTERVAL = 3600    # 稳定态下：空闲这么久 且 距上次检查也这么久，才复检一次
UNHEALTHY_RETRY_INTERVAL = 8    # 异常态下：多快重试一次
POLL_TICK = 30                  # 主循环轮询间隔（只判断是否到了该检查的时机，不产生网络请求）

# 抖动容忍：单次探测失败后，原地对同一节点重试这么多次（不含首次），
# 每次间隔这么多秒，全部失败才真正判定节点不可用、触发切换。
JITTER_RETRIES = 2
JITTER_RETRY_GAP = 4

# 用于"真实搜索流程"探测的查询词：随便一个大概率有结果的常见词即可，重点是
# 走完整的搜索请求（含 PoW 挑战求解、结果解析），不是简单 ping 首页。
PROBE_QUERY = "book"

# 状态机：initializing（刚启动未探测过）-> connecting（探测/重新选择中）
# -> direct（直连可用） / proxy_ok（代理节点已验证可访问）
# -> switching（当前节点已不可访问，正在自动换节点） -> unavailable（换完仍不行）
_VALID_STATUSES = ("initializing", "connecting", "direct", "proxy_ok", "switching", "unavailable")


def _check_with_jitter_tolerance(probe: Callable[[], bool]) -> bool:
    """对同一个探测函数原地重试几次，任意一次成功就算通过。

    用于区分「节点真的坏了」和「刚好这一次请求抖动」——只有连续
    `JITTER_RETRIES + 1` 次都失败，才认为是真的不可用。
    """
    if probe():
        return True
    for i in range(JITTER_RETRIES):
        time.sleep(JITTER_RETRY_GAP)
        if probe():
            log.info("首次探测失败但重试第 %d 次已恢复，判定为线路瞬时抖动，不切换节点", i + 1)
            return True
    return False


def _probe_via_search() -> bool:
    """走一次真实搜索流程确认站点搜索功能本身可用（不只是首页能连上）。"""
    try:
        client = access_state.make_client()
    except Exception as e:  # noqa: BLE001
        log.info("搜索探测：无法建立客户端: %s", e)
        return False
    try:
        return bool(client.search(PROBE_QUERY, page=1))
    except Exception as e:  # noqa: BLE001
        log.info("搜索探测失败: %s: %s", type(e).__name__, e)
        return False
    finally:
        client.close()


class HealthMonitor:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.status = "initializing"
        self.node: str | None = None
        self.site: str | None = None
        self.message: str = ""
        self.error: str | None = None
        self.checked_at: float = 0.0
        # 启动时刻当作"最近一次活动"，避免进程刚启动、还没人用过就被当成
        # "空闲超过一小时"，立刻触发一次没必要的复检。
        self.last_activity_at: float = time.time()
        self.last_check_at: float = 0.0
        self._thread: threading.Thread | None = None
        self._wake = threading.Event()
        self._refresh_requested = False

    def _set(self, **kw) -> None:
        with self._lock:
            for k, v in kw.items():
                setattr(self, k, v)
            self.checked_at = time.time()

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "status": self.status, "node": self.node, "site": self.site,
                "message": self.message, "error": self.error, "checked_at": self.checked_at,
            }

    def record_activity(self) -> None:
        """由真实搜索/下载接口调用打点，用于判断"是否空闲"。"""
        with self._lock:
            self.last_activity_at = time.time()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, daemon=True, name="health-monitor")
        self._thread.start()
        log.info("健康监控线程已启动，进程启动即开始探测")

    def request_refresh(self) -> None:
        """请求后台立即复检，接口本身不等待结果。"""
        with self._lock:
            self._refresh_requested = True
        self._wake.set()

    # ---------- 主循环 ----------

    def _loop(self) -> None:
        self._initial_probe()
        self.last_check_at = time.time()

        while True:
            self._wake.wait(POLL_TICK)
            self._wake.clear()
            now = time.time()
            with self._lock:
                forced = self._refresh_requested
                self._refresh_requested = False
                status = self.status
                last_activity = self.last_activity_at
                last_check = self.last_check_at
            if forced:
                self._set(status="switching", message="收到刷新请求，正在检查当前线路...")
                self._recover_via_rotation()
                with self._lock:
                    self.last_check_at = time.time()
                continue
            if status in ("direct", "proxy_ok"):
                idle_long_enough = (now - last_activity) >= IDLE_RECHECK_INTERVAL
                check_due = (now - last_check) >= IDLE_RECHECK_INTERVAL
                if idle_long_enough and check_due:
                    log.info("空闲超过 %ds 且距上次检查也超过该时长，趁空闲复检一次", IDLE_RECHECK_INTERVAL)
                    self._recheck_stable()
                    with self._lock:
                        self.last_check_at = time.time()
            elif (now - last_check) >= UNHEALTHY_RETRY_INTERVAL:
                self._recheck_unhealthy()
                with self._lock:
                    self.last_check_at = time.time()

    def _initial_probe(self) -> None:
        self._set(status="connecting", message="正在检测直连 / 选择可用代理...")
        try:
            access_state.ensure()
        except Exception as e:  # noqa: BLE001
            log.warning("初始接入失败: %s", e)
            self._set(status="unavailable", error="当前网络暂不可用，请稍后重试")
            return

        self._set(message="正在用真实搜索流程验证...")
        if _probe_via_search():
            self._sync_from_access_state()
            return

        log.info("首次搜索验证失败，尝试切换节点直到搜索验证通过")
        self._set(status="switching", message="首次搜索验证失败，正在切换节点...")
        self._recover_via_rotation()

    def _sync_from_access_state(self) -> None:
        _cfg, site, _proxy_url, pm = access_state.ensure()
        if pm is None:
            self._set(status="direct", site=site, node=None, error=None, message="")
        else:
            self._set(status="proxy_ok", site=site, node=pm.current_node(), error=None, message="")

    def _recheck_stable(self) -> None:
        """稳定态下的复检：走真实搜索流程；单次失败先原地重试排除抖动，
        仍不行才真的换节点。"""
        if _check_with_jitter_tolerance(_probe_via_search):
            self._sync_from_access_state()
            return
        self._set(status="switching", message="当前节点连续多次搜索验证失败，正在切换节点...")
        self._recover_via_rotation()

    def _recheck_unhealthy(self) -> None:
        """异常态下的快速恢复：先用轻量请求（+抖动容忍）找候选，找到后再用
        真实搜索流程最终确认，确认通过才声明恢复健康。"""
        try:
            _cfg, site, _proxy_url, pm = access_state.ensure()
        except Exception:  # noqa: BLE001
            self._set(status="unavailable", error="当前网络暂不可用，请稍后重试")
            return

        if pm is None:
            if _check_with_jitter_tolerance(lambda: check_direct(site, timeout=8)):
                if _probe_via_search():
                    self._set(status="direct", site=site, node=None, error=None, message="")
                    return
            log.info("直连已失效，重新走接入流程（可能需要切到代理或备用站点）")
            self._set(status="connecting", message="直连已失效，重新选择接入方式...")
            self._recover_via_rotation()
            return

        if _check_with_jitter_tolerance(lambda: check_via_proxy(pm.proxy_url(), site, timeout=15)):
            if _probe_via_search():
                self._set(status="proxy_ok", site=site, node=pm.current_node(), error=None, message="")
                return
        self._set(status="switching", site=site, node=pm.current_node(),
                  message="当前节点无法正常搜索，正在按主站/备用站恢复...")
        self._recover_via_rotation()

    def _recover_via_rotation(self) -> None:
        """按节点优先、站点兜底重新选择，并用真实搜索确认。"""
        try:
            _cfg, site, _proxy_url, pm = access_state.recover()
        except Exception:
            self._set(status="unavailable", error="暂未找到可正常搜索的站点或节点，请稍后重试")
            return
        if pm is None:
            if _probe_via_search():
                self._set(status="direct", site=site, node=None, error=None, message="")
            else:
                self._set(status="unavailable", site=site, error="当前网络暂不可用，请稍后重试")
            return
        if check_via_proxy(pm.proxy_url(), site, timeout=15) and _probe_via_search():
            self._set(status="proxy_ok", site=site, node=pm.current_node(), error=None, message="")
        else:
            self._set(status="unavailable", site=site, node=pm.current_node(),
                      error="暂未找到可正常搜索的站点或节点，请稍后重试")


# 进程内单例：跟 app 生命周期一致，main.py 启动时调用 start()。
health_monitor = HealthMonitor()
