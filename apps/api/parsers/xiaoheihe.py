from __future__ import annotations

import hashlib
import random
import re
import time
from html import unescape

import httpx

from .common import DESKTOP_HEADER, ParsedMedia, ParseError, image_item, normalize_url, page_image, page_title, prefer_playable, unique_urls, video_item
from .xiaoheihe_fingerprint import V4_DATA, V4_EP

CHAR_TABLE = "AB45STUVWZEFGJ6CH01D237IXYPQRKLMN89"
LINK_RE = re.compile(r"(?:link_id=|/app/bbs/link/)([0-9a-zA-Z]+)")
GAME_RE = re.compile(r"/app/topic/game/([a-z]+)/([0-9a-zA-Z]+)")
SHARE_GAME_RE = re.compile(r"appid=([0-9a-zA-Z]+).*?game_type=([a-z]+)")
VIDEO_RE = re.compile(r"https?://[^\"'\s<>]+?\.(?:m3u8|mp4|mov)(?:\?[^\"'\s<>]*)?", re.I)
VIDEO_FIELD_RE = re.compile(r'"video_url"\s*:\s*"(https?:[^"]+)"')


def parse_xiaoheihe(client: httpx.Client, url: str, cookies: str) -> ParsedMedia:
    if match := LINK_RE.search(url):
        return _parse_link(client, url, match.group(1), cookies)
    game = GAME_RE.search(url) or SHARE_GAME_RE.search(url)
    if game:
        appid, game_type = (game.group(2), game.group(1)) if GAME_RE.search(url) else (game.group(1), game.group(2))
        return _parse_game(client, url, appid, game_type)
    raise ParseError("未能识别小黑盒链接")


def _parse_link(client: httpx.Client, url: str, link_id: str, cookies: str) -> ParsedMedia:
    token = _cookie_token(cookies)
    device_id = token[1:] if token and token.startswith("B") else ""
    if not token:
        token, device_id = _fetch_token(client)
    headers = {
        **DESKTOP_HEADER,
        "accept": "application/json, text/plain, */*",
        "referer": "https://www.xiaoheihe.cn/",
        "origin": "https://www.xiaoheihe.cn",
    }
    params = {
        "os_type": "web",
        "app": "heybox",
        "client_type": "web",
        "version": "999.0.4",
        "web_version": "2.5",
        "x_client_type": "web",
        "x_app": "heybox_website",
        "heybox_id": "",
        "x_os_type": "Windows",
        "device_info": "Chrome",
        "device_id": device_id or "",
        "link_id": link_id,
        "owner_only": "1",
        **_sign("/bbs/app/link/tree"),
    }
    response = client.get(
        "https://api.xiaoheihe.cn/bbs/app/link/tree",
        params=params,
        headers=headers,
        cookies={"x_xhh_tokenid": token},
        follow_redirects=True,
    )
    if response.status_code >= 400:
        raise ParseError(f"小黑盒帖子请求失败（{response.status_code}）")
    payload = response.json()
    if payload.get("status") != "ok":
        message = str(payload.get("msg") or payload.get("status") or "未知错误").strip()
        raise ParseError(f"小黑盒帖子请求失败：{message}。可在解析器设置中填写 Cookie 后重试")
    link = (payload.get("result") or {}).get("link") or {}
    if not isinstance(link, dict):
        raise ParseError("小黑盒帖子数据为空")
    video = str(link.get("video_url") or "").strip()
    images = _images_from_text(str(link.get("text") or ""))
    variants = prefer_playable(([video_item(video)] if video else []) + [image_item(item) for item in images])
    if not variants:
        raise ParseError("这条小黑盒帖子没有可下载的视频或图片")
    user = link.get("user") or {}
    title = unescape(str(link.get("title") or "")).strip() or "小黑盒帖子"
    return ParsedMedia(
        source_url=url,
        normalized_url=f"https://www.xiaoheihe.cn/app/bbs/link/{link_id}",
        platform="xiaoheihe",
        title=title[:500],
        author=str(user.get("username") or user.get("nickname") or "") or None,
        thumbnail_url=images[0] if images else None,
        platform_video_id=link_id,
        variants=variants,
    )


