from __future__ import annotations

from pydantic import BaseModel


class AccountInfo(BaseModel):
    email: str
    downloads_today: int
    limit: int
    remaining: int | None
    available: bool


class AddAccountRequest(BaseModel):
    email: str
    password: str


class SearchRequest(BaseModel):
    query: str
    page: int = 1
    account_email: str = ""  # "" / "anonymous" 表示匿名搜索
    force_refresh: bool = False  # True 时跳过缓存，强制发一次新搜索


class BookOut(BaseModel):
    title: str
    author: str
    year: str
    language: str
    format: str
    size: str
    rating: float
    book_id: str
    hash: str
    detail_url: str
    download_url: str = ""
    match_score: int = 0


class DownloadRequest(BaseModel):
    book_id: str
    hash: str
    title: str
    author: str = ""
    year: str = ""
    language: str = ""
    format: str = ""
    size: str = ""
    rating: float = 0.0
    detail_url: str = ""
    download_url: str = ""
    account_email: str = ""


class JobStatus(BaseModel):
    id: str
    status: str
    progress: float
    message: str
    error: str
    archived_id: int | None = None
    phase: str = ""  # downloading|uploading|done
    share_url: str | None = None


class ArchivedBookOut(BaseModel):
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
    downloaded_at: float
    share_url: str = ""


class StatusOut(BaseModel):
    # initializing | connecting | direct | proxy_ok | switching | unavailable
    status: str
    node: str | None = None
    site: str | None = None
    message: str = ""
    error: str | None = None
    checked_at: float = 0.0


class BaiduStatus(BaseModel):
    configured: bool       # baidu.yaml 是否已配置 cookies
    logged_in: bool        # BaiduPCS-Go 当前是否处于登录态
    account: str = ""      # who 命令返回的账号信息
    binary_version: str = ""


class AddBaiduRequest(BaseModel):
    cookies: str
