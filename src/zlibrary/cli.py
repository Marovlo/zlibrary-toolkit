"""CLI 入口：zlib search / zlib download。

全链路：
1. 检测直连 → 不通则起mihomo（后台常驻，跨进程复用，不会每次重新起）选最优代理
2. 下载账号策略：accounts.yaml 里有today额度未用尽的账号 → 优先登录用账号下载
   （体验更稳）；没配置账号或账号额度都用尽 → 自动回退匿名下载（不消耗账号额度，
   但受站点按出口IP计算的每日限额限制，撞到限额会自动换代理节点重试）。
3. 默认列出候选（含年份/大小/评分等参考信息）供用户手动选择；加 `-y` 才自动
   下载排序最优的那个候选，不再询问。

详细说明见 `zlib help`。

后台持久化说明：
- mihomo代理进程用 `start_new_session=True` 启动，不依附于当前 CLI 进程，退出后仍在后台运行；
  下次调用会通过 API 健康检查探测到已在运行，直接复用，不会重复启动/测速。
- 登录态cookie 存到 data/session.json，下次调用先做一次轻量校验（GET /profile），
  仍有效则直接复用，无需重新走账号轮换登录；失效才自动重新登录并刷新保存的登录态。
- `zlib stop` 可手动停止后台代理；`zlib status` 查看当前状态。
"""
from __future__ import annotations

import logging
import re
import secrets
import string
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import click
import httpx

from .client import CloudflareError, IpQuotaExceeded, SearchServiceUnavailable, SiteRejected
from .config import Config, project_root
from .mail import MailConfig, MailError, VerificationMailbox
from .site_checker import check_direct, check_via_proxy

log = logging.getLogger("zlib")

# 日志格式
_FMT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(level=level, format=_FMT, datefmt="%H:%M:%S")
    # 抑制噪声库
    for n in ("httpx", "httpcore", "playwright", "urllib3"):
        logging.getLogger(n).setLevel(logging.WARNING)


def _session_path() -> Path:
    return project_root() / "data" / "session.json"


def _ensure_access(cfg: Config, preferred_site: str | None = None):
    """选择稳定的「代理节点 + 站点」组合。

    站点按主站/备用顺序排列；同一站点先尝试当前节点和近地区节点，
    近节点都失败后才切换站点。成功后由 Web AccessState/CLI 调用方缓存，
    不在每个请求中重新盲测全部域名。
    """
    configured_sites = cfg.sites()
    if not configured_sites:
        raise click.ClickException("没有配置可用的 Z-Library 站点")
    from .proxy_manager import ProxyManager
    pm = ProxyManager(cfg)
    persisted_site = pm._load_state().get("site")
    sticky_site = preferred_site if preferred_site in configured_sites else persisted_site
    if sticky_site not in configured_sites:
        sticky_site = None
    sites = ([sticky_site] if sticky_site else []) + [
        site for site in configured_sites if site != sticky_site
    ]

    direct_timeout = min(8, cfg.access.httpx_timeout)
    for site in sites:
        if check_direct(site, timeout=direct_timeout):
            log.info("✓ 直连可用，无需代理: %s", site)
            pm._save_state({"site": site})
            return site, None, None
    log.info("✗ 直连不可达，启动/复用代理...")

    try:
        best = pm.setup_and_select_best()
    except RuntimeError as e:
        raise click.ClickException(str(e)) from e
    if not best:
        raise click.ClickException("无可用代理节点能连通Z-Library")
    log.info("✓ 选定代理节点: %s (%dms)", best.name, best.delay_ms)

    # 先在当前节点和近地区节点上验证各站点；每个站点开始新一轮节点尝试。
    for site in sites:
        pm.reset_rotation_cycle()
        if _ensure_site_reachable(pm, site, near_only=True):
            pm._save_state({"site": site})
            return site, pm.proxy_url(), pm
        log.info("近地区节点暂不可访问 %s，尝试下一个备用站点", site)

    # 所有站点的近地区节点都失败后，才使用远地区节点兜底。
    for site in sites:
        pm.reset_rotation_cycle()
        if _ensure_site_reachable(pm, site, near_only=False, far_only=True):
            pm._save_state({"site": site})
            return site, pm.proxy_url(), pm

    # 保留原有的最后兜底：线路可能在探测窗口内抖动，请求层仍可继续重试。
    log.warning("暂未找到能访问任何 Z-Library 站点的节点，先保留主站继续尝试")
    return sites[0], pm.proxy_url(), pm


def _ensure_site_reachable(
    pm,
    site: str,
    max_tries: int = 32,
    near_only: bool = False,
    far_only: bool = False,
    timeout: int = 8,
) -> bool:
    """确认当前节点能访问指定站点；近节点阶段不会触碰远节点。"""
    for i in range(1, max_tries + 1):
        if check_via_proxy(pm.proxy_url(), site, timeout=timeout):
            log.info("✓ 当前节点可访问 Z-Library: %s (%s)", pm.current_node(), site)
            return True
        nxt = pm.rotate_node(near_only=near_only, far_only=far_only)
        if not nxt:
            break
        log.info("节点 %d 不能访问 %s，已换 -> %s", i, site, nxt)
    return False


