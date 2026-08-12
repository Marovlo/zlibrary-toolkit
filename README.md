# zlib — Z-Library 一键搜索下载工具

全自动：找官网(低优) → 测连通 → 选最优代理 → 登录 → 搜索 → 下载（账号自动轮换）。

## 安装

```bash
git clone <本仓库地址> zlibrary
cd zlibrary
./install.sh
```

`install.sh` 会自动创建虚拟环境、安装依赖、生成 `config.yaml`/`accounts.yaml`/`mail.yaml`
（从模板复制）。**mihomo 二进制不需要装**——本仓库自带 `vendor/` 目录下的预编译
二进制，首次运行 `zlib` 命令时会自动解压出来，全程不联网、不需要能访问 GitHub
（详见下方"mihomo 二进制"一节）。

## 配置

1. **config.yaml** — 主配置。`install.sh` 会从 `config.example.yaml` 生成，
   **必须编辑其中的 `subscription_url` 为你自己的订阅链接**才能使用。
2. **accounts.yaml** — Z-Library 账号池，`install.sh` 会生成一个空模板。
   推荐用命令添加已有账号（会先做真实登录测试，成功才写入，不会存入错误密码）：

```bash
source .venv/bin/activate
zlib add-account your_email@example.com your_password
```

   也可以用自己的 `@marovlo.cloud` Catch-all 邮箱自动注册并验证新账号：

```bash
zlib register-account
# 或指定邮箱本地部分：
zlib register-account --email test-001@marovlo.cloud
```

   注册命令会从根目录 `mail.yaml` 读取 QQ 邮箱 IMAP 配置，等待转发的确认码；
   不提供密码时可交互输入，直接回车则自动生成随机密码。只有注册、邮箱验证和
   最终登录都成功后才会写入 `accounts.yaml`。不自动绕过 CAPTCHA 或其他人机验证。

3. **mail.yaml** — QQ 邮箱 IMAP 配置，仅用于自动读取 `@marovlo.cloud` Catch-all
   转发的注册验证码。`install.sh` 会从 `mail-example.yaml` 生成空模板；请填写
   QQ 邮箱授权码（不是网页登录密码），并确保文件权限为 `600`。该文件已被 Git 忽略。

也可以添加多个账号（每号每日 10 本，自动轮换，额度用尽自动切下一个）。

## 使用

```bash
source .venv/bin/activate     # 或全程用 .venv/bin/zlib 完整路径，不激活也行

# 搜索（自动选代理 + 登录 + 搜索，结果含完整下载信息）
zlib search "三体"

# 搜索并下载（完全匹配自动下评分最高；否则列出让选）
zlib download "三体"
zlib download "三体" -y# 完全匹配直接下，不询问
```

详细日志加 `-v`。

## 全链路行为

1. 直连测试 Z-Library（会校验响应确实来自 Z-Library，防DNS 投毒误判），通则跳过代理；不通则启动 mihomo
2. mihomo拉取订阅 → 解析节点 → 测节点存活 → 再真的验一次「能否访问 Z-Library」→ 选定出口
3. 选可用账号登录（登录态本地持久化，下次免登录）
4. 搜索一次拿全信息（含 book id + hash + 下载直链）
5. 下载：自动解站点浏览器校验、失效记录自动换候选版本、线路抖动自动换节点

**mihomo 不影响系统**：只开本地几个端口（HTTP/SOCKS/控制API/DNS，见下方"本地端口"一节），不开 TUN，系统其他程序不受影响。

## 本地端口：自动挑选空闲端口，不跟你机器上已有的代理冲突

mihomo 需要占用几个本地端口（HTTP/SOCKS 混合端口、控制 API、DNS）。很多人机器上
本来就常驻另一个 mihomo/clash，如果这里硬编码 clash 系工具最常用的默认端口
7890/7891/9090，大概率会跟已有的那个冲突（启动失败，或更隐蔽地互相干扰）。

所以这四类端口默认都**不写死**：程序内置了四组互不重叠、专门避开默认值的候选高位端口，
启动时逐个探测哪个当前空闲就用哪个，选中的端口会持久化，只要它还空闲，下次启动继续用
同一个（减少不必要的变动）；四类端口的候选全部被占用才会报错，报错信息会提示可以在
`config.yaml` 里手动指定端口自救。想固定端口（比如要给其它程序对接）也可以在
`config.yaml` 里显式配置 `mihomo.http_port` 等字段，配置了就优先用它。

