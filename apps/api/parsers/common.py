from __future__ import annotations

import re
import ipaddress
import os
import socket
from dataclasses import dataclass, field
from html import unescape
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

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
HDSL_SIZE_RE = re.compile(r"@[0-9]+[wh](?:_[0-9]+[wh])?$", re.I)
URL_RE = re.compile(r"https?://[^\s<>\"'，。、]+", re.I)
OG_TITLE_RE = re.compile(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']', re.I)
OG_TITLE_RE_ALT = re.compile(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:title["\']', re.I)
OG_IMAGE_RE = re.compile(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']', re.I)
OG_IMAGE_RE_ALT = re.compile(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']', re.I)
TWITTER_IMAGE_RE = re.compile(r'<meta[^>]+name=["\']twitter:image["\'][^>]+content=["\']([^"\']+)["\']', re.I)
ITEM_IMAGE_RE = re.compile(r'<meta[^>]+itemprop=["\']image["\'][^>]+content=["\']([^"\']+)["\']', re.I)

PLATFORM_HOSTS = (
    ("bilibili", ("bilibili.com", "b23.tv")),
    ("douyin", ("douyin.com", "iesdouyin.com")),
    ("kuaishou", ("kuaishou.com", "gifshow.com", "chenzhongtech.com")),
    ("weibo", ("weibo.com", "weibo.cn")),
    ("xhs", ("xiaohongshu.com", "xhslink.com", "xhslink.cn")),
    ("youtube", ("youtube.com", "youtu.be")),
    ("tiktok", ("tiktok.com",)),
    ("zhihu", ("zhihu.com",)),
    ("twitter", ("twitter.com", "x.com")),
    ("pixiv", ("pixiv.net",)),
    ("xiaoheihe", ("xiaoheihe.cn",)),
    ("shipinhao", ("weixin.qq.com", "channels.weixin.qq.com")),
    ("qzone", ("qzone.qq.com",)),
)
PROXY_BY_DEFAULT = {"youtube", "tiktok", "twitter"}
BILI_QN = {"_360P": 16, "_480P": 32, "_720P": 64, "_1080P": 80, "_4K": 120}


class ParseError(Exception):
    pass


# 198.18.0.0/15 (RFC 2544) 是 Clash/Mihomo 等代理 Fake-IP 模式的默认段：本地
# 开发机开着代理时所有域名都解析成这段地址，真实出口由代理决定。把它豁免，
# 真正的内网段（127/10/172.16/192.168/169.254 等）仍然严格拒绝。
FAKE_IP_NET = ipaddress.ip_network("198.18.0.0/15")


def host_matches(host: str, domain: str) -> bool:
    normalized_host = host.rstrip(".").lower()
    normalized_domain = domain.rstrip(".").lower()
    return normalized_host == normalized_domain or normalized_host.endswith(f".{normalized_domain}")


def validate_public_http_url(url: str) -> str:
    """拒绝会让服务端访问本机、内网或元数据地址的 URL。"""
    if os.environ.get("PARSE_SSRF_GUARD", "").lower() in {"off", "0", "false"}:
        return urlparse(str(url).strip()).geturl()
    parsed = urlparse(str(url).strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ParseError("仅支持有效的 HTTP(S) 链接")
    try:
        addresses = {
            item[4][0].split("%", 1)[0]
            for item in socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80))
        }
    except OSError as exc:
        raise ParseError("链接域名无法解析") from exc
    if not addresses:
        raise ParseError("链接域名没有可用地址")
    for value in addresses:
        try:
            address = ipaddress.ip_address(value)
        except ValueError as exc:
            raise ParseError("链接域名解析结果无效") from exc
        if address in FAKE_IP_NET:
            continue
        if not address.is_global:
            raise ParseError("拒绝访问本机、内网或保留网络地址")
    return parsed.geturl()


def validate_httpx_request(request) -> None:
    validate_public_http_url(str(request.url))


@dataclass
class MediaItem:
    format: str
    quality: str
    media_type: str
    source_url: str
    file_size: int | None = None
    extra: dict | None = None  # 平台专属附加信息（如 Pixiv ugoira 帧延迟表）


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
    tracking = {"share_source", "share_medium", "source", "from", "spm_id_from", "feature"}
    query = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower() not in tracking
    ]
    return urlunparse((parsed.scheme.lower(), parsed.netloc.lower(), path, "", urlencode(sorted(query)), ""))


