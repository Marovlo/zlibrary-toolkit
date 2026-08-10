# Z-Library 一键工具 开发手册

> 本文档随开发持续更新，记录对齐方案、设计决策、关键技术、踩坑实录。

---

## 一、项目目标

构建一个全自动 Z-Library 工具，全链路：

```
找官网(低优) → 测连通 → 选最优代理 → 登录 → 搜索 → 下载(账号自动轮换)
```

核心痛点：官网多变 + 国内需代理 + Cloudflare 防护 + 多账号限流。

---

## 二、对齐方案（已确认）

| 决策点 | 选择 | 说明 |
|---|---|---|
| 代理引擎 | **自动下载 mihomo 二进制** | 我下载、生成配置、启动、用 RESTful API 控制。支持订阅全部协议。 |
| 代理影响范围 | **只影响本程序** | mihomo不开 TUN，只开本地 HTTP/SOCKS/控制API/DNS 端口（均为自动挑选的不常见高位端口，避免跟用户机器上已有的 mihomo/clash冲突，见十六节），系统其他程序不受影响。 |
| 访问方式 | **httpx 优先，playwright 回退** | 日常 httpx+代理；遇 Cloudflare/登录失败自动起 playwright。 |
| 工具形态 | **CLI** | `zlib search "书名"` / `zlib download`。交互走终端。 |
| 减少访问 | **一次搜索拿全信息** | 搜索结果包含下载所需的 book id/hash，用户选择后直接构造下载请求，不二次搜索。 |
| 账密存储 | **明文 yaml** | `accounts.yaml`，权限 0600。多账号轮换，每号每日 10 本。 |

---

## 三、架构设计

### 模块划分

> 以下为当前实际结构（历史开发记录中出现的旧模块名/端口号等以下方"开发记录"章节
> 为准，反映的是发生时的状态，可能与现状不同）。

```
src/zlibrary/
├── cli.py              # CLI 入口（search/download/status/stop/logout/add-account/upgrade-mihomo）
├── config.py           # 配置加载
├── subscription.py     # 订阅拉取 + 解析
├── proxy_manager.py    # mihomo 全生命周期（含随包引导/端口自动选择/代理升级）+ 测速选优+ 节点轮换
├── challenge.py        # 站点浏览器校验（SHA-1 PoW）求解
├── site_finder.py      # [低优] web 搜索官网，结果缓存
├── site_checker.py     # 连通性检测（校验响应确实来自 Z-Library）
├── accounts.py         # 账密管理器：轮换、次数跟踪
└── client.py           # Z-Library 客户端：登录/搜索/下载（下载落盘逻辑也在这里，没有独立的 downloader.py）
```

### 全链路流程

1. `site_finder`（低优，可缓存）：搜官网，返回候选域名。当前 `zh.z-library.sk` 已知，默认用。
2. `site_checker`：直连测试。通→跳过代理；不通→进 proxy_manager。
3. `proxy_manager`：拉订阅→解析节点→逐节点测「到 Z-Library 的连通性+延迟」→选最优→配置代理。
4. `client`：`accounts` 选当日有次数的账号→登录→搜索。
5. `downloader`：完全匹配→自动下评分最高；不完全匹配→列表交互选择→下载；次数耗尽自动换号。

### 关键设计：减少访问次数

- 搜索一次，结果对象包含全部下载所需字段（book id / hash / 下载链接或下载 API 端点）。
- 用户选择后直接用缓存信息发起下载，**不二次搜索**。
- 全程只访问 Z-Library：搜索 1 次 + 下载 1 次（+ 可能的登录 1 次）。

---

## 四、关键技术

### 1. mihomo (Clash.Meta) 控制

- **不依赖系统 clash**：随包自带二进制，本地解压到 `data/mihomo`（见十五节），无需联网。
- **非 TUN 模式**：配置只开 `mixed-port`(HTTP+SOCKS) 和 `socks-port`，不开 `tun.enable`。
  只有显式走本地代理端口的请求才被代理，系统其他程序不受影响。
- **本地端口自动选择**（见十六节）：HTTP/SOCKS/控制API/DNS 四类端口默认不写死为
  7890/7891/9090 这类常见默认值，而是从内置的一组不常见高位端口候选里探测选一个
  当前空闲的，避免跟用户机器上已有的 mihomo/clash抢端口。
- **RESTful API**：
  - `GET /proxies`：列出所有代理组和节点
  - `GET /proxies/{name}/delay?url=...&timeout=...`：测某节点到目标 URL 的延迟
  - `PUT /proxies/{group}` + `{name}`：切换选择器组的当前节点
- **配置生成**：把订阅的 proxies 直接放进 mihomo config，selector 组手动构造，方便 API 切换。
- **外部控制**：`external-controller`指向自动选中的 API 端口，`secret` 走 config.yaml 里配置的固定值。

### 2. httpx + playwright 回退

- httpx 配代理 URL（`http://127.0.0.1:{自动选中的端口}`），带合理 UA 和 header。
- 检测浏览器校验挑战：见十三节，纯 SHA-1 PoW，代码里自动解，不依赖 Cloudflare 判断逻辑。
- playwright 用 chromium，同样走本地自动选中的代理端口，仅作为httpx 失败时的最后回退。

### 3. 账号轮换

- `accounts.yaml`：列表，每账号 `email/password/downloads_today/last_reset_date`。
- 每日 0 点（按本地日期）重置 `downloads_today=0`。
- 选号策略：优先 `downloads_today < 10` 的；下载成功 +1；达 10 标记不可用，自动切下一个。
- 登录后从账户页读真实剩余次数校正（避免偏差）。

### 4. 官网查找（低优）

- 用搜索引擎（DuckDuckGo/Bing）搜 "z-library official"。
- 结果缓存到 `data/known_sites.json`，带时间戳。
- 当前默认 `zh.z-library.sk`，查找失败不影响主流程。

---

## 五、配置文件

实际模板见 `config.example.yaml`/`accounts.example.yaml`（提交到仓库），
真实的 `config.yaml`/`accounts.yaml`（含真实token/密码）已 gitignore，不提交。
端口字段现在默认留空由程序自动选择，不再像下面这个早期示例一样写死具体端口号
（写死端口是十六节修复前的旧状态，仅作历史参照）：

### config.yaml（早期示例，端口部分已过时）

```yaml
subscription_url: "https://your-subscription-provider.example.com/api/v1/client/subscribe?token=..."
default_site: "https://zh.z-library.sk"
download_dir: "~/Downloads/zlibrary"
format_preference: ["epub", "pdf", "mobi", "azw3"]
mihomo:
  binary_path: "data/mihomo"
  api_secret: "zlib-local"
  # http_port/socks_port/api_port/dns_port 留空即可，自动选择空闲端口（见十六节）
```

### accounts.yaml

```yaml
accounts:
  - email: "a@example.com"
    password: "xxx"
    downloads_today: 0
    last_reset_date: "2025-08-08"
```

---

## 六、开发记录

### 2025-08-08 启动

- 订阅拉取：直接 curl 被 reset（`Connection reset by peer`）。换 clash UA 仍慢/超时。
- **踩坑#1**：订阅域名是punycode（中文域名转码）形式，DNS 解析或 TLS 在当前网络可能被干扰。**结论**：订阅拉取在用户真实环境运行时再验证，代码层做好 UA 设置和重试。
- 决定：不阻塞于订阅拉取，先写代码，用 mock 订阅验证解析逻辑。

### 2025-08-08 包名冲突

- **踩坑#2**：最初包目录命名为 `zlib`，与 Python 标准库 `zlib`（C 扩展）冲突。`import zlib` 加载的是标准库，报 `'zlib' is not a package`。
- **修复**：包目录重命名 `src/zlib` → `src/zlibrary`，内部全用相对导入无需改。CLI 命令名 `zlib` 不变（entry point 改为 `zlibrary.cli:main`）。

### 2025-08-08 mihomo 二进制下载