def _make_client(cfg: Config, site: str, proxy_url, pm):
    """构造客户端。把节点轮换能力注入进去：请求遇到线路抖动或被某个后端拒绝时，
    客户端可自行换出口节点重试，无需上层介入。"""
    from .client import ZLibraryClient

    return ZLibraryClient(
        site=site, proxy_url=proxy_url, user_agent=cfg.access.user_agent,
        httpx_timeout=cfg.access.httpx_timeout, playwright_timeout=cfg.access.playwright_timeout,
        rotate_node=(lambda: pm.rotate_node(near_only=True)) if pm else None,
    )


def _load_accounts_optional(cfg: Config):
    """尝试加载账号池；文件不存在或未配置任何账号时返回 `None`——这不是错误，
    表示"当前无账号可用"，调用方（search/download）据此自动回退匿名模式。"""
    from .accounts import AccountStore, DEFAULT_DAILY_LIMIT

    path = project_root() / "accounts.yaml"
    store = AccountStore.load(path, limit=DEFAULT_DAILY_LIMIT)
    return store if store.accounts else None


def _prepare_account(cfg: Config, client, force_anonymous: bool = False):
    """决定并（如需要）执行本次调用的登录策略：**优先账号，无账号才匿名**。

    返回 `(acc, store)`：
    - accounts.yaml 里有今日额度未用尽的账号，且未指定 `--anonymous` → 登录并
      返回 `(acc, store)`，之后的搜索/下载都会带着登录态（体验更稳）。
    - 未配置账号、或全部账号今日额度都已用尽、或显式 `--anonymous` → 返回
      `(None, None)`，调用方走匿名流程（不登录，不消耗账号额度）。

    注意：若账号存在但登录本身失败（密码错误/网络问题等），会直接抛
    `ClickException` 终止命令，**不会**静默回退匿名——账号配置有问题需要用户
    知道并处理，不应该被隐藏掉。
    """
    if force_anonymous:
        log.info("已指定 --anonymous，强制匿名模式")
        return None, None
    store = _load_accounts_optional(cfg)
    if store is None:
        log.info("未配置账号（accounts.yaml 不存在或为空），使用匿名模式")
        return None, None
    if not store.next_available():
        log.info("accounts.yaml 中账号今日下载额度均已用尽，回退匿名模式下载")
        return None, None
    acc = _login(client, store)
    log.info("✓ 使用账号 %s 登录，优先账号下载", acc.email)
    return acc, store


def _login(client, store):
    """先尝试复用本地保存的登录态（免登录）；失效或不存在时才走账号轮换登录，
    成功后保存登录态供下次复用。同一账号本次会话登录失败后不再重试。"""
    session_path = _session_path()
    saved_email = client.load_session(session_path)
    if saved_email:
        acc = store.by_email(saved_email)
        state = client.check_logged_in() if acc and acc.available(store.limit) else False
        if state is None:
            raise click.ClickException(
                "当前网络无法访问 Z-Library（已尝试全部代理节点均超时）。\n"
                "这不是账号问题，请稍后重试，或检查代理订阅是否正常（zlib status）。"
            )
        if acc and state:
            # 以站点为准核对账号身份：本地记录过期时会把额度记到错误的账号上
            real = client.current_account_email()
            if real and real != saved_email:
                log.warning("本地登录态记的是 %s，但站点实际登录的是 %s，按站点为准",
                            saved_email, real)
                real_acc = store.by_email(real)
                if real_acc is None:
                    log.info("账号 %s 不在账号池，重新登录以对齐", real)
                else:
                    acc = real_acc
                    client.save_session(session_path, real)
            if acc.available(store.limit):
                log.info("✓ 复用已保存的登录态: %s（免登录）", acc.email)
                return acc
        log.info("已保存的登录态失效或账号额度已用尽，重新登录")

    tried: set[str] = set()
    last_err = ""
    while True:
        acc = store.next_available(exclude=tried)
        if not acc:
            if tried:
                raise click.ClickException(f"所有账号登录均失败，最后错误: {last_err}")
            raise click.ClickException("所有账号今日下载次数已用尽，请明天再试或补充账号")
        log.info("尝试登录: %s", acc.email)
        res = client.login(acc.email, acc.password)
        if res.ok:
            if res.remaining is not None:
                store.set_remaining(acc, res.remaining)
                if res.remaining <= 0:
                    log.info("账号 %s 剩余次数为 0，切换下一个", acc.email)
                    tried.add(acc.email)
                    continue
            log.info("✓ 登录成功 (%s)，剩余下载 %s", res.method, res.remaining if res.remaining is not None else "未知")
            client.save_session(session_path, acc.email)
            return acc
        tried.add(acc.email)
        last_err = res.error
        log.warning("登录失败: %s，尝试下一个账号", last_err)


@click.group()
@click.option("-v", "--verbose", is_flag=True, help="详细日志")
def cli(verbose: bool) -> None:
    """Z-Library 一键搜索下载工具。"""
    _setup_logging(verbose)


