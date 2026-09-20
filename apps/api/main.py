from __future__ import annotations

import copy
import hashlib
import os
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from urllib.parse import quote

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from .parser_runtime import ParseError, bili_play_url, cover_referer, download_bytes, parse_media
from .templates_loader import HOME_DEFAULT_NAMES, find_builtin_template, load_builtin_templates, template_sort_key
from sqlalchemy import JSON, DateTime, Float, ForeignKey, String, Text, create_engine, func, or_, select, text
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship, sessionmaker

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./media_platform.db")
PUBLIC_API_URL = os.getenv("PUBLIC_API_URL", "http://localhost:8000")
STORAGE_ROOT = Path(os.getenv("STORAGE_ROOT", "./storage"))
STORAGE_ROOT.mkdir(parents=True, exist_ok=True)
MEETILY_DATA = Path(os.getenv("MEETILY_DATA_DIR", str(Path.home() / "Library/Application Support/com.meetily.ai")))
DEFAULT_MOSS_MODEL_PATH = os.getenv("MOSS_MODEL_PATH", str(MEETILY_DATA / "funasr-models/OpenMOSS-Team/MOSS-Transcribe-Diarize"))
DEFAULT_SUMMARY_MODEL_PATH = os.getenv("SUMMARY_MODEL_PATH", str(MEETILY_DATA / "models/summary/Qwen3.5-4B-Q4_K_M.gguf"))
COVER_DIR = STORAGE_ROOT / "covers"
COVER_DIR.mkdir(parents=True, exist_ok=True)
connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


class VideoAsset(Base):
    __tablename__ = "video_assets"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    source_url: Mapped[str] = mapped_column(Text)
    normalized_url: Mapped[str] = mapped_column(Text, index=True)
    platform: Mapped[str | None] = mapped_column(String(80), nullable=True)
    platform_video_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    title: Mapped[str] = mapped_column(String(500), default="未命名视频")
    author: Mapped[str | None] = mapped_column(String(255), nullable=True)
    thumbnail_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    thumbnail_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_platform: Mapped[str | None] = mapped_column(String(80), nullable=True)
    available_formats: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, nullable=True)
    latest_summary_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    last_processed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    parse_status: Mapped[str] = mapped_column(String(32), default="parsed")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))
    jobs: Mapped[list["ProcessingJob"]] = relationship(back_populates="video", cascade="all, delete-orphan")


class ProcessingJob(Base):
    __tablename__ = "processing_jobs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    video_asset_id: Mapped[str] = mapped_column(ForeignKey("video_assets.id", ondelete="CASCADE"), index=True)
    job_type: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(32), default="queued")
    progress: Mapped[float] = mapped_column(Float, default=0)
    current_stage: Mapped[str] = mapped_column(String(64), default="queued")
    input_media_type: Mapped[str | None] = mapped_column(String(16), nullable=True)
    fallback_used: Mapped[bool] = mapped_column(default=False)
    error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    logs_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=lambda: [])
    progress_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    video: Mapped[VideoAsset] = relationship(back_populates="jobs")


class Transcript(Base):
    __tablename__ = "transcripts"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    job_id: Mapped[str] = mapped_column(ForeignKey("processing_jobs.id", ondelete="CASCADE"))
    video_asset_id: Mapped[str] = mapped_column(ForeignKey("video_assets.id", ondelete="CASCADE"), index=True)
    model: Mapped[str] = mapped_column(String(255))
    language: Mapped[str] = mapped_column(String(32), default="auto")
    text: Mapped[str] = mapped_column(Text)
    segments_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    duration_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))


class Summary(Base):
    __tablename__ = "summaries"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    job_id: Mapped[str] = mapped_column(ForeignKey("processing_jobs.id", ondelete="CASCADE"))
    video_asset_id: Mapped[str] = mapped_column(ForeignKey("video_assets.id", ondelete="CASCADE"), index=True)
    template_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    provider: Mapped[str] = mapped_column(String(80))
    model: Mapped[str] = mapped_column(String(255))
    content_markdown: Mapped[str] = mapped_column(Text)
    content_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    source_transcript_id: Mapped[str] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))


class SummaryTemplate(Base):
    __tablename__ = "summary_templates"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String(255))
    description: Mapped[str] = mapped_column(Text, default="")
    origin: Mapped[str] = mapped_column(String(32), default="custom")
    sections_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    is_active: Mapped[bool] = mapped_column(default=True)
    show_on_home: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))


class MediaVariant(Base):
    __tablename__ = "media_variants"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    video_asset_id: Mapped[str] = mapped_column(ForeignKey("video_assets.id", ondelete="CASCADE"), index=True)
    format: Mapped[str] = mapped_column(String(32), default="mp4")
    quality: Mapped[str | None] = mapped_column(String(64), nullable=True)
    media_type: Mapped[str] = mapped_column(String(16), default="video")
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    local_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    file_size: Mapped[int | None] = mapped_column(nullable=True)
    download_status: Mapped[str] = mapped_column(String(32), default="available")


class ConfigRecord(Base):
    __tablename__ = "config_records"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))


Base.metadata.create_all(engine)
with engine.begin() as conn:
    if DATABASE_URL.startswith("sqlite"):
        existing = {row[1] for row in conn.execute(text("PRAGMA table_info(video_assets)")).fetchall()}
        alters = {
            "source_platform": "ALTER TABLE video_assets ADD COLUMN source_platform VARCHAR(80)",
            "available_formats": "ALTER TABLE video_assets ADD COLUMN available_formats TEXT",
            "latest_summary_id": "ALTER TABLE video_assets ADD COLUMN latest_summary_id VARCHAR(36)",
            "last_processed_at": "ALTER TABLE video_assets ADD COLUMN last_processed_at DATETIME",
        }
        for column, statement in alters.items():
            if column not in existing:
                conn.execute(text(statement))
        template_cols = {row[1] for row in conn.execute(text("PRAGMA table_info(summary_templates)")).fetchall()}
        if "show_on_home" not in template_cols:
            conn.execute(text("ALTER TABLE summary_templates ADD COLUMN show_on_home BOOLEAN DEFAULT 1"))
        job_cols = {row[1] for row in conn.execute(text("PRAGMA table_info(processing_jobs)")).fetchall()}
        if "logs_json" not in job_cols:
            conn.execute(text("ALTER TABLE processing_jobs ADD COLUMN logs_json JSON"))
        if "progress_json" not in job_cols:
            conn.execute(text("ALTER TABLE processing_jobs ADD COLUMN progress_json JSON"))
    else:
        for statement in (
            "ALTER TABLE processing_jobs ADD COLUMN IF NOT EXISTS input_media_type VARCHAR(16)",
            "ALTER TABLE processing_jobs ADD COLUMN IF NOT EXISTS fallback_used BOOLEAN DEFAULT FALSE",
            "ALTER TABLE processing_jobs ADD COLUMN IF NOT EXISTS logs_json JSONB",
            "ALTER TABLE processing_jobs ADD COLUMN IF NOT EXISTS progress_json JSONB",
            "ALTER TABLE video_assets ADD COLUMN IF NOT EXISTS source_platform VARCHAR(80)",
            "ALTER TABLE video_assets ADD COLUMN IF NOT EXISTS available_formats JSONB",
            "ALTER TABLE video_assets ADD COLUMN IF NOT EXISTS latest_summary_id VARCHAR(36)",
            "ALTER TABLE video_assets ADD COLUMN IF NOT EXISTS last_processed_at TIMESTAMPTZ",
            "ALTER TABLE summary_templates ADD COLUMN IF NOT EXISTS show_on_home BOOLEAN DEFAULT TRUE",
        ):
            conn.execute(text(statement))