- GitHub API `api.github.com/repos/MetaCubeX/mihomo/releases/latest` 在本环境可达，取到最新版 `v1.19.29`。
- **踩坑#3**：GitHub releases 直连下载约 90 秒后 `Server disconnected`。镜像兜底已实现（ghproxy.net / mirror.ghproxy.com），但本环境三个源全失败：
  - 直连：Server disconnected without sending a response
  - ghproxy.net：下载 15.5MB/17.8MB 后 peer closed
  - mirror.ghproxy.com：Network is unreachable
- **踩坑#4（引导启动/bootstrapping 问题）**：下载代理二进制本身需要联网，而联网恰是我们要解决的问题。若用户环境同样无法直连 GitHub，需手动放置二进制。
- **解决方案**：代码已支持——若 `data/mihomo` 已存在且可执行则跳过下载。用户可手动下载 `mihomo-linux-amd64-{ver}.gz` 解压到 `data/mihomo` 并 `chmod +x`。下载逻辑含多镜像兜底，多数环境可用。

### 2025-08-08 本地验证结果

以下在本环境已通过（`tests/test_local.py`）：

- ✅ 所有模块 import 正常
- ✅ 配置加载（site/download_dir/mihomo binary 路径）
- ✅ 订阅解析（mock 订阅 3 节点：ss/vmess/trojan）
- ✅ mihomo 配置生成（selector 组 `ZLIB-SELECT` + `MATCH,ZLIB-SELECT` 规则）
- ✅ 账号管理器（加载/跨日重置/可用判断）
- ✅ 书名匹配逻辑（完全匹配=100 / 包含=50 / 不匹配=0）
- ✅ mihomo 版本 API（取到 v1.19.29）
- ✅ 多镜像下载兜底逻辑（依次尝试、报错清晰）

以下需在用户真实环境（可访问外网/有代理）验证：

- ⏳ 实际订阅拉取（本环境订阅域名不可达）
- ⏳ mihomo 二进制完整下载与解压（本环境 GitHub 不可达）
- ⏳ mihomo 启动 + 逐节点测速到 Z-Library
- ⏳ Z-Library 直连/代理访问、登录、搜索、下载

### 技术要点备忘

- **mihomo 非影响系统**：配置 `mixed-port`+`socks-port`，不开 `tun.enable`，`allow-lan: false`。只有显式走 127.0.0.1:7890 的请求被代理。
- **测速 API**：`GET /proxies/{name}/delay?url={test_url}&timeout={ms}` 返回 `{"delay": N}`，4xx 时返回 `{"message":...}`。
- **切换节点**：`PUT /proxies/ZLIB-SELECT` body `{"name":"节点名"}`。
- **减少访问**：搜索结果 `BookResult` 含 `book_id`+`hash`+`detail_url`，用户选定后直接 `get_download_url` 取下载链接再下载，不二次搜索。
- **Cloudflare 检测**：状态 403/503 + html 含 `just a moment`/`cf-` 标记 → 回退 playwright。
- **账号轮换**：登录后从 profile 读真实剩余次数校正本地计数；下载失败也计数防死循环。

---

## 七、2026-08-08 全链路打通记录

用户提供的 clash 订阅（`clash-config.yaml`）+ mihomo 二进制 + 真实账号密码，在用户环境下把整条链路跑通了：**直连检测 → 代理测速选优 → 登录 → 搜索 → 完全匹配 → 下载**，全部验证成功（实际下载了 13.48MB 的《Sapiens》epub）。

### 架构评估结论

原方案的分层设计（httpx 优先 + playwright 兜底、直连优先 + 代理兜底、多账号轮换）是合理的，**不需要推翻重做**。但发现并修复了以下问题：

### 踩坑 #5：日志格式变量名打错

`cli.py` 里 `logging.basicConfig(format=__FMT)`，变量名多写了一个下划线（实际定义是 `_FMT`），一运行就`NameError` 崩溃。

### 踩坑 #6：mihomo 进程泄漏

`search` 命令 `finally` 里忘了 `pm.stop()`，每次运行都会让 mihomo 常驻进程堆积。

### 踩坑 #7：账号轮换死循环

`_login` 用 `while True` 循环选账号，只要账号登录失败（非"今日额度用尽"原因），`next_available()` 还会选中同一个账号，导致**永久死循环**（已实测复现）。修复：记录本次会话已失败的账号（`exclude`集合），全部失败后立即报错退出，不再无限重试。

### 踩坑 #8：`_parse_remaining`正则被 HTML 标签"隔断"导致误判登录失败

z-library 页面顶部有个持久化小组件显示"`0/10`... `每日限额`"，但两者之间隔着 `</div><i class="..."></i><span>` 等标签。原正则直接对**原始 HTML**做 `\s*` 匹配，标签不是空白字符，永远匹配不上 → `remaining` 恒为 `None` → 误判为"登录后未能确认"，即使登录其实已经成功。**修复**：改用 `BeautifulSoup(html).get_text(" ")` 先转纯文本再做正则匹配。这是本次卡点里最隐蔽的一个坑——账号密码是对的，登录也是成功的，但程序自己误判失败。

### 踩坑 #9：playwright 提交表单后过早读取页面内容

登录表单提交是 JS 异步跳转（cookie 写入和页面跳转不同步），`wait_for_load_state("domcontentloaded")` 有时在跳转真正发生前就返回，导致读到的还是登录页内容。修复：提交后循环等待 URL 离开 `/login`（最多 10s）+额外等 `networkidle`，再读取内容判定。

### 踩坑 #10（本次最大的坑）：登录态 cookie 从未在 httpx 和 playwright 之间同步

这是导致"登录成功但搜索 0 结果"的根因。`_login_playwright` 用独立的浏览器 context 登录，成功后直接 `browser.close()`，cookie 全部丢弃，从未写回 httpx session；随后 `_search_httpx`（用没有登录态的 httpx client）和 `_search_playwright`（每次都开全新匿名context）实际都是以**未登录状态**在访问 z-library，搜索页因此渲染不出结果。

**修复**：加了 `_cookies_for_playwright()` / `_sync_playwright_cookies()` 两个方法，实现 httpx ↔ playwright 的 cookie 双向同步——playwright 登录成功后把 cookie 写回 httpx session；之后每次新开 playwright context（search / download 的兜底路径）都会先把 httpx session 里已有的 cookie（含登录态、CF clearance）注入进去。

### 踩坑 #11：z-library 页面结构已改版，原搜索解析器完全失效

原代码假设的搜索结果结构（`[data-book_id]` / `.resItemBox` / `/book/{id}/{hash}/{slug}` 链接格式）已经过时。**实际结构**是自定义 web component：

```html
<z-bookcard id="2639909" termshash="525a..." href="/book/9ZjK5GY3O0/sapiens.html"
            download="/dl/vXBezmvLwb" deleted="0" language="英语" year="2015"
            extension="epub" filesize="13.48 MB" rating="5.0">
    <div slot="title">Sapiens</div>
    <div slot="author">Yuval Noah Harari</div>
</z-bookcard>
```

**意外的好消息**：`download` 属性直接给出了下载链接，**搜索结果里就自带下载地址**，比原设计"搜索后还要访问详情页解析下载链接"更省一次请求。已重写 `_parse_search`/新增 `_parse_bookcard`，`BookResult` 加了 `download_url` 字段，`get_download_url()` 优先直接用它（原详情页解析逻辑保留作兜底）。

### 性能/资源优化（用户反馈后新增）

- **订阅缓存**：`config.yaml` 新增 `mihomo.subscription_cache_hours`（默认 24），有效期内直接复用本地 `sub.yaml`，不再每次运行都拉取订阅；过期或全部节点测速失败才强制刷新重拉。
- **代理节点复用**：`ProxyManager` 新增 `state.json`（存最近一次选中的节点名），下次启动先只测这一个节点的延迟，通则直接复用（跳过全量测速）；不通才触发全量测速选优。实测复用生效，日志里能看到"复用上次节点 XXX，跳过全量测速"。

### 验证结果