def _search_with_status(client, query: str, page: int = 1):
    """搜索的统一入口：区分「这本书搜不到」和「站点搜索服务本身故障」。

    后者实测表现为 HTTP 200 + 完整页面框架，但结果区域只有一句
    "Search service temporary unavailable!"，跟真的0 结果长得一样，不特别检测的话
    会被误报成"未找到相关书籍"，让人误以为是这本书的问题。

    这是站点后端的间歇故障，不是网络/代理/账号问题，换节点、换 playwright 都没用。
    只做一次快速重试（等5秒）过滤掉几秒钟的抖动，不做死等——仍不行就如实告知。
    """
    try:
        return client.search(query, page=page)
    except SearchServiceUnavailable:
        log.warning("站点搜索服务临时不可用，5 秒后重试一次")
        time.sleep(5)
        try:
            return client.search(query, page=page)
        except SearchServiceUnavailable:
            raise


def _is_route_failure(exc: Exception) -> bool:
    return isinstance(exc, (CloudflareError, SearchServiceUnavailable, httpx.TransportError))


def _search_cli_with_recovery(
    cfg: Config,
    client,
    site: str,
    proxy_url,
    pm,
    query: str,
    page: int,
    force_anonymous: bool,
):
    """当前组合失败后只做一次外层恢复；客户端内部节点重试先于此处切站点。"""
    try:
        acc, store = _prepare_account(cfg, client, force_anonymous)
        results = _search_with_status(client, query, page=page)
        return client, site, proxy_url, pm, acc, store, results
    except Exception as e:
        if not _is_route_failure(e):
            raise
    client.close()
    site, proxy_url, pm = _ensure_access(cfg, preferred_site=site)
    client = _make_client(cfg, site, proxy_url, pm)
    try:
        acc, store = _prepare_account(cfg, client, force_anonymous)
        results = _search_with_status(client, query, page=page)
        return client, site, proxy_url, pm, acc, store, results
    except Exception:
        client.close()
        raise


@cli.command()
@click.argument("query")
@click.option("-p", "--page", default=1, help="页码")
@click.option("--anonymous", "force_anonymous", is_flag=True, help="强制匿名搜索，即使配置了账号也不登录")
def search(query: str, page: int, force_anonymous: bool) -> None:
    """搜索书籍。"""
    cfg = Config.load()
    site, proxy_url, pm = _ensure_access(cfg)
    client = _make_client(cfg, site, proxy_url, pm)
    try:
        try:
            client, site, proxy_url, pm, _acc, _store, results = _search_cli_with_recovery(
                cfg, client, site, proxy_url, pm, query, page, force_anonymous,
            )
        except SearchServiceUnavailable:
            raise click.ClickException(
                'Z-Library 站点搜索服务临时故障，主站和备用站均未恢复，请稍后重试。'
            ) from None
        if not results:
            click.echo("未找到相关书籍")
            return
        _display_results(results, query)
    finally:
        client.close()
        # 注意：代理进程（mihomo）后台常驻，不在这里 stop，跨次调用复用。用 `zlib stop` 手动停止。


@cli.command()
@click.argument("query")
@click.option("--yes", "-y", is_flag=True, help="自动下载排序最优的候选，不进入交互选择列表")
@click.option("--limit", default=10, help="交互列表显示候选数量")
@click.option("--anonymous", "force_anonymous", is_flag=True, help="强制匿名下载，即使配置了账号也不登录")
def download(query: str, yes: bool, limit: int, force_anonymous: bool) -> None:
    """搜索并下载书籍。默认列出候选（含年份/大小/评分）供手动选择；加-y 才自动
    下载排序最优的那个候选，不再询问。详细账号/IP限额策略见 zlib help。
    """
    cfg = Config.load()
    site, proxy_url, pm = _ensure_access(cfg)
    client = _make_client(cfg, site, proxy_url, pm)
    try:
        try:
            client, site, proxy_url, pm, acc, store, results = _search_cli_with_recovery(
                cfg, client, site, proxy_url, pm, query, 1, force_anonymous,
            )
        except SearchServiceUnavailable:
            raise click.ClickException(
                'Z-Library 站点搜索服务临时故障，主站和备用站均未恢复，请稍后重试。'
            ) from None
        if not results:
            click.echo("未找到相关书籍")
            return

        candidates = _rank_candidates(results, query, cfg)
        if not candidates:
            click.echo("搜到结果但均缺少下载链接，无法下载")
            return

        if yes:
            _echo_auto_pick(candidates[0], query)
            client, pm = _do_download(client, pm, acc, store, candidates, cfg)
            return

        # 默认：列出候选让用户自己选，不自动下载
        _display_results(results[:limit], query)
        choice = click.prompt("输入序号下载（回车跳过）", default="", show_default=False)
        if not choice.strip():
            click.echo("已跳过")
            return
        try:
            idx = int(choice) - 1
        except ValueError:
            click.echo("无效输入")
            return
        if not (0 <= idx < len(results[:limit])):
            click.echo("序号超出范围")
            return
        book = results[:limit][idx]
        # 用户明确选了某一条，就以它为首选，其余同名候选作为失效兜底
        others = [b for b in candidates if b is not book]
        client, pm = _do_download(client, pm, acc, store, [book] + others, cfg)
    finally:
        client.close()
        # 注意：代理进程（mihomo）后台常驻，不在这里 stop，跨次调用复用。用 `zlib stop` 手动停止。


