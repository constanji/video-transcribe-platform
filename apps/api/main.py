from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
import traceback
import uuid
import base64
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from urllib.parse import quote

import httpx
from cryptography.fernet import Fernet, InvalidToken
from fastapi import Depends, FastAPI, File, HTTPException, Query, UploadFile
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
COOKIE_ENCRYPTION_KEY = os.getenv("COOKIE_ENCRYPTION_KEY", "")
COVER_DIR = STORAGE_ROOT / "covers"
COVER_DIR.mkdir(parents=True, exist_ok=True)
UPLOAD_DIR = STORAGE_ROOT / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
MEETING_DIR = STORAGE_ROOT / "meetings"
MEETING_DIR.mkdir(parents=True, exist_ok=True)
TEMP_DIR = STORAGE_ROOT / "tmp"
TEMP_DIR.mkdir(parents=True, exist_ok=True)
MAX_UPLOAD_BYTES = 500 * 1024 * 1024
UPLOAD_FORMATS = {".mp3": "audio", ".wav": "audio", ".m4a": "audio", ".mp4": "video", ".webm": "video", ".mov": "video"}
connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def _cookie_cipher() -> Fernet:
    secret = COOKIE_ENCRYPTION_KEY or os.getenv("API_SECRET", "local-dev-token")
    # Derive a Fernet-compatible key so deployments can provide a normal secret
    # without needing to pre-format it. COOKIE_ENCRYPTION_KEY is preferred.
    key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode("utf-8")).digest())
    return Fernet(key)


def encrypt_cookie(value: str) -> str:
    if not value:
        return ""
    if value.startswith("enc:v1:"):
        return value
    token = _cookie_cipher().encrypt(value.encode("utf-8")).decode("ascii")
    return f"enc:v1:{token}"


def decrypt_cookie(value: str) -> str:
    if not value:
        return ""
    if not value.startswith("enc:v1:"):
        # Legacy installations stored the value directly. It is re-encrypted
        # when settings are next persisted.
        return value
    try:
        return _cookie_cipher().decrypt(value[7:].encode("ascii")).decode("utf-8")
    except (InvalidToken, UnicodeDecodeError, ValueError) as exc:
        raise RuntimeError("Cookie 加密密钥不匹配，无法读取已保存的 Cookie") from exc


def transform_parser_cookies(data: dict[str, Any], encrypt: bool = False) -> tuple[dict[str, Any], bool]:
    payload = copy.deepcopy(data)
    changed = False
    for parser in (payload.get("parsers") or {}).values():
        value = str(parser.get("cookies") or "")
        if encrypt:
            encoded = encrypt_cookie(value)
            changed = changed or encoded != value
            parser["cookies"] = encoded
        else:
            decoded = decrypt_cookie(value)
            changed = changed or decoded != value
            parser["cookies"] = decoded
    return payload, changed


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