- 密码更正（用户确认，原密码少输了末尾一个特殊符号）后，登录立即成功。
- 实测选中节点多为香港/台湾节点（延迟 1.2~2s级别），能稳定绕过 Cloudflare 并访问 z-library。
- `zlib search "sapiens"` → 解析出 51 本书，完全匹配标记正确。
- `zlib download "sapiens" -y` → 自动选完全匹配评分最高的版本 → 下载成功，13.48MB epub 落盘，账号计数 `1/10` 正确累加。

---

## 八、2026-08-08 后台持久化改造（一次连接/代理/登录多次复用）

用户反馈"每次 search 都要重新走一遍全流程太慢"，改造为持久化：**代理进程后台常驻+ 登录态本地持久化，过期自动重新登录**。

### 改造点

1. **mihomo 代理后台常驻**：`ProxyManager.start()` 用 `start_new_session=True` 启动，脱离当前 CLI 进程的会话组；`stderr` 从 `PIPE`（父进程退出后管道关闭，子进程写入会 SIGPIPE）改成写到 `data/mihomo_run/mihomo.log` 文件。新增 `mihomo.pid` 文件，使不同 CLI 进程之间也能互相识别/管理（`is_running()` 靠 API 健康检查跨进程识别；`stop()` 支持通过 pid 文件杀掉另一进程启动的实例）。CLI 的 `search`/`download` 命令 **不再在 finally 里 `pm.stop()`**，代理进程用完不主动杀，下次直接复用。
2. **登录态本地持久化**：`ZLibraryClient` 新增 `save_session()`/`load_session()`（存取`data/session.json`，含 cookie +账号 email）和 `check_logged_in()`（用现有 cookie 轻量请求 `/profile` 校验是否仍登录，不做完整登录流程）。`cli._login()` 改为：先加载本地 session，若账号额度未用尽且 `check_logged_in()` 通过 → 直接复用（免登录，跳过账号轮换和Cloudflare/playwright 整套流程）；校验失败才走原有的账号轮换登录，成功后覆盖保存新session。
3. **新增管理命令**：`zlib status`（查看代理/登录态状态）、`zlib stop`（手动停止后台代理）、`zlib logout`（清除本地登录态强制下次重新登录）。

### 踩坑 #12：直连探测占满了完整的 30s 超时

改造后发现即使命中"代理+登录态全复用"，单次 search 仍要 30+ 秒。排查发现瓶颈根本不在代理/登录逻辑，而是**每次运行开头的直连探测**（`check_direct`）用了完整的 `httpx_timeout`（配置里是 30s）；本环境网络异常时（DNS/路由层面）该请求会一直挂到超时上限才失败，白白吃掉 30s。这个探测本质上只是个「试一下能不能直连」的快速判断，不该用跟正式业务请求一样长的超时。**修复**：直连探测单独用 `min(8, httpx_timeout)` 的短超时，与正式请求超时解耦。

### 效果对比（实测，加上 #12 修复后）

| 场景 | 耗时 | 说明 |
|---|---|---|
| 冷启动（无代理、无登录态） | ~49s | 建代理+全量测速+账号轮换登录(走playwright) |
| 代理+登录态均复用 | **~13s** | 8s 直连探测(本环境网络异常导致的固定开销) + 2s 单节点验活 + 2s 登录态校验+搜索 |
| 登录态失效（模拟 cookie 失效）自动重登 | ~16s | 代理复用+检测到登录态失效+账号轮换重登(这次走 httpx 更快)+自动保存新 session |

用户实际环境如果直连判断能快速失败（DNS/网络层面比本沙盒环境正常），复用场景应能进一步降到 5s 内。

### 使用方式

```bash
# 正常用，无需额外操作 —— 代理和登录态会自动持久化并在后续调用中复用
zlib search "书名"
zlib download "书名" -y

# 查看当前后台代理节点 / 登录态状态
zlib status

# 手动停止后台代理进程（比如要切换网络环境时）
zlib stop

# 强制下次重新登录（比如怀疑账号异常/想切账号验证）
zlib logout
```

代理和登录态状态存放位置：`data/mihomo_run/`（mihomo 运行目录，含 `mihomo.pid`/`mihomo.log`/`state.json`）、`data/session.json`（登录态）。这些都在 `.gitignore`覆盖范围内（本项目未初始化 git，暂无泄露风险，但注意 `session.json` 含真实登录 cookie，不要分享出去）。

---

## 九、2026-08-08 新增 add-account 命令 + 下载环节两个真实 bug

### 新增：`zlib add-account <email> [password]`

添加账号前先做一次真实登录测试，成功才写入 `accounts.yaml`（新账号新增/已存在则更新密码+剩余次数），失败直接报出登录失败的具体原因，不写入、不污染账号池。已用错误密码/正确密码/不存在假账号三种场景验证。

### 踩坑 #13：下载内容为空也被当成"下载成功"

`download()` 原逻辑只判断 httpx 状态码是否报错（`raise_for_status()`），完全没校验响应体是否真的是书籍文件。实测下载《DK魔法百科》时，`/dl/{code}` 端点返回 200 但内容是 **0 字节空响应**（推测是反爬机制在编辑/边缘层静默拦截，没有走明确的 403/503），代码把空文件当成"下载完成"直接返回，还顺带正常扣了账号下载额度。

**修复**：`download()` 现在会校验内容——0字节 或 content-type/内容特征像HTML（挑战页/错误页）都视为无效，自动回退到 playwright 真实浏览器下载（用 `page.expect_download()` 捕获浏览器原生下载事件，能绕开 httpx 无法执行 JS 的限制）。

### 踩坑 #14：`_wait_cf`漏检第一阶段挑战页，导致完全没等就判定"已通过"

z-library 的反爬挑战是两段式：第一段 `<title>` 是 `"Checking your browser ..."`（js 检测cookie 支持后reload），第二段才是含`"Just a moment"` 文案的 **PoW挑战**（浏览器要跑 SHA1暴力搜索特定前缀的 hash，找到后写 cookie 再 reload 到真实内容）。原 `_wait_cf()` 只查 title 是否含 `"just a moment"` / `"challenge"`——第一段 title 两者都不含，直接被误判为"没有挑战，可以继续"，实际上完全没等挑战完成。**修复**：改成同时扫正文内容关键字（复用 `CF_MARKERS`），并把 `"checking your browser"` 也加进判定条件。

修复后用独立调试脚本验证：**下载端点的这个 PoW 挑战在无头浏览器环境下等了 90 秒仍卡在 "Checking your browser..." 从未推进、从未触发下载事件**——这不是等待时间不够的问题，而是这套反爬机制大概率专门检测出了headless/自动化环境，故意给出算不出解的挑战（常见反爬对抗手段，防止批量下载）。同一账号/同一代理节点下载《Sapiens》完全正常（httpx 直接成功，13.48MB 完整文件），证明不是账号或代理被封，是**这本书的下载链接被针对性拦截**。

**结论**：这是站点反爬的真实限制，不是代码 bug 能单纯"改逻辑"解决的（继续对抗 headless 检测容易演变成规避安全机制的军备竞赛，超出合理范围）。已加防御性修复：
- **踩坑 #15：换号重试没有上限**，一本书下载不了会一直换账号重试直到把所有账号的每日额度全部烧光。`_do_download` 加了 `max_attempts=2` 的重试上限，超过就报出清晰原因（"可能是该书下载链接被反爬拦截，建议手动浏览器下载"），不再无脑换号。

### 教训 /遗留问题

- 测试过程中这本书的失败重试消耗了不少账号额度（`downloads_today` 从 3涨到 7），因为当时重试上限还没加上。以后同类问题会在 2 次内止损。
- 遇到这类"某本书下载不了、其他书正常"的情况，大概率是该书触发了更严格的反爬（可能是冷门书/字体版权特殊处理/单本限流），建议：换个时间点重试、或用保存的 `data/session.json` cookie 手动在真实浏览器里登录下载。

---

## 十、2026-08-08纠正结论：DK魔法百科下载失败的真正原因不是"反爬检测"，是站点主动拒绝该书下载

用户提出疑问"既然反爬这么严重要不要干脆全用playwright"，为回答这个问题做了对比实验（用户提供的另一本已知能下载的书《威卡魔法》做对照组），结果**推翻了第九节里"这是反爬 PoW挑战识别出自动化环境"的猜测**，找到了更准确的根因。

