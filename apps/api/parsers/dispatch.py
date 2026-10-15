from __future__ import annotations

import httpx

from .bilibili import parse_bilibili
from .common import (
    IOS_HEADER,
    ParsedMedia,
    ParseError,
    detect_platform,
    extract_url,
    parser_config,
    parser_proxy,
    validate_httpx_request,
    validate_public_http_url,
)
from .douyin import parse_douyin
from .kuaishou import parse_kuaishou
from .pixiv import parse_pixiv
from .qzone import parse_qzone
from .shipinhao import parse_shipinhao
from .tiktok import parse_tiktok
from .twitter import parse_twitter
from .weibo import parse_weibo
from .xhs import parse_xhs
from .xiaoheihe import parse_xiaoheihe
from .youtube import parse_youtube
from .zhihu import parse_zhihu


def parse_media(text: str, shipinhao_cookie: str = "", settings: dict | None = None) -> ParsedMedia:
    settings = settings or {}
    url = extract_url(text)
    validate_public_http_url(url)
    platform = detect_platform(url)
    config = parser_config(settings, platform)
    if platform != "unknown" and not config["enable"]:
        raise ParseError("该平台解析器已关闭，请到设置中重新启用")
    if platform == "shipinhao" and not config["cookies"]:
        config["cookies"] = shipinhao_cookie.strip()
    proxy = parser_proxy(settings, platform)
    common_timeout = max(3.0, min(float(settings.get("common_timeout") or 15), 120.0))
    client_kwargs: dict = {
        "headers": IOS_HEADER,
        "timeout": common_timeout,
        "follow_redirects": False,
        "event_hooks": {"request": [validate_httpx_request]},
    }
    if proxy:
        client_kwargs["proxy"] = proxy
    with httpx.Client(**client_kwargs) as client:
        if platform == "douyin":
            return parse_douyin(client, url, text)
        if platform == "bilibili":
            return parse_bilibili(client, url, config["qn"])
        if platform == "kuaishou":
            return parse_kuaishou(client, url)
        if platform == "weibo":
            return parse_weibo(client, url)
        if platform == "xhs":
            return parse_xhs(client, url, config["cookies"])
        if platform == "youtube":
            return parse_youtube(url, config["cookies"], proxy)
        if platform == "tiktok":
            return parse_tiktok(client, url, config["cookies"], proxy)
        if platform == "zhihu":
            return parse_zhihu(client, url)
        if platform == "twitter":
            return parse_twitter(client, url, config["cookies"])
        if platform == "pixiv":
            return parse_pixiv(client, url, config["cookies"])
        if platform == "xiaoheihe":
            return parse_xiaoheihe(client, url, config["cookies"])
        if platform == "shipinhao":
            return parse_shipinhao(client, url, config["cookies"])
        if platform == "qzone":
            return parse_qzone(client, url, config["cookies"])
        raise ParseError("暂不支持这个平台的链接")