`zlib status` 会显示当前实际使用的端口。

## 为什么能「所有书都下得下来」

三个关键机制（踩坑细节见 [DEV.md](DEV.md) 第十三节）：

1. **自动通过站点的浏览器校验**。Z-Library 在缺少有效 `c_token` 时会对任何请求返回 `503` +
   "Checking your browser ..." 页面。它**不是 Cloudflare**，而是一道纯 SHA-1 工作量证明：
   暴力找出使 `SHA1(挑战串 + i)` 指定两个字节等于 `0xB0 0x0B` 的最小 `i`。
   `challenge.py` 直接算（**0.02~0.1秒**），不需要浏览器。
2. **失效记录自动换候选版本**。同一本书在站点上常有多条记录，部分记录的文件已失效，
   其 `/dl/` 返回 `204 No Content`。工具把搜索结果排成候选队列逐个尝试，
   遇 204 就换下一条——这是"有些书死活下不了"的主因（跟 IP/节点/浏览器指纹无关）。
3. **线路抖动自动换出口节点**。`connect` 超时单独设为 8 秒以便快速失败换节点，
   读取超时仍给足（下载大文件需要）；下载 CDN 也是独立主机，同样支持换节点重试。

下载失败**不会**扣账号额度，只有真正下载成功才计数。

站点搜索服务本身偶发临时故障时（返回 "Search service temporary unavailable!"），
会明确提示这是站点侧问题、与网络/账号/节点无关，不会误报成"未找到相关书籍"，
也不会死等浏览器回退（换浏览器一样会看到同样的故障提示）。

## 常用命令

```bash
# 注意：以下命令需先 source .venv/bin/activate，否则用 .venv/bin/zlib 完整路径
zlib status         # 查看后台代理节点 / 登录态 / mihomo 版本
zlib stop           # 停止后台代理
zlib logout         # 清除本地登录态
zlib add-account <email> [password]   # 先做真实登录测试，成功才写入账号池
zlib upgrade-mihomo # 经当前代理线路检查并升级 mihomo 到 GitHub 最新版
```

## mihomo 二进制：随包自带，解决"先鸡先蛋"问题

多数需要这个工具的网络环境，恰恰是因为连不上 GitHub 才需要代理；但装好代理之前
又连不上 GitHub 去下载 mihomo——这是个先鸡先蛋的死结。解法：

- **本仓库在 `vendor/mihomo-linux-amd64.gz` 自带一份预编译二进制**（当前
  `vendor/MIHOMO_VERSION` 记录的版本）。首次运行 `zlib` 时，`ensure_binary()`
  会直接从这个文件本地解压到 `data/mihomo`，**全程不联网**，自然也不需要能访问
  GitHub。
- 只有你的 CPU 架构不是 amd64（`vendor/` 里没有对应预编译包）时，才会回退到
  联网下载（GitHub 官方地址 + 两个镜像兜底），这种情况下第一次启动确实需要
  网络能直连其中至少一个地址。
- 随包版本可能不是最新的，但不影响正常使用。想升级时用 `zlib upgrade-mihomo`：
  这时代理已经跑起来了，命令会**经这条已经验证过能访问 Z-Library 的代理线路**去连
  GitHub 检查新版本——这条线路大概率也能到 GitHub，不再依赖你的网络本身能直连。

## 文件结构

```
src/zlibrary/
├── cli.py              # CLI 入口（search/download/status/stop/logout/add-account/upgrade-mihomo）
├── config.py            # 配置加载
├── subscription.py     # 订阅拉取 + 解析
├── proxy_manager.py    # mihomo 全生命周期（含随包引导 + 代理升级）+ 测速选优 + 节点轮换
├── challenge.py        # 站点浏览器校验（SHA-1 PoW）求解
├── site_finder.py      # [低优] web 搜官网
├── site_checker.py     # 连通性检测（校验响应确实来自 Z-Library）
├── accounts.py         # 账号轮换 + 次数跟踪
└── client.py           # Z-Library 客户端（登录/搜索/下载）

vendor/                 # 随包自带的 mihomo 二进制（gzip 压缩），解决安装时的"先鸡先蛋"问题
config.example.yaml     # 主配置模板（复制为 config.yaml 后填入自己的订阅链接）
accounts.example.yaml   # 账号池模板
install.sh              # 一键安装脚本
```

开发细节与踩坑见 [DEV.md](DEV.md)。