class ParseRequest(BaseModel):
    url: str = Field(min_length=3)


class JobRequest(BaseModel):
    video_id: str
    job_type: str = Field(pattern="^(download|transcribe|summarize)$")
    model: str | None = None
    template_id: str | None = None


class TemplateRequest(BaseModel):
    name: str | None = None
    description: str | None = None
    sections: list[dict[str, Any]] | None = None
    show_on_home: bool | None = None


app = FastAPI(title="Video Transcribe Platform API", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition"],
)


def db_session():
    with SessionLocal() as session:
        yield session


def video_flags(session: Session, video_id: str) -> dict[str, Any]:
    has_transcript = session.scalar(select(Transcript.id).where(Transcript.video_asset_id == video_id).limit(1)) is not None
    has_summary = session.scalar(select(Summary.id).where(Summary.video_asset_id == video_id).limit(1)) is not None
    return {"has_transcript": has_transcript, "has_summary": has_summary}


def variant_dict(item: MediaVariant) -> dict[str, Any]:
    return {"id": item.id, "format": item.format, "quality": item.quality, "media_type": item.media_type, "download_status": item.download_status, "file_size": item.file_size}


_THINK_RE = re.compile(
    r"(?is)<think>.*?</think>|<thinking>.*?</thinking>|"
    r"\[start thinking\].*?\[end thinking\]|"
    r"\[start thinking\].*?(?=\n## |\n# |\Z)|"
    r"thinking process:.*?(?=\n## |\n# |\Z)"
)
_NOISE_LINE_RE = re.compile(
    r"(?ix)^(loading\s+model\b.*|build\s*:.*|model\s*:.*|ftype\s*:.*|modalities\s*:.*|"
    r"available\s+commands:.*|/exit\b.*|/regen\b.*|/clear\b.*|/read\b.*|/glob\b.*|"
    r".*stop or exit.*|.*regenerate the last.*|.*clear the chat.*|.*add a text file.*|"
    r".*globbing pattern.*|exiting\.{0,3})$"
)
_BLOCK_ART_RE = re.compile(r"^[\s█░▒▓▄▀━─│|/\\-]+$")


def clean_summary_text(raw: str | None) -> str:
    original = (raw or "").replace("\r\n", "\n").strip()
    if not original:
        return ""
    lines: list[str] = []
    for line in original.split("\n"):
        value = line.strip()
        if not value or (not _BLOCK_ART_RE.fullmatch(value) and not _NOISE_LINE_RE.fullmatch(value)):
            lines.append(line)
    text = _THINK_RE.sub("", "\n".join(lines))
    last = -1
    marker = ""
    for item in ("转写文本：", "转写："):
        index = text.rfind(item)
        if index > last:
            last = index
            marker = item
    if last >= 0:
        text = text[last + len(marker):].lstrip()
    text = _THINK_RE.sub("", text).strip()
    heading = re.search(r"(?m)^(?:##|#)\s+", text)
    if heading:
        text = text[heading.start():].strip()
    elif re.search(r"(?i)loading model|available commands:|\[start thinking\]|thinking process:", original):
        return ""
    return text.strip()


def summary_dict(item: Summary) -> dict[str, Any]:
    return {
        "id": item.id,
        "provider": item.provider,
        "model": item.model,
        "content_markdown": clean_summary_text(item.content_markdown),
        "template_id": item.template_id,
        "created_at": item.created_at.isoformat(),
    }


def video_dict(v: VideoAsset, session: Session | None = None) -> dict[str, Any]:
    payload = {
        "id": v.id,
        "source_url": v.source_url,
        "platform": v.platform or v.source_platform,
        "source_platform": v.source_platform or v.platform,
        "title": v.title,
        "author": v.author,
        "thumbnail_url": (
            f"{PUBLIC_API_URL}/api/videos/{v.id}/thumbnail"
            if v.thumbnail_path and Path(v.thumbnail_path).exists()
            else v.thumbnail_url
        ),
        "thumbnail_path": v.thumbnail_path,
        "duration_seconds": v.duration_seconds,
        "parse_status": v.parse_status,
        "available_formats": v.available_formats or [],
        "latest_summary_id": v.latest_summary_id,
        "created_at": v.created_at.isoformat(),
        "last_processed_at": (v.last_processed_at or v.updated_at or v.created_at).isoformat(),
    }
    if session is not None:
        payload.update(video_flags(session, v.id))
        payload["variants"] = [variant_dict(item) for item in session.scalars(select(MediaVariant).where(MediaVariant.video_asset_id == v.id)).all()]
    return payload


def job_dict(job: ProcessingJob) -> dict[str, Any]:
    return {
        "id": job.id,
        "video_id": job.video_asset_id,
        "job_type": job.job_type,
        "status": job.status,
        "progress": job.progress,
        "current_stage": job.current_stage,
        "input_media_type": job.input_media_type,
        "fallback_used": job.fallback_used,
        "error_code": job.error_code,
        "error_message": job.error_message,
        "created_at": job.created_at.isoformat(),
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
        "logs": job.logs_json or [],
        "progress_detail": job.progress_json or None,
    }


PLATFORMS = [
    {"id": "bilibili", "name": "B站", "logo": "bilibili.png", "featured": True},
    {"id": "douyin", "name": "抖音", "logo": "douyin.png", "featured": True},
    {"id": "kuaishou", "name": "快手", "logo": "kuaishou.png", "featured": True},
    {"id": "weibo", "name": "微博", "logo": "weibo.png", "featured": True},
    {"id": "xhs", "name": "小红书", "logo": "xhs.png", "featured": True},
    {"id": "youtube", "name": "YouTube", "logo": "youtube.png", "featured": True},
    {"id": "tiktok", "name": "TikTok", "logo": "tiktok.png", "featured": True},
    {"id": "zhihu", "name": "知乎", "logo": None, "featured": True},
    {"id": "twitter", "name": "Twitter", "logo": "twitter.png", "featured": False},
    {"id": "pixiv", "name": "Pixiv", "logo": "pixiv.png", "featured": False},
    {"id": "xiaoheihe", "name": "小黑盒", "logo": None, "featured": False},
    {"id": "shipinhao", "name": "微信视频号", "logo": None, "featured": False},
    {"id": "qzone", "name": "QQ空间", "logo": None, "featured": False},
]

PLATFORM_HINTS = (
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
    ("shipinhao", ("channels.weixin.qq.com",)),
    ("qzone", ("qzone.qq.com",)),
)

