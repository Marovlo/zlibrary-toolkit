"""SearchServiceUnavailable 检测的离线自测：不需要网络。

用实际抓到的两种真实响应片段（正常有结果 / 站点搜索服务故障）验证 `_parse_search`
能正确区分"真的没搜到"和"站点搜索服务本身故障"，不会把后者误判成前者。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from zlibrary.client import SearchServiceUnavailable, ZLibraryClient

# 2026-08-10 实测抓到的真实故障页片段（结果区域整个被替换成这一句提示，无任何 z-bookcard）
UNAVAILABLE_HTML = """
<html><body>
<div id="searchResultBox">
<div class="cBox1">Search service temporary unavailable!</div>
</div>
</body></html>
"""

# 正常有结果的最小片段
NORMAL_HTML_WITH_RESULTS = """
<html><body>
<z-bookcard id="123" href="/book/123/abcd/title.html" title="Some Book"
    author="Some Author" year="2020" language="english" extension="pdf"
    filesize="10MB" rating="4.5" termshash="abcd"></z-bookcard>
</body></html>
"""

# 真的搜不到任何书（正常空结果，不应触发 SearchServiceUnavailable）
NORMAL_HTML_NO_RESULTS = """
<html><body>
<div class="notFound">未找到符合条件的图书</div>
</body></html>
"""


def _dummy_client() -> ZLibraryClient:
    return ZLibraryClient(site="https://zh.z-library.sk", proxy_url=None,
                           user_agent="test", rotate_node=None)


def test_detects_search_service_unavailable() -> None:
    c = _dummy_client()
    try:
        c._parse_search(UNAVAILABLE_HTML)
        raise AssertionError("应抛出 SearchServiceUnavailable")
    except SearchServiceUnavailable:
        pass


def test_normal_results_not_misclassified() -> None:
    c = _dummy_client()
    books = c._parse_search(NORMAL_HTML_WITH_RESULTS)
    assert len(books) == 1, f"应解析出 1 本书，实际 {len(books)}"


def test_genuine_empty_result_not_misclassified() -> None:
    """真的搜不到书时（0 结果但没有故障文案）不应误报成站点故障。"""
    c = _dummy_client()
    books = c._parse_search(NORMAL_HTML_NO_RESULTS)
    assert books == []


if __name__ == "__main__":
    test_detects_search_service_unavailable()
    test_normal_results_not_misclassified()
    test_genuine_empty_result_not_misclassified()
    print("✓ SearchServiceUnavailable 检测自测全部通过")
