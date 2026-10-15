from __future__ import annotations

import json
import re

import httpx

from .common import DESKTOP_HEADER, IOS_HEADER, ParsedMedia, ParseError, image_item, normalize_url, prefer_playable, require_cookie, video_item

STATE_RE = re.compile(r"window\.__INITIAL_STATE__\s*=\s*(.*?)</script>", re.S)
NOTE_RE = re.compile(r"(?:explore|discovery/item)/([0-9a-zA-Z]+)")


def parse_xhs(client: httpx.Client, url: str, cookies: str) -> ParsedMedia:
    require_cookie(cookies, "小红书解析需要配置 Cookie，请在解析器设置中填写后再试")
    headers = {**DESKTOP_HEADER, "Cookie": cookies, "Referer": "https://www.xiaohongshu.com/"}
    response = client.get(url, headers={**IOS_HEADER, "Cookie": cookies}, follow_redirects=True)
    if response.status_code >= 400:
        raise ParseError(f"小红书页面请求失败（{response.status_code}）")
    current = str(response.url)
    matched = STATE_RE.search(response.text)
    if not matched:
        response = client.get(current, headers=headers, follow_redirects=True)
        matched = STATE_RE.search(response.text)
    if not matched:
        raise ParseError("小红书页面未返回笔记数据，Cookie 可能已失效")
    try:
        state = json.loads(matched.group(1).replace("undefined", "null"))
    except json.JSONDecodeError as exc:
        raise ParseError("小红书笔记数据解析失败") from exc
    note_id = NOTE_RE.search(current)
    note = _find_note(state, note_id.group(1) if note_id else "")
    if not note:
        raise ParseError("小红书笔记不存在或无权查看")
    video = _master_url(note.get("video"))
    images = []
    for item in note.get("imageList") or note.get("image_list") or []:
        if isinstance(item, dict):
            images.append(item.get("urlDefault") or item.get("urlSizeLarge") or item.get("url"))
    variants = prefer_playable(([video_item(video)] if video else []) + [image_item(item) for item in images if item])
    if not variants:
        raise ParseError("小红书未返回可下载媒体")
    user = note.get("user") or {}
    title = str(note.get("title") or note.get("desc") or "").strip() or "未命名小红书笔记"
    return ParsedMedia(
        source_url=url,
        normalized_url=normalize_url(current),
        platform="xhs",
        title=title[:500],
        author=user.get("nickname") or user.get("nickName"),
        thumbnail_url=images[0] if images else None,
        platform_video_id=(note_id.group(1) if note_id else None),
        variants=variants,
    )


def _find_note(state: dict, note_id: str) -> dict | None:
    detail = ((state.get("note") or {}).get("noteDetailMap") or {})
    if note_id and isinstance(detail.get(note_id), dict):
        note = (detail[note_id].get("note")) or detail[note_id]
        if isinstance(note, dict):
            return note
    for value in detail.values():
        if isinstance(value, dict) and isinstance(value.get("note"), dict):
            return value["note"]
    note_data = state.get("noteData") or {}
    nested = ((note_data.get("data") or {}).get("noteData")) if isinstance(note_data, dict) else None
    return nested if isinstance(nested, dict) else None


def _master_url(video: object) -> str | None:
    if not isinstance(video, dict):
        return None
    stream = ((video.get("media") or {}).get("stream")) or {}
    for key in ("h265", "h264", "av1", "h266"):
        items = stream.get(key) or []
        if items and isinstance(items[0], dict) and items[0].get("masterUrl"):
            return str(items[0]["masterUrl"])
    return None
