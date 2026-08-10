"""CLI 入口：zlib search / zlib download。

全链路：
1. 检测直连 → 不通则起mihomo（后台常驻，跨进程复用，不会每次重新起）选最优代理
2. 复用本地保存的登录态；失效才重新选账号登录，成功后保存登录态
3. 搜索（一次拿全信息）
4. 完全匹配→自动下载最高评分；否则列出供选择

后台持久化说明：
- mihomo 代理进程用 `start_new_session=True` 启动，不依附于当前 CLI 进程，退出后仍在后台运行；
  下次调用会通过 API 健康检查探测到已在运行，直接复用，不会重复启动/测速。
- 登录态 cookie 存到 data/session.json，下次调用先做一次轻量校验（GET /profile），
  仍有效则直接复用，无需重新走账号轮换登录；失效才自动重新登录并刷新保存的登录态。
- `zlib stop` 可手动停止后台代理；`zlib status` 查看当前状态。
"""
from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

import click

from .client import SearchServiceUnavailable, SiteRejected
from .config import Config, project_root
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


def _ensure_access(cfg: Config):
    """检测直连，不通则启动代理选最优（若已有后台常驻实例则直接复用）。
    返回 (site, proxy_url_or_None, proxy_manager_or_None)。"""
    site = cfg.default_site
    # 直连探测只是个快速试探，用短超时（而非完整业务超时），
    # 避免网络不可达时白白等满httpx_timeout（例如 DNS/路由异常时可能耗尽整个超时）。
    if check_direct(site, timeout=min(8, cfg.access.httpx_timeout)):
        log.info("✓ 直连可用，无需代理")
        return site, None, None

    log.info("✗ 直连不可达，启动/复用代理...")
    from .proxy_manager import ProxyManager

    pm = ProxyManager(cfg)
    try:
        best = pm.setup_and_select_best()
    except RuntimeError as e:
        # 端口耗尽（ensure_ports 里全部候选都被占用）等场景，转成干净的 CLI 报错，
        # 不让用户看到 Python traceback。
        raise click.ClickException(str(e)) from e
    if not best:
        raise click.ClickException("无可用代理节点能连通Z-Library")
    log.info("✓ 选定代理节点: %s (%dms)", best.name, best.delay_ms)
    _ensure_site_reachable(pm, site)
    return site, pm.proxy_url(), pm


def _ensure_site_reachable(pm, site: str, max_tries: int = 20) -> None:
    """确认当前出口节点真的能打到 z-library，不行就换节点。

    「节点存活」和「节点能访问 z-library」是两件事：实测出现过 31/31 个节点访问
    `gstatic.com` 全部正常（最快 68ms），但其中大部分节点到 z-library 全是
    TLS handshake timeout。所以选完最快节点后还要真的验一次，提前把好节点挑出来，
    免得后面登录/搜索/下载每一步都各自去踩一遍。
    """
    for i in range(1, max_tries + 1):
        if check_via_proxy(pm.proxy_url(), site):
            log.info("✓ 当前节点可访问 Z-Library: %s", pm.current_node())
            return
        nxt = pm.rotate_node()
        if not nxt:
            break
        log.info("节点 %d 不能访问 Z-Library，已换 -> %s", i, nxt)
    log.warning("暂未找到能访问 Z-Library 的节点，仍继续尝试（请求层还会自动换节点）")


def _make_client(cfg: Config, site: str, proxy_url, pm):
    """构造客户端。把节点轮换能力注入进去：请求遇到线路抖动或被某个后端拒绝时，
    客户端可自行换出口节点重试，无需上层介入。"""
    from .client import ZLibraryClient

    return ZLibraryClient(
        site=site, proxy_url=proxy_url, user_agent=cfg.access.user_agent,
        httpx_timeout=cfg.access.httpx_timeout, playwright_timeout=cfg.access.playwright_timeout,
        rotate_node=(pm.rotate_node if pm else None),
    )


def _load_accounts(cfg: Config):
    from .accounts import AccountStore, DEFAULT_DAILY_LIMIT

    path = project_root() / "accounts.yaml"
    store = AccountStore.load(path, limit=DEFAULT_DAILY_LIMIT)
    if not store.accounts:
        raise click.ClickException(
            "accounts.yaml 无账号，请先填写（至少一个 email/password）"
        )
    return store


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
            raise click.ClickException(
                'Z-Library 站点返回："Search service temporary unavailable!"'
                "（搜索服务临时故障，站点侧问题，与你的书名、账号、代理节点均无关）。\n"
                "这通常几分钟到几十分钟内会自行恢复，请稍后重试；无需换账号或换节点。"
            ) from None


@cli.command()
@click.argument("query")
@click.option("-p", "--page", default=1, help="页码")
def search(query: str, page: int) -> None:
    """搜索书籍。"""
    cfg = Config.load()
    site, proxy_url, pm = _ensure_access(cfg)
    store = _load_accounts(cfg)
    client = _make_client(cfg, site, proxy_url, pm)
    try:
        _login(client, store)
        results = _search_with_status(client, query, page=page)
        if not results:
            click.echo("未找到相关书籍")
            return
        _display_results(results, query)
    finally:
        client.close()
        # 注意：代理进程（mihomo）后台常驻，不在这里 stop，跨次调用复用。用 `zlib stop` 手动停止。


