from __future__ import annotations

import re

import httpx

from .common import ParsedMedia, ParseError, image_item, normalize_url, prefer_playable, video_item

ANCHOR_RE = re.compile(r"<a\b([^>]*)>(.*?)</a>", re.I | re.S)
HREF_RE = re.compile(r'href="([^"]+)"', re.I)
TAG_RE = re.compile(r"<[^>]+>")


def parse_twitter(client: httpx.Client, url: str, cookies: str) -> ParsedMedia:
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/x-www-form-urlencoded",
        "Origin": "https://xdown.app",
        "Referer": "https://xdown.app/",
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
    }
    # xdown.app 是第三方服务，绝不能把用户的 Twitter 登录凭据转发给它。
    # 保留 cookies 参数是为了兼容统一解析器签名。
    _ = cookies
    response = client.post(
        "https://xdown.app/api/ajaxSearch",
        data={"q": url, "lang": "zh-cn"},
        headers=headers,
        follow_redirects=True,
    )
    if response.status_code >= 400:
        raise ParseError(f"Twitter 解析接口请求失败（{response.status_code}）")
    try:
        payload = response.json()
    except ValueError as exc:
        raise ParseError("Twitter 解析接口没有返回数据") from exc
    if payload.get("status") != "ok" or not payload.get("data"):
        raise ParseError("Twitter 解析失败，链接可能已失效或不是推文")
    html = str(payload["data"])
    video = None
    images: list[str] = []
    for attrs, inner in ANCHOR_RE.findall(html):
        if "tw-button-dl" not in attrs and "abutton" not in attrs:
            continue
        href = HREF_RE.search(attrs)
        if not href:
            continue
        text = TAG_RE.sub("", inner)
        if "下载 MP4" in text or "MP4" in text.upper() and "GIF" not in text.upper():
            video = href.group(1)
            break
        if "下载图片" in text or "image" in text.lower():
            images.append(href.group(1))
    title_match = re.search(r"<h3[^>]*>(.*?)</h3>", html, re.I | re.S)
    title = TAG_RE.sub("", title_match.group(1)).strip() if title_match else ""
    status = re.search(r"/status/(\d+)", url)
    variants = prefer_playable(([video_item(video)] if video else []) + [image_item(item) for item in images])
    if not variants:
        raise ParseError("这条推文没有可下载的视频或图片")
    return ParsedMedia(
        source_url=url,
        normalized_url=normalize_url(url.replace("twitter.com", "x.com")),
        platform="twitter",
        title=(title or "Twitter")[:500],
        platform_video_id=status.group(1) if status else None,
        variants=variants,
    )
