"""challenge 模块的离线自测：不需要网络，验证 PoW 求解与挑战页解析。

历史真实数据来自实际抓到的挑战页与站点下发过的 c_token，可作为回归基准：
若站点改算法，这个测试会第一时间失败。
"""
import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from zlibrary import challenge

# 站点真实下发过的 c_token（40位挑战串 + 命中的 i）
REAL_TOKEN = "476DCBD42A4DCFD94C5AECB6AA62E442084AAA532524"
REAL_CHALLENGE, REAL_I = REAL_TOKEN[:40], "2524"

CHALLENGE_HTML = (
    "<!DOCTYPE html><html><head><title>Checking your browser ...</title></head><body>"
    "<script>const a0_0x2a54=['" + REAL_CHALLENGE + "','c_token=','array'];"
    "let n1=parseInt('0x'+c[0]);"
    "if((s[n1]===0xb0)&&(s[n1+0x1]===0xb)){document['cookie']='c_token='+c+i;}"
    "</script></body></html>"
)


def test_algorithm_matches_site() -> None:
    """反推的规则必须能解释站点真实给过的 c_token。"""
    digest = hashlib.sha1((REAL_CHALLENGE + REAL_I).encode()).digest()
    n1 = int(REAL_CHALLENGE[0], 16)
    assert (digest[n1], digest[n1 + 1]) == (0xB0, 0x0B), "PoW 规则与站点不一致"


def test_extract_and_solve() -> None:
    ch = challenge.extract(CHALLENGE_HTML)
    assert ch is not None, "未能从挑战页提取挑战参数"
    assert ch.token == REAL_CHALLENGE
    assert (ch.byte1, ch.byte2) == (0xB0, 0x0B)
    assert ch.index == int(REAL_CHALLENGE[0], 16)

    solved = challenge.solve(ch)
    assert solved is not None, "PoW 未解出"
    token, elapsed = solved
    # 页面 JS 取的是最小的 i，所以必须和站点给过的完全一致
    assert token == REAL_TOKEN, f"解出 {token}，期望 {REAL_TOKEN}"
    assert elapsed >= 0


def test_non_challenge_page_ignored() -> None:
    assert not challenge.looks_like_challenge("<html><body>正常书籍页</body></html>")
    assert challenge.extract("<html><body>正常书籍页</body></html>") is None


if __name__ == "__main__":
    test_algorithm_matches_site()
    test_extract_and_solve()
    test_non_challenge_page_ignored()
    print("✓ challenge 模块全部自测通过")