DEFAULT_PARSER = {
    "log_level": "info",
    "enable_proxy": False,
    "proxy": "",
    "source_max_size": 90,
    "source_max_minute": 15,
    "download_timeout": 280,
    "common_timeout": 15,
    "download_retry_times": 2,
    "temp_retain_hours": 24,
    "clean_cron": "30 2 * * *",
    "parsers": {},
}

DEFAULT_TRANSCRIPTION = {
    "moss_url": os.getenv("MOSS_URL", "http://localhost:9000"),
    "default_model": "MOSS-Transcribe-Diarize",
    "moss_model_path": DEFAULT_MOSS_MODEL_PATH,
    "default_language": "zh",
    "audio_format": "wav",
    "sample_rate": 16000,
    "fallback_to_mp4": True,
    "max_concurrency": 1,
    "keep_timestamps": True,
    "default_provider": "local",
    "summary_model": "Qwen3.5-4B-Q4_K_M",
    "summary_model_path": DEFAULT_SUMMARY_MODEL_PATH,
    "api_base_url": "",
    "api_key": "",
    "timeout": 60,
    "max_context": 8000,
    "chunk_size": 4000,
    "retry_times": 2,
}

DEFAULT_SERVICES = {
    "web_api_url": "http://localhost:8000",
    "moss_url": os.getenv("MOSS_URL", ""),
    "storage_root": str(STORAGE_ROOT),
    "temp_dir": str(STORAGE_ROOT / "tmp"),
    "cover_dir": str(STORAGE_ROOT / "covers"),
    "retention_days": 30,
    "auto_clean": True,
    "version": "0.1.0",
}

SETTING_DEFAULTS = {"parser": DEFAULT_PARSER, "transcription": DEFAULT_TRANSCRIPTION, "services": DEFAULT_SERVICES}


def detect_platform(url: str) -> str:
    lowered = url.lower()
    for platform_id, hints in PLATFORM_HINTS:
        if any(hint in lowered for hint in hints):
            return platform_id
    return "unknown"


def safe_filename(title: str, ext: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in " ._-" else "_" for ch in title).strip() or "video"
    return f"{cleaned[:80].rstrip('.')}.{ext.lstrip('.')}"


def mask_settings(group: str, data: dict[str, Any]) -> dict[str, Any]:
    payload = copy.deepcopy(data)
    if group == "transcription":
        secret = payload.get("api_key") or ""
        payload["api_key"] = ""
        payload["api_key_masked"] = f"{secret[:3]}****{secret[-2:]}" if len(secret) > 5 else ("已配置" if secret else "")
    for parser in (payload.get("parsers") or {}).values():
        cookies = parser.get("cookies") or ""
        if cookies:
            parser["cookies"] = ""
            parser["cookies_masked"] = "已保存"
    return payload


def merge_settings(group: str, stored: dict[str, Any] | None) -> dict[str, Any]:
    base = SETTING_DEFAULTS[group]
    merged = {**base, **(stored or {})}
    if group == "transcription":
        if merged.get("default_language") in (None, "", "auto"):
            merged["default_language"] = "zh"
        for key in ("moss_model_path", "summary_model_path", "moss_url", "default_model", "summary_model"):
            if not merged.get(key):
                merged[key] = base.get(key, "")
        # 旧数据默认是 Ollama；没有远程地址时改用本机 Qwen 小模型。
        if not (stored or {}).get("api_base_url") and merged.get("default_provider") in (None, "", "ollama"):
            merged["default_provider"] = "local"
        merged["moss_model_found"] = Path(str(merged.get("moss_model_path") or "")).exists()
        merged["summary_model_found"] = Path(str(merged.get("summary_model_path") or "")).exists()
    return merged


def persist_transcription_defaults() -> None:
    with SessionLocal() as session:
        record = session.get(ConfigRecord, "transcription")
        merged = merge_settings("transcription", record.value_json if record else {})
        stored = {
            key: value
            for key, value in merged.items()
            if key not in {"moss_model_found", "summary_model_found", "api_key_masked"}
        }
        if record is None:
            session.add(ConfigRecord(key="transcription", value_json=stored))
            session.commit()
            return
        if record.value_json != stored:
            record.value_json = stored
            session.commit()


def template_dict(item: SummaryTemplate) -> dict[str, Any]:
    return {
        "id": item.id,
        "name": item.name,
        "description": item.description,
        "origin": item.origin,
        "sections": item.sections_json or [],
        "show_on_home": bool(item.show_on_home),
        "can_delete": item.origin != "builtin",
    }


def seed_builtin_templates() -> None:
    with SessionLocal() as session:
        for item in load_builtin_templates():
            names = list({item["name"], *(item.get("aliases") or [])})
            rows = list(session.scalars(select(SummaryTemplate).where(SummaryTemplate.name.in_(names))).all())
            primary = next((row for row in rows if row.origin == "builtin"), rows[0] if rows else None)
            if primary:
                primary.name = item["name"]
                primary.origin = "builtin"
                primary.is_active = True
                if not primary.sections_json:
                    primary.description = item["description"]
                    primary.sections_json = item["sections"]
                for extra in rows:
                    if extra.id != primary.id:
                        extra.is_active = False
            else:
                session.add(SummaryTemplate(
                    name=item["name"],
                    description=item["description"],
                    origin="builtin",
                    sections_json=item["sections"],
                    show_on_home=item["name"] in HOME_DEFAULT_NAMES,
                ))
        session.commit()


seed_builtin_templates()
persist_transcription_defaults()


def current_transcription() -> dict[str, Any]:
    with SessionLocal() as session:
        record = session.get(ConfigRecord, "transcription")
        return merge_settings("transcription", record.value_json if record else {})


def moss_service_url(transcription: dict[str, Any] | None = None) -> str:
    settings = transcription if transcription is not None else current_transcription()
    return str(os.getenv("MOSS_URL") or settings.get("moss_url") or "http://localhost:9000").rstrip("/")


def probe_moss(url: str) -> dict[str, Any]:
    try:
        with httpx.Client(timeout=2.5) as client:
            response = client.get(f"{url}/health")
            response.raise_for_status()
            payload = response.json() if response.content else {}
            if not isinstance(payload, dict):
                payload = {"raw": payload}
            payload["reachable"] = True
            return payload
    except Exception as exc:
        return {"reachable": False, "error": str(exc)}


@app.get("/api/system/health")
def health():
    transcription = current_transcription()
    moss_url = moss_service_url(transcription)
    moss = probe_moss(moss_url)
    return {
        "status": "ok",
        "database": "connected",
        "moss_url": moss_url or "not-configured",
        "moss_reachable": bool(moss.get("reachable")),
        "moss_model_path": transcription["moss_model_path"],
        "moss_model_found": bool(moss.get("model_found") if moss.get("reachable") else transcription["moss_model_found"]),
        "moss_model_loaded": bool(moss.get("model_loaded")),
        "moss_model_loading": bool(moss.get("model_loading")),
        "moss_device": moss.get("device"),
        "moss_status": moss.get("status"),
        "moss_warmup_error": moss.get("warmup_error"),
        "moss_health": moss,
        "summary_model_path": transcription["summary_model_path"],
        "summary_model_found": transcription["summary_model_found"],
        "default_language": "zh",
    }


