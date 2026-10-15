from __future__ import annotations

import re

import httpx

from .common import DESKTOP_HEADER, ParsedMedia, ParseError, image_item, video_item

PID_RE = re.compile(r"pixiv\.net/(?:artworks|en/artworks)/(\d+)")


def parse_pixiv(client: httpx.Client, url: str, cookies: str) -> ParsedMedia:
    matched = PID_RE.search(url)
    if not matched:
        raise ParseError("未能从 Pixiv 链接中提取作品 ID")
    pid = matched.group(1)
    headers = {**DESKTOP_HEADER, "Referer": "https://www.pixiv.net/"}
    if cookies:
        headers["Cookie"] = cookies
    detail = _body(client, f"https://www.pixiv.net/ajax/illust/{pid}", headers)
    title = str(detail.get("title") or "").strip() or f"Pixiv {pid}"
    author = ((detail.get("userName")) or None)
    illust_type = int(detail.get("illustType") or 0)
    cover = ((detail.get("urls") or {}).get("regular")) or None
    variants = []
    if illust_type == 2:
        meta = _body(client, f"https://www.pixiv.net/ajax/illust/{pid}/ugoira_meta", headers, allow_error=True)
        original = ((meta or {}).get("originalSrc")) if isinstance(meta, dict) else None
        if original:
            variants.append(video_item(str(original), "动图"))
            variants[-1].format = "zip"
            variants[-1].media_type = "file"
    if not variants:
        pages = _body(client, f"https://www.pixiv.net/ajax/illust/{pid}/pages", headers)
        if isinstance(pages, list):
            for page in pages:
                original = ((page.get("urls") or {}).get("original")) if isinstance(page, dict) else None
                if original:
                    variants.append(image_item(str(original), "原图"))
    if not variants:
        raise ParseError("Pixiv 未返回可下载媒体，Cookie 可能已失效")
    return ParsedMedia(
        source_url=url,
        normalized_url=f"https://www.pixiv.net/artworks/{pid}",
        platform="pixiv",
        title=title[:500],
        author=str(author) if author else None,
        thumbnail_url=str(cover) if cover else None,
        platform_video_id=pid,
        variants=variants,
    )


def _body(client: httpx.Client, url: str, headers: dict, allow_error: bool = False):
    try:
        response = client.get(url, headers=headers, follow_redirects=True)
    except httpx.HTTPError as exc:
        if allow_error:
            return None
        raise ParseError("Pixiv 网络请求失败，请稍后重试") from exc
    if response.status_code in {401, 403}:
        if allow_error:
            return None
        raise ParseError("Pixiv 解析需要配置 Cookie，请在解析器设置中填写后再试")
    if response.status_code >= 400:
        if allow_error:
            return None
        raise ParseError(f"Pixiv 接口请求失败（{response.status_code}），请检查 Cookie")
    try:
        payload = response.json()
    except ValueError as exc:
        raise ParseError("Pixiv 接口没有返回 JSON") from exc
    if payload.get("error"):
        if allow_error:
            return None
        raise ParseError(payload.get("message") or "Pixiv 接口返回错误，请检查 Cookie")
    return payload.get("body")
