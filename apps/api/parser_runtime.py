from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from html import unescape
from urllib.parse import parse_qs, urlparse

import httpx

IOS_HEADER = {
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 "
        "(KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1"
    )
}
DESKTOP_HEADER = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
}
BILI_BV_RE = re.compile(r"(BV[0-9A-Za-z]+)")
BILI_AV_RE = re.compile(r"(?:aid=|/video/av|av)(\d+)", re.I)
HDSL_SIZE_RE = re.compile(r"@[0-9]+[wh](?:_[0-9]+[wh])?$", re.I)

URL_RE = re.compile(r"https?://[^\s<>\"'，。、]+", re.I)
OG_TITLE_RE = re.compile(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']', re.I)
OG_TITLE_RE_ALT = re.compile(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:title["\']', re.I)
OG_IMAGE_RE = re.compile(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']', re.I)
OG_IMAGE_RE_ALT = re.compile(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']', re.I)
TWITTER_IMAGE_RE = re.compile(r'<meta[^>]+name=["\']twitter:image["\'][^>]+content=["\']([^"\']+)["\']', re.I)
ITEM_IMAGE_RE = re.compile(r'<meta[^>]+itemprop=["\']image["\'][^>]+content=["\']([^"\']+)["\']', re.I)
ROUTER_DATA_RE = re.compile(r"window\._ROUTER_DATA\s*=\s*(.*?)</script>", re.S)
VIDEO_ID_RE = re.compile(r"/(?:video|note|slides)/(\d+)")


class ParseError(Exception):
    pass


@dataclass
class MediaItem:
    format: str
    quality: str
    media_type: str
    source_url: str
    file_size: int | None = None


@dataclass
class ParsedMedia:
    source_url: str
    normalized_url: str
    platform: str
    title: str
    author: str | None = None
    thumbnail_url: str | None = None
    duration_seconds: float | None = None
    platform_video_id: str | None = None
    variants: list[MediaItem] = field(default_factory=list)


def extract_url(text: str) -> str:
    raw = text.strip()
    if match := URL_RE.search(raw):
        return match.group(0).rstrip(").,，。]/")
    if raw.startswith(("http://", "https://")):
        return raw
    raise ParseError("没有识别到有效链接，请粘贴包含 http 的视频地址")


def normalize_url(url: str) -> str:
    parsed = urlparse(url.strip())
    path = parsed.path.rstrip("/")
    return f"{parsed.scheme}://{parsed.netloc}{path}"


def detect_platform(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    mapping = (
        ("bilibili", ("bilibili.com", "b23.tv")),
        ("douyin", ("douyin.com", "iesdouyin.com")),
        ("kuaishou", ("kuaishou.com", "gifshow.com")),
        ("weibo", ("weibo.com", "weibo.cn")),
        ("xhs", ("xiaohongshu.com", "xhslink.com")),
        ("youtube", ("youtube.com", "youtu.be")),
        ("tiktok", ("tiktok.com",)),
        ("zhihu", ("zhihu.com",)),
        ("twitter", ("twitter.com", "x.com")),
        ("pixiv", ("pixiv.net",)),
        ("xiaoheihe", ("xiaoheihe.cn",)),
        ("shipinhao", ("weixin.qq.com",)),
        ("qzone", ("qzone.qq.com",)),
    )
    for platform, hosts in mapping:
        if any(host.endswith(item) for item in hosts):
            return platform
    return "unknown"


def parse_media(text: str) -> ParsedMedia:
    url = extract_url(text)
    platform = detect_platform(url)
    with httpx.Client(headers=IOS_HEADER, timeout=25.0, follow_redirects=False) as client:
        if platform == "douyin":
            parsed = _parse_douyin(client, url)
            if parsed.title == "未命名抖音视频":
                if match := re.search(r"【([^】]+)】", text):
                    parsed.title = match.group(1)
                elif parsed.author:
                    parsed.title = f"{parsed.author}的视频"
            return parsed
        if platform == "bilibili":
            return _parse_bilibili(client, url)
        return _parse_open_graph(client, url, platform)


def _request(client: httpx.Client, method: str, url: str, **kwargs) -> httpx.Response:
    return client.request(method, url, **kwargs)


def _parse_douyin(client: httpx.Client, url: str) -> ParsedMedia:
    current = url
    if "v.douyin.com" in url or "jx.douyin.com" in url:
        response = _request(client, "GET", url)
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
    response = _request(client, "GET", share_url, headers={**IOS_HEADER, "Referer": "https://www.iesdouyin.com/"})
    if response.status_code >= 400:
        raise ParseError(f"抖音分享页请求失败（{response.status_code}）")
    matched = ROUTER_DATA_RE.search(response.text)
    if not matched:
        raise ParseError("抖音页面未返回可用媒体数据")
    try:
        payload = json.loads(matched.group(1).strip())
        loader = payload.get("loaderData") or {}
        page = loader.get("video_(id)/page") or loader.get("note_(id)/page") or {}
        items = ((page.get("videoInfoRes") or {}).get("item_list")) or []
        video = items[0]
    except (json.JSONDecodeError, IndexError, TypeError) as exc:
        raise ParseError("抖音媒体数据解析失败") from exc

    author = ((video.get("author") or {}).get("nickname")) or None
    title = unescape((video.get("desc") or "").strip()) or "未命名抖音视频"
    media = video.get("video") or {}
    cover = _first_url(((media.get("cover") or {}).get("url_list")) or [])
    play_addr = media.get("play_addr") or {}
    play_urls = [item.replace("playwm", "play") for item in (play_addr.get("url_list") or [])]
    duration = media.get("duration") or 0
    if duration > 1000:
        duration = duration / 1000
    variants = [
        MediaItem(format="mp4", quality=label, media_type="video", source_url=play_url)
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
        thumbnail_url=cover,
        duration_seconds=float(duration) if duration else None,
        platform_video_id=video_id,
        variants=variants,
    )


def _ensure_ttwid(client: httpx.Client) -> None:
    if client.cookies.get("ttwid"):
        return
    response = _request(
        client,
        "POST",
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
        _request(client, "GET", redirect, headers={**IOS_HEADER, "Referer": "https://www.iesdouyin.com/"})


def _extract_bvid(*texts: str) -> str | None:
    for text in texts:
        if match := BILI_BV_RE.search(text or ""):
            return match.group(1)
    return None


def _bili_canonical(bvid: str | None, fallback: str) -> str:
    if bvid:
        return f"https://www.bilibili.com/video/{bvid}"
    return normalize_url(fallback)


def _parse_bilibili(client: httpx.Client, url: str) -> ParsedMedia:
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
        parsed = _parse_open_graph(client, current, "bilibili")
        parsed.source_url = url
        parsed.platform_video_id = bvid
        parsed.normalized_url = _bili_canonical(bvid, parsed.normalized_url)
        parsed.variants = _bili_variants(client, bvid, None)
        return parsed
    title = unescape(str(data.get("title") or "")).strip() or "未命名 B 站视频"
    owner = (data.get("owner") or {}).get("name")
    pic = _abs_url(data.get("pic"))
    duration = data.get("duration") or 0
    bvid = data.get("bvid") or bvid or ""
    cid = data.get("cid")
    return ParsedMedia(
        source_url=url,
        normalized_url=_bili_canonical(bvid, current),
        platform="bilibili",
        title=title[:500],
        author=owner,
        thumbnail_url=pic,
        duration_seconds=float(duration) if duration else None,
        platform_video_id=bvid or None,
        variants=_bili_variants(client, bvid, cid),
    )


def _bili_variants(client: httpx.Client, bvid: str | None, cid: int | None) -> list[MediaItem]:
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
            cid = ((view.get("data") or {}).get("cid"))
        except Exception:
            cid = None
    if not cid:
        return []
    try:
        probe = client.get(
            "https://api.bilibili.com/x/player/playurl",
            params={"bvid": bvid, "cid": cid, "qn": 80, "fnval": 1, "platform": "html5"},
            headers=headers,
            follow_redirects=True,
        ).json()
    except Exception:
        return []
    data = probe.get("data") or {}
    qualities = list(zip(data.get("accept_quality") or [data.get("quality")], data.get("accept_description") or ["默认清晰度"]))
    if not qualities and data.get("durl"):
        qualities = [(data.get("quality") or 64, "默认清晰度")]
    items: list[MediaItem] = []
    seen: set[str] = set()
    for qn, label in qualities:
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


def _parse_open_graph(client: httpx.Client, url: str, platform: str) -> ParsedMedia:
    response = client.get(url, headers=IOS_HEADER, follow_redirects=True)
    if response.status_code >= 400:
        raise ParseError(f"页面请求失败（{response.status_code}）")
    html = response.text
    title = _meta(html, OG_TITLE_RE, OG_TITLE_RE_ALT)
    if not title and "<title>" in html:
        title = unescape(re.sub(r"<.*?>", "", html.split("<title>", 1)[1].split("</title>", 1)[0]))
    image = _abs_url(_meta(html, OG_IMAGE_RE, OG_IMAGE_RE_ALT, TWITTER_IMAGE_RE, ITEM_IMAGE_RE))
    if not title:
        raise ParseError("当前平台尚未返回标题，请检查链接或到设置中填写 Cookie")
    if platform == "bilibili" and title:
        title = re.sub(r"(_哔哩哔哩.*|_bilibili.*)$", "", title, flags=re.I).strip()
    return ParsedMedia(
        source_url=url,
        normalized_url=normalize_url(str(response.url)),
        platform=platform,
        title=unescape(title).strip()[:500],
        thumbnail_url=image,
        variants=[],
    )


def _abs_url(url: str | None) -> str | None:
    if not url:
        return None
    value = unescape(url).strip()
    if value.startswith("//"):
        value = "https:" + value
    elif value.startswith("http://"):
        value = "https://" + value[len("http://"):]
    return HDSL_SIZE_RE.sub("", value)


def _meta(html: str, *patterns: re.Pattern[str]) -> str | None:
    for pattern in patterns:
        if match := pattern.search(html):
            return unescape(match.group(1))
    return None


def _first_url(urls: list[str]) -> str | None:
    return urls[0] if urls else None


def cover_referer(image_url: str, source_url: str) -> str:
    host = (urlparse(image_url).hostname or "").lower()
    if "hdslb.com" in host or "bilibili.com" in host or "bilivideo.com" in host:
        return "https://www.bilibili.com/"
    if any(item in host for item in ("douyin", "byteimg", "iesdouyin")):
        return "https://www.iesdouyin.com/"
    return source_url


def bili_play_url(bvid: str, quality: str | None = None) -> str | None:
    with httpx.Client(headers=DESKTOP_HEADER, timeout=25.0, follow_redirects=True) as client:
        items = _bili_variants(client, bvid, None)
    if quality:
        matched = next((item for item in items if item.quality == quality), None)
        if matched:
            return matched.source_url
    return items[0].source_url if items else None


def download_bytes(url: str, referer: str | None = None) -> bytes:
    headers = dict(DESKTOP_HEADER if "hdslb.com" in url or "bilibili.com" in url or "bilivideo.com" in url else IOS_HEADER)
    if referer:
        headers["Referer"] = referer
    with httpx.Client(headers=headers, timeout=120.0, follow_redirects=True) as client:
        response = client.get(url)
        response.raise_for_status()
        return response.content
