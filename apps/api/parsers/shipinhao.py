from __future__ import annotations

from time import time
from urllib.parse import parse_qs, urljoin, urlparse

import httpx

from .common import DESKTOP_HEADER, ParsedMedia, ParseError, abs_url, normalize_url, video_item

WECHAT_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 MicroMessenger/8.0.40"
)


def parse_shipinhao(client: httpx.Client, url: str, cookies: str) -> ParsedMedia:
    resolved = _resolve_short_url(client, url)
    token, export_id = _token_eid(resolved)
    if not (token and export_id):
        token, export_id = _token_eid(url)
    if not (token and export_id):
        if not cookies:
            raise ParseError("微信视频号短链需要配置腾讯元宝 Cookie，长链请使用 channels.weixin.qq.com 链接")
        token, export_id = _yuanbao(client, resolved, cookies)
    return _feed(client, url, token, export_id)


def _resolve_short_url(client: httpx.Client, url: str) -> str:
    """短链先跟 301。跳到已带 token 的长链时，后面就不必再找元宝 Cookie。"""
    if "weixin.qq.com/sph" not in url:
        return url
    try:
        response = client.get(url, headers={"User-Agent": WECHAT_UA}, follow_redirects=False)
    except httpx.HTTPError:
        return url
    location = response.headers.get("location")
    if not location:
        return url
    return location if location.startswith("http") else urljoin(url, location)


def _yuanbao(client: httpx.Client, url: str, cookies: str) -> tuple[str, str]:
    response = client.post(
        "https://yuanbao.tencent.com/api/weixin/get_parse_result",
        json={"type": "video_channel_url", "url": url, "scene": 1},
        headers={
            **DESKTOP_HEADER,
            "Content-Type": "application/json",
            "Cookie": cookies,
            "Origin": "https://yuanbao.tencent.com",
            "Referer": "https://yuanbao.tencent.com/chat",
        },
    )
    if response.status_code == 401:
        raise ParseError("腾讯元宝 Cookie 已失效，请重新登录并更新 Cookie")
    if response.status_code >= 400:
        raise ParseError(f"腾讯元宝解析接口请求失败（{response.status_code}）")
    payload = response.json().get("data") or {}
    token, export_id = _token_eid(payload.get("playable_url") or "")
    export_id = export_id or str(payload.get("wx_export_id") or "")
    if not export_id:
        raise ParseError("未能从视频号链接获取内容 ID，链接可能已失效")
    return token, export_id


def _feed(client: httpx.Client, url: str, token: str, export_id: str) -> ParsedMedia:
    rid = f"{int(time()):x}"
    feed = client.post(
        "https://channels.weixin.qq.com/finder-preview/api/feed/get_feed_info",
        params={"_rid": rid, "_pageUrl": "https://channels.weixin.qq.com/finder-preview/pages/feed"},
        json={"baseReq": {"generalToken": token}, "exportId": export_id},
        headers={
            **DESKTOP_HEADER,
            "Content-Type": "application/json",
            "Origin": "https://channels.weixin.qq.com",
            "Referer": f"https://channels.weixin.qq.com/finder-preview/pages/feed?token={token}&eid={export_id}",
        },
    )
    if feed.status_code >= 400:
        raise ParseError(f"视频号预览接口请求失败（{feed.status_code}）")
    body = feed.json()
    if body.get("errCode") not in (None, 0):
        raise ParseError(body.get("errMsg") or "视频号预览接口返回错误")
    info = (body.get("data") or {}).get("feedInfo") or {}
    author = (body.get("data") or {}).get("authorInfo") or {}
    err = info.get("errMsg") or {}
    if isinstance(err, dict) and err.get("type"):
        raise ParseError(err.get("title") or "该视频号内容无法解析")
    video_url = None
    for key in ("h264VideoInfo", "h265VideoInfo"):
        video_url = ((info.get(key) or {}).get("videoUrl")) or video_url
    video_url = video_url or info.get("videoUrl")
    if not video_url:
        raise ParseError("未获取到视频号媒体地址，内容可能已删除或权限不足")
    duration = ((info.get("h264VideoInfo") or {}).get("duration") or info.get("videoDuration"))
    try:
        duration = float(duration) if duration else None
    except (TypeError, ValueError):
        duration = None
    title = str(info.get("description") or "视频号视频").strip()
    return ParsedMedia(
        source_url=url,
        normalized_url=normalize_url(url),
        platform="shipinhao",
        title=title[:500],
        author=author.get("nickname") or None,
        thumbnail_url=abs_url(info.get("coverUrl")),
        duration_seconds=duration,
        platform_video_id=export_id,
        variants=[video_item(video_url)],
    )


def _token_eid(url: str) -> tuple[str, str]:
    query = parse_qs(urlparse(url).query)
    return (query.get("token") or [""])[0], (query.get("eid") or query.get("exportId") or [""])[0]