def _echo_auto_pick(book, query: str) -> None:
    score = book.match_score(query)
    if score == 100:
        tag = "完全匹配"
    elif score >= 90:
        tag = "前缀匹配"
    elif score >= 50:
        tag = "近似匹配"
    else:
        tag = "标题匹配度低，请确认是否是你要的书"
    click.echo(f"自动选择（{tag}）：《{book.title}》- {book.author} "
               f"({book.year} {(book.format or '?').upper()} {book.size}) 评分 {book.rating}")


def _rank_candidates(results, query: str, cfg: Config, max_candidates: int = 6) -> list:
    """把搜索结果排成「候选下载列表」。

    为什么需要候选列表而不是只挑一本：同一本书在 z-library 上往往有多条记录（不同
    上传者/版本/文件），**其中一部分记录的文件已经失效**——站点对这些记录的 `/dl/`
    直接返回 `204 No Content`。实测搜索"DK魔法百科"多次都会返回一条标题**完全等于**
    查询词、作者/年份明显是垃圾数据的死记录（返回204），而真正能下的那条标题带着
    一堆副标题/丛书信息后缀，只能算"前缀匹配"。原实现只取"完全匹配里评分最高"的
    那一条，正好总是命中死记录；现在候选队列覆盖完全匹配+前缀匹配+普通包含匹配的
    全部结果，某条被拒会自动换下一条，不会再卡死在同一条记录上。

    排序：先按标题匹配度（完全100 > 前缀90 > 包含50），再按格式偏好，最后按评分——
    **不看文件大小**（评分代表下载过的人打的分，比大小更能反映内容质量，大小已在
    DEV.md 第十七节讨论过，明确不纳入排序）。
    """
    pref = [f.lower() for f in cfg.format_preference]

    def key(b):
        fmt = (b.format or "").lower()
        return (
            -b.match_score(query),
            pref.index(fmt) if fmt in pref else len(pref),
            -b.rating,
        )

    return sorted([b for b in results if b.download_url or b.detail_url], key=key)[:max_candidates]


def _rebuild_download_client(client, cfg: Config, acc, store):
    """当前客户端完成节点级重试仍失败后，按主站/备用站重建一次客户端。"""
    old_site = client.site
    client.close()
    new_client = None
    try:
        site, proxy_url, pm = _ensure_access(cfg, preferred_site=old_site)
        new_client = _make_client(cfg, site, proxy_url, pm)
        if acc:
            result = new_client.login(acc.email, acc.password)
            if not result.ok:
                raise click.ClickException(f"切换站点后账号登录失败: {result.error}")
            if result.remaining is not None:
                store.set_remaining(acc, result.remaining)
        return new_client, pm
    except Exception:
        if new_client is not None:
            new_client.close()
        raise


def _do_download(client, pm, acc, store, candidates, cfg: Config):
    """按候选顺序逐条尝试下载，第一条成功即返回 `(client, pm)`。

    两个要点：
    1. 只在真正下载成功、且本次调用一开始就决定使用登录账号时才计账号额度
       （`acc` 由 `_prepare_account` 提前决定，贯穿本次调用始终不变）；匿名
       下载成功不消耗任何账号额度。
    2. 某条记录被站点拒绝（204）时换下一条候选记录，而不是换账号/换节点——
       这类拒绝是"这个文件没了"，跟账号、出口IP都无关。匿名模式下若撞到
       "出口IP每日限额用完"（`IpQuotaExceeded`），处理逻辑在 `_download_one`
       里（自动换节点重试）。
    """
    if not isinstance(candidates, list):
        candidates = [candidates]
    errors: list[str] = []
    route_recovered = False
    while True:
        restart_candidates = False
        for idx, book in enumerate(candidates, 1):
            if idx > 1:
                click.echo(f"→ 换下一个候选版本重试:《{book.title[:40]}》"
                           f"（{(book.format or '?').upper()} {book.size}）")
            try:
                path = _download_one(client, pm, book, cfg, acc)
            except SiteRejected as e:
                log.warning("候选 %d 被站点拒绝（该记录的文件已失效）: %s", idx, e)
                errors.append(f"候选{idx}《{book.title[:24]}》: {e}")
                continue
            except Exception as e:  # noqa: BLE001
                if _is_route_failure(e) and not route_recovered:
                    log.warning("当前站点/节点下载失败，按节点优先策略重新选择一次")
                    try:
                        client, pm = _rebuild_download_client(client, cfg, acc, store)
                    except Exception as recovery_error:  # noqa: BLE001
                        errors.append(f"线路恢复失败: {recovery_error}")
                        restart_candidates = False
                        break
                    route_recovered = True
                    restart_candidates = True
                    break
                log.error("候选 %d 下载失败: %s", idx, e)
                errors.append(f"候选{idx}《{book.title[:24]}》: {e}")
                continue
            if acc:
                store.mark_used(acc)
            click.echo(f"下载完成: {path}" + ("" if acc else "（匿名下载，未消耗账号额度）"))
            return client, pm
        if restart_candidates:
            continue
        detail = "\n  ".join(errors)
        raise click.ClickException(
            f"已尝试全部 {len(candidates)} 个候选版本，均未成功:\n  {detail}\n"
            "客户端已自动解过站点浏览器校验、并轮换过出口节点与后端。若全部候选都是 204，"
            "说明这些记录的文件在站点侧已失效/下架，换账号或换节点都无效。"
        )