@app.post("/api/system/moss/warmup")
def warmup_moss():
    moss_url = moss_service_url()
    try:
        with httpx.Client(timeout=900.0) as client:
            response = client.post(f"{moss_url}/v1/models/load")
            if response.status_code >= 400:
                detail = response.text
                try:
                    detail = response.json().get("detail") or detail
                except Exception:
                    pass
                raise HTTPException(response.status_code, str(detail))
            payload = response.json() if response.content else {}
    except httpx.RequestError as exc:
        raise HTTPException(503, f"无法连接转录服务 {moss_url}：{exc}") from exc
    live = probe_moss(moss_url)
    return {
        "ok": True,
        "moss_url": moss_url,
        "model_loaded": bool((payload or {}).get("model_loaded") or live.get("model_loaded")),
        "device": (payload or {}).get("device") or live.get("device"),
        "moss_health": live,
    }


@app.get("/api/platforms")
def platforms():
    return PLATFORMS


@app.post("/api/auth/login")
def login(payload: dict[str, str]):
    if payload.get("username") != os.getenv("ADMIN_USERNAME", "admin") or payload.get("password") != os.getenv("ADMIN_PASSWORD", "change-me"):
        raise HTTPException(401, "用户名或密码错误")
    return {"token": os.getenv("API_SECRET", "local-dev-token"), "user": {"username": payload["username"]}}


def _related_videos(session: Session, parsed) -> list[VideoAsset]:
    matches: list[VideoAsset] = []
    seen: set[str] = set()

    def add(items: list[VideoAsset]) -> None:
        for item in items:
            if item.id not in seen:
                seen.add(item.id)
                matches.append(item)

    if parsed.platform_video_id:
        add(session.scalars(select(VideoAsset).where(VideoAsset.platform == parsed.platform, VideoAsset.platform_video_id == parsed.platform_video_id)).all())
    add(session.scalars(select(VideoAsset).where(VideoAsset.normalized_url == parsed.normalized_url)).all())
    if parsed.platform == "bilibili" and parsed.platform_video_id:
        token = parsed.platform_video_id
        add(session.scalars(select(VideoAsset).where(
            or_(VideoAsset.platform == "bilibili", VideoAsset.source_platform == "bilibili"),
            or_(VideoAsset.normalized_url.contains(token), VideoAsset.source_url.contains(token)),
        )).all())
    matches.sort(key=lambda item: not (item.thumbnail_path or item.thumbnail_url))
    return matches


@app.post("/api/parse")
def parse(request: ParseRequest, session: Session = Depends(db_session)):
    now = datetime.now(timezone.utc)
    try:
        parsed = parse_media(request.url)
    except ParseError as exc:
        raise HTTPException(502, str(exc)) from exc
    related = _related_videos(session, parsed)
    existing = related[0] if related else None
    video = existing or VideoAsset(source_url=parsed.source_url, normalized_url=parsed.normalized_url)
    video.source_url = parsed.source_url
    video.normalized_url = parsed.normalized_url
    video.platform = parsed.platform
    video.source_platform = parsed.platform
    video.platform_video_id = parsed.platform_video_id or video.platform_video_id
    video.title = parsed.title
    video.author = parsed.author
    video.duration_seconds = parsed.duration_seconds
    video.parse_status = "parsed"
    video.last_processed_at = now
    video.available_formats = [{"format": item.format, "quality": item.quality} for item in parsed.variants]
    if existing is None:
        session.add(video)
        session.flush()
    if parsed.thumbnail_url:
        video.thumbnail_url = parsed.thumbnail_url
        cover_path = _store_cover(video.id, parsed.thumbnail_url, parsed.source_url)
        if cover_path:
            video.thumbnail_path = str(cover_path)
    for old in session.scalars(select(MediaVariant).where(MediaVariant.video_asset_id == video.id)).all():
        session.delete(old)
    for item in parsed.variants:
        session.add(MediaVariant(
            video_asset_id=video.id,
            format=item.format,
            quality=item.quality,
            media_type=item.media_type,
            source_url=item.source_url,
            file_size=item.file_size,
            download_status="available",
        ))
    for extra in related[1:]:
        session.delete(extra)
    session.add(ProcessingJob(video_asset_id=video.id, job_type="parse", status="completed", current_stage="parsed", started_at=now, finished_at=now, progress=1))
    session.commit()
    session.refresh(video)
    return video_dict(video, session)


def _store_cover(video_id: str, url: str | None, referer: str) -> Path | None:
    if not url:
        return None
    try:
        payload = download_bytes(url, referer=cover_referer(url, referer))
    except Exception:
        return None
    if not payload:
        return None
    path = COVER_DIR / f"{video_id}.jpg"
    path.write_bytes(payload)
    return path


@app.get("/api/videos")
def list_videos(session: Session = Depends(db_session), q: str | None = None):
    query = select(VideoAsset).order_by(VideoAsset.updated_at.desc())
    if q:
        query = query.where(VideoAsset.title.ilike(f"%{q}%"))
    return [video_dict(v, session) for v in session.scalars(query).all()]


@app.get("/api/videos/recent")
def recent_videos(session: Session = Depends(db_session)):
    videos = session.scalars(
        select(VideoAsset).order_by(func.coalesce(VideoAsset.last_processed_at, VideoAsset.updated_at).desc()).limit(6)
    ).all()
    return [video_dict(v, session) for v in videos]


@app.get("/api/history")
def history(
    session: Session = Depends(db_session),
    q: str | None = None,
    platform: str | None = None,
    job_type: str | None = None,
    status: str | None = None,
):
    query = select(VideoAsset).order_by(VideoAsset.updated_at.desc())
    if q:
        like = f"%{q}%"
        query = query.where(or_(VideoAsset.title.ilike(like), VideoAsset.author.ilike(like), VideoAsset.platform.ilike(like)))
    if platform:
        query = query.where((VideoAsset.platform == platform) | (VideoAsset.source_platform == platform))
    result = []
    for video in session.scalars(query).all():
        jobs = session.scalars(select(ProcessingJob).where(ProcessingJob.video_asset_id == video.id).order_by(ProcessingJob.created_at.desc())).all()
        if job_type and not any(job.job_type == job_type for job in jobs):
            continue
        if status and not any(job.status == status for job in jobs):
            continue
        result.append(video_dict(video, session) | {"jobs": [job_dict(job) for job in jobs]})
    return result


@app.get("/api/history/{video_id}")
def history_detail(video_id: str, session: Session = Depends(db_session)):
    return get_video(video_id, session)


@app.get("/api/videos/{video_id}/variants")
def variants(video_id: str, session: Session = Depends(db_session)):
    if not session.get(VideoAsset, video_id): raise HTTPException(404, "视频不存在")
    return [{"id": x.id, "format": x.format, "quality": x.quality, "media_type": x.media_type, "download_status": x.download_status, "file_size": x.file_size} for x in session.scalars(select(MediaVariant).where(MediaVariant.video_asset_id == video_id)).all()]


