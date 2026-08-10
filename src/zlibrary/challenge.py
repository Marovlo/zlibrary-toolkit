"""z-library 浏览器校验挑战（"Checking your browser ..."）的求解。

## 这是什么

z-library 的前置代理（响应头 `x-zproxy: front-proxy`，`server: nginx`，**不是 Cloudflare**）
在缺少有效 `c_token` cookie 时，会对请求返回 `503` + 一个约 9.6KB 的
`<title>Checking your browser ...</title>` 页面。页面里内联了一份 js-sha1 和一段被
混淆的校验逻辑，去混淆后等价于：

```js
const c  = '<40位大写 hex 挑战串>';   // 页面内联
const n1 = parseInt('0x' + c[0]);      // 取挑战串首个 hex 字符作为字节下标
for (let i = 0; ; i++) {
    const s = sha1.array(c + i);       // SHA1 原始 20 字节
    if (s[n1] === 0xB0 && s[n1 + 1] === 0x0B) {
        document.cookie = 'c_token=' + c + i + '; path=/';
        document.cookie = 'c_time=' + 耗时秒 + '; path=/';
        window.location.reload();
        break;
    }
}
```

也就是一道**纯 SHA1 工作量证明（PoW）**：暴力搜索最小的整数 `i`，使
`SHA1(c + str(i))` 的第 `n1`、`n1+1` 个字节恰为 `0xB0`、`0x0B`。期望约 2^16 次哈希，
Python 里 0.05~0.5 秒即可算完。

## 为什么这件事很关键

它完全不依赖浏览器环境（不检测 webdriver / 不看 UA / 不需要 JS 运行时特征），
所以**用 httpx + 本模块就能通过**，不需要 playwright，也不存在"headless 被识别所以
算不出解"的问题。此前把它误判为 Cloudflare 的反自动化挑战、并因此认为
"某些书只能人工浏览器下载"，是方向性的误判。
"""
from __future__ import annotations

import hashlib
import logging
import re
import time
from dataclasses import dataclass

log = logging.getLogger(__name__)

# 挑战页特征。标题固定为 "Checking your browser ..."，正文里一定出现 c_token 赋值逻辑。
_MARKERS = ("checking your browser", "c_token=", "just a moment")

_CHALLENGE_RE = re.compile(r"\b([0-9A-F]{40})\b")
# 形如 (s[n1]===0xb0)&&(s[n1+0x1]===0xb)，字节值理论上可能被服务端调整，优先从页面里读
_TARGET_RE = re.compile(
    r"===\s*0x([0-9a-fA-F]{1,2})\s*\)\s*&&\s*\(\s*[^)]*?\+\s*0x1\s*\]\s*===\s*0x([0-9a-fA-F]{1,2})\s*\)"
)

DEFAULT_TARGET = (0xB0, 0x0B)
# 2^16 期望值，给 400倍余量；正常几万次就命中，跑满也只是几秒
MAX_ITERATIONS = 30_000_000


@dataclass
class Challenge:
    token: str            # 40 位 hex 挑战串
    byte1: int            # 期望的第 n1 个字节
    byte2: int            # 期望的第 n1+1 个字节

    @property
    def index(self) -> int:
        """字节下标 n1 = 挑战串首个 hex 字符的数值。"""
        return int(self.token[0], 16)


def looks_like_challenge(html: str) -> bool:
    """判断响应正文是否是浏览器校验挑战页。"""
    if not html:
        return False
    low = html[:6000].lower()
    return any(m in low for m in _MARKERS)


def extract(html: str) -> Challenge | None:
    """从挑战页 HTML 中提取挑战参数。解析不出返回 None。"""
    if not looks_like_challenge(html):
        return None
    m = _CHALLENGE_RE.search(html)
    if not m:
        log.warning("挑战页里找不到 40 位 hex 挑战串，站点可能改版")
        return None
    t = _TARGET_RE.search(html)
    if t:
        b1, b2 = int(t.group(1), 16), int(t.group(2), 16)
    else:
        b1, b2 = DEFAULT_TARGET
        log.debug("未能从挑战页解析目标字节，沿用默认 %#04x/%#04x", b1, b2)
    return Challenge(token=m.group(1), byte1=b1, byte2=b2)


def solve(ch: Challenge, max_iterations: int = MAX_ITERATIONS) -> tuple[str, float] | None:
    """求解 PoW。返回 (c_token 值, 耗时秒)；超出迭代上限返回 None。

    c_token 的值就是「挑战串 + 命中的 i」拼接（与页面 JS 完全一致）。
    """
    n1, b1, b2 = ch.index, ch.byte1, ch.byte2
    base = ch.token.encode()
    sha1 = hashlib.sha1
    t0 = time.perf_counter()
    for i in range(max_iterations):
        digest = sha1(base + str(i).encode()).digest()
        if digest[n1] == b1 and digest[n1 + 1] == b2:
            elapsed = time.perf_counter() - t0
            log.info("已解出浏览器校验挑战: i=%d，耗时 %.2fs", i, elapsed)
            return f"{ch.token}{i}", elapsed
    log.error("浏览器校验挑战在 %d 次迭代内未解出（算法可能已变更）", max_iterations)
    return None


def solve_html(html: str) -> tuple[str, float] | None:
    """便捷入口：直接从挑战页 HTML 解出 (c_token, 耗时秒)。"""
    ch = extract(html)
    return solve(ch) if ch else None