class ApiKey(Base):
    __tablename__ = "api_keys"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String(255), default="默认密钥")
    prefix: Mapped[str] = mapped_column(String(16))
    hashed: Mapped[str] = mapped_column(String(64), index=True)
    revoked: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Meeting(Base):
    __tablename__ = "meetings"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    title: Mapped[str] = mapped_column(String(500), default="未命名会议")
    mode: Mapped[str] = mapped_column(String(32), default="live")
    status: Mapped[str] = mapped_column(String(32), default="created")
    language: Mapped[str] = mapped_column(String(32), default="zh")
    audio_source: Mapped[str] = mapped_column(String(32), default="mic")
    audio_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    video_asset_id: Mapped[str | None] = mapped_column(ForeignKey("video_assets.id", ondelete="SET NULL"), nullable=True, index=True)
    api_key_id: Mapped[str | None] = mapped_column(ForeignKey("api_keys.id", ondelete="SET NULL"), nullable=True)
    live_segments_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
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
        if "input_media_type" not in job_cols:
            conn.execute(text("ALTER TABLE processing_jobs ADD COLUMN input_media_type VARCHAR(16)"))
        if "fallback_used" not in job_cols:
            conn.execute(text("ALTER TABLE processing_jobs ADD COLUMN fallback_used BOOLEAN DEFAULT 0"))
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
_cors_origins = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", "").split(",") if o.strip()]
if not _cors_origins:
    # Local development default; deployments must set ALLOWED_ORIGINS.
    _cors_origins = ["http://localhost:3000", "http://127.0.0.1:3000"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_origin_regex=os.getenv("ALLOWED_ORIGIN_REGEX", r"http://(localhost|127\.0\.0\.1):\d+"),
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
    {"id": "meeting", "name": "会议纪要", "logo": None, "featured": False},
    {"id": "upload", "name": "本地上传", "logo": None, "featured": False},
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
    if group == "parser":
        merged, _ = transform_parser_cookies(merged)
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
        parser_settings = merge_settings(
            "parser",
            (session.get(ConfigRecord, "parser") or ConfigRecord(key="parser", value_json={})).value_json,
        )
        shipinhao = (parser_settings.get("parsers") or {}).get("shipinhao") or {}
        parsed = parse_media(request.url, shipinhao_cookie=str(shipinhao.get("cookies") or ""))
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


def _storage_relative(path: Path) -> Path | None:
    try:
        return path.resolve().relative_to(STORAGE_ROOT.resolve())
    except ValueError:
        return None


def _moss_media_root() -> str:
    configured = os.getenv("MOSS_MEDIA_ROOT")
    if configured is not None:
        return configured.rstrip("/")
    # Windows 上 MOSS 跑在 Docker 里，把 STORAGE_ROOT 挂到容器 /data
    return "/data" if os.name == "nt" else ""


def path_for_moss(host_path: str | None) -> str | None:
    if not host_path:
        return None
    path = Path(host_path)
    root = _moss_media_root()
    rel = _storage_relative(path)
    if root and rel is not None:
        return f"{root}/{rel.as_posix()}"
    return str(path)


def _ffprobe_bin() -> str | None:
    return shutil.which("ffprobe") or shutil.which("ffprobe.exe")


def _docker_ffprobe(path: Path) -> subprocess.CompletedProcess[str]:
    rel = _storage_relative(path)
    if rel is None:
        raise HTTPException(503, "服务器未安装 FFmpeg，且文件不在可挂载的 storage 目录")
    container = os.getenv("MOSS_DOCKER_CONTAINER", "moss-transcribe")
    inner = f"{_moss_media_root() or '/data'}/{rel.as_posix()}"
    return subprocess.run(
        ["docker", "exec", container, "ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_type", "-of", "json", inner],
        capture_output=True, text=True, timeout=30,
    )


def _probe_media(path: Path) -> dict[str, Any]:
    args = ["-v", "error", "-show_entries", "format=duration:stream=codec_type", "-of", "json", str(path)]
    ffprobe = _ffprobe_bin()
    try:
        if ffprobe:
            process = subprocess.run([ffprobe, *args], capture_output=True, text=True, timeout=30)
        else:
            process = _docker_ffprobe(path)
    except FileNotFoundError as exc:
        raise HTTPException(503, "服务器未安装 FFmpeg，无法校验媒体文件") from exc
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(400, "媒体文件校验超时，请确认文件未损坏") from exc
    if process.returncode != 0:
        detail = (process.stderr or process.stdout or "").strip()
        if "No such file" in detail or "unable to find" in detail.lower():
            raise HTTPException(503, "转录容器无法读取上传文件，请确认已挂载 storage 目录")
        raise HTTPException(400, "无法识别该媒体文件，请确认文件未损坏")
    try:
        payload = json.loads(process.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise HTTPException(400, "无法识别该媒体文件，请确认文件未损坏") from exc
    if not isinstance(payload, dict):
        raise HTTPException(400, "无法识别该媒体文件，请确认文件未损坏")
    return payload


def _uploaded_media_info(path: Path, expected_type: str) -> float | None:
    payload = _probe_media(path)
    stream_types = {str(item.get("codec_type")) for item in payload.get("streams") or []}
    # 转录只要求音轨；纯音频 MP4 / 录音 WebM 不应因为没有视频轨被拒
    if "audio" not in stream_types:
        raise HTTPException(400, "文件中未检测到可转录的音轨")
    try:
        duration = float((payload.get("format") or {}).get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0
    return duration or None


def _video_detail(video: VideoAsset, session: Session) -> dict[str, Any]:
    jobs = session.scalars(select(ProcessingJob).where(ProcessingJob.video_asset_id == video.id).order_by(ProcessingJob.created_at.desc())).all()
    transcripts = session.scalars(select(Transcript).where(Transcript.video_asset_id == video.id).order_by(Transcript.created_at.desc())).all()
    summaries = session.scalars(select(Summary).where(Summary.video_asset_id == video.id).order_by(Summary.created_at.desc())).all()
    return video_dict(video, session) | {
        "jobs": [job_dict(job) for job in jobs],
        "transcripts": [{"id": t.id, "model": t.model, "language": t.language, "text": t.text, "segments": t.segments_json, "created_at": t.created_at.isoformat()} for t in transcripts],
        "summaries": [summary_dict(item) for item in summaries],
    }


@app.post("/api/uploads")
async def upload_media(file: UploadFile = File(...), session: Session = Depends(db_session)):
    original_name = Path((file.filename or "").replace("\\", "/")).name
    extension = Path(original_name).suffix.lower()
    media_type = UPLOAD_FORMATS.get(extension)
    if not original_name or not media_type:
        raise HTTPException(400, "仅支持 MP3、WAV、M4A、MP4、WebM 和 MOV 文件")
    content_type = (file.content_type or "").lower().split(";")[0].strip()
    if content_type and content_type not in {"application/octet-stream", "application/mp4", "binary/octet-stream"}:
        if not (content_type.startswith("video/") or content_type.startswith("audio/")):
            raise HTTPException(400, "文件类型与扩展名不一致")
    temp_path = TEMP_DIR / f"upload-{uuid.uuid4().hex}.part"
    final_path: Path | None = None
    digest = hashlib.sha256()
    size = 0
    try:
        with temp_path.open("wb") as target:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_UPLOAD_BYTES:
                    raise HTTPException(413, "单个文件不能超过 500MB")
                digest.update(chunk)
                target.write(chunk)
        if size == 0:
            raise HTTPException(400, "不能上传空文件")
        sha256 = digest.hexdigest()
        source_url = f"upload://sha256/{sha256}"
        existing = session.scalars(select(VideoAsset).where(VideoAsset.normalized_url == source_url)).first()
        if existing:
            temp_path.unlink(missing_ok=True)
            return {"video": _video_detail(existing, session), "duplicate": True}
        duration = _uploaded_media_info(temp_path, media_type)
        final_path = UPLOAD_DIR / f"{sha256}{extension}"
        os.replace(temp_path, final_path)
        now = datetime.now(timezone.utc)
        video = VideoAsset(source_url=source_url, normalized_url=source_url, platform="upload", source_platform="upload", platform_video_id=sha256, title=Path(original_name).stem[:500] or "未命名上传", author="本地上传", duration_seconds=duration, parse_status="uploaded", available_formats=[{"format": extension[1:], "quality": "原始文件"}], last_processed_at=now)
        session.add(video)
        session.flush()
        session.add(MediaVariant(video_asset_id=video.id, format=extension[1:], quality="原始文件", media_type=media_type, local_path=str(final_path), file_size=size, download_status="downloaded"))
        session.commit()
        session.refresh(video)
        return {"video": _video_detail(video, session), "duplicate": False}
    except Exception:
        if temp_path.exists():
            temp_path.unlink()
        if final_path and final_path.exists():
            final_path.unlink()
        raise
    finally:
        await file.close()


@app.get("/api/videos")
def list_videos(session: Session = Depends(db_session), q: str | None = None):
    query = (
        select(VideoAsset)
        .where(VideoAsset.platform != "meeting")
        .order_by(VideoAsset.updated_at.desc())
    )
    if q:
        query = query.where(VideoAsset.title.ilike(f"%{q}%"))
    return [video_dict(v, session) for v in session.scalars(query).all()]


@app.get("/api/videos/recent")
def recent_videos(session: Session = Depends(db_session)):
    videos = session.scalars(
        select(VideoAsset)
        .where(VideoAsset.platform != "meeting")
        .order_by(func.coalesce(VideoAsset.last_processed_at, VideoAsset.updated_at).desc())
        .limit(6)
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
    query = select(VideoAsset).where(VideoAsset.platform != "meeting").order_by(VideoAsset.updated_at.desc())
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
    return _video_detail(video, session)


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


def _moss_busy_message(settings: dict[str, Any] | None = None) -> str | None:
    """MOSS 正在跑别的批量任务时返回提示文案，用于「下单前」把排队说清楚。"""
    config = settings or current_transcription()
    moss = probe_moss(moss_service_url(config))
    if moss.get("busy"):
        return f"转录服务正忙于另一个任务（{moss.get('busy_job') or '未知任务'}），请等它完成或取消后再开始"
    return None


@app.delete("/api/jobs/{job_id}")
def cancel_job(job_id: str, session: Session = Depends(db_session)):
    """取消在途任务：MOSS 侧会 kill 掉推理子进程，立即释放推理锁。"""
    job = session.get(ProcessingJob, job_id)
    if not job:
        raise HTTPException(404, "任务不存在")
    if job.status in {"completed", "failed", "cancelled"}:
        return job_dict(job)
    moss_id = (job.progress_json or {}).get("moss_job_id")
    if moss_id:
        try:
            with httpx.Client(timeout=10.0) as client:
                client.delete(f"{moss_service_url(current_transcription())}/v1/transcriptions/{moss_id}")
        except Exception:  # noqa: BLE001 - MOSS 不可用时也要把本地任务结掉
            pass
    job.status = "cancelled"
    job.current_stage = "cancelled"
    job.error_message = "任务已取消"
    job.finished_at = datetime.now(timezone.utc)
    session.commit()
    return job_dict(job)


@app.post("/api/videos/{video_id}/transcribe")
def transcribe(video_id: str, payload: dict[str, Any] | None = None, session: Session = Depends(db_session)):
    video = session.get(VideoAsset, video_id)
    if not video:
        raise HTTPException(404, "视频不存在")
    body = payload or {}
    settings = current_transcription()
    moss = probe_moss(moss_service_url(settings))
    if not moss.get("reachable"):
        raise HTTPException(503, "转录服务未连接，请先启动本机 MOSS")
    if not moss.get("model_loaded"):
        detail = moss.get("warmup_error") or ("模型正在加载，请稍后再试" if moss.get("model_loading") else "转录模型尚未加载")
        raise HTTPException(409, str(detail))
    if moss.get("busy"):
        raise HTTPException(409, f"转录服务正忙于另一个任务（{moss.get('busy_job') or '未知任务'}），请等待或取消后再开始")
    language = (body.get("transcription") or {}).get("language") or settings.get("default_language") or "zh"
    if language not in {"zh", "auto", "en"}:
        raise HTTPException(400, "不支持的转录语言")
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
    running = str(payload.get("status") or "running") == "running"
    # MOSS 已改用 fp32（CPU 上比 bf16 快约 4~5 倍），实时倍数约 2.5；
    # 可用 MOSS_RTF_HINT 覆盖。估时只影响进度条，不影响实际完成判断。
    rtf = float(os.environ.get("MOSS_RTF_HINT") or 2.5) if stage == "transcribing" else 1.0
    estimate = 90 if stage == "loading_model" else (max(audio * rtf, 30) if audio else 160)
    time_ratio = min(0.97, elapsed / estimate) if estimate else 0
    from_time = 0.30 + 0.62 * time_ratio
    from_moss = 0.28 + 0.64 * moss_ratio
    overall = from_moss if moss_ratio >= 0.15 else max(from_moss, from_time)
    if running and elapsed > estimate:
        overall = min(0.94, max(overall, 0.80 + 0.14 * min(1.0, (elapsed - estimate) / max(estimate, 1))))
    overall = min(0.95, max(0.28, overall))
    eta = payload.get("eta_seconds")
    if eta is None and audio:
        eta = max(0, int(estimate - elapsed))
    if running and eta is not None and int(eta) <= 0:
        eta = max(30, int(max(estimate - elapsed, elapsed * 0.25)))
    message = str(payload.get("message") or "正在转录")
    if running and stage == "transcribing":
        message = f"CPU 正在推理 {int(audio or 0)} 秒音频，已用 {elapsed} 秒；百分比仅为估时，完成前不会到 100%"
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
    local_media_path = path_for_moss(media.local_path) if media.local_path and Path(media.local_path).exists() else None
    media_url = None if local_media_path else media.source_url
    _append_job_log(session, job, "正在提交转录任务", "info", "transcribe_queued", 0.28)
    try:
        with httpx.Client(timeout=20.0) as client:
            created = client.post(
                f"{moss_url}/v1/transcriptions",
                json={
                    "media_url": media_url,
                    "media_path": local_media_path,
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
        "moss_job_id": moss_id,
    })
    result = None
    last_stage = ""
    # 轮询上限：至少 15 分钟，或按音频时长 15x 估算。
    # 原实现固定 900×2s（30 分钟）且循环体没有异常保护，任何一次 DB/解析异常
    # 都会让线程静默退出，导致结果早就产出、平台却永远收不到（本次事故根因）。
    max_polls = max(450, int((video.duration_seconds or 0) * 15))
    consecutive_errors = 0
    for waited in range(max_polls):
        time.sleep(2)
        try:
            job = session.get(ProcessingJob, job.id)
            if not job or job.status == "cancelled":
                return None
            with httpx.Client(timeout=15.0) as client:
                status = client.get(f"{moss_url}/v1/transcriptions/{moss_id}")
            if status.status_code >= 400:
                raise RuntimeError(f"转录任务查询返回 HTTP {status.status_code}")
            payload = status.json()
            moss_status = payload.get("status")
            overall, detail, _message = _transcribe_progress(payload, waited, video.duration_seconds)
            # moss_job_id 必须一直保留在 progress_json 里：轮询会整体覆盖 detail，
            # 不带上它的话，重启对账/取消接口就拿不到 moss_id，孤儿任务就回收不掉。
            detail["moss_job_id"] = moss_id
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
        except Exception as exc:  # noqa: BLE001 - 单次失败必须容忍并重试，绝不能静默断掉轮询
            consecutive_errors += 1
            try:
                current = session.get(ProcessingJob, job.id)
                if current and (consecutive_errors <= 3 or consecutive_errors % 20 == 0):
                    _append_job_log(session, current, f"轮询转录服务出错（第 {consecutive_errors} 次，继续重试）：{exc}", "warning")
            except Exception:
                pass
            if consecutive_errors >= 60:
                try:
                    current = session.get(ProcessingJob, job.id)
                    if current:
                        _fail_job(session, current, f"连续 {consecutive_errors} 次轮询转录服务失败：{exc}", "transcribing")
                except Exception:
                    pass
                return None
            continue
        consecutive_errors = 0
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
    raw_stored = record.value_json if record else {}
    current = merge_settings(key, raw_stored)
    # Migrate legacy plaintext cookies as soon as the settings endpoint is used.
    if key == "parser" and record is not None:
        encrypted, changed = transform_parser_cookies(raw_stored, encrypt=True)
        if changed:
            record.value_json = encrypted
            session.commit()
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
            next_value, _ = transform_parser_cookies(next_value, encrypt=True)
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
    if (video.platform or video.source_platform) == "upload":
        active_job = session.scalars(
            select(ProcessingJob).where(
                ProcessingJob.video_asset_id == video_id,
                ProcessingJob.job_type == "transcribe",
                ProcessingJob.status.in_(("queued", "running")),
            ).limit(1)
        ).first()
        if active_job:
            raise HTTPException(409, "转录任务仍在运行，请完成或失败后再删除上传文件")
        upload_root = UPLOAD_DIR.resolve()
        variants = session.scalars(select(MediaVariant).where(MediaVariant.video_asset_id == video_id)).all()
        for variant in variants:
            if not variant.local_path:
                continue
            path = Path(variant.local_path).resolve()
            if not path.is_relative_to(upload_root):
                raise HTTPException(500, "拒绝删除受管上传目录以外的文件")
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:
                raise HTTPException(500, "无法删除上传文件，请检查本地存储权限") from exc
    session.delete(video); session.commit()
    return {"deleted": video_id}


from .meetings import register_meeting_routes

register_meeting_routes(app)


def _stale_seconds() -> float:
    try:
        return float(os.getenv("MEETING_STALE_SECONDS", "180"))
    except (TypeError, ValueError):
        return 180.0


def _utcnow_naive() -> datetime:
    """数据库里的 DateTime 列不带时区（存 UTC），比较时必须用 naive UTC，否则相减会抛 TypeError。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _as_naive_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).replace(tzinfo=None) if value.tzinfo is not None else value


def _has_active_job(session: Session, video_asset_id: str | None) -> bool:
    if not video_asset_id:
        return False
    return session.scalar(
        select(ProcessingJob.id).where(
            ProcessingJob.video_asset_id == video_asset_id,
            ProcessingJob.status.in_(("running", "queued")),
        ).limit(1)
    ) is not None


def _reconcile_meetings(session: Session, now: datetime, include_recording: bool) -> None:
    """把卡死的会议状态收敛掉：跟随最新任务的终态落地。

    只在「确实没有对应活动」时动手，永不误伤正在录音/正在转录的会议。
    """
    from .meetings import finalize_live_meeting

    statuses = ("recording", "processing") if include_recording else ("processing",)
    stale = _stale_seconds()
    for meeting in session.scalars(select(Meeting).where(Meeting.status.in_(statuses))).all():
        if _has_active_job(session, meeting.video_asset_id):
            continue
        # 最新任务已到终态时，会议必须跟着收敛。
        # 少这一步就会出现「任务已 failed、会议永远 processing」：前端既看不到错误，也没有重试出口。
        # 注意这里必须在 audio_path 判断之前：录音文件还在时反而正是需要收敛的场景（文件可用于重试）。
        job = session.scalars(
            select(ProcessingJob)
            .where(ProcessingJob.video_asset_id == meeting.video_asset_id)
            .order_by(ProcessingJob.created_at.desc())
            .limit(1)
        ).first()
        if job is not None and job.status in {"completed", "failed", "cancelled"}:
            if job.status == "completed" and _latest_transcript(session, meeting.video_asset_id):
                meeting.status = "ready"
                meeting.error_message = None
                meeting.updated_at = now
                continue
            if job.status in {"failed", "cancelled"}:
                meeting.status = "failed"
                meeting.error_message = (
                    job.error_message or ("转录任务已取消" if job.status == "cancelled" else "终稿转录失败，请重试")
                )
                meeting.updated_at = now
                continue
        if meeting.audio_path and Path(meeting.audio_path).exists():
            continue
        stamp = _as_naive_utc(meeting.updated_at or meeting.created_at)
        age = (now - stamp).total_seconds() if stamp else 0.0
        # recording 在启动时必然已无活跃 WS；processing 则需超过阈值才算僵尸。
        if not include_recording and age <= stale:
            continue
        if meeting.live_segments_json:
            finalize_live_meeting(session, meeting)
        else:
            meeting.status = "failed"
            meeting.error_message = "服务重启导致转录中断，请重新录制或重试"
            meeting.updated_at = now
    session.commit()


def _cancel_orphan_moss_jobs(session: Session) -> None:
    """重启后，把上一个进程留下的在途任务在 MOSS 侧也取消掉。

    少了这一步，平台重启后 MOSS 里的推理还在跑、结果也没人来取，
    而且一直占着唯一的推理锁，后续任务全部排不上（本次事故的元凶）。
    """
    settings = current_transcription()
    url = moss_service_url(settings)
    for job in session.scalars(
        select(ProcessingJob).where(ProcessingJob.status.in_(("running", "queued")))
    ).all():
        moss_id = (job.progress_json or {}).get("moss_job_id")
        if not moss_id:
            continue
        try:
            with httpx.Client(timeout=10.0) as client:
                client.delete(f"{url}/v1/transcriptions/{moss_id}")
        except Exception:  # noqa: BLE001 - MOSS 不可用时不能阻塞启动
            pass


def _reconcile_on_startup() -> None:
    """启动对账：清理上一进程遗留的僵尸状态。

    注意：仅在单 worker 部署下安全（本仓 docker CMD 未使用 --workers）。
    多 worker 时每个 worker 都会执行，可能误杀其它 worker 正在跑的任务。
    """
    now = _utcnow_naive()
    with SessionLocal() as session:
        # 在途任务的线程随进程一起消失：先让 MOSS 那边也停下，再置失败。
        _cancel_orphan_moss_jobs(session)
        for job in session.scalars(
            select(ProcessingJob).where(ProcessingJob.status.in_(("running", "queued")))
        ).all():
            job.status = "failed"
            job.error_message = job.error_message or "服务重启导致任务中断"
            job.finished_at = now
        session.commit()
        _reconcile_meetings(session, now, include_recording=True)


def _sweep_stale_meetings() -> None:
    interval = float(os.getenv("MEETING_SWEEP_SECONDS", "60") or 60)
    while True:
        time.sleep(interval)
        try:
            with SessionLocal() as session:
                _reconcile_meetings(session, _utcnow_naive(), include_recording=False)
        except Exception:
            traceback.print_exc()


@app.on_event("startup")
def _start_state_guard() -> None:
    try:
        _reconcile_on_startup()
    except Exception:
        traceback.print_exc()
    threading.Thread(target=_sweep_stale_meetings, daemon=True, name="meeting-sweeper").start()

