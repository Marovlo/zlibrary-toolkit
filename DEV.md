# Z-Library 一键工具 开发手册

> 本文档记录设计决策、关键技术、和有实测数据支撑的踩坑结论。已被推翻的中间猜测
> 不保留过程，只保留最终结论；纯粹是bug修复的琐碎记录合并成列表。

---

## 一、项目目标与架构

全自动 Z-Library 工具，全链路：

```
找官网(低优) → 测连通 → 选最优代理 → 优先账号登录(回退匿名) → 搜索 → 下载
```

核心痛点：官网多变 + 国内需代理 + 站点反爬/限额 + DNS投毒。

### 模块划分

```
src/zlibrary/
├── cli.py              # CLI 入口（search/download/set-subscription/status/stop/logout/add-account/upgrade-mihomo/help）
├── config.py            # 配置加载
├── subscription.py      # 订阅拉取 + 解析
├── proxy_manager.py     # mihomo 全生命周期（引导/端口选择/升级/测速选优/节点轮换）
├── challenge.py         # 站点浏览器校验（SHA-1 PoW）求解
├── site_finder.py       # [低优] web搜索官网，结果缓存
├── site_checker.py      # 连通性检测（校验响应确实来自 Z-Library）
├── accounts.py          # 账密管理器：轮换、次数跟踪
└── client.py             # Z-Library 客户端：登录/搜索/下载
```

### 关键设计：减少访问次数

搜索一次即拿到下载所需全部字段（book id/hash/下载链接），用户选定后直接下载，
不二次搜索。全程只访问 Z-Library：搜索 1次 + 下载 1 次（+ 可能的登录 1 次）。

---

## 二、当前下载/账号策略（2026-08-10 最新，之前版本已过时）

1. **优先账号，回退匿名**：`accounts.yaml` 中有今日额度未用尽的账号 → 优先登录用
   账号下载（体验最稳，不受出口IP每日限额影响）。未配置账号或账号额度都用尽 →
   自动回退匿名下载（不消耗账号额度，但受出口IP每日限额限制，见五.3）。
   `--anonymous` 强制匿名，跳过账号。
2. **默认交互式选择，不自动下载**：`download` 命令默认列出候选（含年份/大小/评分）
   供手动选择序号；`-y` 才自动下载排序最优的候选。
3. **候选排序**：标题匹配度（完全100> 前缀90 > 包含50）→ 格式偏好 → 评分，
   **不参与 size**（见六.7结论）。排前面的候选若被站点拒绝（204，文件已失效）
   自动换下一个候选，不换账号/不换节点。
4. **`zlib set-subscription <链接>`**：设置/更新代理订阅，自动验证格式+连通性
   才写入配置，避免坏链接覆盖能用的订阅。
5. **`zlib help`**：详细说明（账号策略、IP限额、候选排序规则、常见问题）。

---

## 三、关键技术

### 1. mihomo (Clash.Meta) 控制
- 随包自带二进制（`vendor/mihomo-linux-{arch}.gz`），首次本地解压，不联网
  （解决"没代理连不上GitHub下代理"的先鸡先蛋问题）；`zlib upgrade-mihomo` 经
  已验证的代理线路去 GitHub 换最新版。
- 非 TUN 模式，只开本地 HTTP/SOCKS/控制API/DNS 端口，系统其他程序不受影响。
- 本地端口自动选择：四类端口默认不写死 7890/7891/9090（易与用户机器上已有的
  clash/mihomo 冲突），从内置不常见高位端口候选里探测选一个当前空闲的，选中结果
  持久化（`state.json`），跨重启尽量保持稳定。
- **DNS 必须显式配置 `enhanced-mode: fake-ip`**：本机对 `z-library.sk` 的系统 DNS
  被投毒（实测解析到Facebook 的 `31.13.x.x`/`face:b00c` 网段，且每次查询返回的
  假地址还不一样）。mihomo 若沿用系统 resolver 解析目标域名，会把流量送到假IP，
  表现为"所有节点测速全部超时/TLS handshake timeout"（看起来像节点全挂了，实际
  节点是好的——同一节点访问 `gstatic.com` 正常）。fake-ip 模式让域名透传给出口节点
  自己解析，从根上免疫本地投毒；同时保留订阅自带的 `nameserver-policy`（给节点
  自己的域名指定专用 DoH，丢了连节点域名都可能解析错）。
