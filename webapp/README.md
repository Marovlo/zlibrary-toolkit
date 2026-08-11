# ZLib私人 Web 面板

在现有 CLI 工具（`src/zlibrary`）基础上加的一个**私人使用**的网页面板：搜索、
下载、账号管理、本地书库存档。跟 CLI 完全解耦，`webapp/` 单独一个目录，不影响
`src/zlibrary` 的正常使用，只是 import 现有模块复用登录/搜索/下载/代理逻辑。

**面板本身没有登录/访问控制**（按你的要求，打开即可用，选账号或匿名）。这意味着
任何能连到 `服务器IP:端口` 的人都能搜索/下载/管理账号池——请自行用防火墙/安全组
把这个端口限制到你信任的 IP，或只在 VPN/内网环境下暴露，不要直接开放到公网。

## 快速开始

```bash
# 0) 前提：仓库根目录已跑过 ./install.sh（CLI 核心依赖 + .venv 已就位）

# 1) 装 web 依赖（fastapi/uvicorn）+ 构建前端（需要 Node.js 18+）
./webapp/install_web.sh

# 2) 启动（前台，Ctrl-C 退出，适合先试用看看效果）
./webapp/run.sh
# 浏览器访问 http://<服务器IP>:8765
```

体验没问题后，想让它开机自启、常驻后台，看下面「生产部署」一节（systemd，
默认 IP:端口明文访问；域名 HTTPS 为可选）。

## 目录结构

```
webapp/
├── backend/# FastAPI 后端，import zlibrary 包复用现有逻辑
│   └── app/
│       ├── main.py         # 入口：挂路由 + 托管前端构建产物 + 启动健康监控
│       ├── access.py       # 复用 cli._ensure_access 做直连/代理接入
│       ├── health.py       # 后台健康监控：启动即选优，空闲够久才用真实搜索复检
│       ├── search_cache.py # 搜索结果内存缓存（12h TTL），减少重复搜索
│       ├── sessions.py     # 每账号独立的登录态 cookie（免重复登录）
│       ├── jobs.py         # 下载任务：后台线程（分阶段状态提示） + 前端轮询
│       ├── archive.py      # 本地书库：sqlite 索引 + 文件目录
│       ├── accounts_store.py
│       ├── errors.py       # 异常 -> 用户友好提示（屏蔽代理/网络细节）
│       ├── schemas.py
│       └── routers/        # accounts / search / download / archive / status
├── frontend/# Vue3 + Vite 前端（搜索/书库/账号 三个页签）
├── install_web.sh       # 装 web 依赖（fastapi/uvicorn）+ 构建前端
├── logging_config.yaml  # uvicorn 日志格式（统一时间戳+logger名称+线程名，跟 CLI 风格一致）
├── Caddyfile            # 可选：域名 HTTPS 反代配置示例（需替换成你自己的已备案域名）
├── zlib-web.service     # systemd 服务单元示例（需按实际路径调整）
└── run.sh               # 启动（前后端同一端口）
```

## 安装说明补充

只克隆仓库用 CLI 的人**不需要**执行 `install_web.sh`——web 依赖（FastAPI/uvicorn）
在 `pyproject.toml` 的 `[project.optional-dependencies].web` 分组里，`pip install -e .`
装的是 CLI 核心依赖，不会连带装 web 依赖；前端的 `node_modules`/`npm` 也完全独立
在 `webapp/frontend/` 下，不影响 CLI。

`./webapp/run.sh` 默认监听 `0.0.0.0:8765`，可选环境变量：`ZLIB_WEB_HOST`
（默认 `0.0.0.0`）、`ZLIB_WEB_PORT`（默认 `8765`）。

## 生产部署：systemd（默认，IP:端口明文访问）

「快速开始」里的 `run.sh` 是前台运行，关掉终端就会停。想让它开机自启、常驻
后台，按下面步骤来即可——**默认就是明文 HTTP、直接 `http://<服务器IP>:8765`
访问，不需要域名、不需要 CDN**。

