from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from .. import archive as archive_store
from ..schemas import ArchivedBookOut

router = APIRouter(prefix="/api/archive", tags=["archive"])


@router.get("", response_model=list[ArchivedBookOut])
def list_archive(q: str = "") -> list[ArchivedBookOut]:
    return [ArchivedBookOut(**b.to_dict()) for b in archive_store.list_all(q)]


@router.get("/{book_id}/file")
def download_file(book_id: int):
    book = archive_store.get(book_id)
    if not book or not Path(book.file_path).exists():
        raise HTTPException(404, "文件不存在")
    filename = f"{book.title}.{book.format or 'epub'}"
    return FileResponse(book.file_path, filename=filename)


@router.delete("/{book_id}")
def delete_archive(book_id: int):
    ok = archive_store.delete(book_id)
    if not ok:
        raise HTTPException(404, "记录不存在")
    return {"ok": True}