- 节点存活测速故意不用 z-library 本身（它响应慢+先返回挑战页，会把大量其实可用
  的节点误判为不可达），只用 `gstatic.com/generate_204` 判断"节点通不通"；能否
  访问 z-library 由客户端在真实请求时判断并自动换节点（`rotate_node()`，按顺序
  找下一个"当前实测可用"的节点，轮换过的记录避免来回换同一批，一圈无果清空重试）。

### 2. 站点反爬机制：不是 Cloudflare，是可秒解的 SHA1 PoW
实测把挑战页（503，约9592字节）反混淆后发现：响应头是`server: nginx` +
`x-zproxy: front-proxy`（**不是 Cloudflare**），算法完全公开在页面里：

```js
const c= '<40位大写hex挑战串>';
const n1 = parseInt('0x' + c[0]);
for (let i = 0; ; i++)
    if (sha1_bytes(c + i)[n1] === 0xB0 && sha1_bytes(c + i)[n1+1] === 0x0B) {
        cookie('c_token', c + i); location.reload();
    }
```
不检测 UA/webdriver/浏览器指纹，纯算力题（期望 2^16 次哈希），Python 单线程求解
耗时 0.02~0.11秒。`challenge.py` 自动求解，`client.py._request()` 对任意路径
（首页/搜索/详情页/`/dl/`）遇到挑战页都会自动解题、写 `c_token` cookie、重放请求，
**完全不需要浏览器**。（此前怀疑"headless浏览器被专门检测卡住"是错误方向，见六.2。）

### 3. 账号轮换
`accounts.yaml`：`email/password/downloads_today/last_reset_date`，每日按本地
日期重置。选号策略：优先 `downloads_today < 限额` 的；**只在真正下载成功时**才
`mark_used()`；登录后从 `/profile` 读真实剩余次数校正本地计数。登录态持久化到
`data/session.json`，下次调用先轻量校验（GET `/profile`）是否仍有效，避免每次
都重新走账号轮换登录。

### 4. 官网查找（低优）
搜索引擎查 "z-library official"，结果缓存 `data/known_sites.json`，失败不影响
主流程，当前默认已知 `zh.z-library.sk`。

---

## 四、配置文件

真实的 `config.yaml`/`accounts.yaml`（含真实token/密码）已 gitignore。模板见
`config.example.yaml`/`accounts.example.yaml`，`install.sh` 首次运行自动从模板
生成。端口/订阅相关字段留空即可自动处理（端口自动选择见三.1；订阅可用
`zlib set-subscription` 设置并自动验证，或手动编辑 `subscription_url`）。

---

## 五、关键实测结论（保留数据，删除过程）

### 1. 「有些书能下、有些不能下」的真正原因：选错了记录（204），不是账号/网络/浏览器问题

同一本书常有多条记录，部分记录文件已在站点侧失效，对这些记录的 `/dl/` 直接返回
`204 No Content`。实测同一本书两条记录、同账号同节点同时刻探测：

| 记录 | 标题匹配度 | 评分 | `/dl/` 响应 |
|---|---|---|---|
| DK魔法百科（带副标题后缀） | 50~90（前缀/近似） | 5.0 | 302→CDN，可下载 |
| DK魔法百科（完全同名） | 100（完全匹配） | 4.9 | 204，被拒 |

原逻辑"只选完全匹配里评分最高的"会精确命中死记录，永远下不了这本书。**修复**：
`_rank_candidates()` 把结果排成候选队列（匹配度→格式偏好→评分），`_do_download()`
逐条尝试，某条 204 就换下一条，不换账号、不烧额度。