@app.get("/api/videos/{video_id}")
def get_video(video_id: str, session: Session = Depends(db_session)):
    video = session.get(VideoAsset, video_id)
    if not video:
        raise HTTPException(404, "视频不存在")
    jobs = session.scalars(select(ProcessingJob).where(ProcessingJob.video_asset_id == video_id).order_by(ProcessingJob.created_at.desc())).all()
    transcripts = session.scalars(select(Transcript).where(Transcript.video_asset_id == video_id).order_by(Transcript.created_at.desc())).all()
    summaries = session.scalars(select(Summary).where(Summary.video_asset_id == video_id).order_by(Summary.created_at.desc())).all()
    return video_dict(video, session) | {
        "jobs": [job_dict(job) for job in jobs],
        "transcripts": [{"id": t.id, "model": t.model, "language": t.language, "text": t.text, "segments": t.segments_json, "created_at": t.created_at.isoformat()} for t in transcripts],
        "summaries": [summary_dict(s) for s in summaries],
    }


@app.get("/api/videos/{video_id}/thumbnail")
def thumbnail(video_id: str, session: Session = Depends(db_session)):
    video = session.get(VideoAsset, video_id)
    if not video or not video.thumbnail_path:
        raise HTTPException(404, "封面不存在")
    path = Path(video.thumbnail_path)
    if not path.exists():
        raise HTTPException(404, "封面不存在")
    return FileResponse(path, media_type="image/jpeg")


@app.get("/api/videos/{video_id}/download/{variant_id}")
def download_variant(video_id: str, variant_id: str, session: Session = Depends(db_session)):
    video = session.get(VideoAsset, video_id)
    variant = session.get(MediaVariant, variant_id)
    if not video or not variant or variant.video_asset_id != video_id:
        raise HTTPException(404, "媒体资源不存在")
    now = datetime.now(timezone.utc)
    job = ProcessingJob(video_asset_id=video_id, job_type="download", status="running", current_stage="download", started_at=now)
    session.add(job)
    video.last_processed_at = now
    if variant.local_path and Path(variant.local_path).exists():
        job.status = "completed"
        job.current_stage = "completed"
        job.finished_at = now
        job.progress = 1
        variant.download_status = "downloaded"
        session.commit()
        filename = safe_filename(video.title, variant.format or "mp4")
        return FileResponse(
            variant.local_path,
            media_type="video/mp4" if variant.format == "mp4" else "application/octet-stream",
            filename=filename,
            content_disposition_type="attachment",
            headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"},
        )
    if variant.source_url:
        play_url = variant.source_url
        try:
            payload = download_bytes(play_url, referer=cover_referer(play_url, video.source_url))
        except Exception:
            payload = None
            if (video.platform or video.source_platform) == "bilibili" and (video.platform_video_id or "").startswith("BV"):
                play_url = bili_play_url(video.platform_video_id, variant.quality)
                if play_url:
                    try:
                        payload = download_bytes(play_url, referer="https://www.bilibili.com/")
                        variant.source_url = play_url
                    except Exception:
                        payload = None
            if payload is None:
                job.status = "failed"
                job.finished_at = now
                job.error_code = "DOWNLOAD_FAILED"
                job.error_message = "下载失败：媒体地址已失效或无法访问"
                variant.download_status = "failed"
                session.commit()
                raise HTTPException(502, job.error_message)
        job.status = "completed"
        job.current_stage = "completed"
        job.finished_at = now
        job.progress = 1
        variant.download_status = "downloaded"
        variant.file_size = len(payload)
        session.commit()
        filename = safe_filename(video.title, variant.format or "mp4")
        return Response(
            content=payload,
            media_type="video/mp4" if variant.format == "mp4" else "application/octet-stream",
            headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"},
        )
    job.status = "failed"
    job.current_stage = "download"
    job.finished_at = now
    job.error_code = "ADAPTER_PENDING"
    job.error_message = "解析器尚未提供可下载文件，无法直接保存到浏览器。"
    variant.download_status = "failed"
    session.commit()
    raise HTTPException(409, job.error_message)


@app.post("/api/jobs")
def create_job(request: JobRequest, session: Session = Depends(db_session)):
    if not session.get(VideoAsset, request.video_id): raise HTTPException(404, "视频不存在")
    job = ProcessingJob(video_asset_id=request.video_id, job_type=request.job_type, current_stage="queued")
    session.add(job); session.commit(); session.refresh(job)
    return {"id": job.id, "video_id": job.video_asset_id, "status": job.status, "progress": job.progress, "current_stage": job.current_stage}


def _create_media_job(session: Session, video: VideoAsset, job_type: str, message: str) -> ProcessingJob:
    now = datetime.now(timezone.utc)
    job = ProcessingJob(
        video_asset_id=video.id,
        job_type=job_type,
        status="running",
        input_media_type="audio",
        current_stage="queued",
        started_at=now,
        progress=0.04,
        logs_json=[{"at": now.isoformat(), "level": "info", "message": message}],
    )
    video.last_processed_at = now
    session.add(job)
    session.commit()
    session.refresh(job)
    return job


@app.post("/api/videos/{video_id}/transcribe")
def transcribe(video_id: str, payload: dict[str, Any] | None = None, session: Session = Depends(db_session)):
    video = session.get(VideoAsset, video_id)
    if not video:
        raise HTTPException(404, "视频不存在")
    body = payload or {}
    settings = current_transcription()
    language = (body.get("transcription") or {}).get("language") or settings.get("default_language") or "zh"
    job = _create_media_job(session, video, "transcribe", "转录任务已创建")
    threading.Thread(target=execute_transcribe_job, args=(job.id, language), daemon=True).start()
    return job_dict(job)


def _append_job_log(session: Session, job: ProcessingJob, message: str, level: str = "info", stage: str | None = None, progress: float | None = None, detail: dict[str, Any] | None = None) -> None:
    logs = list(job.logs_json or [])
    if not logs or logs[-1].get("message") != message:
        logs.append({"at": datetime.now(timezone.utc).isoformat(), "level": level, "message": message})
        job.logs_json = logs
    if stage:
        job.current_stage = stage
    if progress is not None:
        job.progress = progress
    if detail is not None:
        job.progress_json = detail
    if job.status == "queued":
        job.status = "running"
        job.started_at = job.started_at or datetime.now(timezone.utc)
    session.commit()


