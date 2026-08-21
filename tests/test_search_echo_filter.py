"""搜索回声伪卡过滤离线自测。"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from zlibrary.client import BookResult, ZLibraryClient, filter_search_results, is_echo_pseudo_result


def _book(**changes) -> BookResult:
    values = {
        "title": "低空技术与工程导论",
        "author": "低空技术与工程导论",
        "year": "2024",
        "language": "english",
        "format": "pdf",
        "size": "8 MB",
        "rating": 4.9,
        "book_id": "420948",
        "hash": "",
        "detail_url": "https://zlib.bz/book/a/title.html",
        "download_url": "https://zlib.bz/dl/a",
        "isbn": "",
        "publisher": "低空技术与工程导论",
    }
    values.update(changes)
    return BookResult(**values)


def test_echo_card_is_filtered() -> None:
    card = _book()
    assert is_echo_pseudo_result(card, card.title)
    assert filter_search_results([card], card.title) == []


def test_empty_isbn_with_real_metadata_is_kept() -> None:
    card = _book(author="刘慈欣", publisher="重庆出版社", hash="abc123")
    assert not is_echo_pseudo_result(card, "低空技术与工程导论")
    assert filter_search_results([card], "低空技术与工程导论") == [card]


def test_title_match_alone_is_not_filtered() -> None:
    card = _book(author="刘慈欣", publisher="重庆出版社", isbn="9781234567890")
    assert not is_echo_pseudo_result(card, card.title)


def test_normalization_handles_case_punctuation_and_spaces() -> None:
    card = _book(title="低空技术与工程导论", author="低空技术与工程导论", publisher="低空技术与工程导论")
    assert is_echo_pseudo_result(card, "  低空技术与工程导论！ ")


def test_parser_reads_isbn_and_publisher() -> None:
    client = ZLibraryClient("https://example.test", None, "test")
    html = """
    <z-bookcard id="1" href="/book/a/title.html" download="/dl/a"
      year="2024" language="zh" extension="pdf" filesize="8 MB"
      isbn="9781234567890" publisher="出版社" termshash="abc">
      <div slot="title">标题</div><div slot="author">作者</div>
    </z-bookcard>
    """
    book = client._parse_search(html)[0]
    assert book.isbn == "9781234567890"
    assert book.publisher == "出版社"
    assert book.hash == "abc"


def test_search_filters_httpx_results_without_playwright_fallback() -> None:
    client = ZLibraryClient("https://example.test", None, "test")
    card = _book()
    with patch.object(client, "_search_httpx", return_value=[card]), patch.object(
        client, "_search_playwright", side_effect=AssertionError("不应回退 Playwright")
    ):
        assert client.search(card.title) == []


if __name__ == "__main__":
    test_echo_card_is_filtered()
    test_empty_isbn_with_real_metadata_is_kept()
    test_title_match_alone_is_not_filtered()
    test_normalization_handles_case_punctuation_and_spaces()
    test_parser_reads_isbn_and_publisher()
    test_search_filters_httpx_results_without_playwright_fallback()
    print("search echo filter 自测通过")