def _download_one(client, pm, book, cfg: Config, acc):
    """下载单本书。匿名模式（acc为None）下若撞到出口IP每日限额已用完，
    自动换节点重试；换完全部可用节点仍不行才报错。登录模式下理论上不会触发
    IpQuotaExceeded（该限额只按匿名请求计），若真的触发，大概率是该账号
    自身的下载额度在站点侧也已用尽。
    """
    while True:
        try:
            return client.download(book, cfg.download_dir_abs(), cfg.format_preference)
        except IpQuotaExceeded as e:
            log.warning("当前出口IP匿名下载额度已用完（IP: %s）", e.ip or "未知")
            if not pm:
                extra = "（当前已登录账号，可能是该账号在站点侧的下载额度也已用尽）" if acc else ""
                raise click.ClickException(
                    f"当前直连访问（无代理可切换），匿名下载额度已用完，无法继续。{extra}"
                ) from e
            nxt = pm.rotate_node()
            if not nxt:
                hint = ("该账号在站点侧的下载额度可能也已用尽，建议换个账号或明天再试"
                        if acc else "请添加账号（zlib add-account）后重试")
                raise click.ClickException(
                    f"已尝试全部可用出口节点，匿名下载额度均已用完。{hint}"
                ) from e
            log.info("换出口节点重试 -> %s", nxt)
            continue


@cli.command("add-account")
@click.argument("email")
@click.argument("password", required=False)
def add_account(email: str, password: str | None) -> None:
    """添加账号：先做一次真实登录测试，成功后才持久化到 accounts.yaml；失败会提示具体原因，不写入。"""
    if not password:
        password = click.prompt("密码", hide_input=True)

    cfg = Config.load()
    site, proxy_url, pm = _ensure_access(cfg)
    client = _make_client(cfg, site, proxy_url, pm)
    try:
        log.info("测试登录: %s", email)
        res = client.login(email, password)
        if not res.ok:
            raise click.ClickException(f"登录测试失败，账号未添加。原因: {res.error}")
        click.echo(f"✓ 登录测试成功 (方式: {res.method}，剩余下载 {res.remaining if res.remaining is not None else '未知'})")
    finally:
        client.close()

    from .accounts import AccountStore, DEFAULT_DAILY_LIMIT

    path = project_root() / "accounts.yaml"
    store = AccountStore.load(path, limit=DEFAULT_DAILY_LIMIT)
    existed = store.by_email(email) is not None
    store.add_account(email, password, remaining=res.remaining)
    click.echo(f"✓ 账号 {email} 已{'更新' if existed else '添加'}到账号池（{path}）")


_REGISTER_DOMAIN = "marovlo.cloud"
_REGISTER_LOCAL_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._+-]{0,62}[A-Za-z0-9])?\\Z")


def _registration_email(value: str | None, existing: set[str]) -> str:
    if value:
        raw = value.strip()
        if "@" in raw:
            local, domain = raw.rsplit("@", 1)
            if domain.casefold() != _REGISTER_DOMAIN:
                raise click.ClickException(f"注册邮箱必须使用 @{_REGISTER_DOMAIN} 域名")
        else:
            local = raw
        if not _REGISTER_LOCAL_RE.fullmatch(local):
            raise click.ClickException("注册邮箱的本地部分格式无效")
        address = f"{local}@{_REGISTER_DOMAIN}"
        if address.casefold() in existing:
            raise click.ClickException(f"账号已存在，拒绝重复注册: {address}")
        return address

    for _ in range(10):
        local = f"test-{secrets.token_hex(6)}"
        address = f"{local}@{_REGISTER_DOMAIN}"
        if address.casefold() not in existing:
            return address
    raise click.ClickException("无法生成不重复的测试邮箱地址")


def _registration_password(value: str | None) -> tuple[str, bool]:
    if value is not None:
        if value:
            return value, False
        raise click.ClickException("--password 不能是空值")
    typed = click.prompt("Z-Library 密码（直接回车自动生成）", hide_input=True, default="", show_default=False)
    if typed:
        confirmation = click.prompt("确认密码", hide_input=True)
        if typed != confirmation:
            raise click.ClickException("两次输入的密码不一致")
        return typed, False
    alphabet = string.ascii_letters + string.digits + "-_!@#$%^&*"
    return "".join(secrets.choice(alphabet) for _ in range(24)), True


