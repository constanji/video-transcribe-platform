from __future__ import annotations

import httpx

from .common import IOS_HEADER, ParsedMedia, ParseError, image_item, normalize_url, prefer_playable, video_item
from .ytdlp import best_url, extract_info


def parse_tiktok(client: httpx.Client, url: str, cookies: str, proxy: str | None) -> ParsedMedia:
    resolved = _resolve(client, url)
    if "/photo/" in resolved:
        photo = _tikwm(client, resolved)
        if photo:
            return photo
        raise ParseError("TikTok 图文解析失败")
    try:
        info = extract_info(resolved, cookies=cookies, domain="tiktok.com", proxy=proxy)
        play = best_url(info)
        if play:
            duration = info.get("duration") or 0
            title = str(info.get("title") or "").strip() or "未命名 TikTok 视频"
            return ParsedMedia(
                source_url=url,
                normalized_url=normalize_url(info.get("webpage_url") or resolved),
                platform="tiktok",
                title=title[:500],
                author=info.get("uploader") or info.get("channel"),
                thumbnail_url=info.get("thumbnail"),
                duration_seconds=float(duration) if duration else None,
                platform_video_id=str(info.get("id") or "") or None,
                variants=[video_item(play)],
            )
    except ParseError:
        raise
    except Exception as exc:
        message = str(exc).lower()
        if not cookies and any(token in message for token in ("age", "login", "sign in", "cookie", "logged")):
            raise ParseError("TikTok 年龄限制内容需要配置 Cookie，请在解析器设置中填写后再试") from exc
        fallback = _tikwm(client, resolved)
        if fallback:
            return fallback
        if _network_blocked(str(exc)):
            raise ParseError("TikTok 当前网络无法访问，请在解析器设置中开启代理后重试") from exc
        raise ParseError(f"TikTok 解析失败：{exc}") from exc
    fallback = _tikwm(client, resolved)
    if fallback:
        return fallback
    raise ParseError("TikTok 未返回可下载媒体")


def _network_blocked(message: str) -> bool:
    lowered = message.lower()
    return any(token in lowered for token in ("blocked", "403", "timed out", "timeout", "connection"))


def _resolve(client: httpx.Client, url: str) -> str:
    host = url.split("/")[2] if "://" in url else ""
    if host.startswith(("vt.", "vm.")) or "/t/" in url:
        try:
            response = client.get(url, headers={**IOS_HEADER, "Referer": "https://www.tiktok.com/"}, follow_redirects=True)
            if str(response.url).startswith("http"):
                return str(response.url)
        except httpx.HTTPError:
            return url
    return url


def _tikwm(client: httpx.Client, url: str) -> ParsedMedia | None:
    try:
        response = client.get("https://www.tikwm.com/api/", params={"url": url}, follow_redirects=True)
        payload = response.json()
    except (httpx.HTTPError, ValueError):
        return None
    data = payload.get("data") if payload.get("code") == 0 else None
    if not isinstance(data, dict):
        return None
    play = data.get("hdplay") or data.get("play")
    images = [item for item in (data.get("images") or []) if isinstance(item, str)]
    variants = prefer_playable(([video_item(play)] if play else []) + [image_item(item) for item in images])
    if not variants:
        return None
    author = data.get("author") or {}
    duration = data.get("duration") or 0
    return ParsedMedia(
        source_url=url,
        normalized_url=normalize_url(url),
        platform="tiktok",
        title=str(data.get("title") or "TikTok").strip()[:500],
        author=author.get("nickname") or author.get("unique_id"),
        thumbnail_url=data.get("cover"),
        duration_seconds=float(duration) if duration else None,
        platform_video_id=str(data.get("id") or "") or None,
        variants=variants,
    )
