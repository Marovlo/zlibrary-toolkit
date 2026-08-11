"""本地书库存档：sqlite 索引 + 文件落盘目录。

已经下载成功过的书（按 book_id+hash 判重）二次请求时直接从本地文件下发，不再
访问 Z-Library（对应需求：书本存档，二次下载走本地）。存储目录独立于 CLI 的
个人 download_dir（config.yaml），避免互相干扰；位于 `data/webapp/` 下，已被
仓库根目录 .gitignore 的 `data/` 规则整体忽略，无需额外配置。
"""
from __future__ import annotations

import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from zlibrary.config import project_root

_DB_LOCK = threading.Lock()


def data_dir() -> Path:
    d = project_root() / "data" / "webapp"
    d.mkdir(parents=True, exist_ok=True)
    return d


def library_dir() -> Path:
    d = data_dir() / "library"
    d.mkdir(parents=True, exist_ok=True)
    return d


def db_path() -> Path:
    return data_dir() / "archive.db"


@dataclass
class ArchivedBook:
    id: int
    book_id: str
    hash: str
    title: str
    author: str
    year: str
    language: str
    format: str
    size_bytes: int
    rating: float
    file_path: str
    downloaded_at: float

    def to_dict(self) -> dict:
        return {
            "id": self.id, "book_id": self.book_id, "hash": self.hash,
            "title": self.title, "author": self.author, "year": self.year,
            "language": self.language, "format": self.format,
            "size_bytes": self.size_bytes, "rating": self.rating,
            "downloaded_at": self.downloaded_at,
        }


_COLUMNS = ("id", "book_id", "hash", "title", "author", "year", "language",
            "format", "size_bytes", "rating", "file_path", "downloaded_at")
_SELECT = f"SELECT {', '.join(_COLUMNS)} FROM books"


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(db_path())
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS books (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            book_id TEXT NOT NULL,
            hash TEXT NOT NULL,
            title TEXT NOT NULL,
            author TEXT DEFAULT '',
            year TEXT DEFAULT '',
            language TEXT DEFAULT '',
            format TEXT DEFAULT '',
            size_bytes INTEGER DEFAULT 0,
            rating REAL DEFAULT 0,
            file_path TEXT NOT NULL,
            downloaded_at REAL NOT NULL,
            UNIQUE(book_id, hash)
        )
        """
    )
    return conn


def find_by_book(book_id: str, hash_: str) -> ArchivedBook | None:
    with _DB_LOCK, _conn() as conn:
        row = conn.execute(f"{_SELECT} WHERE book_id=? AND hash=?", (book_id, hash_)).fetchone()
    if not row:
        return None
    book = ArchivedBook(*row)
    # 文件可能被手动删除过，确认仍在磁盘上才算命中，否则要重新走网络下载
    if not Path(book.file_path).exists():
        return None
    return book


def add(book_id: str, hash_: str, title: str, author: str, year: str, language: str,
        fmt: str, file_path: Path, rating: float = 0.0) -> ArchivedBook:
    size_bytes = file_path.stat().st_size
    now = time.time()
    with _DB_LOCK, _conn() as conn:
        conn.execute(
            """
            INSERT INTO books
                (book_id, hash, title, author, year, language, format, size_bytes, rating, file_path, downloaded_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(book_id, hash) DO UPDATE SET
                title=excluded.title, author=excluded.author, year=excluded.year,
                language=excluded.language, format=excluded.format,
                size_bytes=excluded.size_bytes, rating=excluded.rating,
                file_path=excluded.file_path, downloaded_at=excluded.downloaded_at
            """,
            (book_id, hash_, title, author, year, language, fmt, size_bytes, rating,
             str(file_path), now),
        )
        conn.commit()
        row = conn.execute(f"{_SELECT} WHERE book_id=? AND hash=?", (book_id, hash_)).fetchone()
    return ArchivedBook(*row)


def list_all(query: str = "") -> list[ArchivedBook]:
    sql = _SELECT
    params: tuple = ()
    if query:
        sql += " WHERE title LIKE ? OR author LIKE ?"
        params = (f"%{query}%", f"%{query}%")
    sql += " ORDER BY downloaded_at DESC"
    with _DB_LOCK, _conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [ArchivedBook(*r) for r in rows]


def get(id_: int) -> ArchivedBook | None:
    with _DB_LOCK, _conn() as conn:
        row = conn.execute(f"{_SELECT} WHERE id=?", (id_,)).fetchone()
    return ArchivedBook(*row) if row else None


def delete(id_: int) -> bool:
    book = get(id_)
    if not book:
        return False
    with _DB_LOCK, _conn() as conn:
        conn.execute("DELETE FROM books WHERE id=?", (id_,))
        conn.commit()
    Path(book.file_path).unlink(missing_ok=True)
    return True


def safe_filename(name: str) -> str:
    name = re.sub(r"[^\w\s\-]+", "", name).strip().replace(" ", "_")
    return name or "book"