def _transcribe_progress(payload: dict[str, Any], waited: int, audio_seconds: float | None) -> tuple[float, dict[str, Any], str]:
    raw = float(payload.get("progress") or 0)
    moss_ratio = raw / 100 if raw > 1 else raw
    elapsed = int(payload.get("elapsed_seconds") or waited * 2)
    audio = payload.get("audio_seconds")
    if audio is None:
        audio = audio_seconds
    audio = float(audio or 0) or None
    stage = str(payload.get("stage") or "transcribing")
    rtf = 1.8 if stage == "transcribing" else 1.0
    estimate = 90 if stage == "loading_model" else (max(audio * rtf, 30) if audio else 160)
    time_ratio = min(0.93, elapsed / estimate) if estimate else 0
    from_time = 0.30 + 0.42 * time_ratio
    from_moss = 0.28 + 0.46 * moss_ratio
    overall = from_moss if moss_ratio >= 0.15 else max(from_moss, from_time)
    overall = min(0.74, max(0.28, overall))
    eta = payload.get("eta_seconds")
    if eta is None and audio:
        eta = max(0, int(estimate - elapsed))
    message = str(payload.get("message") or "正在转录")
    detail = {
        "stage": stage,
        "message": message,
        "percent": int(round(overall * 100)),
        "elapsed_seconds": elapsed,
        "audio_seconds": audio,
        "eta_seconds": eta,
    }
    return overall, detail, message


def _fail_job(session: Session, job: ProcessingJob, message: str, stage: str) -> None:
    _append_job_log(session, job, message, "error", stage=stage)
    job.status = "failed"
    job.error_message = message
    job.finished_at = datetime.now(timezone.utc)
    session.commit()


def _complete_job(session: Session, job: ProcessingJob, message: str, progress: float = 1) -> None:
    _append_job_log(session, job, message, "info", "completed", progress, {
        "stage": "completed",
        "message": message,
        "percent": 100,
    })
    job.status = "completed"
    job.current_stage = "completed"
    job.progress = 1
    job.finished_at = datetime.now(timezone.utc)
    session.commit()


def _latest_transcript(session: Session, video_id: str) -> Transcript | None:
    return session.scalars(
        select(Transcript).where(Transcript.video_asset_id == video_id).order_by(Transcript.created_at.desc())
    ).first()


def _save_transcript(session: Session, job: ProcessingJob, result: dict[str, Any], language: str) -> Transcript:
    text = str(result.get("text") or "").strip()
    item = Transcript(
        job_id=job.id,
        video_asset_id=job.video_asset_id,
        model=str(result.get("model") or "MOSS-Transcribe-Diarize"),
        language=language or "zh",
        text=text,
        segments_json=result.get("segments") or [],
        duration_seconds=result.get("audio_seconds"),
    )
    session.add(item)
    session.commit()
    session.refresh(item)
    return item


def _run_moss_transcription(session: Session, job: ProcessingJob, video: VideoAsset, language: str) -> Transcript | None:
    settings = merge_settings("transcription", (session.get(ConfigRecord, "transcription") or ConfigRecord(key="transcription", value_json={})).value_json)
    moss_url = moss_service_url(settings)
    variants = session.scalars(select(MediaVariant).where(MediaVariant.video_asset_id == job.video_asset_id)).all()
    media = next((item for item in variants if item.source_url or item.local_path), None)
    _append_job_log(session, job, f"开始处理「{video.title}」", "info", "extracting_audio", 0.08)
    if not media:
        _fail_job(session, job, "当前解析结果没有可下载媒体，无法提取音频", "extracting_audio")
        return None
    label = media.quality or media.format or "媒体文件"
    _append_job_log(session, job, f"找到媒体：{label}", "info", "extracting_audio", 0.16)
    if media.local_path and Path(media.local_path).exists():
        media_url = f"{PUBLIC_API_URL}/api/videos/{job.video_asset_id}/download/{media.id}"
    else:
        media_url = media.source_url
    _append_job_log(session, job, "正在提交转录任务", "info", "transcribe_queued", 0.28)
    try:
        with httpx.Client(timeout=20.0) as client:
            created = client.post(
                f"{moss_url}/v1/transcriptions",
                json={
                    "media_url": media_url,
                    "language": language or "zh",
                    "duration_seconds": video.duration_seconds,
                    "referer": video.source_url,
                },
            )
    except httpx.RequestError as exc:
        _fail_job(session, job, f"无法连接转录服务 {moss_url}：{exc}", "transcribe_queued")
        return None
    if created.status_code >= 400:
        detail = created.text
        try:
            detail = created.json().get("detail") or detail
        except ValueError:
            pass
        _fail_job(session, job, f"转录服务拒绝任务：{detail}", "transcribe_queued")
        return None
    moss_id = created.json().get("id")
    _append_job_log(session, job, "转录服务已接收", "info", "transcribing", 0.3, {
        "stage": "queued",
        "message": "转录服务已接收",
        "percent": 30,
    })
    result = None
    last_stage = ""
    for waited in range(900):
        time.sleep(2)
        job = session.get(ProcessingJob, job.id)
        if not job or job.status == "cancelled":
            return None
        try:
            with httpx.Client(timeout=15.0) as client:
                status = client.get(f"{moss_url}/v1/transcriptions/{moss_id}")
        except httpx.RequestError as exc:
            _fail_job(session, job, f"轮询转录服务失败：{exc}", "transcribing")
            return None
        if status.status_code >= 400:
            _fail_job(session, job, "转录任务查询失败", "transcribing")
            return None
        payload = status.json()
        moss_status = payload.get("status")
        overall, detail, _message = _transcribe_progress(payload, waited, video.duration_seconds)
        stage = str(payload.get("stage") or "")
        stage_log = {
            "downloading": "正在下载媒体",
            "converting": "正在提取音频",
            "loading_model": "正在加载转录模型",
            "transcribing": "正在转录推理",
            "completed": "转录完成",
        }.get(stage)
        log_text = stage_log if stage and stage != last_stage else ((job.logs_json or [{}])[-1].get("message") or "正在转录")
        last_stage = stage or last_stage
        _append_job_log(session, job, log_text, "info", "transcribing", overall, detail)
        if moss_status == "completed":
            result = payload.get("result") or {}
            break
        if moss_status == "failed":
            _fail_job(session, job, payload.get("error") or payload.get("message") or "转录失败", "transcribing")
            return None
    if result is None:
        _fail_job(session, job, "转录超时，请稍后重试", "transcribing")
        return None
    transcript = _save_transcript(session, job, result, language)
    _append_job_log(session, job, f"转录完成，共 {len(transcript.text)} 字", "info", "transcribed", 0.72)
    return transcript


def _http_error_detail(response: httpx.Response) -> str:
    try:
        payload = response.json()
        return str(payload.get("detail") or payload.get("error") or payload)
    except Exception:
        return response.text or f"HTTP {response.status_code}"


