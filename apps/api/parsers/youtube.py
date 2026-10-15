from __future__ import annotations

from .common import MediaItem, ParsedMedia, ParseError, media_format, normalize_url, video_item
from .ytdlp import best_audio_url, best_url, extract_info

_SID_COOKIES = ("SAPISID", "__Secure-1PAPISID", "__Secure-3PAPISID")
_COOKIE_HINT = (
    "YouTube 未识别到有效登录 Cookie。请从已登录的 YouTube 页面请求头复制完整内容，"
    "必须同时包含 LOGIN_INFO，以及 SAPISID、__Secure-1PAPISID、__Secure-3PAPISID 之一。"
    "document.cookie 读不到这些 HttpOnly 项。"
)


def _cookie_names(cookies: str) -> set[str]:
    names = set()
    for part in cookies.split(";"):
        if "=" not in part:
            continue
        name = part.split("=", 1)[0].strip()
        if name:
            names.add(name)
    return names


def parse_youtube(url: str, cookies: str, proxy: str | None) -> ParsedMedia:
    info = None
    first_error: Exception | None = None
    try:
        info = extract_info(url, proxy=proxy, js_runtimes={"node": {}})
    except Exception as exc:
        first_error = exc
    if info is None and cookies:
        names = _cookie_names(cookies)
        if "LOGIN_INFO" not in names or not any(name in names for name in _SID_COOKIES):
            raise ParseError(_COOKIE_HINT) from first_error
        try:
            # 带 Cookie 时 yt-dlp 默认会用 tv_downgraded，该客户端目前常被 YouTube 拒绝。
            info = extract_info(
                url,
                cookies=cookies,
                domain="youtube.com",
                proxy=proxy,
                extractor_args={"youtube": {"player_client": ["default", "-tv_downgraded", "web_embedded"]}},
                js_runtimes={"node": {}},
            )
        except Exception as exc:
            raise ParseError(f"YouTube 解析失败：{exc}") from exc
    if info is None:
        message = str(first_error or "")
        if any(token in message.lower() for token in ("not a bot", "sign in", "login", "age")):
            raise ParseError(_COOKIE_HINT) from first_error
        raise ParseError(f"YouTube 解析失败：{message or '未返回媒体信息'}") from first_error
    play = best_url(info)
    if not play:
        raise ParseError("YouTube 未返回可下载视频地址，Cookie 可能已失效")
    title = str(info.get("title") or "").strip() or "未命名 YouTube 视频"
    duration = info.get("duration") or 0
    variants = [video_item(play)]
    audio = best_audio_url(info)
    if audio and audio != play:
        variants.append(MediaItem(
            format=media_format(audio, "m4a"),
            quality="仅音频（转录优先）",
            media_type="audio",
            source_url=audio,
        ))
    return ParsedMedia(
        source_url=url,
        normalized_url=normalize_url(info.get("webpage_url") or url),
        platform="youtube",
        title=title[:500],
        author=info.get("channel") or info.get("uploader"),
        thumbnail_url=info.get("thumbnail"),
        duration_seconds=float(duration) if duration else None,
        platform_video_id=str(info.get("id") or "") or None,
        variants=variants,
    )
