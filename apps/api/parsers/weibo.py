from __future__ import annotations

import re
from html import unescape
from math import ceil
from time import time

import httpx

from .common import DESKTOP_HEADER, ParsedMedia, ParseError, image_item, normalize_url, prefer_playable, video_item

TV_RE = re.compile(r"weibo\.com/tv/show/\d+:\d+.*?[?&]mid=(\d+)")
FID_RE = re.compile(r"(?:fid=)(\d+:\d+)")
WID_RE = re.compile(r"weibo\.(?:com|cn)/(?:status|detail|\d+)/([0-9A-Za-z]+)")
TAG_RE = re.compile(r"<[^>]+>")


def parse_weibo(client: httpx.Client, url: str) -> ParsedMedia:
    current = url
    if "mapp.api.weibo.cn" in url or "t.cn/" in url:
        response = client.get(url, headers=DESKTOP_HEADER, follow_redirects=True)
        current = str(response.url)
    if match := FID_RE.search(current):
        return _parse_fid(client, url, match.group(1))
    weibo_id = None
    if match := TV_RE.search(current):
        weibo_id = _mid2id(match.group(1))
    elif match := WID_RE.search(current):
        weibo_id = match.group(1)
    if not weibo_id:
        raise ParseError("未能从微博链接中提取内容 ID")
    return _parse_status(client, url, weibo_id)


def _parse_fid(client: httpx.Client, source: str, fid: str) -> ParsedMedia:
    headers = {
        **DESKTOP_HEADER,
        "Referer": f"https://h5.video.weibo.com/show/{fid}",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    response = client.post(
        f"https://h5.video.weibo.com/api/component?page=/show/{fid}",
        content=f'data={{"Component_Play_Playinfo":{{"oid":"{fid}"}}}}'.encode(),
        headers=headers,
    )
    if response.status_code >= 400:
        raise ParseError(f"微博视频接口请求失败（{response.status_code}）")
    data = ((response.json().get("data") or {}).get("Component_Play_Playinfo")) or {}
    if not data:
        raise ParseError("微博视频数据为空")
    urls = data.get("urls") if isinstance(data.get("urls"), dict) else {}
    video = next(iter(urls.values()), None) if urls else data.get("stream_url")
    if isinstance(video, str) and video.startswith("//"):
        video = "https:" + video
    if not video:
        raise ParseError("微博未返回可下载视频地址")
    cover = data.get("cover_image")
    if isinstance(cover, str) and cover.startswith("//"):
        cover = "https:" + cover
    user = ((data.get("reward") or {}).get("user")) or {}
    title = str(data.get("title") or "").strip() or _plain(data.get("text")) or "未命名微博视频"
    return ParsedMedia(
        source_url=source,
        normalized_url=normalize_url(f"https://video.weibo.com/show?fid={fid}"),
        platform="weibo",
        title=title[:500],
        author=user.get("name"),
        thumbnail_url=cover,
        platform_video_id=fid,
        variants=[video_item(video)],
    )


def _parse_status(client: httpx.Client, source: str, weibo_id: str) -> ParsedMedia:
    headers = {
        **DESKTOP_HEADER,
        "accept": "application/json, text/plain, */*",
        "referer": f"https://m.weibo.cn/detail/{weibo_id}",
        "origin": "https://m.weibo.cn",
        "x-requested-with": "XMLHttpRequest",
        "mweibo-pwa": "1",
    }
    response = client.get(
        "https://m.weibo.cn/statuses/show",
        params={"id": weibo_id, "_": int(time() * 1000)},
        headers=headers,
        follow_redirects=False,
    )
    if response.status_code in {403, 418}:
        raise ParseError(f"微博被风控拦截（{response.status_code}），请稍后重试")
    if response.status_code != 200 or "json" not in response.headers.get("content-type", ""):
        raise ParseError(f"微博数据请求失败（{response.status_code}）")
    data = (response.json() or {}).get("data") or {}
    if not data:
        raise ParseError("微博未返回内容")
    page = data.get("page_info") or {}
    urls = page.get("urls") if isinstance(page.get("urls"), dict) else {}
    video = urls.get("mp4_720p_mp4") or urls.get("mp4_hd_mp4") or urls.get("mp4_ld_mp4")
    if not video:
        media = page.get("media_info") or {}
        video = media.get("stream_url") or media.get("mp4_hd_url") or media.get("mp4_sd_url")
    images = []
    for pic in data.get("pics") or []:
        if isinstance(pic, dict):
            images.append(((pic.get("large") or {}).get("url")) or pic.get("url"))
    variants = prefer_playable(
        ([video_item(video)] if video else []) + [image_item(item) for item in images if item]
    )
    if not variants:
        raise ParseError("这条微博没有可下载的视频或图片")
    user = data.get("user") or {}
    title = str(page.get("title") or "").strip() or _plain(data.get("text")) or "未命名微博"
    cover = ((page.get("page_pic") or {}).get("url")) if isinstance(page.get("page_pic"), dict) else None
    return ParsedMedia(
        source_url=source,
        normalized_url=normalize_url(f"https://weibo.com/{user.get('id')}/{data.get('bid') or weibo_id}"),
        platform="weibo",
        title=title[:500],
        author=user.get("screen_name"),
        thumbnail_url=cover or (images[0] if images else None),
        platform_video_id=str(data.get("bid") or weibo_id),
        variants=variants,
    )


def _plain(value: object) -> str:
    text = unescape(str(value or "")).replace("<br />", "\n")
    return TAG_RE.sub("", text).strip()[:500]


def _base62(number: int) -> str:
    alphabet = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
    if number == 0:
        return "0"
    result = ""
    while number > 0:
        result = alphabet[number % 62] + result
        number //= 62
    return result


def _mid2id(mid: str) -> str:
    reversed_mid = str(mid)[::-1]
    size = ceil(len(reversed_mid) / 7)
    chunks = []
    for index in range(size):
        block = reversed_mid[index * 7:(index + 1) * 7][::-1]
        encoded = _base62(int(block))
        if index < size - 1 and len(encoded) < 4:
            encoded = "0" * (4 - len(encoded)) + encoded
        chunks.append(encoded)
    chunks.reverse()
    return "".join(chunks)