曾经因此走的弯路（已被推翻，不复述过程）：怀疑是Cloudflare 检测自动化环境、
怀疑是后端负载均衡路由(v2-01/02/03)决定成败、怀疑是代理节点"WAP变体"差异——
实测证明这些都只是"碰巧命中同一条死记录"的伴生现象，**记录本身是否失效才是
唯一决定因素**，同一后端上另一条活记录始终能下载。

### 2. 出口IP每日匿名限额：跟"记录失效(204)"是完全不同的另一个维度

匿名（无cookie）请求下载链接，若当前出口IP当天匿名下载次数超限，站点返回
**HTTP 200 + 完整HTML**（不是204/503），主体是：

```html
<section class="download-limits-error">
  <h1>每日限额已用完</h1>
  <article>在过去的24小时内，从您的IP下载的次数超过{IP}。请登录您的帐户...</article>
</section>
```

检测用 CSS 类名 `download-limits-error`（不受站点语言设置影响）。这个限制按
**出口IP**算，跟具体某本书/某条记录无关，换候选记录没用；只有换一个还有额度的
出口IP，或登录账号（不受此限额影响）才能绕开。`client.py` 检测到该标记抛出
`IpQuotaExceeded`；`cli.py` 的匿名下载路径撞到后自动换节点重试，换完全部节点仍
不行才报错要求登录（实测4个不同出口IP样本可能同时都超限，属正常现象）。

### 3. DNS 投毒与端口冲突（数据见三.1/三.4，此处仅记录验证结果）
- 修复 DNS fake-ip 后节点可达性从 **0/30 恢复到 31/31**。
- 端口自动选择：干净环境下全部命中第一候选（17890/17891/17892/17893）；故意
  占用前几个候选后能正确跳到下一个空闲候选，持久化状态跨实例读回一致。

### 4. 挑战页求解验证
反向验证：把已保存的历史 `c_token`（`476DCBD4...`+`2524`）代入算法，`SHA1` 结果
第4/5字节确实是 `0xB0`/`0x0B`，证明算法推导正确；真实链路里反复解出
`i=17060/104455/126925` 等，耗时 0.00~0.11 秒，无需浏览器。

### 5. 完整下载验证（DK魔法百科，历史上多次卡住的书）
```
✓ 完全匹配的死记录 → 两轮204被拒 → 换下一候选(PDF 47.98MB)
  → 解出PoW挑战(0.08s) → 下载完成，落盘50,307,822 字节
  →文件头%PDF-1.7、文件尾%%EOF 校验通过
```
额度记账：约10轮包含大量失败的调试期间 `downloads_today` 保持不变，只有最终
真正成功那次才+1（修复前每轮失败都会误加）。

### 6. 匿名优先下载全流程验证（威卡魔法）
```
候选1(完全匹配死记录)两轮204→换候选2→匿名请求命中IpQuotaExceeded
→ 自动登录(复用已存登录态)→重新下载成功，82.45MB PDF完整落盘
→ 账号额度从1/10正确增至2/10（两次204拒绝未消耗额度）
```

### 7. 候选排序是否要纳入文件大小（size）——已讨论，结论：不纳入

曾经争论"同格式下文件越大是否越完整"vs"小文件可能是原生排版、大文件可能是
扫描件"，双方都只有直觉、无数据支撑。**当前代码从始至终未把size 纳入排序**
（只用匹配度→格式偏好→评分），这是当前定案的行为，不是待办。若未来要引入
质量信号，建议方向是直接检测 PDF 文字层（`pypdf.extract_text()` 判断是否为
扫描件）而不是猜测 size 与质量的相关性，需积累 5~10 本已知多记录书籍的样本
数据再决定是否改排序权重——**目前无新数据，维持不纳入 size 的现状**。

---

## 六、历次修复的具体 bug（不再展开踩坑过程，仅存档）