def _parse_game(client: httpx.Client, url: str, appid: str, game_type: str) -> ParsedMedia:
    page = f"https://www.xiaoheihe.cn/app/topic/game/{game_type}/{appid}"
    response = client.get(page, headers={**DESKTOP_HEADER, "Referer": "https://www.xiaoheihe.cn/"}, follow_redirects=True)
    if response.status_code >= 400:
        raise ParseError(f"小黑盒游戏页请求失败（{response.status_code}）")
    html = response.text
    videos = unique_urls([*VIDEO_FIELD_RE.findall(html), *VIDEO_RE.findall(html)])
    if not videos:
        raise ParseError("小黑盒游戏页没有可下载视频")
    title = page_title(html) or f"小黑盒游戏 {appid}"
    return ParsedMedia(
        source_url=url,
        normalized_url=normalize_url(page),
        platform="xiaoheihe",
        title=title[:500],
        thumbnail_url=page_image(html),
        platform_video_id=appid,
        variants=prefer_playable([video_item(item) for item in videos[:3]]),
    )


def _cookie_token(cookies: str) -> str | None:
    matched = re.search(r"(?:^|;\s*)x_xhh_tokenid=([^;]+)", cookies)
    return matched.group(1) if matched else None


def _fetch_token(client: httpx.Client) -> tuple[str, str]:
    response = client.post(
        "https://fp-it.portal101.cn/deviceprofile/v4",
        json={
            "appId": "heybox_website",
            "organization": "0yD85BjYvGFAvHaSQ1mc",
            "ep": V4_EP,
            "data": V4_DATA,
            "os": "web",
            "encode": 5,
            "compress": 2,
        },
        headers={"accept": "application/json, text/plain, */*"},
    )
    if response.status_code >= 400:
        raise ParseError(f"小黑盒设备凭证申请失败（{response.status_code}）")
    device_id = str(((response.json().get("detail") or {}).get("deviceId")) or "")
    if not device_id:
        raise ParseError("小黑盒设备凭证申请失败")
    return f"B{device_id}", device_id


def _images_from_text(raw: str) -> list[str]:
    return unique_urls(re.findall(r"https?://[^\"'\s<>]+?\.(?:jpg|jpeg|png|webp)", raw, re.I))


def _sign(path: str) -> dict[str, str | int]:
    now = int(time.time())
    nonce = hashlib.md5((str(now) + str(random.random())).encode()).hexdigest().upper()
    return {"hkey": _hkey(path, now + 1, nonce), "_time": now, "nonce": nonce}


def _hkey(path: str, ts: int, nonce: str) -> str:
    path = "/" + "/".join(part for part in path.split("/") if part) + "/"
    interleaved = _interleave([_av(str(ts), -2), _sv(path), _sv(nonce)])[:20]
    digest = hashlib.md5(interleaved.encode()).hexdigest()
    prefix = _av(digest[:5], -4)
    suffix = str(sum(_mix_columns([ord(char) for char in digest[-6:]])) % 100).zfill(2)
    return prefix + suffix


def _av(text: str, cut: int) -> str:
    table = CHAR_TABLE[:cut]
    return "".join(table[ord(char) % len(table)] for char in text)


def _sv(text: str) -> str:
    return "".join(CHAR_TABLE[ord(char) % len(CHAR_TABLE)] for char in text)


def _interleave(parts: list[str]) -> str:
    result: list[str] = []
    longest = max(len(part) for part in parts)
    for index in range(longest):
        for part in parts:
            if index < len(part):
                result.append(part[index])
    return "".join(result)


def _xtime(value: int) -> int:
    return ((value << 1) ^ 27) & 0xFF if value & 128 else value << 1


def _mul3(value: int) -> int:
    return _xtime(value) ^ value


def _mul6(value: int) -> int:
    return _mul3(_xtime(value))


def _mul12(value: int) -> int:
    return _mul6(_mul3(_xtime(value)))


def _mul14(value: int) -> int:
    return _mul12(value) ^ _mul6(value) ^ _mul3(value)


def _mix_columns(col: list[int]) -> list[int]:
    values = list(col)
    while len(values) < 4:
        values.append(0)
    mixed = [
        _mul14(values[0]) ^ _mul12(values[1]) ^ _mul6(values[2]) ^ _mul3(values[3]),
        _mul3(values[0]) ^ _mul14(values[1]) ^ _mul12(values[2]) ^ _mul6(values[3]),
        _mul6(values[0]) ^ _mul3(values[1]) ^ _mul14(values[2]) ^ _mul12(values[3]),
        _mul12(values[0]) ^ _mul6(values[1]) ^ _mul3(values[2]) ^ _mul14(values[3]),
    ]
    if len(values) > 4:
        mixed.extend(values[4:])
    return mixed
