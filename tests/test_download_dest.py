"""下载落盘路径的离线自测：响应头文件名不得写出目标目录。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from zlibrary.client import _safe_download_dest


def test_content_disposition_stays_inside_dest_dir(tmp_path: Path | None = None) -> None:
    dest_dir = tmp_path or Path("/tmp/zlib-dest-test")
    dest_dir.mkdir(parents=True, exist_ok=True)
    fallback = dest_dir / "book.epub"

    ok = _safe_download_dest(dest_dir, "三体.pdf", fallback)
    assert ok == dest_dir / "三体.pdf"
    assert ok.resolve().is_relative_to(dest_dir.resolve())

    escaped = _safe_download_dest(dest_dir, "../../etc/passwd", fallback)
    assert escaped == dest_dir / "passwd"
    assert escaped.resolve().is_relative_to(dest_dir.resolve())

    win = _safe_download_dest(dest_dir, "..\\..\\etc\\shadow", fallback)
    assert win == dest_dir / "shadow"
    assert win.resolve().is_relative_to(dest_dir.resolve())

    encoded = _safe_download_dest(dest_dir, "book%20title.epub", fallback)
    assert encoded == dest_dir / "book title.epub"

    assert _safe_download_dest(dest_dir, "..", fallback) == fallback
    assert _safe_download_dest(dest_dir, "", fallback) == fallback


if __name__ == "__main__":
    test_content_disposition_stays_inside_dest_dir()
    print("download dest 自测通过")
