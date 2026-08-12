"""Z-Library 客户端。

策略：httpx 优先，playwright 回退。
- httpx + 代理 + session cookie 做登录/搜索/下载
- 检测到 Cloudflare 挑战或登录失败 → 自动起 playwright（同样走代理）
- 搜索结果包含全部下载所需字段（book id + hash），用户选定后直接构造下载，不二次搜索

Z-Library 页面结构（2024-2025 .sk 域名）：
- 登录: GET /login 取表单 → POST /login (email, password, 可能有 csrf)
- 搜索: GET /s/{query}?page=N → HTML 书籍卡片
- 书籍卡片链接: /book/{id}/{hash}/{slug}
- 详情页: GET /book/{id}/{hash}/{slug} → 解析下载按钮 /dl/{id}/{hash}/...
- 下载: GET /dl/{id}/{hash} → 重定向到真实文件
- 个人资料: GET /profile → 解析剩余下载次数
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, urljoin

import httpx
from bs4 import BeautifulSoup

from . import challenge

log = logging.getLogger(__name__)

CF_MARKERS = ("just a moment", "cf-challenge", "challenge-platform", "cf_chl_opt", "cf-ray", "_cf_chl")
LOGIN_FAIL_MARKERS = ("incorrect", "invalid", "captcha", "verify you are human", "错误的电子邮箱", "密码错误", "验证码")
# 匿名（未登录）下载超出出口IP每日限额时，站点返回的错误页面标记（CSS 类名，
# 不受站点语言设置影响，比匹配"每日限额已用完"这类会被翻译的文案更稳）。
IP_QUOTA_MARKER = "download-limits-error"

# 每个请求最多解几次挑战。站点偶尔会连着发两次（换后端时各发一次），给点余量。
MAX_CHALLENGE_ROUNDS = 3
# 传输层报错（节点抖动）时最多重试几次（含"原地重试"和"真的换节点"）。节点整体
# 不稳时需要多试几个。
MAX_TRANSPORT_RETRIES = 12
# 出口节点抖动容忍：遇到传输层错误不要一次失败就真的换节点——大概率只是线路
# 瞬时抖动（同一节点几秒内可能从超时恢复正常），原地重试很可能就恢复了。只有
# 连续失败次数达到这个阈值才真的调用 rotate_node() 切换节点。体感上类似手动
# 用 VPN：找到一个能用的节点后应该尽量长时间沿用，不该稍微抖动就换（换节点还要
# 额外测速/校验，比原地重试开销更大）。
SAME_NODE_RETRIES = 2       # 换节点前，先在同一节点原地重试这么多次
SAME_NODE_RETRY_DELAY = 1.5  # 原地重试的间隔（秒）


@dataclass
class BookResult:
    """搜索结果中的单本书。包含下载所需的全部字段。"""
    title: str
    author: str
    year: str
    language: str
    format: str
    size: str
    rating: float
    book_id: str
    hash: str
    detail_url: str
    download_url: str = ""  # 搜索结果里直接带的下载链接（z-bookcard[download]），有则无需再访问详情页
    cover_url: str = ""
    raw_html: str = ""  # 原始卡片 HTML，调试用

    def match_score(self, query: str) -> int:
        """完全匹配=100；前缀匹配（一方是另一方的前缀）=90；包含（子串出现在
        任意位置）=50；否则 0。

        单独分出"前缀匹配"档是因为 z-library 同一本书常见"用户输入的书名 +
        一堆后缀"的记录（如查询"dk魔法百科"，真实标题是"DK魔法百科（魔法、
        巫術與神祕史…）A History of Magic…"）——**标题以查询词开头**，这种匹配
        比"标题中间随便包含关键词"的弱匹配（如作者名、丛书名里恰好带了关键词）
        可信得多，值得单独一档，让它在候选排序/自动确认时优先于纯子串匹配。

        注意方向性：只认"标题以查询词开头"（`title.startswith(query)`），不认
        反过来"查询词以标题开头"——后者（比如查询"三体2黑暗森林"、标题只是
        "三体"）是标题比查询词还短、纯粹因为查询词凑巧带了这个短标题作前缀，
        可信度和"标题中间包含关键词"差不多，不该被拔高到跟前缀匹配一样可信，
        否则可能让一本明显不是用户想要的书被自动选中下载。
        """
        def norm(s: str) -> str:
            return re.sub(r"[^\w\s]", "", s.lower()).strip()
        nq = norm(query)
        nt = norm(self.title)
        if not nq or not nt:
            return 0
        if nt == nq:
            return 100
        if nt.startswith(nq):
            return 90
        if nq in nt or nt in nq:
            return 50
        return 0


@dataclass
class LoginResult:
    ok: bool
    remaining: int | None = None
    error: str = ""
    method: str = ""  # httpx | playwright


@dataclass
class RegistrationSession:
    action: str
    fields: dict[str, str]
    code_field: str


@dataclass
class RegistrationResult:
    ok: bool
    error: str = ""


class CloudflareError(Exception):
    """检测到无法自动通过的拦截。"""


class _InvalidDownload(Exception):
    """下载内容无效（空/ 挑战页），可通过换后端/节点重试。"""


class SiteRejected(Exception):
    """站点应用层明确拒绝该资源（如 /dl/ 返回 204），换节点/后端可能有救。"""


class IpQuotaExceeded(Exception):
    """未登录（匿名）状态下，当前出口 IP 的每日匿名下载额度已用完。

    实测：匿名（无任何 cookie）直接 GET `/dl/{code}` 时，若该出口 IP 当天匿名下载
    次数已超限，站点返回 **HTTP 200 + 完整 HTML 页面**（不是 204、不是 503挑战页），
    页面主体是`<section class="download-limits-error">`，标题"每日限额已用完"，
    正文形如"在过去的24小时内，从您的IP 下载的次数超过 {IP}。请登录您的帐户或完成
    简单注册以下载更多书籍"。这跟"这条记录文件失效"(`SiteRejected`/204) 是完全不同的
    限制维度：**这是按出口 IP 算的匿名限额，跟这本书/这条记录本身无关**，换一本书、
    换一条候选记录都没用，只有"换一个还有匿名额度的出口 IP"或"登录账号"才能绕开。
    用 CSS 类名 `download-limits-error` 作检测标记（比匹配中文/英文文案更稳，
    不受站点语言设置影响）。
    """
    def __init__(self, ip: str | None = None) -> None:
        self.ip = ip
        super().__init__(f"当前出口IP（{ip or '未知'}）匿名下载额度已用完，需登录账号或换一个出口IP")


class SearchServiceUnavailable(Exception):
    """站点搜索服务本身临时故障（非本工具、非代理、非某本书的问题）。

    实测站点在这种状态下对任何关键词都返回 `HTTP 200` + 完整页面框架，但结果区域
    是一句 `<div class="cBox1">Search service temporary unavailable!</div>`，
    没有任何 `z-bookcard`。这跟"真的搜不到这本书"（同样是0 结果）从表现上完全无法
    区分，必须专门检测这段文案，否则会被误报成"未找到相关书籍"，让人误以为是书不存在。
    换playwright 重试也没用——这是站点后端的问题，不是反爬拦截，浏览器一样会看到
    这句话，纯粹多等 60~90 秒再失败一次。
    """


_SEARCH_UNAVAILABLE_MARKER = "search service temporary unavailable"


class ZLibraryClient:
    def __init__(self, site: str, proxy_url: str | None, user_agent: str,
                 httpx_timeout: int = 30, playwright_timeout: int = 60,
                 rotate_node: Callable[[], str | None] | None = None) -> None:
        self.site = site.rstrip("/")
        self.proxy_url = proxy_url
        self.user_agent = user_agent
        self.httpx_timeout = httpx_timeout
        self.playwright_timeout = playwright_timeout
        # 出口节点轮换回调（由 CLI 注入 ProxyManager.rotate_node）。节点抖动或站点
        # 按出口 IP 路由到了"拒绝该资源"的后端时，用它换个出口再试。
        self.rotate_node = rotate_node
        self._client: httpx.Client | None = None
        self._logged_in = False
        self._email: str = ""

    # ---------- httpx ----------

    def _headers(self) -> dict[str, str]:
        """尽量对齐真实 Chrome 的请求头集合与取值。

        原先只发UA/Accept/Accept-Language 三个头，而 UA 自称 Chrome ——缺少
        `sec-ch-ua*`/`sec-fetch-*` 这类 Chrome 必发的头，属于明显的自动化特征。
        补齐这些头（并启用 HTTP/2，真实 Chrome 不会用 HTTP/1.1 访问 h2 站点）
        可以降低被判定为机器人的概率。
        """
        return {
            "User-Agent": self.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
                      "image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Accept-Encoding": "gzip, deflate, br",
            "sec-ch-ua": '"Chromium";v="126", "Google Chrome";v="126", "Not-A.Brand";v="99"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Linux"',
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-User": "?1",
            "Sec-Fetch-Dest": "document",
            "Upgrade-Insecure-Requests": "1",
        }

    def _http(self) -> httpx.Client:
        if self._client is None:
            kwargs: dict[str, Any] = {
                # 分开设连接/读取超时：建连必须快速失败才能及时换节点（否则一个挂掉的
                # 节点要等满整个业务超时，30s×多节点会拖到几分钟）；而读取超时要留足，
                # 因为下载几十MB 的书本身就慢。
                "timeout": httpx.Timeout(self.httpx_timeout, connect=8),
                "follow_redirects": True,
                "verify": False,
                "http2": True,
                "headers": self._headers(),
            }
            if self.proxy_url:
                kwargs["proxy"] = self.proxy_url
            self._client = httpx.Client(**kwargs)
        return self._client

    def _close_http(self) -> None:
        if self._client:
            self._client.close()
            self._client = None

    @staticmethod
    def _is_cf(response: httpx.Response) -> bool:
        """是否是「我们无法自动通过」的拦截。

        注意：z-library 自己的 PoW 挑战页已由 `_request` 自动解掉，走到这里的挑战页
        说明连解题都没成功，才算真拦截。
        """
        ct = response.headers.get("content-type", "")
        if "text/html" not in ct:
            return False
        if challenge.looks_like_challenge(response.text):
            return True
        if response.status_code in (403, 503):
            return True
        low = response.text[:4000].lower()
        return any(m in low for m in CF_MARKERS)

    def _rotate(self, reason: str) -> bool:
        """换一个出口节点。返回是否换成功。"""
        if not self.rotate_node:
            return False
        try:
            node = self.rotate_node()
        except Exception as e:  # noqa: BLE001
            log.warning("切换出口节点失败: %s", e)
            return False
        if node:
            log.info("因「%s」切换出口节点 -> %s", reason, node)
            return True
        return False

    def _handle_transport_error(self, transport_tries: int, reason: str) -> bool:
        """传输层报错后，决定「原地重试」还是「真的换节点」，返回是否应继续重试。

        先在同一节点原地重试 `SAME_NODE_RETRIES` 次（大概率是线路瞬时抖动，很快能
        恢复），仍然失败才真的调用 `_rotate()` 换一个出口节点。`transport_tries`
        从 1 开始计数。
        """
        if transport_tries % (SAME_NODE_RETRIES + 1) != 0:
            time.sleep(SAME_NODE_RETRY_DELAY)
            return True
        return self._rotate(reason)

    def _reset_backend_affinity(self) -> None:
        """丢掉 `bsrv` cookie。

        z-library 用 `bsrv` 做后端粘性路由（响应头 `x-zbackend` 可见 v2-01/02/03）。
        实测同一本书在不同后端上的表现不同（有的后端直接 204 拒绝），所以被拒时
        清掉这个 cookie，让前置代理重新分配后端。
        """
        if self._client is None:
            return
        try:
            self._client.cookies.delete("bsrv")
        except Exception:  # noqa: BLE001
            # httpx 的 delete 在domain 不匹配时会抛，退化成整体重建jar
            remaining = [(c.name, c.value, c.domain, c.path)
                         for c in self._client.cookies.jar if c.name != "bsrv"]
            self._client.cookies.clear()
            for name, value, domain, path in remaining:
                self._client.cookies.set(name, value, domain=domain, path=path or "/")

    def _request(self, method: str, url: str, *, params: dict | None = None,
                 data: dict | None = None, files: dict | None = None,
                 headers: dict | None = None,
                 allow_challenge_fail: bool = False) -> httpx.Response:
        """统一请求入口：自动解 PoW 挑战 + 传输层报错自动重试/换节点。

        这是本工具能"所有书都下得下来"的关键：站点在缺少有效 `c_token` 时对**任何**
        路径（首页/搜索/详情页/下载端点）都会返回 503 挑战页，解掉即可继续，不需要浏览器。
        """
        client = self._http()
        transport_tries = 0
        challenge_rounds = 0
        while True:
            try:
                r = client.request(method, url, params=params, data=data, files=files, headers=headers)
            except (httpx.TransportError, httpx.RemoteProtocolError) as e:
                transport_tries += 1
                if transport_tries > MAX_TRANSPORT_RETRIES or not self._handle_transport_error(
                    transport_tries, type(e).__name__
                ):
                    raise
                continue

            if not challenge.looks_like_challenge(_peek_text(r)):
                return r

            # 命中挑战页：解PoW，写 cookie，重放同一请求
            challenge_rounds += 1
            if challenge_rounds > MAX_CHALLENGE_ROUNDS:
                if allow_challenge_fail:
                    return r
                raise CloudflareError(f"连续 {challenge_rounds} 次挑战仍未通过: {url}")
            solved = challenge.solve_html(r.text)
            if not solved:
                if allow_challenge_fail:
                    return r
                raise CloudflareError(f"浏览器校验挑战无法求解（站点可能改版）: {url}")
            token, elapsed = solved
            client.cookies.set("c_token", token, domain=_cookie_domain(self.site), path="/")
            client.cookies.set("c_time", f"{elapsed:.3f}", domain=_cookie_domain(self.site), path="/")
            log.info("已通过浏览器校验，重放请求: %s", url)

    def _get(self, path: str, *, params: dict | None = None, allow_cf: bool = False) -> httpx.Response:
        url = path if path.startswith("http") else urljoin(self.site + "/", path.lstrip("/"))
        r = self._request("GET", url, params=params, allow_challenge_fail=allow_cf)
        if not allow_cf and self._is_cf(r):
            raise CloudflareError(f"访问被拦截: {url} -> {r.status_code}")
        return r

    def _post(self, path: str, *, data: dict, allow_cf: bool = False) -> httpx.Response:
        url = path if path.startswith("http") else urljoin(self.site + "/", path.lstrip("/"))
        r = self._request("POST", url, data=data, allow_challenge_fail=allow_cf)
        if not allow_cf and self._is_cf(r):
            raise CloudflareError(f"访问被拦截: {url} -> {r.status_code}")
        return r

    def _post_multipart(self, path: str, *, data: dict, allow_cf: bool = False) -> httpx.Response:
        url = path if path.startswith("http") else urljoin(self.site + "/", path.lstrip("/"))
        files = {key: (None, str(value)) for key, value in data.items()}
        r = self._request("POST", url, files=files, allow_challenge_fail=allow_cf)
        if not allow_cf and self._is_cf(r):
            raise CloudflareError(f"访问被拦截: {url} -> {r.status_code}")
        return r

    # ---------- 注册 ----------

    @staticmethod
    def _form_data(form) -> dict[str, str]:
        data: dict[str, str] = {}
        for inp in form.select("input[type='hidden']"):
            name = inp.get("name")
            if name:
                data[name] = inp.get("value", "")
        return data

    @staticmethod
    def _form_action(form, fallback: str) -> str:
        action = form.get("action") or fallback
        return action if action.startswith(("/", "http://", "https://")) else f"/{action}"

    @staticmethod
    def _registration_blocked(text: str) -> bool:
        low = text[:12000].lower()
        return any(marker in low for marker in (
            "recaptcha", "hcaptcha", "captcha", "verify you are human", "人机验证",
        ))

    @classmethod
    def _find_code_form(cls, soup):
        for form in soup.find_all("form"):
            for inp in form.find_all("input"):
                name = (inp.get("name") or "").lower()
                if any(marker in name for marker in ("code", "confirm", "verification", "verify")):
                    return form, inp.get("name")
        return None, None

    def begin_registration(self, email: str, password: str) -> RegistrationSession:
        """触发站点发送邮箱验证码，返回后续确认表单所需字段。"""
        try:
            page = self._get("/registration")
        except CloudflareError:
            raise
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"无法访问注册页: {e}") from e
        soup = BeautifulSoup(page.text, "html.parser")
        form = soup.find("form")
        if not form:
            raise RuntimeError("注册页没有找到注册表单，网站页面可能已变更")
        fields = self._form_data(form)
        fields["email"] = email
        fields["password"] = password
        # 官网脚本会把 jsRXValue 从 0 改成页面中的动态值；验证码接口
        # 会用它判断请求是否来自已执行页面脚本的正常注册流程。
        rx_match = re.search(
            r"getElementById\(['\"]jsRXValue['\"]\)\.value\s*=\s*([0-9]+)",
            page.text,
        )
        if rx_match:
            fields["rx"] = rx_match.group(1)
        if form.find("input", attrs={"name": "password_confirmation"}):
            fields["password_confirmation"] = password
        elif form.find("input", attrs={"name": "password_confirm"}):
            fields["password_confirm"] = password
        name_input = form.find("input", attrs={"name": "name"})
        if name_input is not None:
            fields["name"] = email.split("@", 1)[0]
        try:
            # 官网通过 JS FormData 调用此 JSON 接口发送验证码；普通
            # application/x-www-form-urlencoded 会被站点判定为未启用 JS。
            response = self._post_multipart("/papi/user/verification/send-code", data=fields)
        except CloudflareError:
            raise
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"注册验证码请求失败: {e}") from e
        if self._registration_blocked(response.text):
            raise RuntimeError("注册流程要求 CAPTCHA 或人工人机验证，已停止，不自动绕过")
        try:
            result = response.json()
        except ValueError as e:
            raise RuntimeError("注册验证码接口返回了无法解析的响应") from e
        if not result.get("success"):
            raise RuntimeError(str(result.get("error") or "站点未接受注册验证码请求"))
        return RegistrationSession(
            action="/registration",
            fields=fields,
            code_field="verifyCode",
        )

    def finish_registration(self, session: RegistrationSession, code: str) -> RegistrationResult:
        """提交邮箱验证码，返回站点是否接受该验证码。"""
        fields = dict(session.fields)
        fields[session.code_field] = code
        try:
            response = self._post(session.action, data=fields)
        except CloudflareError:
            raise
        except Exception as e:  # noqa: BLE001
            return RegistrationResult(ok=False, error=f"验证码提交失败: {e}")
        if self._registration_blocked(response.text):
            return RegistrationResult(ok=False, error="验证码确认流程要求 CAPTCHA 或人工验证，已停止")
        low = response.text[:12000].lower()
        if any(marker in low for marker in ("验证码错误", "确认码错误", "invalid code", "expired code", "code expired")):
            return RegistrationResult(ok=False, error="邮箱验证码无效或已过期")
        # 站点接受验证码后有时仍返回原注册表单（表单为空、没有错误），
        # 不能把这种响应误判为失败；后续 login() 会做最终权威验证。
        error = BeautifulSoup(response.text, "html.parser").select_one(
            ".validation-error, .form-error, .alert-danger"
        )
        if error and error.get_text(" ", strip=True):
            return RegistrationResult(ok=False, error="验证码未被接受")
        return RegistrationResult(ok=True)

    # ---------- 登录 ----------

    def login(self, email: str, password: str) -> LoginResult:
        """登录，httpx 优先，失败/CF 回退 playwright。"""
        self._email = email
        try:
            res = self._login_httpx(email, password)
            if res.ok:
                res.method = "httpx"
                return res
            if "captcha" in res.error.lower() or "cf" in res.error.lower():
                raise CloudflareError(res.error)
            # 其它登录失败（密码错误等），不再回退
            log.warning("httpx 登录失败: %s", res.error)
            return res
        except CloudflareError as e:
            log.warning("httpx 遇 Cloudflare，回退 playwright: %s", e)
            res = self._login_playwright(email, password)
            res.method = "playwright"
            return res

    def _login_httpx(self, email: str, password: str) -> LoginResult:
        # 取登录页拿 csrf
        try:
            r = self._get("/login")
        except CloudflareError as e:
            raise
        except Exception as e:  # noqa: BLE001
            return LoginResult(ok=False, error=f"无法访问登录页: {e}")
        soup = BeautifulSoup(r.text, "html.parser")
        form = soup.select_one("form[action*='login']") or soup.select_one("form")
        data: dict[str, str] = {"email": email, "password": password}
        if form:
            for inp in form.select("input[type='hidden']"):
                name = inp.get("name")
                if name:
                    data[name] = inp.get("value", "")
            action = form.get("action") or "/login"
            if not action.startswith("/"):
                action = "/" + action
        else:
            action = "/login"
        # 提交
        try:
            r2 = self._post(action, data=data)
        except CloudflareError:
            raise
        except Exception as e:  # noqa: BLE001
            return LoginResult(ok=False, error=f"登录请求失败: {e}")
        low = r2.text[:4000].lower()
        if any(m in low for m in LOGIN_FAIL_MARKERS):
            return LoginResult(ok=False, error="登录被拒（凭据错误或需验证码）")
        # 登录成功判定：未跳回登录页 + 有登出/账户链接
        if "/login" in r2.url.path and "logout" not in low and "profile" not in low:
            # 可能仍停在登录页
            return LoginResult(ok=False, error="登录后仍停在登录页")
        self._logged_in = True
        remaining = self._parse_remaining(r2.text)
        if remaining is None:
            remaining = self._fetch_remaining()
        return LoginResult(ok=True, remaining=remaining)

    def _login_playwright(self, email: str, password: str) -> LoginResult:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            return LoginResult(ok=False, error="playwright 未安装，无法回退；请 pip install playwright && playwright install chromium")
        proxy_arg = {"server": self.proxy_url} if self.proxy_url else None
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, proxy=proxy_arg, args=["--disable-blink-features=AutomationControlled"])
            ctx = browser.new_context(user_agent=self.user_agent, ignore_https_errors=True)
            cookies = self._cookies_for_playwright()
            if cookies:
                ctx.add_cookies(cookies)
            page = ctx.new_page()
            page.set_default_timeout(self.playwright_timeout * 1000)
            try:
                page.goto(f"{self.site}/login", wait_until="domcontentloaded")
                # 等 CF 挑战通过
                self._wait_cf(page)
                # 填表
                page.fill("input[name='email']", email)
                page.fill("input[name='password']", password)
                # 提交
                page.click("button[type='submit'], input[type='submit'], button:has-text('Log in'), button:has-text('登录')")
                page.wait_for_load_state("domcontentloaded")
                self._wait_cf(page)
                # 提交后有时是 JS 延迟跳转（cookie 写入和页面跳转不同步），
                # 等 URL 离开 /login 再判断，避免过早读到仍是登录页的内容。
                for _ in range(10):
                    if "/login" not in page.url:
                        break
                    page.wait_for_timeout(1000)
                try:
                    page.wait_for_load_state("networkidle", timeout=8000)
                except Exception:  # noqa: BLE001
                    pass
                low = page.content().lower()
                if any(m in low for m in LOGIN_FAIL_MARKERS):
                    return LoginResult(ok=False, error="登录被拒（凭据错误或需验证码）")
                remaining = self._fetch_remaining_playwright(page)
                self._logged_in = remaining is not None
                if self._logged_in:
                    # 把登录后的 cookie（含session）写回 httpx，
                    # 让后续 search/download 无需重复走 playwright 也能保持登录态。
                    self._sync_playwright_cookies(ctx.cookies())
                    return LoginResult(ok=True, remaining=remaining)
                return LoginResult(ok=False, error="playwright 登录后未能确认")
            except Exception as e:  # noqa: BLE001
                return LoginResult(ok=False, error=f"playwright 错误: {e}")
            finally:
                browser.close()

    def _wait_cf(self, page) -> None:
        """处理挑战页。

        原实现是"干等浏览器自己算完"，实测在无头环境下 PoW 循环可能被节流到几乎不推进
        （页面 JS 每 5万次迭代 `await setTimeout`），等90 秒都停在
        "Checking your browser..."。既然挑战本身是纯 SHA1 PoW，这里改为**我们自己算**，
        算完把 `c_token` 写进浏览器 context 再 reload，秒过且稳定。
        """
        for _ in range(max(3, self.playwright_timeout // 5)):
            try:
                html = page.content()
            except Exception:  # noqa: BLE001
                time.sleep(1)  # 页面正在导航
                continue
            if not challenge.looks_like_challenge(html):
                return
            solved = challenge.solve_html(html)
            if not solved:
                time.sleep(1)
                continue
            token, elapsed = solved
            dom = _cookie_domain(self.site)
            try:
                page.context.add_cookies([
                    {"name": "c_token", "value": token, "domain": dom, "path": "/"},
                    {"name": "c_time", "value": f"{elapsed:.3f}", "domain": dom, "path": "/"},
                ])
                # 同步回 httpx，后续请求也免挑战
                self._http().cookies.set("c_token", token, domain=dom, path="/")
                self._http().cookies.set("c_time", f"{elapsed:.3f}", domain=dom, path="/")
                page.reload(wait_until="domcontentloaded")
            except Exception as e:  # noqa: BLE001
                log.debug("注入 c_token 后 reload 失败: %s", e)
                time.sleep(1)

    # ---------- 剩余次数 ----------

    def _parse_remaining(self, html: str) -> int | None:
        """从页面文本解析剩余下载次数。匹配多种表述。

        z-library 页面里 "0/10" 和 "每日限额" 之间隔着标签（如
        `0/10</div><i></i><span>每日限额</span>`），直接对原始 HTML 正则匹配不到，
        因此先用 BeautifulSoup 提取纯文本再匹配。
        """
        try:
            text = BeautifulSoup(html, "html.parser").get_text(" ")
        except Exception:  # noqa: BLE001
            text = html
        low = text.lower()
        # 匹配 "downloads today: 3 / 10" / "今日下载: 3/10" / "remaining: 7"
        patterns = [
            r"downloads?\s*(?:today|left|remaining)[:\s]*(\d+)\s*[/\\]\s*(\d+)",
            r"(\d+)\s*[/\\]\s*(\d+)\s*(?:downloads|剩余|每日)",
            r"remaining[:\s]*(\d+)",
            r"剩余[^0-9]*(\d+)",
            r"今日下载[^0-9]*(\d+)\s*[/\\]\s*(\d+)",
        ]
        for pat in patterns:
            m = re.search(pat, low)
            if m:
                if len(m.groups()) == 2:
                    used, total = int(m.group(1)), int(m.group(2))
                    return max(0, total - used)
                return int(m.group(1))
        return None

    def _fetch_remaining(self) -> int | None:
        try:
            r = self._get("/profile")
            return self._parse_remaining(r.text)
        except Exception as e:  # noqa: BLE001
            log.debug("读取剩余次数失败: %s", e)
            return None

    def _fetch_remaining_playwright(self, page) -> int | None:
        try:
            page.goto(f"{self.site}/profile", wait_until="domcontentloaded")
            self._wait_cf(page)
            return self._parse_remaining(page.content())
        except Exception:  # noqa: BLE001
            return None

    # ---------- 搜索 ----------

    def search(self, query: str, page: int = 1) -> list[BookResult]:
        """搜索，返回结果列表（含下载所需 id+hash）。"""
        try:
            results = self._search_httpx(query, page)
            if results:
                return results
            log.info("httpx 搜索无结果，尝试 playwright")
        except SearchServiceUnavailable:
            # 站点搜索服务本身故障，playwright 会看到同一句提示，白等一轮没有意义，直接抛出
            raise
        except CloudflareError as e:
            log.warning("搜索遇 Cloudflare，回退 playwright: %s", e)
        except Exception as e:  # noqa: BLE001
            log.warning("httpx 搜索失败，回退 playwright: %s", e)
        return self._search_playwright(query, page)

    def _search_httpx(self, query: str, page: int) -> list[BookResult]:
        q = quote(query)
        path = f"/s/{q}"
        r = self._get(path, params={"page": page})
        if self._is_cf(r):
            raise CloudflareError(f"搜索结果页被 CF 拦截: {path}")
        return self._parse_search(r.text)

    def _parse_search(self, html: str) -> list[BookResult]:
        soup = BeautifulSoup(html, "html.parser")
        out: list[BookResult] = []
        # z-library 现用自定义元素 <z-bookcard> 承载搜索结果，下载链接直接在 download 属性里
        for card in soup.select("z-bookcard"):
            book = self._parse_bookcard(card)
            if book:
                out.append(book)
        if not out and _SEARCH_UNAVAILABLE_MARKER in html.lower():
            raise SearchServiceUnavailable(
                '站点返回 "Search service temporary unavailable!"（搜索服务临时故障）')
        if not out:
            # 兜底：旧版结构（data-book_id / .bookRow 等）
            cards = (
                soup.select("[data-book_id]")
                or soup.select(".bookRow")
                or soup.select(".resItemBox")
                or soup.select(".book-card")
                or soup.select("article")
                or soup.select(".search-result-item")
            )
            for card in cards:
                book = self._parse_card(card)
                if book and book.book_id and book.hash:
                    out.append(book)
        # 再兜底：从所有链接里提取 /book/{shortcode}/{slug}
        if not out:
            for a in soup.select("a[href*='/book/']"):
                href = a.get("href", "")
                m = re.search(r"/book/([\w-]+)/", href)
                if m:
                    out.append(BookResult(
                        title=a.get_text(strip=True) or href,
                        author="", year="", language="", format="",
                        size="", rating=0.0, book_id="", hash=m.group(1),
                        detail_url=href if href.startswith("http") else urljoin(self.site + "/", href),
                    ))
        log.info("解析出 %d 本书", len(out))
        return out

    def _parse_bookcard(self, card) -> BookResult | None:
        """解析 <z-bookcard> 元素。"""
        if card.get("deleted") == "1":
            return None
        bid = card.get("id") or ""
        href = card.get("href") or ""
        dl = card.get("download") or ""
        if not href:
            return None
        title_el = card.select_one("div[slot='title']")
        author_el = card.select_one("div[slot='author']")
        title = title_el.get_text(strip=True) if title_el else ""
        author = author_el.get_text(strip=True) if author_el else ""
        detail_url = href if href.startswith("http") else urljoin(self.site + "/", href)
        dl_url = (dl if dl.startswith("http") else urljoin(self.site + "/", dl)) if dl else ""
        cover = ""
        img = card.select_one("img")
        if img:
            src = img.get("data-src") or img.get("src") or ""
            if src and not src.startswith("data:"):
                cover = src
        return BookResult(
            title=title or bid, author=author,
            year=card.get("year", ""), language=card.get("language", ""),
            format=card.get("extension", ""), size=card.get("filesize", ""),
            rating=_to_float(card.get("rating")),
            book_id=bid, hash=card.get("termshash", ""),
            detail_url=detail_url, download_url=dl_url, cover_url=cover,
        )

    def _parse_card(self, card) -> BookResult | None:
        # book id + hash 从链接 /book/{id}/{hash}
        href = ""
        for a in card.select("a[href*='/book/']"):
            href = a.get("href", "")
            break
        if not href:
            bid = card.get("data-book_id") or card.get("data-book-id")
            h = card.get("data-hash") or ""
            if bid and h:
                href = f"/book/{bid}/{h}"
        if not href:
            return None
        m = re.search(r"/book/(\d+)/([a-f0-9]+)", href)
        if not m:
            return None
        bid, h = m.group(1), m.group(2)
        # title
        title = ""
        for sel in ["h3 a", "h3", ".bookTitle", ".title", "a[href*='/book/']"]:
            el = card.select_one(sel)
            if el:
                title = el.get_text(strip=True)
                if title:
                    break
        # author
        author = ""
        for sel in [".author", ".bookAuthor", ".authors", "[itemprop='author']"]:
            el = card.select_one(sel)
            if el:
                author = el.get_text(strip=True)
                if author:
                    break
        # year / language / format / size 从文本块提取
        text = card.get_text(" ", strip=True)
        year = _find(r"(19|20)\d{2}", text, "")
        lang = _find(r"(?:language|语言)[:\s]*([a-zA-Z\u4e00-\u9fa5]+)", text.lower(), "")
        fmt = _find(r"\b(epub|pdf|mobi|azw3|djvu|txt|fb2)\b", text.lower(), "")
        size = _find(r"(\d+(?:\.\d+)?\s*(?:kb|mb|gb))", text.lower(), "")
        rating = _parse_rating(text, card)
        detail_url = href if href.startswith("http") else urljoin(self.site + "/", href)
        cover = ""
        img = card.select_one("img")
        if img:
            src = img.get("data-src") or img.get("src") or ""
            if src and not src.startswith("data:"):
                cover = src if src.startswith("http") else urljoin(self.site + "/", src)
        return BookResult(
            title=title or bid, author=author, year=year, language=lang,
            format=fmt, size=size, rating=rating, book_id=bid, hash=h,
            detail_url=detail_url, cover_url=cover,
        )

    def _search_playwright(self, query: str, page: int) -> list[BookResult]:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            log.error("playwright 未安装，无法回退搜索")
            return []
        proxy_arg = {"server": self.proxy_url} if self.proxy_url else None
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, proxy=proxy_arg, args=["--disable-blink-features=AutomationControlled"])
            ctx = browser.new_context(user_agent=self.user_agent, ignore_https_errors=True)
            cookies = self._cookies_for_playwright()
            if cookies:
                ctx.add_cookies(cookies)
            pg = ctx.new_page()
            pg.set_default_timeout(self.playwright_timeout * 1000)
            try:
                pg.goto(f"{self.site}/s/{quote(query)}?page={page}", wait_until="domcontentloaded")
                self._wait_cf(pg)
                # 等结果渲染
                for sel in ["z-bookcard", "[data-book_id]", ".resItemBox", ".bookRow", "a[href*='/book/']"]:
                    try:
                        pg.wait_for_selector(sel, timeout=10000)
                        break
                    except Exception:  # noqa: BLE001
                        continue
                html = pg.content()
                return self._parse_search(html)
            except Exception as e:  # noqa: BLE001
                log.error("playwright 搜索失败: %s", e)
                return []
            finally:
                browser.close()

    # ---------- 下载 ----------

    def get_download_url(self, book: BookResult) -> tuple[str, str]:
        """返回 (下载链接, referer_url)。

        实测发现：直接拿搜索结果里嵌的下载直链（book.download_url）跳过详情页直接访问，
        个别书会触发更严格的反爬校验（表现为 /dl/ 直接返回验证码页或被拒）；而像真实用户
        一样先打开书籍详情页、再从页面里取真正的下载链接（带上详情页作为 Referer），更贴近
        正常浏览路径。所以这里优先走"访问详情页解析下载链接"，只有详情页访问失败时才
        回退用book.download_url（无 Referer，跟之前行为一致）。
        """
        try:
            r = self._get(book.detail_url)
            if self._is_cf(r):
                raise CloudflareError("详情页被 CF 拦截")
            url = self._parse_dl_link(r.text, book)
            if url:
                return url, book.detail_url
        except CloudflareError:
            url = self._get_dl_playwright(book)
            if url:
                return url, book.detail_url
        except Exception as e:  # noqa: BLE001
            log.warning("访问详情页解析下载链接失败: %s", e)

        if book.download_url:
            log.info("详情页解析未成功，回退用搜索结果里的下载直链（无 Referer）")
            return book.download_url, self.site + "/"
        return urljoin(self.site + "/", f"/dl/{book.book_id}/{book.hash}"), self.site + "/"

    def _parse_dl_link(self, html: str, book: BookResult) -> str | None:
        """从书籍详情页解析**本书自己**的下载链接。

        注意选择器顺序：详情页除了本书，还会渲染"相关推荐/最受欢迎"等其它书的卡片，
        实测一个详情页里能出现 3 个不同的 `/dl/{code}`。原实现用
        `soup.select_one("a[href*='/dl/']")` 取文档里第一个 `/dl/` 链接，很可能取到
        **别的书**的下载链接（下载到不相干的文件，或该书恰好失效而误判本书不可下）。
        所以这里先用详情页下载按钮专属的类名精确定位，泛化选择器只作最后兜底。
        """
        soup = BeautifulSoup(html, "html.parser")
        # 详情页主下载按钮：z-library 用 .addDownloadedBook / a.dlButton 承载
        for sel in ["a.addDownloadedBook[href*='/dl/']", "a.dlButton[href*='/dl/']",
                ".book-details-button a[href*='/dl/']",
                    "a.download-link", "a.btn-download"]:
            el = soup.select_one(sel)
            if el and el.get("href"):
                href = el["href"]
                return href if href.startswith("http") else urljoin(self.site + "/", href)
        # 兜底：页面里任意 /dl/ 链接（可能不是本书，仅在上面全都没命中时使用）
        el = soup.select_one("a[href*='/dl/']")
        if el and el.get("href"):
            href = el["href"]
            log.debug("详情页未找到专属下载按钮，退而使用页面首个 /dl/ 链接: %s", href)
            return href if href.startswith("http") else urljoin(self.site + "/", href)
        # 正则兜底：直接从 HTML 里找 /dl/{code} 短链
        m = re.search(r"/dl/[\w-]+", html)
        if m:
            return urljoin(self.site + "/", m.group(0))
        return None

    def _get_dl_playwright(self, book: BookResult) -> str | None:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            return None
        proxy_arg = {"server": self.proxy_url} if self.proxy_url else None
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, proxy=proxy_arg)
            ctx = browser.new_context(user_agent=self.user_agent, ignore_https_errors=True)
            cookies = self._cookies_for_playwright()
            if cookies:
                ctx.add_cookies(cookies)
            pg = ctx.new_page()
            pg.set_default_timeout(self.playwright_timeout * 1000)
            try:
                pg.goto(book.detail_url, wait_until="domcontentloaded")
                self._wait_cf(pg)
                for sel in ["a[href*='/dl/']", "a.download-link", "a:has-text('Download')", "a:has-text('下载')"]:
                    el = pg.query_selector(sel)
                    if el:
                        href = el.get_attribute("href")
                        if href:
                            return href if href.startswith("http") else urljoin(self.site + "/", href)
            except Exception as e:  # noqa: BLE001
                log.warning("playwright 取下载链接失败: %s", e)
            finally:
                browser.close()
        return None

    def download(self, book: BookResult, dest_dir: Path, fmt_pref: list[str] | None = None,
                 max_rounds: int = 3) -> Path:
        """下载到 dest_dir，返回文件路径。

        `/dl/{code}` 的失败要分成两类，处理方式完全不同：

        - **可重试类**（`_InvalidDownload`）：503 挑战页、返回 HTML 错误页、线路抖动。
          挑战由 `_request`/`_download_httpx` 自动解 PoW 通过；其余情况清掉 `bsrv`
          粘性 cookie 换个后端、并换出口节点重试。最后还可以回退真实浏览器。
        - **不可重试类**（`SiteRejected`：204 No Content / 0 字节）：站点侧这条记录的
          文件已失效。实测换后端、换出口节点、换账号、用真实浏览器**全都一样是 204**
          （见 DEV.md 四.1，是记录本身的属性，跟账号/节点/后端无关）——所以**不重试**，
          直接抛给上层去**换另一条候选记录**（同一本书往往有多条记录，另一条通常能正常
          下载）。继续在这条记录上换节点/换后端重试只是白白浪费时间。
        """
        dest_dir = dest_dir.expanduser()
        dest_dir.mkdir(parents=True, exist_ok=True)
        ext = book.format or "epub"
        safe_title = re.sub(r"[^\w\s\-]+", "", book.title).strip().replace(" ", "_") or book.book_id
        safe_author = re.sub(r"[^\w\s\-]+", "", book.author).strip().replace(" ", "_") if book.author else ""
        name = f"{safe_title}" + (f" - {safe_author}" if safe_author else "") + f".{ext}"
        dest = dest_dir / name

        last_invalid = ""
        for attempt in range(1, max_rounds + 1):
            # 每轮都重新取下载链接：/dl/ 短码会随会话/后端变化
            dl_url, referer = self.get_download_url(book)
            log.info("下载 (第 %d/%d 轮): %s (referer=%s)", attempt, max_rounds, dl_url, referer)
            try:
                dest = self._download_httpx(dl_url, dest, dest_dir, referer)
                log.info("下载完成: %s (%.2f MB)", dest, dest.stat().st_size / 1048576)
                return dest
            except SiteRejected:
                log.warning("被站点拒绝（该记录的文件已失效），不重试，交给上层换候选")
                raise
            except _InvalidDownload as e:
                last_invalid = str(e)
                log.warning("本轮下载内容无效: %s", e)
                if attempt >= max_rounds:
                    break
                self._reset_backend_affinity()
                self._rotate("下载内容无效")
                time.sleep(1)

        log.warning("httpx 多轮重试仍失败（%s），回退 playwright 真实浏览器下载", last_invalid)
        dl_url, referer = self.get_download_url(book)
        dest = self._download_playwright(dl_url, dest, referer)
        log.info("下载完成 (playwright): %s (%.2f MB)", dest, dest.stat().st_size / 1048576)
        return dest

    def _download_httpx(self, dl_url: str, dest: Path, dest_dir: Path, referer: str | None = None) -> Path:
        """流式下载并校验内容。

        校验很有必要：`/dl/` 端点失败时不一定给错误状态码——实测过 `200` + 0 字节、
        `204No Content`、以及直接吐挑战页/错误页 HTML 这三种"假成功"。
        """
        headers = {"Referer": referer} if referer else {}
        headers["Sec-Fetch-Dest"] = "empty"
        headers["Sec-Fetch-Mode"] = "cors"
        # 下载端点也可能先给一次挑战页；先用普通请求确认能拿到真实内容，再流式落盘
        tries = 0
        transport_tries = 0
        while True:
            try:
                with self._http().stream("GET", dl_url, headers=headers) as r:
                    if r.status_code == 204:
                        raise SiteRejected("后端返回 204 No Content（该后端拒绝提供此文件）")
                    ct = r.headers.get("content-type", "")
                    backend = r.headers.get("x-zbackend", "-")
                    # 必须在 raise_for_status() 之前判挑战页：浏览器校验页是带着
                    # **503** 状态码下发的，先 raise_for_status 就会把它当成普通服务端
                    # 错误抛掉，永远没机会解题（实测日志里表现为"下载端点 HTTP 503"）。
                    if "text/html" in ct:
                        r.read()
                        if challenge.looks_like_challenge(r.text):
                            tries += 1
                            if tries > MAX_CHALLENGE_ROUNDS:
                                raise _InvalidDownload("下载端点反复要求浏览器校验")
                            solved = challenge.solve_html(r.text)
                            if not solved:
                                raise _InvalidDownload("下载端点的浏览器校验无法求解")
                            token, elapsed = solved
                            dom = _cookie_domain(self.site)
                            self._http().cookies.set("c_token", token, domain=dom, path="/")
                            self._http().cookies.set("c_time", f"{elapsed:.3f}", domain=dom, path="/")
                            log.info("下载端点通过浏览器校验，重试下载")
                            continue
                        if IP_QUOTA_MARKER in r.text:
                            raise IpQuotaExceeded(_extract_quota_ip(r.text))
                        raise _InvalidDownload(
                            f"返回 HTML 页面而非书籍文件 (HTTP {r.status_code}, "
                            f"content-type={ct}, backend={backend})")
                    r.raise_for_status()
                    # 服务端给了文件名则优先用
                    cd = r.headers.get("content-disposition", "")
                    mfn = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', cd)
                    if mfn:
                        fname = mfn.group(1).strip()
                        if fname and not fname.endswith("/"):
                            dest = dest_dir / fname
                    log.info("开始落盘 (backend=%s, content-type=%s) -> %s", backend, ct, dest)
                    with open(dest, "wb") as f:
                        for chunk in r.iter_bytes():
                            f.write(chunk)
            except httpx.HTTPStatusError as e:
                raise _InvalidDownload(f"下载端点 HTTP {e.response.status_code}") from e
            except (httpx.TransportError, httpx.RemoteProtocolError) as e:
                # 真实文件在独立的 CDN 主机上（如 dln1.ncdn.ec），当前出口节点到主站通、
                # 到 CDN 不一定通，所以这里也要能重试/换节点，否则一次抖动就白判"该记录失效"。
                transport_tries += 1
                dest.unlink(missing_ok=True)  # 流中断会留下半截文件，先删掉
                if transport_tries > MAX_TRANSPORT_RETRIES:
                    raise _InvalidDownload(f"下载传输失败（已重试 {transport_tries} 次）: {e}") from e
                log.warning("下载传输失败(%s)，重试", type(e).__name__)
                if not self._handle_transport_error(transport_tries, f"下载传输 {type(e).__name__}"):
                    raise _InvalidDownload(f"下载传输失败且无可用节点可换: {e}") from e
                continue
            break

        size = dest.stat().st_size
        if size == 0:
            dest.unlink(missing_ok=True)
            raise SiteRejected("下载到0 字节（该后端静默拒绝了此文件）")
        if size < 20000 and _looks_like_html(dest):
            dest.unlink(missing_ok=True)
            raise _InvalidDownload("落盘内容其实是 HTML 页面")
        return dest

    def _download_playwright(self, dl_url: str, dest: Path, referer: str | None = None) -> Path:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise RuntimeError("playwright 未安装，无法回退下载")
        proxy_arg = {"server": self.proxy_url} if self.proxy_url else None
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, proxy=proxy_arg, args=["--disable-blink-features=AutomationControlled"])
            ctx = browser.new_context(user_agent=self.user_agent, ignore_https_errors=True, accept_downloads=True)
            cookies = self._cookies_for_playwright()
            if cookies:
                ctx.add_cookies(cookies)
            page = ctx.new_page()
            page.set_default_timeout(self.playwright_timeout * 1000)
            try:
                with page.expect_download(timeout=self.playwright_timeout * 1000) as dl_info:
                    try:
                        page.goto(dl_url, wait_until="commit", referer=referer)
                    except Exception:  # noqa: BLE001
                        pass  # goto 触发下载时 playwright 会认为导航被中断，属正常现象
                    # 挑战页需要几秒 JS 才能通过并触发真正的下载
                    self._wait_cf(page)
                download = dl_info.value
                download.save_as(dest)
                self._sync_playwright_cookies(ctx.cookies())
                if dest.stat().st_size == 0:
                    raise RuntimeError("playwright 下载到的文件仍为空")
                return dest
            finally:
                browser.close()

    def close(self) -> None:
        self._close_http()

    # ---------- cookie 同步（httpx <-> playwright） ----------

    def _cookies_for_playwright(self) -> list[dict]:
        """把 httpx session 里已有的 cookie（如登录态、CF clearance）转换成
        playwright `context.add_cookies()` 所需的格式，避免新开的浏览器 context
        是匿名状态。"""
        if self._client is None:
            return []
        out = []
        for c in self._client.cookies.jar:
            out.append({
                "name": c.name,
                "value": c.value,
                "domain": c.domain,
                "path": c.path or "/",
            })
        return out

    def _sync_playwright_cookies(self, cookies: list[dict]) -> None:
        """把 playwright context 里的 cookie 写回 httpx session，
        使后续 httpx 请求（search/download）也带上登录态。"""
        jar = self._http().cookies
        for c in cookies:
            jar.set(c["name"], c["value"], domain=c.get("domain", ""), path=c.get("path", "/"))

    # ---------- 登录态持久化（跨进程复用，避免每次都重新登录） ----------

    def save_session(self, path: Path, email: str) -> None:
        """把当前登录态 cookie 存到本地文件，供下次直接复用，无需重新登录。"""
        if self._client is None:
            return
        cookies = [
            {"name": c.name, "value": c.value, "domain": c.domain, "path": c.path or "/"}
            for c in self._client.cookies.jar
        ]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"email": email, "cookies": cookies, "saved_at": time.time()}), encoding="utf-8")

    def load_session(self, path: Path) -> str | None:
        """从本地文件加载登录态 cookie 到 httpx session。返回对应账号 email；
        文件不存在/损坏/无 cookie 时返回 None。"""
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return None
        cookies = data.get("cookies") or []
        if not cookies:
            return None
        jar = self._http().cookies
        for c in cookies:
            jar.set(c["name"], c["value"], domain=c.get("domain", ""), path=c.get("path", "/"))
        return data.get("email")

    def check_logged_in(self) -> bool | None:
        """用当前 cookie 快速验证是否仍处于登录状态。

        返回值是三态，不能只用 True/False：
        - `True` 仍登录、`False` 登录态确实失效
        - `None` **判断不了**（网络/线路问题，连页面都没拿到）

        区分 `None` 很重要：网络不通时如果当成"登录态失效"，会白跑一整轮账号轮换重新
        登录（而重新登录同样会因为网络不通而失败），最后给用户一个"所有账号登录均失败"
        的误导性报错——真正的原因是网络不通，跟账号毫无关系。
        """
        try:
            r = self._get("/profile", allow_cf=True)
        except (httpx.TransportError, httpx.RemoteProtocolError) as e:
            log.warning("无法访问 /profile 校验登录态（网络问题）: %s", e)
            return None
        except Exception:  # noqa: BLE001
            return None
        if self._is_cf(r):
            return False
        low = r.text.lower()
        if "/login" in str(r.url) and "logout" not in low:
            return False
        return self._parse_remaining(r.text) is not None or "logout" in low

    def current_account_email(self) -> str | None:
        """读取「站点认为当前登录的是哪个账号」。

        本地 `session.json` 记的email 可能和 cookie 实际对应的账号不一致（例如中途
        换号登录但保存失败），实测出现过"本地记 A、站点其实是 B"的情况，会导致下载额度
        记到错误的账号头上。所以复用登录态时要以站点返回的为准。

        注意要排除站点自己的邮箱：页面页脚有 `support@z-lib.fm` 这类客服地址，直接取
        "页面里第一个邮箱"会取到它。
        """
        try:
            r = self._get("/profile", allow_cf=True)
        except Exception:  # noqa: BLE001
            return None
        text = BeautifulSoup(r.text, "html.parser").get_text(" ")
        for m in re.finditer(r"[\w.+-]+@[\w-]+\.[\w.-]+", text):
            addr = m.group(0)
            domain = addr.rsplit("@", 1)[-1].lower()
            if "z-lib" in domain or "zlibrary" in domain or "z-library" in domain:
                continue
            return addr
        return None


# ---------- 工具函数 ----------


def _find(pattern: str, text: str, default: str) -> str:
    m = re.search(pattern, text, re.IGNORECASE)
    return m.group(0) if m else default


def _peek_text(r: httpx.Response) -> str:
    """安全地取响应正文前若干字符用于特征判断。

    非 HTML（书籍二进制流）时不去解码，避免把几十MB 的文件读进内存/解码报错。
    """
    ct = r.headers.get("content-type", "")
    if "text/html" not in ct:
        return ""
    try:
        return r.text[:6000]
    except Exception:  # noqa: BLE001
        return ""


def _cookie_domain(site: str) -> str:
    """从站点 URL 取 cookie 域（挑战页的 cookie 是 host-only，不带前导点）。"""
    return httpx.URL(site).host


def _extract_quota_ip(html: str) -> str | None:
    """从「每日限额已用完」错误页里提取页面标出的出口 IP（仅用于日志/提示，
    提取失败不影响主流程，返回 None 即可）。"""
    m = re.search(r"(?:\d{1,3}\.){3}\d{1,3}", html)
    return m.group(0) if m else None


def _to_float(v: str | None) -> float:
    try:
        return float(v) if v else 0.0
    except ValueError:
        return 0.0


def _looks_like_html(path: Path) -> bool:
    """粗略判断文件开头是否是 HTML（说明拿到的是挑战页/错误页而非真实书籍文件）。"""
    try:
        with open(path, "rb") as f:
            head = f.read(512).lstrip().lower()
        return head.startswith(b"<!doctype html") or head.startswith(b"<html")
    except OSError:
        return False


def _parse_rating(text: str, card) -> float:
    # 星级评分：尝试 data 属性或文本中的 "4.5" 等
    for el in card.select("[data-rating], .rating, .stars, .bookRating"):
        v = el.get("data-rating") or el.get("data-score")
        if v:
            try:
                return float(v)
            except ValueError:
                pass
        t = el.get_text(strip=True)
        m = re.search(r"(\d(?:\.\d)?)", t)
        if m:
            return float(m.group(1))
    m = re.search(r"rating[:\s]*(\d(?:\.\d)?)", text.lower())
    if m:
        return float(m.group(1))
    return 0.0
