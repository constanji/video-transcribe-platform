from __future__ import annotations

import tempfile
import shutil
from pathlib import Path
from typing import Callable

from .common import ParseError


def _ydl():
    try:
        import yt_dlp
    except ImportError as exc:
        raise ParseError("服务器未安装 yt-dlp，无法解析该平台") from exc
    return yt_dlp


def cookie_file(cookies: str, domain: str) -> str | None:
    pairs = []
    for part in cookies.split(";"):
        if "=" not in part:
            continue
        name, value = part.strip().split("=", 1)
        if name:
            pairs.append((name, value))
    if not pairs:
        return None
    handle = tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".txt", delete=False)
    handle.write("# Netscape HTTP Cookie File\n")
    for name, value in pairs:
        handle.write(f".{domain}\tTRUE\t/\tTRUE\t0\t{name}\t{value}\n")
    handle.close()
    return handle.name


def extract_info(
    url: str,
    *,
    cookies: str = "",
    domain: str = "",
    proxy: str | None = None,
    extractor_args: dict | None = None,
    js_runtimes: dict | None = None,
) -> dict:
    yt_dlp = _ydl()
    cookie_path = cookie_file(cookies, domain) if cookies and domain else None
    options: dict = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
        "format": "bv*[height<=720]+ba/b[height<=720]/best",
    }
    if extractor_args:
        options["extractor_args"] = extractor_args
    if js_runtimes:
        options["js_runtimes"] = js_runtimes
    if proxy:
        options["proxy"] = proxy
    if cookie_path:
        options["cookiefile"] = cookie_path
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=False)
    finally:
        if cookie_path:
            Path(cookie_path).unlink(missing_ok=True)
    if not isinstance(info, dict):
        raise ParseError("解析器没有返回媒体信息")
    return info


def best_url(info: dict) -> str | None:
    direct = info.get("url")
    if (
        isinstance(direct, str)
        and direct.startswith("http")
        and not info.get("requested_formats")
        and info.get("vcodec") != "none"
        and info.get("acodec") != "none"
    ):
        return direct
    formats = _url_formats(info)
    progressive = [
        item for item in formats
        if item.get("vcodec") not in (None, "none") and item.get("acodec") not in (None, "none")
    ]
    video = [item for item in formats if item.get("vcodec") not in (None, "none")]
    pool = progressive or video
    if not pool:
        return None
    pool.sort(key=lambda item: (int(item.get("height") or 0), int(item.get("tbr") or 0)), reverse=True)
    capped = [item for item in pool if not item.get("height") or int(item.get("height") or 0) <= 720]
    return str((capped or pool)[0]["url"])


def best_audio_url(info: dict) -> str | None:
    audio = [
        item for item in _url_formats(info)
        if item.get("acodec") not in (None, "none") and item.get("vcodec") in (None, "none")
    ]
    if not audio:
        return None
    audio.sort(key=lambda item: int(item.get("abr") or item.get("tbr") or 0), reverse=True)
    return str(audio[0]["url"])


def _url_formats(info: dict) -> list[dict]:
    candidates = [*(info.get("requested_formats") or []), *(info.get("formats") or [])]
    seen: set[str] = set()
    result: list[dict] = []
    for item in candidates:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "")
        if not url.startswith("http") or url in seen:
            continue
        seen.add(url)
        result.append(item)
    return result


def download_hls_to_path(
    url: str,
    destination: Path,
    *,
    referer: str | None = None,
    proxy: str | None = None,
    max_bytes: int,
    cancel_check: Callable[[], bool] | None = None,
) -> Path:
    yt_dlp = _ydl()
    with tempfile.TemporaryDirectory() as tmp:
        def progress(payload: dict) -> None:
            if cancel_check and cancel_check():
                raise ParseError("下载已取消")
            downloaded = int(payload.get("downloaded_bytes") or 0)
            if downloaded > max_bytes:
                raise ParseError(f"媒体文件超过 {max_bytes // (1024 * 1024)} MB 限制")

        options: dict = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "outtmpl": str(Path(tmp) / "media.%(ext)s"),
            "format": "best",
            "merge_output_format": "mp4",
            "max_filesize": max_bytes,
            "progress_hooks": [progress],
        }
        if proxy:
            options["proxy"] = proxy
        if referer:
            options["http_headers"] = {"Referer": referer}
        with yt_dlp.YoutubeDL(options) as ydl:
            ydl.download([url])
        files = [path for path in Path(tmp).iterdir() if path.is_file() and path.stat().st_size]
        if not files:
            raise ParseError("m3u8 下载失败")
        source = max(files, key=lambda path: path.stat().st_size)
        if source.stat().st_size > max_bytes:
            raise ParseError(f"媒体文件超过 {max_bytes // (1024 * 1024)} MB 限制")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), destination)
        return destination


def download_hls(url: str, referer: str | None = None, proxy: str | None = None, max_bytes: int = 512 * 1024 * 1024) -> bytes:
    with tempfile.TemporaryDirectory() as tmp:
        path = download_hls_to_path(
            url,
            Path(tmp) / "media.mp4",
            referer=referer,
            proxy=proxy,
            max_bytes=max_bytes,
        )
        return path.read_bytes()