@cli.command()
@click.argument("query")
@click.option("--yes", "-y", is_flag=True, help="完全匹配时自动下载，无需确认")
@click.option("--limit", default=10, help="显示候选数量")
def download(query: str, yes: bool, limit: int) -> None:
    """搜索并下载书籍。完全匹配自动下载评分最高；否则交互选择。"""
    cfg = Config.load()
    site, proxy_url, pm = _ensure_access(cfg)
    store = _load_accounts(cfg)
    client = _make_client(cfg, site, proxy_url, pm)
    try:
        acc = _login(client, store)
        results = _search_with_status(client, query)
        if not results:
            click.echo("未找到相关书籍")
            return

        # 完全匹配？
        exact = [b for b in results if b.match_score(query) == 100]
        if exact:
            best_book = max(exact, key=lambda b: b.rating)
            click.echo(f"✓ 完全匹配: 《{best_book.title}》- {best_book.author} 评分 {best_book.rating}")
            if yes or click.confirm("下载这本？", default=True):
                _do_download(client, store, acc, _rank_candidates(results, query, cfg), cfg)
            return

        # 不完全匹配：列出供选择
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
        others = [b for b in _rank_candidates(results, query, cfg) if b is not book]
        _do_download(client, store, acc, [book] + others, cfg)
    finally:
        client.close()
        # 注意：代理进程（mihomo）后台常驻，不在这里 stop，跨次调用复用。用 `zlib stop` 手动停止。


def _rank_candidates(results, query: str, cfg: Config, max_candidates: int = 6) -> list:
    """把搜索结果排成「候选下载列表」。

    为什么需要候选列表而不是只挑一本：同一本书在 z-library 上往往有多条记录（不同
    上传者/版本/文件），**其中一部分记录的文件已经失效**——站点对这些记录的 `/dl/`
    直接返回 `204 No Content`。实测搜索"DK魔法百科"返回 2 条：标题完全匹配的那条
    (6.24MB) 是死记录，返回204；而标题只算近似匹配的另一条 (47.98MB) 能正常下载。
    原实现只取"完全匹配里评分最高"的那一条，正好总是命中死记录，于是这本书永远下不了
    ——这才是"有些书能下、有些书不能下"的主因（跟节点/IP/浏览器指纹都无关）。

    排序：先按标题匹配度，再按格式偏好，最后按评分。
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


def _do_download(client, store, acc, candidates, cfg: Config) -> None:
    """按候选顺序逐条尝试下载，第一条成功即返回。

    两个要点：
    1. **只在真正下载成功时才计账号额度。** 此前实现在失败分支也调用`mark_used()`
       （理由是"防死循环"），但重试本来就有上限，不需要靠烧额度兜底——一次调试就能
       白扣掉好几次额度。
    2. **某条记录被站点拒绝（204）时换下一条候选记录，而不是换账号。** 这类拒绝是
       "这个文件没了"，跟账号无关，换账号只是白烧额度。
    """
    if not isinstance(candidates, list):
        candidates = [candidates]
    errors: list[str] = []
    for idx, book in enumerate(candidates, 1):
        if idx > 1:
            click.echo(f"→ 换下一个候选版本重试: 《{book.title[:40]}》"
                       f"（{(book.format or '?').upper()} {book.size}）")
        try:
            path = client.download(book, cfg.download_dir_abs(), cfg.format_preference)
        except SiteRejected as e:
            log.warning("候选 %d 被站点拒绝（该记录的文件已失效）: %s", idx, e)
            errors.append(f"候选{idx}《{book.title[:24]}》: {e}")
            continue
        except Exception as e:  # noqa: BLE001
            log.error("候选 %d 下载失败: %s", idx, e)
            errors.append(f"候选{idx}《{book.title[:24]}》: {e}")
            continue
        store.mark_used(acc)
        click.echo(f"✓ 下载完成: {path}")
        return

    detail = "\n  ".join(errors)
    raise click.ClickException(
        f"已尝试全部 {len(candidates)} 个候选版本，均未成功:\n  {detail}\n"
        "客户端已自动解过站点浏览器校验、并轮换过出口节点与后端。若全部候选都是 204，"
        "说明这些记录的文件在站点侧已失效/下架，换账号或换节点都无效。"
    )


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
        mark = " [green]★完全匹配[/]" if score == 100 else (" [yellow]~近似[/]" if score >= 50 else "")
        table.add_row(
            str(i), b.title[:40] + mark, b.author[:20], b.year,
            (b.format or "").upper(), b.size, f"{b.rating:.1f}" if b.rating else "-",
        )
    console.print(table)


def main() -> None:
    cli()


if __name__ == "__main__":
    main()