def detect_platform(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    for platform, hosts in PLATFORM_HOSTS:
        if any(host_matches(host, item) for item in hosts):
            return platform
    return "unknown"


def abs_url(url: str | None) -> str | None:
    if not url:
        return None
    value = unescape(str(url)).strip()
    if value.startswith("//"):
        value = "https:" + value
    elif value.startswith("http://"):
        value = "https://" + value[len("http://"):]
    return HDSL_SIZE_RE.sub("", value)


def meta(html: str, *patterns: re.Pattern[str]) -> str | None:
    for pattern in patterns:
        if match := pattern.search(html):
            return unescape(match.group(1))
    return None


def page_title(html: str) -> str | None:
    title = meta(html, OG_TITLE_RE, OG_TITLE_RE_ALT)
    if not title and "<title>" in html:
        title = unescape(re.sub(r"<.*?>", "", html.split("<title>", 1)[1].split("</title>", 1)[0]))
    return unescape(title).strip() if title else None


def page_image(html: str) -> str | None:
    return abs_url(meta(html, OG_IMAGE_RE, OG_IMAGE_RE_ALT, TWITTER_IMAGE_RE, ITEM_IMAGE_RE))


def parser_config(settings: dict | None, platform: str) -> dict:
    raw = ((settings or {}).get("parsers") or {}).get(platform) or {}
    quality = str(raw.get("video_quality") or "_720P")
    return {
        "enable": raw.get("enable", True) is not False,
        "cookies": str(raw.get("cookies") or "").strip(),
        "use_proxy": bool(raw.get("use_proxy", platform in PROXY_BY_DEFAULT)),
        "video_quality": quality,
        "qn": BILI_QN.get(quality, 64),
    }


def parser_proxy(settings: dict | None, platform: str) -> str | None:
    if not (settings or {}).get("enable_proxy"):
        return None
    if not parser_config(settings, platform)["use_proxy"]:
        return None
    proxy = str((settings or {}).get("proxy") or "").strip()
    return proxy or None


def require_cookie(cookies: str, message: str) -> str:
    if not cookies:
        raise ParseError(message)
    return cookies


def media_format(url: str, default: str = "mp4") -> str:
    path = urlparse(url).path.lower()
    for ext in ("m3u8", "mp4", "webm", "mov", "mp3", "m4a", "jpg", "jpeg", "png", "webp", "gif", "zip"):
        if path.endswith(f".{ext}"):
            return "jpg" if ext == "jpeg" else ext
    return default


def video_item(url: str, quality: str = "原始质量", size: int | None = None) -> MediaItem:
    fmt = media_format(url, "mp4")
    kind = "audio" if fmt in {"mp3", "m4a"} else "video"
    return MediaItem(format=fmt, quality=quality, media_type=kind, source_url=url, file_size=size)


def image_item(url: str, quality: str = "图片") -> MediaItem:
    return MediaItem(format=media_format(url, "jpg"), quality=quality, media_type="image", source_url=url)


def prefer_playable(items: list[MediaItem]) -> list[MediaItem]:
    """视频直链排在图片和 m3u8 前面，转录会取第一条。"""
    def rank(item: MediaItem) -> tuple[int, int]:
        kind = 0 if item.media_type in {"video", "audio"} else 1
        hls = 1 if ".m3u8" in item.source_url.lower() else 0
        return (kind, hls)
    return sorted(items, key=rank)


def unique_urls(urls: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for url in urls:
        value = abs_url(url)
        if not value or value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result
