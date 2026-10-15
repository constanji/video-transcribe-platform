from __future__ import annotations

import json
import re

import httpx

from .common import IOS_HEADER, ParsedMedia, ParseError, image_item, normalize_url, prefer_playable, video_item

INIT_STATE_RE = re.compile(r"window\.INIT_STATE\s*=\s*(.*?)</script>", re.S)


def parse_kuaishou(client: httpx.Client, url: str) -> ParsedMedia:
    headers = {**IOS_HEADER, "Referer": "https://v.kuaishou.com/"}
    response = client.get(url, headers=headers, follow_redirects=True)
    final = str(response.url).replace("/fw/long-video/", "/fw/photo/")
    if final != str(response.url):
        response = client.get(final, headers=headers, follow_redirects=True)
    if response.status_code >= 400:
        raise ParseError(f"快手页面请求失败（{response.status_code}）")
    matched = INIT_STATE_RE.search(response.text)
    if not matched:
        raise ParseError("快手页面未返回视频数据，链接可能已失效")
    try:
        state = json.loads(matched.group(1).strip().replace("undefined", "null"))
    except json.JSONDecodeError as exc:
        raise ParseError("快手媒体数据解析失败") from exc
    photo = _photo(state)
    if not photo:
        raise ParseError("快手页面没有可解析的视频")
    play_urls = [item.get("url") for item in (photo.get("mainMvUrls") or []) if isinstance(item, dict) and item.get("url")]
    covers = [item.get("url") for item in (photo.get("coverUrls") or []) if isinstance(item, dict) and item.get("url")]
    images = _atlas_images(photo)
    variants = [video_item(play, "原始质量" if index == 0 else "备用清晰度") for index, play in enumerate(play_urls)]
    variants.extend(image_item(item) for item in images)
    variants = prefer_playable(variants)
    if not variants:
        raise ParseError("快手未返回可下载媒体")
    duration = photo.get("duration") or 0
    try:
        duration = float(duration)
    except (TypeError, ValueError):
        duration = 0
    if duration > 1000:
        duration = duration / 1000
    photo_id = str(photo.get("photoId") or photo.get("id") or "") or None
    title = str(photo.get("caption") or "").strip() or "未命名快手视频"
    return ParsedMedia(
        source_url=url,
        normalized_url=normalize_url(str(response.url)),
        platform="kuaishou",
        title=title[:500],
        author=str(photo.get("userName") or "").replace("\u3164", "").strip() or None,
        thumbnail_url=covers[0] if covers else (images[0] if images else None),
        duration_seconds=duration or None,
        platform_video_id=photo_id,
        variants=variants,
    )


def _photo(state: object) -> dict | None:
    if not isinstance(state, dict):
        return None
    for value in state.values():
        if isinstance(value, dict) and isinstance(value.get("photo"), dict):
            return value["photo"]
    return None


def _atlas_images(photo: dict) -> list[str]:
    atlas = ((photo.get("ext_params") or {}).get("atlas")) or {}
    routes = atlas.get("list") or []
    cdns = atlas.get("cdnList") or []
    cdn = ""
    if cdns and isinstance(cdns[0], dict):
        cdn = str(cdns[0].get("cdn") or "")
    if not cdn or not routes:
        return []
    return [f"https://{cdn}/{path}" for path in routes if path]