### 对比实验过程

1. `zlib download 威卡魔法` —— **httpx 直接成功**，82.45MB PDF 完整下载，全程无挑战、无 playwright 兜底。
2. 用同一个 httpx session（相同 cookie、相同真实浏览器 UA/headers、相同代理节点）分别直接请求两本书的 `/dl/{code}` 链接对比：
   - 威卡魔法：`302` 重定向到真实 CDN (`dln1.ncdn.ec/...`)，文件头是标准 PDF 魔数，一次成功。
   - DK魔法百科：`204 No Content` —— **没有挑战页，没有 403/503，是后端应用直接返回"处理了但无内容"**，这是明确的拒绝信号，跟走Cloudflare/PoW 挑战的表现完全不同。
3. 关键交叉验证：z-library 后端是负载均衡的（响应头 `x-zbackend: v2-01`/`v2-02`，靠 `bsrv` cookie 做粘性路由）。**同一个后端（v2-01）**上反复测试：威卡魔法始终 `302` 成功，DK 始终 `204` 拒绝，稳定复现，跟节点（香港/台湾）、跟"是不是第一次请求"都无关。换到另一个后端（v2-02）时 DK 变成显示 PoW 挑战页——说明挑战页只是负载均衡到的某台后端的另一层机制，**不是 DK 下载失败的根本原因**，根本原因是应用层（v2-01 这台后端）对这本书的下载**直接拒绝**。

### 纠正后的结论

- DK 这本书的下载链接被z-library 自己的后端应用逻辑主动拒绝（`204`），大概率是**版权/DMCA 下架或地区分发限制**（威卡魔法重定向 CDN 链接里带了 `countryCode=hk` 参数，说明确实存在按地区限制下载的机制），跟客户端用httpx 还是 playwright、headless 还是有头浏览器**完全无关**——这否定了第九节里"专门针对自动化环境给出解不开的验证挑战"的猜测。
- 回答用户"是否该弃用httpx 全用 playwright"的问题：**结论不变，依然不该弃用**，但理由更新为——这本书的失败根本不是"反爬技术层面能不能绕过"的问题，是站点应用层的主动限制，playwright 也一样会被 `204` 拒绝（挑战页只在换到另一台后端时才出现，且就算解出challenge 也不代表 v2-01 那台后端会放行）。换协议/换工具解决不了"平台不给这本书下载"的问题。
- 第九节里记录的两个真实代码 bug（#13 下载内容零字节校验缺失、#14 `_wait_cf` 漏检第一阶段挑战页）依然是有效且必要的修复，只是它们解决的是"如何正确处理挑战/无效响应"的通用问题，跟 DK 这本书本身能不能下载是两件不完全相关的事——即使这两个 bug 都修好了，DK 依然会因为 `204` 拒绝而下载失败，这是预期行为，不是 bug。

---

## 十一、2026-08-09 关键新发现：订阅里存在"WAP"节点变体，且后端路由(v2-01/02/03)才是决定下载成败的开关

用户反馈手机 VPN 连接的节点名叫"香港WAP-优化-Gemini"，且新账号在这个节点下点开DK魔法百科**能直接下载**。查订阅发现节点列表里同一地区确实存在两套并行节点：

```
香港-优化-Gemini / 香港-优化2-Gemini / 香港-优化3-Gemini        （常规）
香港WAP-优化-Gemini / 香港WAP-优化2-Gemini / 香港WAP-优化3-Gemini（WAP变体）
```

服务器此前一直复用的是常规节点（`香港-优化2-Gemini`），从未测过WAP 变体——这是此前"手机能下、服务器不能下"的一个有力候选解释。

### 验证实验与新发现

用已保存的登录 cookie，直接对 DK 的下载直链 (`/dl/XBen3lGmYw`) 发GET 请求（不走完整下载流程，不消耗账号下载额度），在 9 个 香港/香港WAP/新加坡 节点间切换对比，观察响应头 `x-zbackend`：

- **相同的 dl 链接 + 相同的登录 cookie，仅切换出口节点，落到的后端就不同**（v2-02 / v2-03 都出现过），且*与是否 WAP 无关*——WAP 和非WAP 节点都命中过v2-02/v2-03。
- 命中 v2-02/v2-03 时，响应统一是 `503` + 长度 9592 字节的页面（Cloudflare "Just a moment" 挑战页），不是第十节里记录的 v2-01 那种"静默 204 拒绝"。
- 之后尝试遍历全部 9 个节点做完整映射时，**本机到香港所有节点（含常规和WAP）的连接全部超时/SSL失败**，只有新加坡节点能连通——这是本地网络到HK 节点当时的连通性问题（非 z-library 端问题），导致无法完成一次干净的"WAP vs 非WAP"对照实验，被迫中止。

### 修正后的理解

之前第十节的结论"是应用层按书主动拒绝(204)，跟客户端/浏览器无关"需要补充：**204只是碰到v2-01 后端时的表现**；同一本书换到 v2-02/v2-03 后端时，表现是**可能可解的Cloudflare 挑战（503）**，而不是硬拒绝。也就是说：

- 落到 v2-01 → 硬拒绝（204），任何客户端都无法绕过，这是站点对该书的地区/版权限制。
- 落到 v2-02/v2-03 → 给一道 CF 挑战，**真实浏览器（手机）大概率能算出来通过**，但本工具用 headless playwright 自动化环境此前测试中卡了 60~90 秒始终无法完成挑战（第九节踩坑 #14 后的复测记录）。
- 出口节点（IP）会影响负载均衡器把请求路由到哪个后端，所以"换节点有时能下、有时不能"是真实现象，但**不是"WAP节点比普通节点更好"这么简单**——同一批WAP/非WAP 节点都能命中 v2-02/v2-03，只是概率/时机问题，无法稳定保证下次换节点一定能避开 v2-01 或避开需要真机才能过的挑战。

### 代价与遗留

- 本次验证过程中有一次完整走CLI 下载流程的重试（为了拿到干净对照前先确认"当前节点是否还能重现失败"），两个账号各消耗了 1 次下载额度（`downloads_today` 均+1），且都以失败告终（0 字节 → playwright 等挑战超时）。
- 结论：DK 这类书的下载失败根因是**后端路由的随机性 + headless 浏览器无法通过某些后端的 Cloudflare 挑战**的组合，不是单一"IP 信誉"或"账号"问题，服务器和手机用同一 VPN 订阅商这个事实不矛盾（因为决定因素是"这次连接具体路由到哪个后端"，跟訂閱商无关，跟每次连接的运气/节点有关）。
- 尚未做、如需要可作为下一步：把 playwright 换成非 headless（真实有头浏览器，配合 `xvfb-run` 或类似方案）看是否能通过 v2-02/v2-03 的挑战；但落到 v2-01 的书依然无法可靠下载，这部分只能人工浏览器下载。

---

## 十二、交接记录（2026-08-09）：DK魔法百科下载问题现状 + 手机端成功路径完整还原 + 后续方向

用户即将切换到另一个 agent 继续排查，本节完整记录现状、证据、用户手机上描述的所有成功场景细节、以及可能的后续方向，供接手者直接使用，**不需要重新做前面已经做过的实验**。

### 1. 问题本质（一句话）

同一本书《DK魔法百科》的下载链接 `https://zh.z-library.sk/dl/XBen3lGmYw`，在**本服务器上用httpx/playwright（headless）自动化始终下载失败**，但**用户手机用真实浏览器 App 能直接成功下载**，且服务器和手机走的是**同一个 VPN 订阅商**。目标是搞清楚差异到底在哪，让服务器也能稳定下载。

### 2. 已确认的技术机制（可信度高，有实测证据）

