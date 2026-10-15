from __future__ import annotations

import re
from html import unescape

import httpx

from .common import DESKTOP_HEADER, IOS_HEADER, MediaItem, ParsedMedia, ParseError, abs_url, normalize_url

BILI_BV_RE = re.compile(r"(BV[0-9A-Za-z]+)")
BILI_AV_RE = re.compile(r"(?:aid=|/video/av|av)(\d+)", re.I)


def parse_bilibili(client: httpx.Client, url: str, preferred_qn: int = 64) -> ParsedMedia:
    headers = {**DESKTOP_HEADER, "Referer": "https://www.bilibili.com/"}
    response = client.get(url, headers=headers, follow_redirects=True)
    if response.status_code >= 400:
        raise ParseError(f"B 站页面请求失败（{response.status_code}）")
    current = str(response.url)
    params: dict[str, str] = {}
    bvid = _extract_bvid(current, url)
    if bvid:
        params["bvid"] = bvid
    elif match := BILI_AV_RE.search(current) or BILI_AV_RE.search(url):
        params["aid"] = match.group(1)
    if not params:
        raise ParseError("未能从 B 站链接中提取视频 ID")
    api = client.get(
        "https://api.bilibili.com/x/web-interface/view",
        params=params,
        headers=headers,
        follow_redirects=True,
    )
    payload = {}
    try:
        payload = api.json()
    except ValueError:
        payload = {}
    data = payload.get("data") if api.status_code < 400 and payload.get("code") == 0 else None
    if not data:
        raise ParseError("B 站未返回视频信息，链接可能已失效")
    title = unescape(str(data.get("title") or "")).strip() or "未命名 B 站视频"
    owner = (data.get("owner") or {}).get("name")
    duration = data.get("duration") or 0
    bvid = data.get("bvid") or bvid or ""
    variants = _bili_variants(client, bvid, data.get("cid"), preferred_qn)
    if not variants:
        raise ParseError("B 站未返回可下载视频地址")
    return ParsedMedia(
        source_url=url,
        normalized_url=_canonical(bvid, current),
        platform="bilibili",
        title=title[:500],
        author=owner,
        thumbnail_url=abs_url(data.get("pic")),
        duration_seconds=float(duration) if duration else None,
        platform_video_id=bvid or None,
        variants=variants,
    )


def bili_play_url(bvid: str, quality: str | None = None) -> str | None:
    with httpx.Client(headers=DESKTOP_HEADER, timeout=25.0, follow_redirects=True) as client:
        items = _bili_variants(client, bvid, None, 64)
    if quality:
        matched = next((item for item in items if item.quality == quality), None)
        if matched:
            return matched.source_url
    return items[0].source_url if items else None


def _extract_bvid(*texts: str) -> str | None:
    for text in texts:
        if match := BILI_BV_RE.search(text or ""):
            return match.group(1)
    return None


def _canonical(bvid: str | None, fallback: str) -> str:
    if bvid:
        return f"https://www.bilibili.com/video/{bvid}"
    return normalize_url(fallback)


def _bili_variants(client: httpx.Client, bvid: str | None, cid: int | None, preferred_qn: int) -> list[MediaItem]:
    if not bvid:
        return []
    headers = {**DESKTOP_HEADER, "Referer": f"https://www.bilibili.com/video/{bvid}/"}
    if not cid:
        try:
            view = client.get(
                "https://api.bilibili.com/x/web-interface/view",
                params={"bvid": bvid},
                headers=headers,
                follow_redirects=True,
            ).json()
            cid = (view.get("data") or {}).get("cid")
        except Exception:
            cid = None
    if not cid:
        return []
    try:
        probe = client.get(
            "https://api.bilibili.com/x/player/playurl",
            params={"bvid": bvid, "cid": cid, "qn": preferred_qn, "fnval": 1, "platform": "html5"},
            headers=headers,
            follow_redirects=True,
        ).json()
    except Exception:
        return []
    data = probe.get("data") or {}
    qualities = list(zip(data.get("accept_quality") or [data.get("quality")], data.get("accept_description") or ["默认清晰度"]))
    if not qualities and data.get("durl"):
        qualities = [(data.get("quality") or preferred_qn, "默认清晰度")]
    preferred = [item for item in qualities if item[0] == preferred_qn]
    rest = [item for item in qualities if item[0] != preferred_qn]
    items: list[MediaItem] = []
    seen: set[str] = set()
    for qn, label in [*preferred, *rest]:
        try:
            payload = client.get(
                "https://api.bilibili.com/x/player/playurl",
                params={"bvid": bvid, "cid": cid, "qn": qn, "fnval": 1, "platform": "html5"},
                headers=headers,
                follow_redirects=True,
            ).json()
        except Exception:
            continue
        durl = ((payload.get("data") or {}).get("durl") or [{}])[0]
        play_url = durl.get("url") or (durl.get("backup_url") or [None])[0]
        if not play_url or play_url in seen:
            continue
        seen.add(play_url)
        items.append(MediaItem(
            format="mp4",
            quality=str(label),
            media_type="video",
            source_url=play_url,
            file_size=durl.get("size"),
        ))
    return items
