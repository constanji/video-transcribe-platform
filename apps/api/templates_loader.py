from __future__ import annotations

import json
from pathlib import Path
from typing import Any

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
PREFERRED_NAMES = ["通用视频总结", "新闻热点"]
HOME_DEFAULT_NAMES = {"通用视频总结", "新闻热点"}

NAME_ALIASES = {
    "video_summary": ["通用视频总结"],
    "news_hotspot": ["新闻热点"],
    "standard_meeting": ["Standard Meeting Notes", "标准会议纪要"],
    "daily_standup": ["Daily Standup", "每日站会"],
    "project_sync": ["Project Sync / Status Update", "项目同步 / 进度更新"],
    "psychatric_session": ["Psychiatric Session Note (SOAP + AI Hybrid)", "精神科会谈记录（SOAP + AI）"],
    "retrospective": ["Retrospective (Agile)", "敏捷回顾会"],
    "sales_marketing_client_call": ["Client / Sales Meeting", "客户 / 销售会议"],
}


def template_sort_key(name: str) -> tuple[int, int | str]:
    if name in PREFERRED_NAMES:
        return (0, PREFERRED_NAMES.index(name))
    return (1, name)


def load_builtin_templates() -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    if not TEMPLATE_DIR.exists():
        return items
    files = sorted(TEMPLATE_DIR.glob("*.json"), key=lambda path: (path.stem not in {"video_summary", "news_hotspot"}, path.stem))
    for path in files:
        payload = json.loads(path.read_text(encoding="utf-8"))
        name = payload.get("name") or path.stem
        items.append({
            "slug": path.stem,
            "name": name,
            "description": payload.get("description") or "",
            "sections": payload.get("sections") or [],
            "aliases": NAME_ALIASES.get(path.stem, [name]),
        })
    items.sort(key=lambda item: template_sort_key(item["name"]))
    return items


def find_builtin_template(name: str) -> dict[str, Any] | None:
    for item in load_builtin_templates():
        names = {item["name"], *(item.get("aliases") or [])}
        if name in names:
            return item
    return None
