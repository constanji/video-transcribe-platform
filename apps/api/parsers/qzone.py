from __future__ import annotations

import re
from html import unescape
import httpx

from .common import DESKTOP_HEADER, ParsedMedia, ParseError, normalize_url, page_image, page_title, prefer_playable, require_cookie, unique_urls, video_item

ATTR_RE = re.compile(
    r"(?:data-video-url|data-videourl|data-play-url|data-playurl|videosrc)=[\"']([^\"']+)[\"']",
    re.I,
)
MEDIA_RE = re.compile(r"https?:[^\"'\s<>]+?\.(?:mp4|m3u8)(?:\?[^\"'\s<>]*)?", re.I)


def parse_qzone(client: httpx.Client, url: str, cookies: str) -> ParsedMedia:
    require_cookie(cookies, "QQ 空间解析需要配置 Cookie，请在解析器设置中填写后再试")
    headers = {**DESKTOP_HEADER, "Cookie": cookies, "Referer": "https://qzone.qq.com/"}
    response = client.get(url, headers=headers, follow_redirects=True)
    if response.status_code >= 400:
        raise ParseError(f"QQ 空间页面请求失败（{response.status_code}）")
    html = response.text
    final = str(response.url)
    if "login" in final or "ptlogin" in final:
        raise ParseError("QQ 空间 Cookie 已失效，请重新登录并更新 Cookie")
    urls = unique_urls([*ATTR_RE.findall(html), *MEDIA_RE.findall(html)])
    urls = [item for item in urls if "qpic" not in item and not item.lower().endswith((".jpg", ".jpeg", ".png", ".webp", ".gif"))]
    if not urls:
        raise ParseError("该 QQ 空间内容没有可下载视频，或 Cookie 无权查看")
    title = page_title(html) or "QQ 空间视频"
    return ParsedMedia(
        source_url=url,
        normalized_url=normalize_url(final),
        platform="qzone",
        title=unescape(title)[:500],
        thumbnail_url=page_image(html),
        variants=prefer_playable([video_item(item) for item in urls[:3]]),
    )