```bash
# 面板服务：装仓库自带的 unit（默认绑 0.0.0.0:8765，对外直接明文访问）
#    注意：unit 文件里 WorkingDirectory/ExecStart 路径写死为 /root/zlibrary，
#    如果你的仓库路径不同，先编辑 webapp/zlib-web.service 改成实际路径。
sudo cp webapp/zlib-web.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now zlib-web

# 看日志
sudo journalctl -u zlib-web -f        # 面板日志
```

完成后访问 `http://<服务器IP>:8765` 即可。

> **关于端口放行**：云服务器安全组需放行 **8765**（TCP，入站）。面板绑在
> `0.0.0.0`，明文 HTTP，无 TLS。

> **关于访问控制**：面板本身无登录/鉴权，挂到公网后任何知道 IP:端口的人都能
> 搜索/下载/管理账号池。如需限制，可在安全组里做 IP 白名单，或用 Caddy 反代并加
> `basic_auth`（见下一节）。

## 可选：用域名 + HTTPS（Caddy 反代）

如果你有自己的**已备案域名**，想用 `https://你的域名` 访问（而不是裸 IP:端口），
仓库自带 `webapp/Caddyfile` 可用 [Caddy](https://caddyserver.com/) 做反向代理，
自动申请并续期 Let's Encrypt 证书。下面以 `zlib.example.com` 为例，**请换成你
自己的域名**。注意：未备案域名在国内会被拦截，此方案仅适用于已备案域名。

### 前置：DNS 解析

在域名 DNS 管理面板加一条 **A 记录**：

| 字段     | 值                        |
| -------- | ------------------------- |
| 主机记录 | `zlib`                    |
| 记录类型 | `A`                       |
| 记录值   | 本机公网 IP               |
| TTL      | 默认即可                  |

加完后用 `dig zlib.example.com +short` 或 `nslookup zlib.example.com`
确认已解析到本机 IP 再继续（否则 Caddy 申请证书会失败并反复重试）。

### 安装 Caddy（Debian/Ubuntu，自带 systemd 服务）

```bash
sudo apt install -y debian-keyring debian-archive-keyring apt-transport-https
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
  | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
  | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt update && sudo apt install -y caddy
```

### 部署配置 + 启动

Caddy 用 systemd 常驻，开机自启、自动重启。

```bash
# 把 zlib 块**追加**到现有 Caddyfile（不要用 cp 覆盖！
#    机器上 Caddy 可能已在管其他站点，覆盖会丢掉它们）
#    把下面的 zlib.example.com 换成你自己的域名。
sudo tee -a /etc/caddy/Caddyfile > /dev/null << 'EOF'

# zlib 私人面板（由 webapp/Caddyfile 追加）
zlib.example.com {
	reverse_proxy 127.0.0.1:8765
}
EOF
sudo systemctl reload caddy

# 看日志
sudo journalctl -u caddy -f           # 证书申请 / 反代日志
```

完成后访问 `https://zlib.example.com`（换成你自己的域名）即可。

> `run.sh` 仍保留，用于开发/临时调试（前台跑，Ctrl-C 退出）；
> 生产常驻用上面的 systemd 服务，两者启动的是同一个 uvicorn，只是托管方式不同。

> **关于 80/443 端口**：云服务器需要在安全组放行 80 和 443（TCP）；
> 80 仅用于证书申请与跳转，443 是实际 HTTPS 流量。8765 仍是对外明文端口
> （除非把 unit 改回绑 127.0.0.1 只让本机 Caddy 转发）。

## 功能

1. **账号**：读写仓库根目录同一份 `accounts.yaml`（跟 CLI 共用账号池，含你已有的
   默认账号），列表显示每个账号今日已下载/剩余额度；可在页面里添加新账号（会先做
   一次真实登录测试，成功才保存，跟 `zlib add-account` 行为一致）。搜索/下载时可选
   "匿名" 或指定某个账号。
2. **搜索**：两级搜索，先本地后云端。点"搜索"先查本地书库（`/api/archive`，SQL
   LIKE 匹配标题/作者，几乎不耗时），命中就直接展示，标"直接下载"（不触网，秒开）；
   按钮同时变成"云端搜索"，需要你再点一次才会真正联网（复用 `client.search()`，
   展示标题/作者/年份/格式/大小/评分/匹配度，支持"加载更多"翻页，保持站点原始
   返回顺序不额外重新排序）。云端搜索耗时长时按钮旁会出现"取消"，见下面「交互
   模型与并发」一节。**暂不展示封面图片**（低优先级：z-library 的图片直链依赖
   当前不稳定的代理链路，一次搜索多张图片对本来就不稳的网络更不友好，后续如需
   要可以再评估）。
3. **下载**：点击某条搜索结果直接下载（同一本书站点上往往有多条记录/候选版本，
   网页上你能看到全部候选，某条候选的文件若已被站点判定失效会立刻报错，直接换
   下一条候选点即可——不会在同一条失效记录上重试浪费时间）。后台线程执行，前端
   轮询进度并展示分阶段提示（连接节点/账号 → 获取下载链接 → 下载中 xx% → 写入
   本地书库）；下载成功后自动写入本地书库存档，并**自动触发浏览器保存到本地**，
   无需再手动点击确认。
4. **书库存档**：已下载成功的书（按 book_id+hash 判重）落盘在 `data/webapp/library/`，
   sqlite 索引在 `data/webapp/archive.db`（都在已被 gitignore 的 `data/` 目录下）。
   同一本书二次点击下载会直接命中本地文件，不再访问 Z-Library。书库页可以浏览、
   下载、删除已存档的书。
5. **网络问题屏蔽**：所有底层异常（代理超时、节点抖动、Cloudflare 挑战等）在后端
   统一转成简单友好的提示（如"网络暂时不稳定，请稍后重试"），完整细节只记服务端
   日志，不透传给前端。web 服务自身的监听端口是独立的 TCP socket，跟 mihomo 代理
   完全无关（mihomo 只作为出站代理显式传给访问 Z-Library 的 httpx client，从不
   设置全局代理环境变量），因此 mihomo 的运行状态不会影响这个面板本身的可访问性。
6. **代理健康监控**：进程启动后立刻在后台线程开始选优（直连检测/起代理/测速，
   近距离节点——港澎台/新马/日韩/印度——优先，远距离节点如欧美只作兜底），选定后
   额外走一次**真实搜索流程**（不是简单 ping）做最终确认。之后稳定态下**基本不
   再主动折腾**：只有"空闲超过1小时（没有真实搜索/下载操作）且距上次检查也超过
   1小时"才会用真实搜索流程复检一次；异常态下才 8 秒一次快速重试，且单次探测
   失败会先原地重试几次排除线路瞬时抖动，仍不行才真的判定节点不可用去切换。
   顶部状态徽标展示 `正在探测中`/`直连可用`/`代理: <节点名>`/`节点异常正在切换`/
   `暂无可用节点`。
7. **搜索结果缓存**：同一 (查询词, 页码) 在 12 小时内命中缓存直接返回，不用每次
   都重新搜索一遍（省下 PoW 解题 + 网络往返的时间）。**下载链接不缓存**——`/dl/`
   短码本就随会话/后端变化，每次下载都会重新访问详情页解析，缓存搜索结果跟下载
   新鲜度互不影响。想跳过缓存强制重搜时，勾选搜索框下方的"忽略缓存重新搜索"。

## 交互模型与并发

面板上各个操作之间会不会互相卡住、冲突？梳理如下（webapp 引入的是 CLI 原本没有
的"多个操作同时进行"场景，下面两处并发安全隐患已经在核心层修复，不只是文档层面
"应该没问题"）：

- **请求级并发**：FastAPI 对同步接口用线程池处理，一个耗时的 `/api/search`
  请求占着一个线程慢慢跑，完全不会阻塞其他请求（比如同时点的 `/api/download`）
  ——两者在不同线程里独立执行。已用多线程压测验证：耗时 ~9s 的云端搜索进行中，
  同时发起的下载请求几毫秒内就返回，互不影响。
- **下载本地已存档的书 = 零网络开销**：`jobs.py` 处理下载任务时第一步就查本地库
  （`archive.find_by_book()`），命中直接返回成功——**完全不创建网络客户端、不碰
  代理**。所以"正在跑一个云端搜索，同时想直接下载书库里的书"这个场景没有任何
  冲突，两者压根不共享资源。
- **取消云端搜索**：搜索按钮变成"云端搜索"后，进行中会出现"取消"按钮，点击用
  `AbortController` 立刻中止前端等待、恢复界面可操作。**后端那次请求会在线程池
  里自然跑完**（结果直接丢弃，不影响任何东西）——已验证：客户端提前断开/超时
  不会导致后端异常或服务不稳定，只是那个线程多花几秒钱把该做的事做完。之所以
  不做"真正中止后端那次网络请求"，是因为 CLI 核心的 httpx 调用是同步阻塞的，
  要支持协作式取消需要较大改动核心请求逻辑，收益（省几秒 CPU/网络）跟改动量不
  成正比，对私人面板这种量级没必要。
- **已修复的并发安全隐患**（webapp 让 CLI 核心第一次在多线程下跑，暴露出两处
  需要加锁的地方）：
  - `accounts.py` 的 `mark_used()`（下载成功后给账号计数 +1）加了锁——两个下载
    任务如果碰巧同时用同一个账号完成，之前有极小概率漏计数，现已用 200 并发
    压力测试验证计数不再丢失。
  - `proxy_manager.py` 的 `rotate_node()`（换出口节点）加了锁——真实请求线程和
    后台健康监控线程可能同时触发换节点，不加锁会互相踩踏对方选的节点；现在换
    节点操作是串行化的，同一时刻只有一个线程在真正切换。

## 日志

`run.sh` 用 `logging_config.yaml` 统一了 uvicorn 自身日志和 CLI 核心模块
（`zlibrary.*`）复用产生的日志格式：统一带时间戳，且格式里同时带 **logger 名称**
和 **线程名**，两者结合起来才能看清一条日志到底来自哪个模块/哪次操作：

```
11:03:38 INFO    [health-monitor] zlib: ✓ 当前节点可访问 Z-Library: 新加坡-优化2-Gemini-GPT
11:03:40 INFO    [download_0] zlibrary.client: 因「ConnectTimeout」切换出口节点 -> 新加坡-优化3-Gemini
```

- `logger 名称`（如 `zlibrary.client`）只能看出"哪个模块的代码"打的日志，但
  健康监控和真实下载都会调用到 `zlibrary.*` 里同样的函数，单看这个字段区分不了
  是哪一次操作触发的。
- `[线程名]` 能精确区分来源：`health-monitor` 固定是后台健康监控线程自己的探测
  （每次探测只是发一次轻量请求，跟真实下载无关）；`download_N` 是某次真实下载
  任务的线程（`jobs.py` 的 `thread_name_prefix="download"`）；处理搜索等同步
  HTTP 接口的是 FastAPI/Starlette 线程池的工作线程。

比如上面例子里，两条日志分别是健康监控和一次真实下载各自独立探测同一个节点，
时间点不同、结果不同（线路抖动），不是矛盾或 bug。

## 开发模式（前端热更新）

```bash
# 先启动后端（另开一个终端）
./webapp/run.sh
# 再启动前端 dev server（默认 5173 端口，自动把 /api 转发到后端 8765）
cd webapp/frontend && npm run dev
```