- z-library 后端是负载均衡的多机集群，响应头 `x-zbackend` 会显示具体命中哪台后端（观察到 v2-01 / v2-02 / v2-03）。
- **命中 v2-01**：对 DK 这本书直接返回 `204 No Content`（无Cloudflare 挑战、无 403/503，是应用层静默拒绝），推测是版权/地区分发限制，**任何客户端（httpx/playwright/真机浏览器）都无法绕过**。作为对照，另一本书《威卡魔法》在同一 v2-01 后端上稳定 `302` 重定向到 CDN 成功下载——证明 v2-01 是"按书区分"地主动拒绝，不是通用反爬。
- **命中 v2-02 / v2-03**：对 DK 返回 `503` +一个 9592 字节的 Cloudflare "Just a moment" 挑战页（需要 JS 跑 PoW 才能拿到 clearance cookie）。
- **出口节点（代理IP）会影响负载均衡把请求路由到哪台后端**，但目前的抽样测试显示这个路由**跟节点是不是"WAP"变体无关**——WAP 节点和普通节点都命中过 v2-02/v2-03，样本还太小，不能排除"某些具体节点更容易落到 v2-01"这种更细粒度的规律。
- **headless playwright 卡在 CF 挑战页无法通过**：第九节的独立测试里，headless 浏览器在 v2-02 这类后端的挑战页上等了 90秒，页面标题一直停在 "Checking your browser..."，从未推进、从未触发下载事件——这是自动化环境被 CF 检测出来后故意给出算不出解的挑战（常见反自动化手段）。

### 3. 用户手机上实测过的所有成功场景（原始描述，未加工，接手者应重点参考）

这些是用户在其手机浏览器 App 上、连接**同一个 VPN 订阅商**、真人操作时观察到的现象，按时间顺序：

1. **场景A**（香港节点，具体节点名当时未记录）：打开z-library 网站首页时弹出"checking browser"挑战页，通过后发现自己已经是登录状态；搜索到DK魔法百科，点击下载，**下载过程本身完全没有弹挑战**，直接就下载成功了。即：挑战只在"打开网站首页"这一步出现过，下载这一步没有再触发挑战。
2. **场景B**（新账号，该账号有2周无限下载特权）：在网站主页随便点了一本书（非DK），可以直接下载，全程无挑战。
3. **场景C**（同一新账号，退出手机浏览器App但网页会话保留，重新打开App直接停留在已登录的主页）：这次**没有弹 checking browser**；点开某一本书的详情页时**弹出了挑战，但基本5秒内自动通过**；之后再点击下载，**没有再弹挑战，直接就能下载**。
4. **场景D（本次对话，最新）**：用户明确说明，这次连的节点是 **"香港WAP-优化-Gemini"**（订阅里 HK 节点存在"普通"和"WAP"两组变体，服务器此前一直只用过"普通"组），账号是新账号，**下载能直接下载**（未描述是否弹过挑战，大概率跟场景A/C类似——最多弹一次几秒内能过的挑战）。

**这些场景共同的模式**：挑战最多出现一次（在打开首页或打开书籍详情页时），且**在真机浏览器上总能在几秒内自动通过**；一旦通过，同一 session 内后续包括"点击下载"都不会再触发新的挑战、不会被拒绝。这跟服务器上 headless 浏览器"完全卡死在挑战页、从未通过"形成明显反差。

### 4. 已做过的实验（避免重复）

- 密码/账号问题、登录逻辑、cookie同步、搜索解析、性能优化 —— 均已解决（详见第五~九节）。
- 用httpx直连测试同一下载链接在v2-01/v2-02/v2-03下的响应差异（204 vs 503）—— 已完成，见第十、十一节。
- 遍历订阅里全部 9 个香港/香港WAP/新加坡节点，试图建立"节点 → 后端 → 响应"的完整映射 —— **未完成**，因为测试过程中本服务器到香港所有节点（含WAP）突然连接超时/SSL失败（本地网络问题，非z-library问题），只测完了3~4个节点的数据点（结论见第十一节）。
- 用真实的 CLI 完整下载流程重试 DK —— 已测试2次（消耗了两个账号各1次今日下载额度），均失败（0字节→playwright挑战超时）。**今日不建议再用真实下载流程重试，优先用"直接GET下载直链看响应头"的轻量方式验证，不消耗账号额度**。

### 5. 可能的后续方向（未验证，按优先级排列，供接手 agent 参考）

1. **headless →有头/真实浏览器指纹**：把 playwright 的 `_download_playwright` 改成非 headless 模式（`headless=False`，配合服务器上装 `xvfb-run` 提供虚拟显示），并加上更接近真实浏览器的 fingerprint（真实 UA、`navigator.webdriver=false`补丁等常见反检测手段）。这是最可能复现"手机能过、服务器不能过"差异的方向，因为已确认v2-02/v2-03的CF挑战本质是"检测出自动化环境故意给不可解的题"。
2. **重新做一次完整的"节点→后端"映射**，等本地网络到香港节点恢复稳定后，用第十一节里写的轻量GET测试方法（不消耗账号额度）把9个节点都测一遍，看是否有某几个节点稳定命中v2-01（那本书直接判死，没救）、哪些稳定命中v2-02/v2-03（挑战页，理论上有救）。
3. **确认v2-01 是否真的对DK这本书100%拒绝，不存在任何绕过方式**——如果确认是纯粹的地区/版权限制且无法绕过，那么"下载失败"里的一部分案例本质上是不可解的（站方限制），后续应聚焦在"如何避免落到v2-01"或"如何检测到204后自动换节点重试（而不是一直重试同一个大概率还是204的节点）"。
4. 观察 `bsrv` cookie 与后端路由的关系：目前证据显示同一个 `bsrv` cookie值在不同出口IP下会被路由到不同后端，说明**负载均衡是按出口IP（或IP+cookie联合）hash 的，不是纯粹按cookie**。如果能确认具体的哈希规则，也许能主动挑选/构造能命中v2-02/v2-03（而非v2-01）的节点组合。

### 6. 关键上下文（接手时直接可用）

- 测试用书：《DK魔法百科》，下载直链 `https://zh.z-library.sk/dl/XBen3lGmYw`（详情页可能因缓存等给出不同 hash，需要时重新搜索确认）。
- 对照书（已知能稳定下载）：《威卡魔法》。
- 账号：账号池见本地 `accounts.yaml`（不提交到仓库，用 `zlib add-account` 添加）。**账号额度会因调试消耗，请谨慎使用真实下载流程测试。**
- 代理节点清单：订阅共30个节点，HK/HK-WAP/SG 相关节点名见 `data/mihomo_run/sub.yaml`（缓存文件，24小时有效，`config.yaml`里`subscription_cache_hours`控制）。
- 已保存的登录态：`data/session.json`（含cookie，可直接用于轻量GET测试，避免重新登录）。
- 代理管理：`ProxyManager.switch_node(name)` 可直接切换mihomo当前出口节点（见`src/zlibrary/proxy_manager.py`），配合 `httpx.Client(proxy="http://127.0.0.1:7890", cookies=..., timeout=..)` 直接GET下载直链看`x-zbackend`响应头，是目前最省资源(不消耗账号额度)的验证方式。

---

## 十三、2026-08-09 结案：找到真正根因，前面十二节的多个结论被推翻

接手后重新做了一轮实证排查，**推翻了第九~十二节的核心结论**。此前一直在"反爬/ Cloudflare /节点 IP / WAP变体 / 后端路由"方向上找原因，但这些都不是主因。真正的原因是三个彼此独立的问题，且都是本工具自己的问题，不是站点限制。

### 根因 1（决定性）：「有些书能下、有些书不能下」是**选错了记录**，跟网络、IP、浏览器指纹完全无关

同一本书在 z-library 上常有**多条记录**（不同上传者/版本/文件），其中一部分记录的文件在站点侧已失效，对这些记录的 `/dl/` 会返回 `204 No Content`。

实测搜索 `DK魔法百科` 返回 2 条记录，用同一个账号、同一个 cookie、同一个出口节点、同一时刻逐条探测：

| # | 标题 | 大小 | 标题匹配度 | 评分 | `/dl/` 响应 |
|---|---|---|---|---|---|
| 1 | DK魔法百科 (魔法、巫術與神祕史…) A History of Magic… | 47.98 MB | 50（近似） | 5.0 | **302 → CDN，可下载** |
| 2 | DK魔法百科 | 6.24 MB | **100（完全匹配）** | 4.9 | **204，被拒** |