| 问题 | 修复 |
|---|---|
| 下载内容 0 字节也当成功 | 校验响应体非空/非HTML挑战页才算成功 |
| 下载失败也扣账号额度 | 只在真正下载成功时`mark_used()` |
| 详情页取到"相关推荐"卡片的下载链接 | 改用主下载按钮专属选择器精确定位本书 |
| `check_direct()` 只看状态码 | 校验响应确实来自 z-library（认特征头/文案） |
| 节点测速用z-library 本身当目标 | 改用轻量`gstatic.com/generate_204` |
| httpx 统一用30s超时 | 拆开：connect 8s快速失败换节点，读取仍给30s |
| 订阅里的"伪节点"公告牌当真节点 | 生成配置时按名称关键词过滤 |
| 登录态记的账号跟站点实际登录的不一致 | 用 `current_account_email()` 以站点为准核对纠正 |
| httpx 请求头过于精简（明显自动化特征） | 补齐 Chrome 请求头集合 + 启用 HTTP/2 |
| 节点轮换一轮无果就放弃 | 轮换一圈无果清空记录再试一轮 |
| 下载端点先 `raise_for_status()` 再看正文 | 挑战页带503状态码下发，改成先判挑战页解题 |
| `current_account_email()` 取到客服邮箱 | 跳过 `z-lib`/`z-library` 域名的地址 |
| 传输中断留下半截文件 | 异常时 `dest.unlink()` |
| 站点搜索服务临时故障被误报"未找到" | 检测特征文案抛`SearchServiceUnavailable`，不回退playwright死等，5秒快速重试后如实告知 |
| 账号轮换死循环 | 记录本次会话已失败账号，全部失败后立即报错，不无限重试 |
| 包名与标准库 `zlib` 冲突 | 包目录改名 `src/zlibrary`，CLI 命令名不变 |

---

## 七、遗留/注意事项

- 本机到 z-library 的线路时段性不稳定是独立于本工具的环境问题（同一节点访问
  `gstatic.com` 稳定，访问 z-library 时段性 TLS handshake timeout）。工具用
  "connect 8s快速失败+换节点+轮换一圈后重试"尽量扛过去，并给出明确提示而不是
  误导性的登录失败报错。
- `tests/test_local.py` 会用 mock 订阅覆盖 `data/mihomo_run/config.yaml`（3个假
  节点），跑过后下次 `zlib` 调用会自动用真实订阅重新生成，不影响使用。
- `tests/test_challenge.py` 内置站点真实下发过的 `c_token` 作基准做离线回归——
  如果站点改了 PoW 算法，这个测试会第一时间失败，是最省事的预警。
- 候选排序的 size 争论见五.7，明确不纳入，非待办。

## 八、webapp 域名访问（HTTPS）—— 备案拦截与 DNS-01 绕过

### 背景
给 webapp 面板加 `https://zlib.marovlo.cloud` 域名访问，Caddy 反代到 `127.0.0.1:8765`。

### 踩坑：大陆服务器未备案，HTTP-01/TLS-ALPN-01 全被拦截
腾讯云大陆机器（81.70.166.231），`*.marovlo.cloud` 未做 ICP 备案。ACME 默认的
HTTP-01（80）和 TLS-ALPN-01（443）验证全被拦截：
- HTTP-01：Let's Encrypt 访问 `http://zlib.marovlo.cloud/.well-known/...` 被腾讯云
  按 Host 头重定向到 `dnspod.qcloud.com/static/webblock.html`，返回 403。
- TLS-ALPN-01：443 上 `Connection reset by peer`。
- 本机 `curl -H "Host: zlib.marovlo.cloud" http://127.0.0.1/` 正常（308），证明拦截
  只针对**外部入站**流量，本机回环不受影响——这是诊断备案拦截的关键判据。

同一个拦截也导致 `img.marovlo.cloud` 证书过期 3.9 天续不下来（一直 queuing for
renewal 但 HTTP-01 永远失败）。

### 解法：DNS-01 验证（绕过备案）
验证不走 80/443，而是 Caddy 直接调腾讯云 DNS API 写
`_acme-challenge.<域名>` 的 TXT 记录，Let's Encrypt 查 DNS 即放行。备案拦截碰不到
DNS 记录。

