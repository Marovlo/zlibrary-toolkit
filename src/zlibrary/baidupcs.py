"""百度网盘集成：BaiduPCS-Go 二进制管理 + 上传/分享。

随包自带 BaiduPCS-Go 二进制（`vendor/baidupcs/`），首次使用时解压到
`data/baidupcs/BaiduPCS-Go`，模式与 mihomo 完全一致（vendor 引导 → data 运行）。
登录凭证（cookies）存储在 `baidu.yaml`（项目根，与 accounts.yaml 同级、同样 gitignore）。
所有分享默认永久（`-period 0`）。

BaiduPCS-Go 的登录态存在 `~/.config/MyBaiduPCS-Go/`，CLI 与 webapp 同用户运行时共享，
不必每次调用都重新登录；session 失效时用 baidu.yaml 里存的 cookies 重新登录。
"""
from __future__ import annotations

import logging
import os
import platform
import re
import shutil
import stat
import subprocess
import threading
import zipfile
from pathlib import Path

import httpx
import yaml

from .config import Config, project_root

log = logging.getLogger(__name__)

BAIDUPCS_REPO = "qjfoidnh/BaiduPCS-Go"
BAIDUPCS_FALLBACK_VERSION = "v4.0.1"


def _arch() -> str:
    """当前系统架构 -> BaiduPCS-Go release 资产名里用的架构标识。"""
    m = platform.machine().lower()
    if m in ("x86_64", "amd64"):
        return "amd64"
    if m in ("aarch64", "arm64"):
        return "arm64"
    if m.startswith("armv7") or m == "armv7l":
        return "armv7"
    return m


# ---------- cookies 持久化（baidu.yaml，仿 accounts.yaml） ----------

def _cookies_path() -> Path:
    return project_root() / "baidu.yaml"


def load_cookies() -> str | None:
    """读取已保存的百度网盘 cookies。未配置返回 None。"""
    p = _cookies_path()
    if not p.exists():
        return None
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return None
    return data.get("cookies") or None


def save_cookies(cookies: str) -> None:
    """原子写入 cookies 到 baidu.yaml，权限 0600（含敏感登录凭证）。"""
    p = _cookies_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        yaml.safe_dump({"cookies": cookies}, f, allow_unicode=True, sort_keys=False)
    os.replace(tmp, p)
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass


# ---------- 上传进度解析 ----------

# BaiduPCS-Go upload 子命令的 stdout 关键行 -> 估算进度（0~1）。
# 秒传命中时跳过 "开始上传文件"，直接到 "秒传成功"。
_PROGRESS_MARKERS: list[tuple[str, float]] = [
    ("加入上传队列", 0.05),
    ("准备上传", 0.10),
    ("计算文件元信息", 0.20),
    ("计算文件分块md5", 0.30),
    ("开始上传文件", 0.50),
    ("秒传成功", 0.90),
    ("上传文件成功", 0.95),
    ("上传结束", 1.00),
]


def _parse_upload_progress(line: str) -> float | None:
    """从一行 stdout 估算上传进度。无匹配返回 None。"""
    for marker, pct in _PROGRESS_MARKERS:
        if marker in line:
            return pct
    return None


# 网盘路径正则：匹配 "保存到网盘路径: /xxx/yyy.epub"（秒传和普通上传都输出这行）
# 用 .+ 而非 \S+：文件名常含空格（如 "三体 (刘慈欣).epub"），\S+ 会在空格处截断
_PAN_PATH_RE = re.compile(r"保存到网盘路径:\s*(.+)")
# 文件已存在时 BaiduPCS-Go 输出 "目标文件, /xxx/yyy, 已存在, 跳过..."（无"保存到网盘路径"行）
_PAN_PATH_EXISTS_RE = re.compile(r"目标文件,\s*(.+?),\s*已存在")
# 分享链接正则：share set -f 输出 "shareID: xxx, 链接: https://pan.baidu.com/s/xxx?pwd=xxxx"
_SHARE_URL_RE = re.compile(r"https://pan\.baidu\.com/s/\S+")