def _chat_summary(settings: dict[str, Any], prompt: str) -> tuple[str, str]:
    base = str(settings.get("api_base_url") or "").rstrip("/")
    provider = str(settings.get("default_provider") or "local")
    model = str(settings.get("summary_model") or "Qwen3.5-4B-Q4_K_M")
    if not base:
        raise RuntimeError("未配置 API Base URL")
    headers = {"Content-Type": "application/json"}
    secret = str(settings.get("api_key") or "")
    if secret:
        headers["Authorization"] = f"Bearer {secret}"
    timeout = max(float(settings.get("timeout") or 60), 180)
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": "你是视频总结助手，只用简体中文输出 Markdown。"},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.2,
    }
    url = f"{base}/chat/completions" if base.endswith("/v1") or "/v1" in base else f"{base}/v1/chat/completions"
    with httpx.Client(timeout=timeout) as client:
        if provider == "ollama" and not base.endswith("/v1"):
            response = client.post(f"{base}/api/chat", json={"model": model, "messages": body["messages"], "stream": False})
            if response.status_code < 400:
                payload = response.json()
                text = str((payload.get("message") or {}).get("content") or "").strip()
                if text:
                    return text, model
        response = client.post(url, headers=headers, json=body)
        if response.status_code >= 400:
            raise RuntimeError(_http_error_detail(response))
        payload = response.json()
        text = str(((payload.get("choices") or [{}])[0].get("message") or {}).get("content") or "").strip()
        if not text:
            raise RuntimeError("总结接口没有返回内容")
        return text, str(payload.get("model") or model)


def _local_summary(settings: dict[str, Any], prompt_payload: dict[str, Any]) -> tuple[str, str]:
    moss_url = moss_service_url(settings)
    timeout = max(float(settings.get("timeout") or 60), 600)
    with httpx.Client(timeout=timeout) as client:
        response = client.post(f"{moss_url}/v1/summaries", json=prompt_payload)
    if response.status_code >= 400:
        raise RuntimeError(_http_error_detail(response))
    payload = response.json()
    text = str(payload.get("text") or "").strip()
    if not text:
        raise RuntimeError("本地总结没有返回内容")
    return text, str(payload.get("model") or settings.get("summary_model") or "local")


def _wait_summary(session: Session, job: ProcessingJob, settings: dict[str, Any], template: SummaryTemplate | None, transcript: Transcript, title: str) -> tuple[str, str]:
    box: dict[str, Any] = {"done": False, "result": None, "error": None}

    def worker() -> None:
        try:
            provider = str(settings.get("default_provider") or "local")
            if provider in {"openai", "ollama"} or settings.get("api_base_url"):
                box["result"] = _chat_summary(settings, _summary_user_prompt(template, transcript.text, title))
            else:
                box["result"] = _local_summary(settings, {
                    "text": transcript.text,
                    "title": title,
                    "template_name": template.name if template else None,
                    "sections": template.sections_json if template else [],
                })
        except Exception as exc:
            box["error"] = exc
        finally:
            box["done"] = True

    threading.Thread(target=worker, daemon=True, name="summary-wait").start()
    started = time.time()
    while not box["done"]:
        time.sleep(2)
        current = session.get(ProcessingJob, job.id)
        if not current or current.status == "cancelled":
            raise RuntimeError("任务已取消")
        elapsed = int(time.time() - started)
        progress = min(0.96, 0.82 + elapsed / 500)
        _append_job_log(
            session,
            current,
            f"正在生成总结",
            "info",
            "summarizing",
            progress,
            {"stage": "summarizing", "message": f"正在生成总结，已用 {elapsed} 秒", "percent": int(progress * 100), "elapsed_seconds": elapsed},
        )
    if box["error"]:
        raise box["error"]
    return box["result"]


def _summary_user_prompt(template: SummaryTemplate | None, text: str, title: str) -> str:
    sections = (template.sections_json if template else None) or [{"title": "摘要", "instruction": "概括主要内容"}]
    lines = [f"请总结视频「{title}」的转写。按以下章节输出 Markdown："]
    for item in sections:
        lines.append(f"## {item.get('title')}\n{item.get('instruction') or ''}")
    lines.extend(["", "转写：", text[:14000]])
    return "\n".join(lines)


def _generate_summary(session: Session, job: ProcessingJob, video: VideoAsset, transcript: Transcript, template_id: str | None) -> None:
    settings = merge_settings("transcription", (session.get(ConfigRecord, "transcription") or ConfigRecord(key="transcription", value_json={})).value_json)
    template = session.get(SummaryTemplate, template_id) if template_id else None
    if not transcript.text.strip():
        _fail_job(session, job, "转录结果为空，无法生成总结", "summarizing")
        return
    _append_job_log(session, job, "正在生成总结", "info", "summarizing", 0.82, {
        "stage": "summarizing",
        "message": "正在生成总结",
        "percent": 82,
    })
    provider = str(settings.get("default_provider") or "local")
    try:
        content, model = _wait_summary(session, job, settings, template, transcript, video.title)
        content = clean_summary_text(content)
        if not content:
            raise RuntimeError("总结模型没有返回可用结果，请重试")
    except Exception as exc:
        _fail_job(session, job, f"总结失败：{exc}", "summarizing")
        return
    job = session.get(ProcessingJob, job.id)
    video = session.get(VideoAsset, video.id)
    if not job or not video:
        return
    item = Summary(
        job_id=job.id,
        video_asset_id=job.video_asset_id,
        template_id=template.id if template else None,
        provider=provider,
        model=model,
        content_markdown=content,
        source_transcript_id=transcript.id,
    )
    session.add(item)
    video.latest_summary_id = item.id
    session.commit()
    _complete_job(session, job, "总结完成")


def execute_transcribe_job(job_id: str, language: str) -> None:
    with SessionLocal() as session:
        job = session.get(ProcessingJob, job_id)
        if not job:
            return
        video = session.get(VideoAsset, job.video_asset_id)
        if not video:
            _fail_job(session, job, "视频不存在", "queued")
            return
        transcript = _run_moss_transcription(session, job, video, language)
        if not transcript:
            return
        job = session.get(ProcessingJob, job_id)
        if job:
            _complete_job(session, job, "转录完成")


def execute_summary_job(job_id: str, template_id: str | None, language: str, reuse_transcript: bool = True) -> None:
    with SessionLocal() as session:
        job = session.get(ProcessingJob, job_id)
        if not job:
            return
        video = session.get(VideoAsset, job.video_asset_id)
        if not video:
            _fail_job(session, job, "视频不存在", "queued")
            return
        transcript = _latest_transcript(session, job.video_asset_id) if reuse_transcript else None
        if transcript and transcript.text.strip():
            _append_job_log(session, job, f"使用已有转录，共 {len(transcript.text)} 字", "info", "transcribed", 0.72)
        else:
            transcript = _run_moss_transcription(session, job, video, language)
            if not transcript:
                return
        job = session.get(ProcessingJob, job_id)
        video = session.get(VideoAsset, job.video_asset_id) if job else None
        if not job or not video:
            return
        _append_job_log(session, job, "准备生成总结", "info", "summarize_queued", 0.8)
        _generate_summary(session, job, video, transcript, template_id)


def _summary_ready(settings: dict[str, Any]) -> bool:
    provider = str(settings.get("default_provider") or "local")
    if provider == "local":
        return bool(settings.get("summary_model_found"))
    return bool(settings.get("api_base_url") or provider)