@cli.command("register-account")
@click.option("--email", "email_value", default=None, help="邮箱地址或 @marovlo.cloud 前的本地部分")
@click.option("--password", default=None, help="Z-Library 密码；不提供则隐藏询问或自动生成")
@click.option("--mail-timeout", type=click.FloatRange(min=1), default=None, show_default=False,
              help="等待验证码的最长秒数（默认读取 mail.yaml）")
def register_account(email_value: str | None, password: str | None, mail_timeout: float | None) -> None:
    """注册一个 Z-Library 账号并通过邮箱验证码完成验证。"""
    from .accounts import AccountStore, DEFAULT_DAILY_LIMIT

    try:
        mail_cfg = MailConfig.load()
        cfg = Config.load()
        accounts_path = project_root() / "accounts.yaml"
        store = AccountStore.load(accounts_path, limit=DEFAULT_DAILY_LIMIT)
        existing = {a.email.casefold() for a in store.accounts}
        email = _registration_email(email_value, existing)
        password, generated_password = _registration_password(password)
        mailbox = VerificationMailbox(mail_cfg)
        seen_uids = mailbox.snapshot(email)
        not_before = datetime.now(timezone.utc)
    except (MailError, FileNotFoundError, OSError, ValueError, TypeError, KeyError) as e:
        raise click.ClickException(str(e)) from e

    click.echo(f"注册邮箱: {email}")
    if generated_password:
        click.echo(f"自动生成的 Z-Library 密码（请保存）: {password}")

    client = None
    try:
        site, proxy_url, pm = _ensure_access(cfg)
        client = _make_client(cfg, site, proxy_url, pm)
        log.info("提交 Z-Library 注册请求")
        registration = client.begin_registration(email, password)
        click.echo("注册请求已提交，等待邮箱验证码...")
        code = mailbox.wait_for_code(email, seen_uids, not_before, timeout=mail_timeout)
        click.echo("已收到验证码，正在完成注册...")
        result = client.finish_registration(registration, code)
        if not result.ok:
            raise click.ClickException(f"邮箱验证失败: {result.error}")
        login_result = client.login(email, password)
        if not login_result.ok:
            raise click.ClickException(f"注册完成但登录验证失败: {login_result.error}")
    except MailError as e:
        raise click.ClickException(str(e)) from e
    except click.ClickException:
        raise
    except Exception as e:  # noqa: BLE001
        raise click.ClickException(f"注册失败: {e}") from e
    finally:
        if client is not None:
            client.close()

    store.add_account(email, password, remaining=login_result.remaining)
    click.echo(f"✓ 注册并登录验证成功，账号已添加到账号池（{accounts_path}）")


def _write_config_value(path: Path, key: str, value: str) -> None:
    """把config.yaml 顶层某个字符串字段替换成新值，只改这一行，保留其余内容
    （注释、其它字段）不变。字段不存在则追加到文件末尾。"""
    text = path.read_text(encoding="utf-8")
    pattern = re.compile(rf'^{re.escape(key)}:\s*.*$', re.MULTILINE)
    new_line = f'{key}: "{value}"'
    if pattern.search(text):
        text = pattern.sub(new_line, text, count=1)
    else:
        text = text.rstrip("\n") + f"\n{new_line}\n"
    path.write_text(text, encoding="utf-8")


@cli.command("set-subscription")
@click.argument("url")
def set_subscription(url: str) -> None:
    """设置/更新 mihomo 代理订阅链接，并立即验证是否有效。

    验证顺序：拉取订阅（校验是否是合法 clash 格式）→ 写入 config.yaml → 用新订阅
    重启代理 → 测速全部节点 → 挑最优节点验证真的能访问 Z-Library。只要拉取/格式
    校验失败就不会写入配置，避免把一个坏链接留下来覆盖原本能用的订阅。
    """
    import tempfile

    from .subscription import fetch_subscription, parse_subscription

    tmp_path = Path(tempfile.mktemp(suffix=".yaml"))
    try:
        fetch_subscription(url, tmp_path)
        nodes = parse_subscription(tmp_path)
    except Exception as e:  # noqa: BLE001
        raise click.ClickException(f"订阅链接拉取/解析失败，未写入配置。原因: {e}") from e
    finally:
        tmp_path.unlink(missing_ok=True)
    if not nodes:
        raise click.ClickException("订阅拉取成功但解析出 0 个节点，未写入配置（可能不是有效的 clash 订阅）")
    click.echo(f"✓ 订阅格式有效，解析出 {len(nodes)} 个节点")

    config_path = project_root() / "config.yaml"
    _write_config_value(config_path, "subscription_url", url)
    click.echo(f"✓ 已写入 {config_path}")

    cfg = Config.load()  # 重新加载，读到刚写入的新subscription_url
    from .proxy_manager import ProxyManager

    pm = ProxyManager(cfg)
    if pm.is_running():
        pm.stop()
    pm.prepare_subscription(force=True)
    pm.start()
    click.echo("正在测速全部节点...")
    results = pm.test_all_nodes()
    ok = [r for r in results if r.ok]
    click.echo(f"节点可达: {len(ok)}/{len(results)}")
    if not ok:
        raise click.ClickException("订阅已保存，但全部节点测速均不可达，请检查订阅是否已过期/欠费/被限速")
    best = pm.select_best(results)
    click.echo(f"✓ 最快节点: {best.name} ({best.delay_ms}ms)")
    click.echo("验证节点能否访问 Z-Library...")
    _ensure_site_reachable(pm, cfg.default_site)
    click.echo("✓ 订阅设置完成且已验证可用")