而原来的选书逻辑是「先筛 `match_score == 100`，再取评分最高」——**恰好每次都选中那条死记录 (#2)，而真正能下载的 #1 因为标题只算"近似匹配"，从来没被尝试过**。所以这本书在服务器上"永远"下不了，而用户在手机上是人工浏览、点了能下的那一条，自然就成功了。这就是"手机能下、服务器不能下"的全部原因。

推翻的旧结论：
- ❌ 第十节"DK 被z-library 后端按版权/地区主动拒绝，任何客户端都无法绕过" —— **错**。同一本书另一条记录随时可下，`204` 只代表**这一条记录的文件没了**，不是这本书被封。
- ❌ 第十一节"后端路由 v2-01/02/03 才是决定下载成败的开关" —— **错**。实测在 `v2-02` 上，死记录稳定 204、活记录稳定 302，**同一后端上两条记录表现相反**，所以后端不是变量，记录才是。
- ❌ 第十一节"WAP 节点可能是关键差异" —— 与下载成败无关。
- ❌ `Referer` 也无关：用「详情页作 Referer + 完整浏览器导航头」、「无 Referer」、「站点根作 Referer」三种方式请求同一个活记录的 `/dl/`，**全部 302 成功**。

**修复**：新增候选列表机制。`_rank_candidates()` 把搜索结果按「匹配度 → 格式偏好 → 评分」排成候选队列，`_do_download()` 逐条尝试，某条返回 204（`SiteRejected`）就换下一条，而不是在死记录上反复重试或换账号。

### 根因 2（决定性）：那个"过不去的挑战"其实是**纯 SHA1 工作量证明，0.05 秒就能算出来**

第九节记录的"headless 浏览器等 90 秒也过不去、大概率是专门检测自动化环境给了算不出解的挑战"这个判断是**错的**。把503挑战页（9592 字节）扒下来反混淆后发现：

- 它**不是 Cloudflare**。响应头是 `server: nginx` + `x-zproxy: front-proxy`，是 z-library 自己的前置代理。
- 算法完全公开在页面里，等价于：

```js
const c  = '<40位大写hex挑战串>';
const n1 = parseInt('0x' + c[0]);        // 取挑战串首字符做字节下标
for (let i = 0; ; i++)
    if (sha1_bytes(c + i)[n1] === 0xB0 && sha1_bytes(c + i)[n1+1] === 0x0B) {
        cookie('c_token', c + i); cookie('c_time',耗时秒); location.reload();
    }
```

- 它**不检测 UA / webdriver / 浏览器指纹**，纯算力题，期望 2^16 次哈希。
- 反向验证：把之前 `data/session.json` 里已存的 `c_token=476DCBD42A4DCFD94C5AECB6AA62E442084AAA53` + `2524` 代入，`SHA1` 的第 4、5 字节正好是 `0xB00x0B` —— 算法推导完全正确。
- 实测求解耗时 **0.02~0.05 秒**（Python 单线程）。

至于为什么无头浏览器过不去：页面 PoW 循环每 5 万次迭代 `await new Promise(r=>setTimeout(r))`，在无头/后台环境下这个让出会被严重节流，所以它是**卡在节流上**，不是"解不出"。

**修复**：新增 `src/zlibrary/challenge.py`，自己解PoW。`_request()` 现在对**任意路径**（首页/搜索/详情页/`/dl/`）遇到挑战页都会自动解题、写 `c_token` cookie、重放请求，**完全不需要浏览器**。playwright兜底路径里的 `_wait_cf()` 也从"干等"改成"我们算好了塞给浏览器再 reload"。

### 根因 3：mihomo 生成的配置**丢掉了订阅里的 DNS 段**，导致"所有节点全部超时"

`generate_config()` 只把订阅的 `proxies` 抄进新配置，`dns` 段整段丢弃。后果：

- 本机对 `z-library.sk` 的 DNS 是**被投毒**的，实测系统解析返回 Facebook 的地址（`31.13.76.99` / `2a03:2880:f10d:183:face:b00c:0:25de`，`face:b00c` 是 Facebook 的标志性网段），而且每次查询返回的假地址还不一样。用干净 DoH（Google / Cloudflare 结果一致）查到的真实地址是 `179.43.175.250`。
- mihomo作为 HTTP 代理收到 `CONNECT zh.z-library.sk:443` 后要自己解析域名，用系统 resolver 就把流量发往假地址 → 表现为**全部 30 个节点测速超时 / TLS handshake timeout**，看起来像"节点全挂了"，其实节点好得很（同一节点访问 `gstatic.com/generate_204` 1.1 秒返回 204）。
- 订阅的 `dns.nameserver-policy` 还给节点自己的域名指定了专用 DoH，丢掉它连节点域名都可能解析错（实测 `t.cnmjcn.cyou` 被系统 DNS 解析成 `93.46.8.90`，这是个典型的投毒地址）。

**修复**：`generate_config()` 现在写入 `enhanced-mode: fake-ip` 的 DNS 段（域名不在本地做真实解析，直接透传给出口节点，从根上免疫本地投毒），并保留订阅自带的 `nameserver-policy`。

### 顺带修掉的其它真实问题

| # | 问题 | 影响 | 修复 |
|---|---|---|---|
| 16 | `_parse_dl_link()` 取「页面里第一个 `/dl/` 链接」 | 详情页除本书外还渲染"相关推荐"卡片，实测一个详情页有 **3 个不同的 `/dl/`**，可能取到**别的书**的下载链接| 改为优先用详情页主下载按钮专属选择器 `a.addDownloadedBook` / `a.dlButton`精确定位本书 |
| 17 | 下载失败也`mark_used()` 扣额度 | 第九、十一节反复吐槽的"调试烧光额度"就是它。原注释说是"防死循环"，但重试本来就有上限 | 只在真正下载成功时才计数。修复后实测 4 轮失败消耗 **0** 次额度 |
| 18 | `check_direct()` 只看 `status_code < 500` | DNS 被投毒时若假服务器回个 4xx，会被判成"直连可用"，于是整条链路都不走代理、全部请求打向错误的服务器 | 校验响应内容确实来自 z-library（认`x-zbackend`/`x-zproxy` 头或站点特征文案） |
| 19 |节点测速用 `https://zh.z-library.sk` 当目标 | z-library 会先回 503 挑战页且响应慢，**把 20 个存活节点全判成不可达** | 测速改用 `gstatic.com/generate_204` 只判"节点通不通"；能否访问 z-library 由客户端在真实请求时判断并自动换节点 |
| 20 | `httpx` 统一用一个 30s 超时 | 挂掉的节点要等满 30s 才换下一个，30 个节点能拖十几分钟 | 拆开：`connect=8s` 快速失败换节点，读取仍给足30s（下载大文件需要） |
| 21 | 订阅里的"伪节点"当真节点用 | `剩余流量：40.37GB`/`套餐到期：长期有效`/`过滤掉15条线路` 这类公告牌条目指向真实服务器，会被选中当出口，还白占测速时间 | `generate_config()` 过滤掉（30 → 27 个真节点） |
| 22 | `session.json` 记的账号和站点实际登录的账号不一致 | 实测本地记的邮箱和站点 `/profile` 显示的实际账号不一致。额度会记到错误的账号头上 | 复用登录态时用`current_account_email()` 以站点为准核对并纠正 |
| 23 | `httpx` 只发 UA/Accept/Accept-Language 三个头、且走 HTTP/1.1 | UA 自称 Chrome 却不带 `sec-ch-ua*`/`sec-fetch-*`、还用 HTTP/1.1，是明显的自动化特征 | 补齐 Chrome 请求头集合 + 启用 HTTP/2 |
| 24 | 节点轮换一轮后就彻底放弃 | 线路抖动是常态，几十秒前不通的节点可能已恢复 | 轮换一圈无果后清空记录再试一轮 |
| 25 | `_download_httpx()` 先 `raise_for_status()` 再看正文 | **浏览器校验页是带着 503 状态码下发的**，先 `raise_for_status` 就把它当普通服务端错误抛掉了，下载端点的挑战永远没机会解（日志里表现为"下载端点 HTTP 503"） | 改成先判挑战页、解题重试，`raise_for_status()` 放到之后 |
| 26 | `current_account_email()` 取页面里第一个邮箱 | 取到页脚客服地址 `support@z-lib.fm`，误判"站点登录的是另一个账号" | 跳过 `z-lib`/`z-library` 域名的地址 |
| 27 | 下载传输中断留下半截文件 | 流式写盘中途断开，残留不完整文件 | 传输异常时 `dest.unlink(missing_ok=True)` |
| 28 | 节点存活 ≠ 能访问 z-library | 实测 31/31 节点访问 `gstatic` 全部正常（最快 68ms），但多数节点到 z-library 全是 TLS handshake timeout | 选完最快节点后用 `_ensure_site_reachable()` 真的验一次 z-library，不行就换，提前挑出好节点 |

### 现在的完整下载策略

```
搜索 → 排出候选队列（匹配度 → 格式偏好 → 评分）
  ↓ 对每个候选:
  取详情页里本书自己的 /dl/ 链接
  ↓ GET /dl/
  ├─ 503 挑战页  → 自己解 SHA1 PoW（0.05s）写 c_token，重放          [自动通过]
  ├─ 传输层报错  → 换出口节点重试（最多 8 次，connect 超时 8s）        [自动恢复]
  ├─ 返回 HTML   → 清 bsrv 换后端 + 换节点重试                        [自动恢复]
  ├─ 204 / 0字节 → 换后端确认一次，仍拒则判定「这条记录的文件已失效」，
  │                **换下一个候选记录**（不换账号、不烧额度）           [关键修复]
  └─ 302 → CDN   → 落盘，成功才计账号额度
```

### 验证结果

- ✅ **PoW 求解**：反向验证历史 `c_token` 通过；在真实链路里反复解出 `i=17060/ 18749 / 20523 / 40157 / 104455 / 126925` 等，耗时 **0.00~0.11秒**，解完 `/profile`、搜索、详情页请求全部正常返回。**不需要任何浏览器。**
- ✅ **`zlib search sapiens`** → 51 条结果，完全匹配标记正确。
- ✅ **候选回退机制跑通，DK魔法百科下载成功**（这本书此前在服务器上从未成功过）。完整日志实录：
  ```
  ✓ 当前节点可访问Z-Library: 新加坡-优化2-Gemini-GPT
  ✓ 复用已保存的登录态（免登录）
  解析出 2 本书 → ✓ 完全匹配: 《DK魔法百科》评分 4.9
  下载 (第 1/3 轮): /dl/xBonOWEk6j
    → 被当前后端拒绝(204)，换后端确认一次
  下载 (第 2/3 轮): /dl/xBonOWEk6j → 仍 204
    → 候选 1 被站点拒绝（该记录的文件已失效）
  → 换下一个候选版本重试: 《DK魔法百科 (魔法、巫術與神祕史…)》（PDF 47.98 MB）
    已解出浏览器校验挑战: i=104455，耗时 0.08s
    下载端点通过浏览器校验，重试下载
    开始落盘 (content-type=application/pdf)
  下载完成: DK魔法百科 … .pdf (47.98 MB)
  账号 xxx@qq.com 今日下载 2/10
  ```
  落盘校验：`50307822` 字节，文件头 `%PDF-1.7`、文件尾 `%%EOF`，**完整有效**。
- ✅ **DK 那条活记录确认可下载**：`302 → https://dln1.ncdn.ec/books-files/...`，三种 Referer 组合（详情页 / 无 / 站点根）**全部成功**。
- ✅ **额度记账**：连续约 10 轮包含大量失败的测试期间，`downloads_today` 全程保持不变；只有最后那次**真正下载成功**才+1（1/10 → 2/10）。修复前每轮失败都会 +1~+4。
- ✅ **DNS 修复效果**：节点可达性从 **0/30**恢复到 **31/31 存活**。

### 遗留/ 注意

- **本机到 z-library 的线路时段性极不稳定，这是独立于本工具的环境问题。** 同一晚出现过"20 个节点可达 → 十几分钟后全部 TLS handshake timeout → 又恢复"。同一时刻同一节点访问 `gstatic.com` 稳定 1 秒内返回 204、`example.com`/`github.com`/`wikipedia.org` 全部 200，说明**只有到 z-library 这条路径**有问题（不是节点挂了、也不是 DNS）。线路整体不通的窗口内只能等，工具会用"connect 8s 快速失败 + 换节点 + 轮换一圈后重试"尽量扛过去，并给出明确的"这不是账号问题"提示而不是误导性的登录失败报错。
- `format_preference` 在 `download()` 里不参与文件名/格式决策（格式由记录本身决定），它现在的作用体现在候选排序上。
- 挑战解出的 `c_token` 不与 IP 绑定（纯 PoW），所以换出口节点不会导致校验失效——这点比 Cloudflare的 `cf_clearance` 友好得多。
- `tests/test_challenge.py` 是离线回归测试（不需要网络），内置了站点真实下发过的 `c_token` 作为基准：**如果站点改了 PoW 算法，这个测试会第一时间失败**，是最省事的预警。
- 注意 `tests/test_local.py` 会用 mock 订阅覆盖 `data/mihomo_run/config.yaml`（3 个假节点）。跑过它之后下次 `zlib` 调用会自动从真实 `sub.yaml` 重新生成，不影响使用。

## 十四、2026-08-10 新增：识别「站点搜索服务临时故障」，不再误报成"未找到相关书籍"

用户反馈：站点当前能打开、能登录，但搜索任何关键词都提示 "search service temporary
unavailable"。这是**站点后端的间歇性故障**，跟具体某本书、代理节点、账号都无关，但此前
的代码会把它和"真的没搜到这本书"混为一谈——两者在`search()` 层面表现完全一样：
`_parse_search()` 解析出 0 个 `z-bookcard`。

抓包实测确认：这种故障下站点返回 **HTTP 200** + 完整页面框架，结果区域只有一句
`<div class="cBox1">Search service temporary unavailable!</div>`，没有任何 `z-bookcard`。
更麻烦的是，原逻辑一旦 httpx 拿到 0 结果就会自动回退 playwright 重试一次——但playwright
看到的是**同一个站点后端**，同样会命中这句提示，纯粹多等 60~90 秒后一样失败，属于"死等"。

**修复**：
-新增 `SearchServiceUnavailable` 异常，`_parse_search()` 检测到该文案时立即抛出（区别于
  真正的0结果——真0结果不含这句文案，仍返回`[]`，正常提示"未找到相关书籍"）。
- `search()` 遇到这个异常不再回退 playwright（换浏览器也没用，白等）。
- CLI 层 `_search_with_status()`：只做一次 5 秒后的快速重试（过滤掉几秒钟的抖动），仍不行
  就明确告知"这是站点侧问题，不是你的书/账号/节点的问题，通常几分钟到几十分钟内自行恢复"。

验证：`tests/test_search_unavailable.py` 离线单测（真实故障片段 / 正常有结果 / 真的0结果
三种场景都不误判）；线上复现时端到端实测，从登录完成到给出准确诊断只用了 6 秒
（而不是死等 60~90 秒后才含糊报"未找到"）。

## 十五、2026-08-10 打包分发：解决"先鸡先蛋"、清理敏感信息、补齐安装脚本

用户要把工具打包给别人用，提出一个关键问题：**mihomo 的下载本身有"先鸡先蛋"问题**——
国内大部分网络正是因为连不上 GitHub 才需要这个代理工具，但装好代理之前又恰恰连不上
GitHub 去下载 mihomo。用户的方案：随包自带一份 mihomo，装好后用它连订阅起代理，
再用这条已经打通的代理线路去 GitHub 升级到最新版。这个方案是合理的，照此实现：

### mihomo 引导链路重构（`proxy_manager.py`）
- `ensure_binary()` 现在优先找`vendor/mihomo-linux-{arch}.gz`（随包自带），本地解压，
  **全程不联网**；只有当前架构没有预置包时才回退联网下载（GitHub + 两个镜像，原有逻辑）。
- 新增 `upgrade_binary(proxy_url)`：经**已经验证过能访问 Z-Library 的代理线路**去连
  GitHub 检查/下载最新版本，替换二进制后自动重启 mihomo 进程。这条线路大概率也能到
  GitHub，不依赖用户网络本身能直连——即"先用旧版本把代理跑起来，再用这条代理换新版本"。
- 新增 CLI 命令 `zlib upgrade-mihomo`；`zlib status` 增加显示当前 mihomo 版本
  （`binary_version()`，解析 `mihomo -v` 输出）。
- 架构判断 `_mihomo_arch()`：`x86_64→amd64`、`aarch64→arm64`，跟mihomo release 资产
  命名对齐（原来硬编码 `amd64`，现在用检测到的架构，为后续加更多架构预置包留了口子）。

验证：临时删除本地 `data/mihomo`，调用 `ensure_binary()`，确认**完全不触网**、仅用
`vendor/mihomo-linux-amd64.gz`恢复出与原文件字节级一致（sha256 相同）的可执行文件，
`binary_version()` 正确解析出 `v1.19.29`；另在一个完全独立的临时目录（rsync 排除
`.venv`/`data`/真实配置，模拟"刚 clone 下来"的状态）跑通 `install.sh` 全流程（建虚拟环境
→ 装依赖 → 装playwright chromium → 生成配置模板），并验证 `ensure_binary()` 在新路径下
同样能找到 `vendor/` 正确引导，证明路径解析（`project_root()`）在不同工作目录下都正确。

### 敏感信息清理（打包为公开仓库前必须做）
`accounts.yaml`（真实邮箱+密码）、`config.yaml`（真实订阅token）、`clash-config.yaml`
（解析后的真实节点列表，等价于泄露订阅内容）、`data/`（真实登录 cookie、运行期缓存）
一律加入 `.gitignore`，不提交。改为提供 `config.example.yaml`/`accounts.example.yaml`
模板，`install.sh` 首次运行时自动从模板复制生成。`DEV.md` 里此前记录调试过程时明文写入
的真实邮箱、QQ号、密码全部替换为占位符（内容/结论不受影响，只脱敏）。

### 新增 `install.sh`
职责边界很清楚：只管Python 环境（建venv、装依赖、装 playwright chromium 可选失败不阻塞）
和配置模板生成，**不管mihomo**——mihomo 的随包引导是程序自己的逻辑（`ensure_binary()`），
不需要装的时候单独处理，用户第一次跑 `zlib` 命令时自动透明完成。

### 目录整理
- 根目录里散落的 `mihomo-linux-amd64-v1.19.29.gz`移入 `vendor/mihomo-linux-amd64.gz`
  （去掉文件名里的版本号，版本单独记在 `vendor/MIHOMO_VERSION`，避免每次升级预置包都要
  改代码里的文件名匹配逻辑）。
- `data/mock_sub.yaml`（测试用假订阅fixture，非真实节点）移到 `tests/fixtures/`，
  这样 `data/` 可以完整地整体 gitignore，不用为了保留这一个无害文件单独开洞。
- 清掉 `src/zlib.egg-info/`、`__pycache__/`（构建产物，`pip install -e .` 会自动重新生成）。

## 十六、2026-08-10 端口冲突规避：自动挑选不常见空闲端口，不再硬编默认端口

打包给别人用后暴露了一个新问题：本工具用的 mihomo **只服务于本工具自己**，跟用户
机器上可能已经在跑的另一个 mihomo/clash（很多人本来就常驻一个梯子）是两个完全独立
的进程，但此前配置里硬编码的 `http_port: 7890` / `socks_port: 7891` / `api_port: 9090`
恰好是 clash 系工具最常用的默认端口——如果用户机器上已经有一个 clash 在跑，这三个端口
大概率被占用，会导致 mihomo 启动失败或者（更隐蔽地）意外抢占/干扰对方。

### 方案：候选端口列表 + 探测 + 持久化

`proxy_manager.py` 新增四组写死的候选端口（每类5个，故意用不常见的高位端口，
四类互不重叠，避开7890/7891/9090 这些默认值）：

```python
_HTTP_PORT_CANDIDATES= [17890, 27890, 37890, 47890, 57890]
_SOCKS_PORT_CANDIDATES = [17891, 27891, 37891, 47891, 57891]
_API_PORT_CANDIDATES   = [17892, 27892, 37892, 47892, 57892]
_DNS_PORT_CANDIDATES   = [17893, 27893, 37893, 47893, 57893]
```

新增 `ensure_ports()`：对每一类端口，用 `socket.bind()`（而非 `connect()`——bind 才能
真正证明"这个端口现在能被我占住"，connect 失败只能证明"没人在监听"）逐个探测，
候选顺序是「用户在 config.yaml 里显式配置的值（若有，最高优先级）→ 上次持久化选中的值
（若仍空闲，尽量让端口在多次重启之间保持稳定）→ 内置候选列表」，选中第一个探测通过的。
四类全部占满才报错，报错信息里明确提示可以在 config.yaml 手动指定端口自救。

结果持久化进 `state.json` 的 `ports` 字段（与已有的 `node` 字段共享同一个文件）。
这里顺手修了一个潜在 bug：原来的 `_save_state()` 是整体覆盖写入，`ensure_ports()`
存的 `ports` 和 `rotate_node()`/`select_best()` 存的 `node` 是两个独立调用方各自
维护的字段，谁后调用就会把对方刚存的字段整个抹掉；改成先读旧内容再 `dict.update()`
合并后再写回。

`MihomoConfig` 里 `http_port`/`socks_port`/`api_port`/`dns_port` 四个字段全部改成
`int | None = None`（原来是必填的 int），语义变成"留空 = 交给自动挑选"；
`config.example.yaml`/`config.yaml` 相应把固定端口那几行注释掉，只在文档里提示
"有特殊需求要固定端口才需要手动填"。

### 调用时机的两个细节坑

1. **端口探测必须在确认"当前没有正在运行的实例"之后才做**，否则会把自己已经占着的
   端口误判成"被占用、需要换"。所以`ensure_ports()` 只在 `start()` 内、`is_running()`
   判定为 False 之后才调用；而 `__init__` 里单独有一个轻量的 `_load_persisted_ports()`
   （只读取 state.json，不做任何探测），确保 `zlib status`/`zlib stop` 这类不经过
   `start()` 的命令，也能知道去问哪个端口才能找到已经在跑的实例，而不是拿
   config.yaml 里的默认值（现在是 None）去问一个必然错误的端口、把"在运行"误判成
   "未运行"。
2. **`setup_and_select_best()` 原来的调用顺序是"生成配置→启动"**，但生成配置这一步
   要往yaml 里写 `mixed-port`/`socks-port`/`external-controller` 等，必须用解析后的
   端口——而端口解析这时候还没发生（在 `start()`内部才做）。改成端口解析和生成配置都
   挪到 `start()` 内部顺序执行（`ensure_ports() → ensure_binary() → generate_config()`
   → 启动进程），`setup_and_select_best()` 直接调 `start()`，不再在外面单独调一次
   `generate_config()`（避免用尚未解析的陈旧端口生成一份马上被扔掉的配置）。

### 验证
- 离线验证候选跳过逻辑：故意用 `socket.bind()` 占住 http 的前2 个候选端口和 api的
  第 1 个候选端口，调用 `ensure_ports()`，确认自动跳到下一个空闲候选（http 选中第3
  候选、api 选中第 2 候选），且新建的 `ProxyManager` 实例能从持久化状态正确读回同一
  组端口。
- 真实端到端验证：先手动结束残留的 mihomo 进程、清空 `state.json` 里的端口记录，
  模拟"全新首次运行"，执行 `zlib search`，日志确认：`本地端口: http=17890 socks=17891
  api=17892 dns=17893`（全部命中第一候选，因为环境干净），随后完整走通登录+搜索51条
  结果；`zlib status` 也正确报告出这组端口，而不是旧的 7890/9090。