@app.post("/api/videos/{video_id}/summary")
def summary(video_id: str, payload: dict[str, Any] | None = None, session: Session = Depends(db_session)):
    video = session.get(VideoAsset, video_id)
    if not video:
        raise HTTPException(404, "视频不存在")
    settings = current_transcription()
    if not _summary_ready(settings):
        raise HTTPException(400, "总结模型未配置")
    body = payload or {}
    language = (body.get("transcription") or {}).get("language") or settings.get("default_language") or "zh"
    job = _create_media_job(session, video, "summarize", "总结任务已创建")
    threading.Thread(
        target=execute_summary_job,
        args=(job.id, body.get("template_id"), language, bool(body.get("reuse_transcript", True))),
        daemon=True,
    ).start()
    return job_dict(job)


@app.post("/api/videos/{video_id}/summaries")
def summary_from_transcript(video_id: str, payload: dict[str, Any] | None = None, session: Session = Depends(db_session)):
    video = session.get(VideoAsset, video_id)
    if not video:
        raise HTTPException(404, "视频不存在")
    settings = current_transcription()
    if not _summary_ready(settings):
        raise HTTPException(400, "总结模型未配置")
    if not _latest_transcript(session, video_id):
        raise HTTPException(409, "还没有转录结果，请先转录")
    body = payload or {}
    language = settings.get("default_language") or "zh"
    job = _create_media_job(session, video, "summarize", "根据已有转录生成总结")
    threading.Thread(target=execute_summary_job, args=(job.id, body.get("template_id"), language, True), daemon=True).start()
    return job_dict(job)


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str, session: Session = Depends(db_session)):
    job = session.get(ProcessingJob, job_id)
    if not job: raise HTTPException(404, "任务不存在")
    return job_dict(job)


@app.post("/api/jobs/{job_id}/retry")
def retry_job(job_id: str, session: Session = Depends(db_session)):
    job = session.get(ProcessingJob, job_id)
    if not job: raise HTTPException(404, "任务不存在")
    job.status, job.progress, job.current_stage, job.error_message = "queued", 0, "queued", None
    session.commit(); return job_dict(job)


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str, session: Session = Depends(db_session)):
    job = session.get(ProcessingJob, job_id)
    if not job: raise HTTPException(404, "任务不存在")
    job.status, job.current_stage = "cancelled", "cancelled"
    session.commit(); return job_dict(job)


@app.get("/api/jobs/{job_id}/events")
def job_events(job_id: str, session: Session = Depends(db_session)):
    if not session.get(ProcessingJob, job_id): raise HTTPException(404, "任务不存在")
    def stream():
        yield "event: status\ndata: {\"job_id\": \"%s\", \"status\": \"queued\", \"progress\": 0}\n\n" % job_id
    return StreamingResponse(stream(), media_type="text/event-stream")


@app.get("/api/templates")
def list_templates(listed: bool | None = Query(None), session: Session = Depends(db_session)):
    query = select(SummaryTemplate).where(SummaryTemplate.is_active.is_(True))
    if listed:
        query = query.where(SummaryTemplate.show_on_home.is_(True))
    items = [template_dict(item) for item in session.scalars(query).all()]
    items.sort(key=lambda item: template_sort_key(item["name"]))
    return items


def config_endpoint(key: str, session: Session, payload: dict[str, Any] | None = None):
    record = session.get(ConfigRecord, key)
    current = merge_settings(key, record.value_json if record else {})
    if payload is not None:
        next_value = {**current, **payload}
        next_value.pop("moss_model_found", None)
        next_value.pop("summary_model_found", None)
        next_value.pop("api_key_masked", None)
        if key == "transcription" and not payload.get("api_key"):
            next_value["api_key"] = current.get("api_key") or ""
        if key == "parser":
            parsers = {**(current.get("parsers") or {}), **(payload.get("parsers") or {})}
            for parser_id, item in parsers.items():
                previous = (current.get("parsers") or {}).get(parser_id) or {}
                if item.get("cookies") in (None, ""):
                    item["cookies"] = previous.get("cookies") or ""
            next_value["parsers"] = parsers
        if record is None:
            record = ConfigRecord(key=key, value_json=next_value)
            session.add(record)
        else:
            record.value_json = next_value
        session.commit()
        current = merge_settings(key, next_value)
    return mask_settings(key, current)


@app.get("/api/settings/{group}")
def get_settings(group: str, session: Session = Depends(db_session)):
    if group not in SETTING_DEFAULTS:
        raise HTTPException(404, "设置分组不存在")
    return config_endpoint(group, session)


@app.put("/api/settings/{group}")
def put_settings(group: str, payload: dict[str, Any], session: Session = Depends(db_session)):
    if group not in SETTING_DEFAULTS:
        raise HTTPException(404, "设置分组不存在")
    return config_endpoint(group, session, payload)


@app.post("/api/templates")
def create_template(request: TemplateRequest, session: Session = Depends(db_session)):
    if not (request.name or "").strip():
        raise HTTPException(400, "请填写模板名称")
    template = SummaryTemplate(
        name=request.name.strip(),
        description=(request.description or "").strip(),
        sections_json=request.sections or [],
        show_on_home=True if request.show_on_home is None else request.show_on_home,
        origin="custom",
    )
    session.add(template)
    session.commit()
    session.refresh(template)
    return template_dict(template)


@app.put("/api/templates/{template_id}")
def update_template(template_id: str, request: TemplateRequest, session: Session = Depends(db_session)):
    template = session.get(SummaryTemplate, template_id)
    if not template or not template.is_active:
        raise HTTPException(404, "模板不存在")
    if request.name is not None:
        if not request.name.strip():
            raise HTTPException(400, "请填写模板名称")
        template.name = request.name.strip()
    if request.description is not None:
        template.description = request.description.strip()
    if request.sections is not None:
        template.sections_json = request.sections
    if request.show_on_home is not None:
        template.show_on_home = request.show_on_home
    session.commit()
    return template_dict(template)


@app.post("/api/templates/{template_id}/restore")
def restore_template(template_id: str, session: Session = Depends(db_session)):
    template = session.get(SummaryTemplate, template_id)
    if not template or not template.is_active:
        raise HTTPException(404, "模板不存在")
    if template.origin != "builtin":
        raise HTTPException(400, "只有内置模板可以恢复")
    builtin = find_builtin_template(template.name)
    if not builtin:
        raise HTTPException(404, "找不到对应的内置模板")
    template.name = builtin["name"]
    template.description = builtin["description"]
    template.sections_json = builtin["sections"]
    session.commit()
    return template_dict(template)


@app.delete("/api/templates/{template_id}")
def delete_template(template_id: str, session: Session = Depends(db_session)):
    template = session.get(SummaryTemplate, template_id)
    if not template:
        raise HTTPException(404, "模板不存在")
    if template.origin == "builtin":
        raise HTTPException(400, "内置模板不可删除，可以关闭首页显示或恢复默认")
    template.is_active = False
    session.commit()
    return {"deleted": template_id}


@app.delete("/api/videos/{video_id}")
def delete_video(video_id: str, session: Session = Depends(db_session)):
    video = session.get(VideoAsset, video_id)
    if not video: raise HTTPException(404, "视频不存在")
    session.delete(video); session.commit()
    return {"deleted": video_id}