@cli.command()
def status() -> None:
    """查看后台代理/登录态状态。"""
    cfg = Config.load()
    from .proxy_manager import ProxyManager

    pm = ProxyManager(cfg)
    if pm.is_running():
        node = pm.current_node() or "未知"
        click.echo(f"代理: 运行中 (节点: {node})")
        click.echo(f"本地端口: HTTP/SOCKS 127.0.0.1:{pm.mcfg.http_port}，"
                   f"控制API 127.0.0.1:{pm.mcfg.api_port}")
    else:
        click.echo("代理: 未运行")
    click.echo(f"mihomo 版本: {pm.binary_version()}")

    session_path = _session_path()
    if session_path.exists():
        import json

        data = json.loads(session_path.read_text(encoding="utf-8"))
        click.echo(f"登录态: 已保存 (账号: {data.get('email', '未知')})")
    else:
        click.echo("登录态: 未保存")

    # 百度网盘
    from .baidupcs import BaiduPCSManager, load_cookies

    mgr = BaiduPCSManager(cfg)
    click.echo(f"BaiduPCS-Go 版本: {mgr.binary_version()}")
    if load_cookies():
        if mgr.is_logged_in():
            who = mgr._run("who").stdout.strip()
            click.echo(f"百度网盘: 已登录 ({who or '未知账号'})")
        else:
            click.echo("百度网盘: 已配置 cookies（当前未登录，下次使用时会自动登录）")
    else:
        click.echo("百度网盘: 未配置（zlib add-baidu-cookies 添加）")


@cli.command("upgrade-mihomo")
def upgrade_mihomo() -> None:
    """检查并升级 mihomo 到 GitHub 最新版本。

    随包自带的 mihomo 可能不是最新版，但这不影响首次启动（老版本一样能用）。
    升级时优先经**当前已经验证过能访问 Z-Library 的代理线路**去连 GitHub——
    这条线路大概率也能到 GitHub，不依赖用户网络本身能直连（这就是"先鸡先蛋"
    问题的解法：先用旧版本把代理跑起来，再用这条代理去换新版本）。
    """
    cfg = Config.load()
    site, proxy_url, pm = _ensure_access(cfg)
    if pm is None:
        from .proxy_manager import ProxyManager

        pm = ProxyManager(cfg)
        pm.ensure_binary()
    click.echo(f"当前版本: {pm.binary_version()}，检查更新中...")
    old, new = pm.upgrade_binary(proxy_url)
    if old == new:
        click.echo(f"已是最新版本: {new}")
    else:
        click.echo(f"✓ mihomo 已升级: {old} -> {new}")


@cli.command("add-baidu-cookies")
@click.argument("cookies", required=False)
def add_baidu_cookies(cookies: str | None) -> None:
    """添加百度网盘登录凭证：先验证 cookies 能否登录，成功才写入 baidu.yaml。

    cookies 获取方法：浏览器登录 pan.baidu.com → F12 → Application → Cookies →
    复制 BDUSS 和 STOKEN（必须从 pan.baidu.com 取），或直接复制整段 Cookie 字符串。
    """
    if not cookies:
        click.echo("请粘贴百度网盘 cookies（从 pan.baidu.com 的 F12 → Cookies 取）：")
        cookies = click.prompt("cookies", hide_input=True)

    cfg = Config.load()
    from .baidupcs import BaiduPCSManager

    mgr = BaiduPCSManager(cfg)
    mgr.ensure_binary()
    click.echo("验证 cookies 中...")
    ok, msg = mgr.login(cookies)
    if not ok:
        raise click.ClickException(f"cookies 验证失败，未写入。原因: {msg}")
    click.echo(f"✓ 登录成功: {msg}")
    from .baidupcs import save_cookies

    save_cookies(cookies)
    click.echo(f"✓ cookies 已写入 {project_root() / 'baidu.yaml'}")


@cli.command("upgrade-baidupcs")
def upgrade_baidupcs() -> None:
    """检查并升级 BaiduPCS-Go 到 GitHub 最新版本（经当前代理线路下载）。"""
    cfg = Config.load()
    site, proxy_url, pm = _ensure_access(cfg)
    from .baidupcs import BaiduPCSManager

    mgr = BaiduPCSManager(cfg)
    mgr.ensure_binary()
    click.echo(f"当前版本: {mgr.binary_version()}，检查更新中...")
    old, new = mgr.upgrade_binary(proxy_url)
    if old == new:
        click.echo(f"已是最新版本: {new}")
    else:
        click.echo(f"✓ BaiduPCS-Go 已升级: {old} -> {new}")


@cli.command()
def stop() -> None:
    """停止后台常驻的代理进程（mihomo）。"""
    cfg = Config.load()
    from .proxy_manager import ProxyManager

    pm = ProxyManager(cfg)
    if pm.is_running():
        pm.stop()
        click.echo("已停止代理")
    else:
        click.echo("代理未在运行")


