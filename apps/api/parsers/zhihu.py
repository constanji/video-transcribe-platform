from __future__ import annotations

import json
import re

import httpx

from .common import DESKTOP_HEADER, ParsedMedia, ParseError, normalize_url, page_image, page_title, prefer_playable, unique_urls, video_item

ZVIDEO_RE = re.compile(r"zhihu\.com/zvideo/(\d+)")
INITIAL_RE = re.compile(r'<script id="js-initialData"[^>]*>(.*?)</script>', re.S)
VIDEO_RE = re.compile(r"https?:[^\"'\\s<>]+?\.(?:m3u8|mp4)(?:\?[^\"'\\s<>]*)?", re.I)


def parse_zhihu(client: httpx.Client, url: str) -> ParsedMedia:
    headers = {**DESKTOP_HEADER, "Referer": "https://www.zhihu.com/"}
    if match := ZVIDEO_RE.search(url):
        parsed = _zvideo(client, url, match.group(1), headers)
        if parsed:
            return parsed
    response = client.get(url, headers=headers, follow_redirects=True)
    if response.status_code >= 400:
        raise ParseError(f"知乎页面请求失败（{response.status_code}）")
    html = response.text
    if "登录" in (page_title(html) or "") and "zhihu.com/signin" in str(response.url):
        raise ParseError("知乎要求登录后才能查看该内容")
    videos = _videos_from_state(html) or unique_urls(
        item.replace("\\/", "/") for item in VIDEO_RE.findall(html)
    )
    videos = [item for item in videos if _looks_like_video(item)]
    if not videos:
        raise ParseError("该知乎内容没有可下载视频")
    variants = prefer_playable([video_item(item, "原始质量" if index == 0 else "备用清晰度") for index, item in enumerate(videos[:4])])
    title = page_title(html) or "知乎视频"
    title = re.sub(r"\s*[-|]\s*知乎.*$", "", title).strip() or "知乎视频"
    return ParsedMedia(
        source_url=url,
        normalized_url=normalize_url(str(response.url)),
        platform="zhihu",
        title=title[:500],
        thumbnail_url=page_image(html),
        variants=variants,
    )


def _zvideo(client: httpx.Client, url: str, video_id: str, headers: dict) -> ParsedMedia | None:
    response = client.get(f"https://www.zhihu.com/api/v4/zvideos/{video_id}", headers=headers, follow_redirects=True)
    if response.status_code >= 400:
        return None
    try:
        payload = response.json()
    except ValueError:
        return None
    video = payload.get("video") or {}
    playlist = video.get("playlist") or {}
    urls = []
    if isinstance(playlist, dict):
        for item in playlist.values():
            if isinstance(item, dict):
                urls.append(item.get("play_url") or item.get("url"))
    urls.append(video.get("play_url"))
    variants = prefer_playable([video_item(item) for item in unique_urls([item for item in urls if isinstance(item, str)])])
    if not variants:
        return None
    author = ((payload.get("author") or {}).get("name")) if isinstance(payload.get("author"), dict) else None
    return ParsedMedia(
        source_url=url,
        normalized_url=f"https://www.zhihu.com/zvideo/{video_id}",
        platform="zhihu",
        title=str(payload.get("title") or "知乎视频")[:500],
        author=author,
        thumbnail_url=video.get("thumbnail") or payload.get("image_url"),
        platform_video_id=video_id,
        variants=variants,
    )


def _videos_from_state(html: str) -> list[str]:
    matched = INITIAL_RE.search(html)
    if not matched:
        return []
    try:
        payload = json.loads(matched.group(1))
    except json.JSONDecodeError:
        return []
    found: list[str] = []
    _walk(payload, found)
    return unique_urls(found)


def _walk(node: object, found: list[str]) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            if isinstance(value, str) and _looks_like_video(value) and any(token in key.lower() for token in ("url", "play", "src", "video")):
                found.append(value)
            else:
                _walk(value, found)
    elif isinstance(node, list):
        for item in node:
            _walk(item, found)


def _looks_like_video(url: str) -> bool:
    lowered = url.lower()
    if any(token in lowered for token in (".jpg", ".jpeg", ".png", ".webp", ".gif")):
        return False
    return any(token in lowered for token in (".m3u8", ".mp4", "video.zhihu.com", "vdn.vzuu.com", "zhvideo"))