#### 重编译带 DNS 插件的 Caddy
标准版 Caddy 不含 DNS provider 插件，需用 xcaddy 重编译。服务器没装 Go，临时下载
到 /tmp 用完不污染系统。编译要访问 GitHub，**复用 webapp 健康监控已起的 mihomo**
（混合端口 `127.0.0.1:17890`）做代理，不单独起、不影响其他应用：

```bash
# 临时 Go（用完即弃，不装到系统）
curl -fsSLO https://golang.google.cn/dl/go1.23.4.linux-amd64.tar.gz
tar -xzf go1.23.4.linux-amd64.tar.gz
export GOROOT=/tmp/go PATH=$GOROOT/bin:$PATH GOPATH=/tmp/gopath

# xcaddy + 带 tencentcloud 插件编译（走 mihomo 代理）
export HTTPS_PROXY=http://127.0.0.1:17890 HTTP_PROXY=http://127.0.0.1:17890
go install github.com/caddyserver/xcaddy/cmd/xcaddy@latest
/tmp/gopath/bin/xcaddy build --with github.com/caddy-dns/tencentcloud --output /tmp/caddy-dns

# 替换系统 caddy（原版备份在 /usr/bin/caddy.bak.YYYYMMDD）
sudo systemctl stop caddy
sudo cp /usr/bin/caddy /usr/bin/caddy.bak.$(date +%Y%m%d)
sudo cp /tmp/caddy-dns /usr/bin/caddy
sudo systemctl start caddy
caddy list-modules | grep tencentcloud   # 验证插件装入
```

> **不用 `caddy-dns/dnspod` 插件**：它跟新版 libdns（Record 结构改了 ID/Name/Value
> 字段）不兼容，编译报 `record.ID undefined`。`tencentcloud` 插件用腾讯云 API
> 的 SecretId/SecretKey（不是 DNSPod 的 ID,Token 格式），在
> https://console.cloud.tencent.com/cam/capi 创建。

#### Caddyfile 全局 acme_dns
在全局块加 `acme_dns tencentcloud`，**所有站点**（zlib + img）都走 DNS-01，
顺带解决了 img 续不下来的老问题：

```caddyfile
{
    email admin@marovlo.cloud
    acme_dns tencentcloud {
        secret_id {env.TENCENTCLOUD_SECRET_ID}
        secret_key {env.TENCENTCLOUD_SECRET_KEY}
    }
}

zlib.marovlo.cloud {
    reverse_proxy 127.0.0.1:8765
}
```

#### 凭证用 systemd drop-in 注入（不写进 Caddyfile）
`/etc/systemd/system/caddy.service.d/dns-env.conf`：
```ini
[Service]
Environment=TENCENTCLOUD_SECRET_ID=AKID...
Environment=TENCENTCLOUD_SECRET_KEY=...
```
改完 `sudo systemctl daemon-reload && sudo systemctl restart caddy`。

#### Let's Encrypt 失败次数限流
HTTP-01 阶段连续失败 5 次后，LE 对该 identifier 限流 1 小时
（`too many failed authorizations`，`retry after <UTC时间>`）。期间 Caddy 自动转
ZeroSSL 兜底，但 ZeroSSL 的 DNS-01 实测会卡住不回包。**等 LE 限流解除后
`restart caddy` 触发重试，DNS-01 秒过**（10 秒内 `authorization finalized: valid`
+ `certificate obtained successfully`）。

### 最终架构
```
公网 → Caddy(443, DNS-01 自动证书) → 127.0.0.1:8765 (zlib-web.service) → FastAPI
```
两个 systemd 服务：`zlib-web`（面板）、`caddy`（反代+证书）。面板 8765 只绑
127.0.0.1，不对外暴露；80/443 放行给 Caddy。

### 遗留
- 面板无鉴权（用户明确接受风险）。需要时在 Caddyfile 的 zlib 块加 `basic_auth`
  或 `remote_ip` 白名单，不用改 Python 代码。
- `/tmp/go` 和 `/tmp/gopath` 是编译用临时目录，可手动清理（不影响运行的 caddy）。