@cli.command()
def logout() -> None:
    """清除本地保存的登录态，下次运行会重新登录。"""
    session_path = _session_path()
    if session_path.exists():
        session_path.unlink()
        click.echo("已清除登录态")
    else:
        click.echo("没有已保存的登录态")


@cli.command()
def help() -> None:  # noqa: A001 - 故意用 help 这个名字，符合用户对`zlib help` 的直觉
    """显示详细使用说明（命令列表、账号策略、IP限额说明）。"""
    click.echo(_HELP_TEXT)


_HELP_TEXT = """
Z-Library 一键搜索下载工具 - 详细说明
========================================

命令列表
--------
  zlib search <书名>                搜索书籍
  zlib download <书名>              搜索并列出候选下载列表，手动选择序号下载
  zlib download <书名> -y           自动下载排序最优的候选，不进入交互选择
  zlib add-account <邮箱> [密码]     添加/更新已有账号（先真实登录测试，成功才写入 accounts.yaml）
  zlib register-account              用 @marovlo.cloud Catch-all 邮箱注册并验证新账号
  zlib add-baidu-cookies [cookies]   添加百度网盘凭证（先验证能否登录，成功才写入 baidu.yaml）
  zlib set-subscription <链接>      设置/更新代理订阅链接，并立即验证是否有效
  zlib status                       查看代理/登录态/百度网盘状态
  zlib upgrade-mihomo                升级本地 mihomo 代理内核到最新版
  zlib upgrade-baidupcs              升级 BaiduPCS-Go 到最新版
  zlib stop                          停止后台常驻的代理进程
  zlib logout                        清除本地保存的登录态
  全局选项 -v/--verbose 放在 zlib 之后可看详细日志，如 zlib -v download 三体

账号下载策略（优先账号，回退匿名）
--------------------------------
  1. accounts.yaml 中存在今日下载额度未用尽的账号，优先登录用账号下载
     （体验最稳定，不受下面的"出口IP每日限额"影响）。
  2. 未配置账号，或全部账号额度都已用尽，自动回退匿名下载（不登录、不消耗
     账号额度），但匿名下载受Z-Library 按出口 IP 计算的每日限额限制。
  3. 用 --anonymous 可强制跳过账号，即使配置了账号也用匿名下载。

出口IP每日限额说明
------------------
  Z-Library 对未登录的匿名请求，按下载方的出口 IP 统计每日下载次数，超额会
  返回"每日限额已用完，请登录账号"的提示。本工具检测到这种情况会自动切换
  代理节点（相当于换一个出口IP）重试；如果换完所有可用节点仍然超额，说明
  这批代理节点的出口 IP 大概率是共享 IP、被其它用户占用了额度，此时只能：
    - 添加一个账号（zlib add-account），账号下载不受IP限额影响；或
    - 更换代理订阅（zlib set-subscription），换一批新的出口 IP。

候选排序规则
-----------
  同一本书在站点上常有多条记录（不同上传者/版本），部分记录的文件已失效
  （下载会被拒绝）。下载候选按以下优先级排序：
    1. 标题匹配度：与书名完全一致 > 标题以书名开头 > 标题包含书名
    2. 格式偏好（config.yaml 的 format_preference，如 epub 优先于 pdf）
    3. 评分（不看文件大小 - 评分反映真实读者的下载后评价，比文件大小更能
       代表内容质量；同名书优先下载评分高的那条）
  若排在前面的候选被站点拒绝（文件失效），会自动换下一个候选重试。

常见问题
-------
  Q: 为什么有的书能下载，有的提示额度已用完？
  A: 见上面"出口IP每日限额说明"，跟你选的这本书无关。

  Q: 下载总失败，报该记录文件已失效？
  A: 这本书在站点侧的这条具体记录已下架，换个候选（如果有多个，会自动换）或过段时间再试。

  Q: 怎么换代理订阅？
  A: zlib set-subscription <新的clash订阅链接>，会自动验证格式和连通性。
"""


def _display_results(results, query: str) -> None:
    from rich.console import Console
    from rich.table import Table

    console = Console()
    table = Table(show_header=True, header_style="bold cyan", title=f"搜索: {query}")
    table.add_column("#", style="dim", width=3)
    table.add_column("标题", max_width=40)
    table.add_column("作者", max_width=20)
    table.add_column("年", width=5)
    table.add_column("格式", width=6)
    table.add_column("大小", width=8)
    table.add_column("评分", width=5)
    for i, b in enumerate(results, 1):
        score = b.match_score(query)
        mark = (" [green]★完全匹配[/]" if score == 100 else
                " [cyan]≈前缀匹配[/]" if score >= 90 else
                " [yellow]~近似[/]" if score >= 50 else "")
        table.add_row(
            str(i), b.title[:40] + mark, b.author[:20], b.year,
            (b.format or "").upper(), b.size, f"{b.rating:.1f}" if b.rating else "-",
        )
    console.print(table)


def main() -> None:
    cli()


if __name__ == "__main__":
    main()
