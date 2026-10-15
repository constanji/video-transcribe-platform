from __future__ import annotations

import json
import re
from html import unescape

import httpx

from .common import IOS_HEADER, ParsedMedia, ParseError, normalize_url, video_item

ROUTER_DATA_RE = re.compile(r"window\._ROUTER_DATA\s*=\s*(.*?)</script>", re.S)
VIDEO_ID_RE = re.compile(r"/(?:video|note|slides)/(\d+)")


def parse_douyin(client: httpx.Client, url: str, raw_text: str = "") -> ParsedMedia:
    current = url
    if "v.douyin.com" in url or "jx.douyin.com" in url:
        response = client.get(url)
        location = response.headers.get("location")
        if response.status_code not in {301, 302, 303, 307, 308} or not location:
            raise ParseError("抖音短链无法跳转，请检查链接是否失效")
        current = location if location.startswith("http") else f"https://www.iesdouyin.com{location}"

    video_id_match = VIDEO_ID_RE.search(current)
    if not video_id_match:
        raise ParseError("未能从抖音链接中提取视频 ID")
    video_id = video_id_match.group(1)
    kind = "note" if "/note/" in current else "video"
    share_url = f"https://www.iesdouyin.com/share/{kind}/{video_id}/"
    _ensure_ttwid(client)
    response = client.get(share_url, headers={**IOS_HEADER, "Referer": "https://www.iesdouyin.com/"})
    if response.status_code >= 400:
        raise ParseError(f"抖音分享页请求失败（{response.status_code}）")
    matched = ROUTER_DATA_RE.search(response.text)
    if not matched:
        raise ParseError("抖音页面未返回可用媒体数据")
    try:
        payload = json.loads(matched.group(1).strip())
        loader = payload.get("loaderData") or {}
        page = loader.get("video_(id)/page") or loader.get("note_(id)/page") or {}
        video = ((page.get("videoInfoRes") or {}).get("item_list") or [])[0]
    except (json.JSONDecodeError, IndexError, TypeError) as exc:
        raise ParseError("抖音媒体数据解析失败") from exc

    author = ((video.get("author") or {}).get("nickname")) or None
    title = unescape((video.get("desc") or "").strip()) or "未命名抖音视频"
    if title == "未命名抖音视频":
        if match := re.search(r"【([^】]+)】", raw_text):
            title = match.group(1)
        elif author:
            title = f"{author}的视频"
    media = video.get("video") or {}
    covers = ((media.get("cover") or {}).get("url_list")) or []
    play_urls = [item.replace("playwm", "play") for item in ((media.get("play_addr") or {}).get("url_list") or [])]
    duration = media.get("duration") or 0
    if duration and duration > 1000:
        duration = duration / 1000
    variants = [
        video_item(play_url, label)
        for label, play_url in zip(("原始质量", "备用清晰度"), play_urls)
    ]
    if not variants:
        raise ParseError("抖音未返回可下载视频地址")
    return ParsedMedia(
        source_url=url,
        normalized_url=normalize_url(share_url),
        platform="douyin",
        title=title[:500],
        author=author,
        thumbnail_url=covers[0] if covers else None,
        duration_seconds=float(duration) if duration else None,
        platform_video_id=video_id,
        variants=variants,
    )


def _ensure_ttwid(client: httpx.Client) -> None:
    if client.cookies.get("ttwid"):
        return
    response = client.post(
        "https://ttwid.bytedance.com/ttwid/union/register/",
        json={
            "region": "cn",
            "aid": 1768,
            "needFid": False,
            "service": "www.iesdouyin.com",
            "union": True,
            "fid": "",
        },
        headers={**IOS_HEADER, "Content-Type": "application/json", "Referer": "https://www.iesdouyin.com/"},
    )
    try:
        redirect = response.json().get("redirect_url")
    except ValueError:
        redirect = None
    if redirect:
        client.get(redirect, headers={**IOS_HEADER, "Referer": "https://www.iesdouyin.com/"})
