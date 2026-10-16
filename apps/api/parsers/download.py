from __future__ import annotations

import tempfile
import time
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

import httpx

from .common import DESKTOP_HEADER, IOS_HEADER, ParseError, validate_httpx_request, validate_public_http_url
from .ytdlp import download_hls_to_path


def cover_referer(image_url: str, source_url: str) -> str:
    host = (urlparse(image_url).hostname or "").lower()
    rules = (
        (("hdslb.com", "bilibili.com", "bilivideo.com"), "https://www.bilibili.com/"),
        (("douyin", "byteimg", "iesdouyin", "byte"), "https://www.iesdouyin.com/"),
        (("kuaishou", "yximgs", "gifshow", "chenzhongtech"), "https://www.kuaishou.com/"),
        (("weibo", "sinaimg"), "https://weibo.com/"),
        (("xiaohongshu", "xhscdn"), "https://www.xiaohongshu.com/"),
        (("pixiv", "pximg"), "https://www.pixiv.net/"),
        (("weixin", "findermp"), "https://channels.weixin.qq.com/"),
        (("tiktok", "tiktokcdn"), "https://www.tiktok.com/"),
        (("zhihu", "zhimg"), "https://www.zhihu.com/"),
        (("xiaoheihe",), "https://www.xiaoheihe.cn/"),
        (("qzone",), "https://qzone.qq.com/"),
    )
    for tokens, referer in rules:
        if any(token in host for token in tokens):
            return referer
    return source_url


def download_to_path(
    url: str,
    destination: Path,
    *,
    referer: str | None = None,
    max_bytes: int = 512 * 1024 * 1024,
    timeout: float = 120.0,
    retries: int = 0,
    cancel_check: Callable[[], bool] | None = None,
    on_progress: Callable[[int, int], None] | None = None,
) -> Path:
    validate_public_http_url(url)
    if ".m3u8" in url.lower():
        return download_hls_to_path(
            url,
            destination,
            referer=referer or cover_referer(url, url),
            max_bytes=max_bytes,
            cancel_check=cancel_check,
        )
    headers = dict(DESKTOP_HEADER if _desktop_host(url) else IOS_HEADER)
    if referer:
        headers["Referer"] = referer
    attempts = max(0, min(int(retries), 5)) + 1
    last_error: Exception | None = None
    for attempt in range(attempts):
        destination.unlink(missing_ok=True)
        try:
            with httpx.Client(
                headers=headers,
                timeout=max(3.0, min(float(timeout), 600.0)),
                follow_redirects=True,
                event_hooks={"request": [validate_httpx_request]},
            ) as client:
                with client.stream("GET", url) as response:
                    response.raise_for_status()
                    total = int(response.headers.get("Content-Length") or 0)
                    if total > max_bytes:
                        raise ParseError(f"媒体文件超过 {max_bytes // (1024 * 1024)} MB 限制")
                    received = 0
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    with destination.open("wb") as output:
                        for chunk in response.iter_bytes(256 * 1024):
                            if cancel_check and cancel_check():
                                raise ParseError("下载已取消")
                            received += len(chunk)
                            if received > max_bytes:
                                raise ParseError(f"媒体文件超过 {max_bytes // (1024 * 1024)} MB 限制")
                            output.write(chunk)
                            if on_progress:
                                try:
                                    on_progress(received, total)
                                except Exception:
                                    pass
            if not destination.stat().st_size:
                raise ParseError("下载的媒体文件为空")
            return destination
        except (httpx.HTTPError, OSError, ParseError) as exc:
            destination.unlink(missing_ok=True)
            last_error = exc
            if isinstance(exc, ParseError) or attempt + 1 >= attempts:
                raise
            time.sleep(min(0.5 * (2 ** attempt), 2.0))
    raise last_error or ParseError("媒体下载失败")


def download_bytes(
    url: str,
    referer: str | None = None,
    *,
    max_bytes: int = 20 * 1024 * 1024,
    timeout: float = 120.0,
    retries: int = 0,
) -> bytes:
    with tempfile.TemporaryDirectory() as tmp:
        path = download_to_path(
            url,
            Path(tmp) / "download",
            referer=referer,
            max_bytes=max_bytes,
            timeout=timeout,
            retries=retries,
        )
        return path.read_bytes()


def _desktop_host(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    tokens = (
        "hdslb.com", "bilibili.com", "bilivideo.com", "weibo", "sinaimg",
        "kuaishou", "yximgs", "xiaohongshu", "xhscdn", "pixiv", "pximg",
        "weixin", "zhihu", "zhimg", "xiaoheihe", "qzone",
    )
    return any(token in host for token in tokens)