class BaiduPCSManager:
    """BaiduPCS-Go 二进制管理 + 网盘操作。

    单例不必要：实例轻量（只存配置和二进制路径），按需创建即可。
    二进制路径来自 `cfg.baidupcs.binary_abs()`，默认 `data/baidupcs/BaiduPCS-Go`。
    """

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.binary = cfg.baidupcs.binary_abs()

    # ---------- 1. 二进制管理（仿 ProxyManager.ensure_binary / upgrade_binary） ----------

    def ensure_binary(self) -> Path:
        """确保本地有可执行的 BaiduPCS-Go。首次从 vendor/ 解压，不联网。"""
        if self.binary.exists() and os.access(self.binary, os.X_OK):
            return self.binary
        self.binary.parent.mkdir(parents=True, exist_ok=True)

        vendored = self._vendored_asset()
        if vendored:
            log.info("使用随包自带的 BaiduPCS-Go (%s)，无需联网下载", vendored.name)
            self._extract_zip(vendored, self.binary)
            log.info("BaiduPCS-Go 就绪: %s", self.binary)
            return self.binary

        log.warning("未找到随包自带的 BaiduPCS-Go（当前架构 %s 无预置包），尝试联网下载", _arch())
        version = self._latest_version()
        self._download_and_extract(version, self.binary, proxy_url=None)
        log.info("BaiduPCS-Go 就绪: %s", self.binary)
        return self.binary

    def _vendored_asset(self) -> Path | None:
        """在 vendor/baidupcs/ 下找当前架构的 zip。文件名含版本号，用 glob。"""
        d = project_root() / "vendor" / "baidupcs"
        if not d.exists():
            return None
        matches = sorted(d.glob(f"BaiduPCS-Go-*-linux-{_arch()}.zip"))
        return matches[-1] if matches else None

    @staticmethod
    def _extract_zip(zip_path: Path, dest: Path) -> None:
        """从 zip 里提取 BaiduPCS-Go 二进制（zip 内是 `目录/BaiduPCS-Go` 结构）。"""
        with zipfile.ZipFile(zip_path) as zf:
            # 找到以 /BaiduPCS-Go 结尾的成员（zip 内路径形如 "BaiduPCS-Go-vX.Y.Z-linux-amd64/BaiduPCS-Go"）
            member = next((m for m in zf.namelist() if m.endswith("/BaiduPCS-Go")), None)
            if member is None:
                raise RuntimeError(f"zip 内未找到 BaiduPCS-Go 二进制: {zip_path}")
            with zf.open(member) as src, open(dest, "wb") as out:
                shutil.copyfileobj(src, out)
        dest.chmod(dest.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    def _download_and_extract(self, version: str, dest: Path, proxy_url: str | None) -> None:
        """下载指定版本 zip 并解压。3 源兜底（github + 2 镜像）。"""
        asset = f"BaiduPCS-Go-{version}-linux-{_arch()}.zip"
        urls = [
            f"https://github.com/{BAIDUPCS_REPO}/releases/download/{version}/{asset}",
            f"https://ghproxy.net/https://github.com/{BAIDUPCS_REPO}/releases/download/{version}/{asset}",
            f"https://mirror.ghproxy.com/https://github.com/{BAIDUPCS_REPO}/releases/download/{version}/{asset}",
        ]
        tmp_zip = dest.with_suffix(".zip")
        last_err: Exception | None = None
        client_kwargs: dict = {"timeout": 120, "follow_redirects": True}
        if proxy_url:
            client_kwargs["proxy"] = proxy_url
        for url in urls:
            try:
                log.info("下载 BaiduPCS-Go %s: %s%s", version, url, "（经本地代理）" if proxy_url else "")
                with httpx.Client(**client_kwargs) as c:
                    with c.stream("GET", url) as r:
                        r.raise_for_status()
                        with open(tmp_zip, "wb") as f:
                            for chunk in r.iter_bytes():
                                f.write(chunk)
                break
            except Exception as e:  # noqa: BLE001
                log.warning("下载失败 (%s): %s", url, e)
                last_err = e
        else:
            raise RuntimeError(f"BaiduPCS-Go 二进制下载失败（所有源）：{last_err}")
        self._extract_zip(tmp_zip, dest)
        tmp_zip.unlink(missing_ok=True)

    def _latest_version(self, proxy_url: str | None = None) -> str:
        try:
            kwargs: dict = {"timeout": 8}
            if proxy_url:
                kwargs["proxy"] = proxy_url
            with httpx.Client(**kwargs) as c:
                r = c.get(f"https://api.github.com/repos/{BAIDUPCS_REPO}/releases/latest")
                r.raise_for_status()
                tag = r.json().get("tag_name")
                if tag:
                    return tag
        except Exception as e:  # noqa: BLE001
            log.warning("取 BaiduPCS-Go 最新版本失败，回退 %s: %s", BAIDUPCS_FALLBACK_VERSION, e)
        return BAIDUPCS_FALLBACK_VERSION

    def binary_version(self) -> str:
        if not (self.binary.exists() and os.access(self.binary, os.X_OK)):
            return "未安装"
        try:
            out = subprocess.run(
                [str(self.binary), "--version"], capture_output=True, text=True, timeout=5
            ).stdout
            m = re.search(r"v[\d.]+", out)
            return m.group(0) if m else out.strip()[:40] or "未知"
        except Exception:  # noqa: BLE001
            return "未知"

    def upgrade_binary(self, proxy_url: str | None) -> tuple[str, str]:
        """下载最新版并原子替换（.new → os.replace）。"""
        old_version = self.binary_version()
        new_version = self._latest_version(proxy_url=proxy_url)
        if new_version == old_version:
            return old_version, new_version
        tmp_new = self.binary.with_suffix(".new")
        try:
            self._download_and_extract(new_version, tmp_new, proxy_url=proxy_url)
            os.replace(tmp_new, self.binary)
            self.binary.chmod(self.binary.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        finally:
            tmp_new.unlink(missing_ok=True)
        return old_version, new_version

    # ---------- 2. 子进程调用 ----------

    def _run(self, *args: str, timeout: int = 120) -> subprocess.CompletedProcess:
        """同步运行 BaiduPCS-Go 子命令，返回 CompletedProcess。"""
        self.ensure_binary()
        cmd = [str(self.binary), *args]
        log.debug("运行: %s", " ".join(cmd))
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)

    # ---------- 3. 登录管理 ----------

    def login(self, cookies: str) -> tuple[bool, str]:
        """用 cookies 登录并验证。返回 (成功, 消息)。"""
        self.ensure_binary()
        try:
            r = subprocess.run(
                [str(self.binary), "login", f"-cookies={cookies}"],
                capture_output=True, text=True, timeout=30,
            )
        except subprocess.TimeoutExpired:
            return False, "登录超时"
        out = (r.stdout + r.stderr).strip()
        if r.returncode != 0 or "登录成功" not in out:
            return False, out or "登录失败"
        # 验证账号信息
        who = self._run("who").stdout.strip()
        return True, who or "登录成功"

    def is_logged_in(self) -> bool:
        """检查当前是否已登录（BaiduPCS-Go 的登录态存在 ~/.config/ 下）。"""
        if not (self.binary.exists() and os.access(self.binary, os.X_OK)):
            return False
        try:
            r = self._run("who", timeout=10)
            return r.returncode == 0 and "uid" in r.stdout
        except Exception:  # noqa: BLE001
            return False

    def ensure_logged_in(self) -> bool:
        """检查登录态，失效时用 baidu.yaml 存的 cookies 重新登录。"""
        if self.is_logged_in():
            return True
        cookies = load_cookies()
        if not cookies:
            return False
        ok, _ = self.login(cookies)
        return ok

    # ---------- 4. 上传 + 分享 ----------

    def upload(
        self,
        local_path: Path,
        pan_dir: str,
        on_output: "callable | None" = None,
        timeout: int = 600,
    ) -> str:
        """上传文件到网盘指定目录，返回网盘路径。

        `on_output(line)` 回调用于实时反馈进度（每读一行 stdout 调一次）。
        超时默认 600 秒（大文件/慢网络；秒传命中时实际 1~2 秒完成）。
        """
        self.ensure_binary()
        cmd = [str(self.binary), "upload", str(local_path), pan_dir]
        log.info("上传: %s -> %s", local_path.name, pan_dir)
        pan_path = ""

        # Popen 逐行读 stdout，实时回调进度
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
        )
        collected: list[str] = []
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                line = line.rstrip()
                if not line:
                    continue
                collected.append(line)
                log.debug("upload: %s", line)
                m = _PAN_PATH_RE.search(line)
                if m:
                    pan_path = m.group(1).strip()
                m2 = _PAN_PATH_EXISTS_RE.search(line)
                if m2 and not pan_path:
                    pan_path = m2.group(1).strip()
                if on_output is not None:
                    try:
                        on_output(line)
                    except Exception:  # noqa: BLE001 -- 回调失败不应中断上传
                        pass
        finally:
            proc.wait(timeout=30)
        if proc.returncode != 0:
            raise RuntimeError(f"上传失败 (exit {proc.returncode}): {''.join(collected)[-300:]}")
        if not pan_path:
            # 兜底：BaiduPCS-Go 上传到目录时文件名保持原名，路径 = pan_dir/basename
            pan_path = f"{pan_dir.rstrip('/')}/{local_path.name}"
            log.debug("未解析到网盘路径，使用兜底构造: %s", pan_path)
        return pan_path

    def share(self, pan_path: str, period: int = 0) -> str:
        """创建永久分享链接（-period 0 -f），返回完整链接（含 ?pwd=xxxx）。"""
        self.ensure_binary()
        r = self._run("share", "set", pan_path, "-f", "--period", str(period), timeout=30)
        out = r.stdout + r.stderr
        if r.returncode != 0:
            raise RuntimeError(f"分享失败: {out.strip()[-300:]}")
        m = _SHARE_URL_RE.search(out)
        if not m:
            raise RuntimeError(f"未解析到分享链接: {out.strip()[-300:]}")
        return m.group(0)

    def upload_and_share(
        self,
        local_path: Path,
        pan_dir: str,
        on_output: "callable | None" = None,
        period: int = 0,
    ) -> str:
        """上传 + 创建分享，一步完成。返回分享链接。"""
        pan_path = self.upload(local_path, pan_dir, on_output=on_output)
        share_url = self.share(pan_path, period=period)
        return share_url
