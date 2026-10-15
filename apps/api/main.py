from __future__ import annotations

import copy
import hashlib
import io
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
import traceback
import uuid
import zipfile
import base64
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from urllib.parse import quote

import httpx
from cryptography.fernet import Fernet, InvalidToken
from fastapi import BackgroundTasks, Depends, FastAPI, File, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

from .parser_runtime import ParseError, bili_play_url, cover_referer, download_bytes, download_to_path, parse_media
from .parsers.ugoira import compose_ugoira
from .auth import get_current_user, get_current_user_flex, require_admin
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
PREVIEW_DIR = STORAGE_ROOT / "previews"
PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
IMAGE_MEDIA_TYPES = {
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "webp": "image/webp",
    "gif": "image/gif",
}
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
    user_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
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
    is_default: Mapped[bool] = mapped_column(default=False)
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
    extra_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)


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
    user_id: Mapped[str | None] = mapped_column(String(36), nullable=True)


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
    user_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    username: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    display_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    role: Mapped[str] = mapped_column(String(16), default="user")        # admin | user
    status: Mapped[str] = mapped_column(String(16), default="active")    # active | disabled
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))


class UserSession(Base):
    """refresh 令牌存储：一次性轮换 + 可撤销（登出/改密/禁用即删）。"""

    __tablename__ = "user_sessions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    refresh_token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    revoked: Mapped[bool] = mapped_column(default=False)
    user_agent: Mapped[str | None] = mapped_column(String(255), nullable=True)
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


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
        if "is_default" not in template_cols:
            conn.execute(text("ALTER TABLE summary_templates ADD COLUMN is_default BOOLEAN DEFAULT 0"))
        job_cols = {row[1] for row in conn.execute(text("PRAGMA table_info(processing_jobs)")).fetchall()}
        if "logs_json" not in job_cols:
            conn.execute(text("ALTER TABLE processing_jobs ADD COLUMN logs_json JSON"))
        if "progress_json" not in job_cols:
            conn.execute(text("ALTER TABLE processing_jobs ADD COLUMN progress_json JSON"))
        if "input_media_type" not in job_cols:
            conn.execute(text("ALTER TABLE processing_jobs ADD COLUMN input_media_type VARCHAR(16)"))
        if "fallback_used" not in job_cols:
            conn.execute(text("ALTER TABLE processing_jobs ADD COLUMN fallback_used BOOLEAN DEFAULT 0"))
        variant_cols = {row[1] for row in conn.execute(text("PRAGMA table_info(media_variants)")).fetchall()}
        if "extra_json" not in variant_cols:
            conn.execute(text("ALTER TABLE media_variants ADD COLUMN extra_json JSON"))
        for owner_table in ("video_assets", "meetings", "api_keys"):
            owner_cols = {row[1] for row in conn.execute(text(f"PRAGMA table_info({owner_table})")).fetchall()}
            if "user_id" not in owner_cols:
                conn.execute(text(f"ALTER TABLE {owner_table} ADD COLUMN user_id VARCHAR(36)"))
        for index_name, index_table in (
            ("ix_video_assets_user_id", "video_assets"),
            ("ix_meetings_user_id", "meetings"),
            ("ix_api_keys_user_id", "api_keys"),
        ):
            conn.execute(text(f"CREATE INDEX IF NOT EXISTS {index_name} ON {index_table}(user_id)"))
    else:
        for statement in (
            "ALTER TABLE processing_jobs ADD COLUMN IF NOT EXISTS input_media_type VARCHAR(16)",
            "ALTER TABLE processing_jobs ADD COLUMN IF NOT EXISTS fallback_used BOOLEAN DEFAULT FALSE",
            "ALTER TABLE processing_jobs ADD COLUMN IF NOT EXISTS logs_json JSONB",
            "ALTER TABLE processing_jobs ADD COLUMN IF NOT EXISTS progress_json JSONB",
            "ALTER TABLE media_variants ADD COLUMN IF NOT EXISTS extra_json JSONB",
            "ALTER TABLE video_assets ADD COLUMN IF NOT EXISTS source_platform VARCHAR(80)",
            "ALTER TABLE video_assets ADD COLUMN IF NOT EXISTS available_formats JSONB",
            "ALTER TABLE video_assets ADD COLUMN IF NOT EXISTS latest_summary_id VARCHAR(36)",
            "ALTER TABLE video_assets ADD COLUMN IF NOT EXISTS last_processed_at TIMESTAMPTZ",
            "ALTER TABLE summary_templates ADD COLUMN IF NOT EXISTS show_on_home BOOLEAN DEFAULT TRUE",
            "ALTER TABLE summary_templates ADD COLUMN IF NOT EXISTS is_default BOOLEAN DEFAULT FALSE",
            "ALTER TABLE video_assets ADD COLUMN IF NOT EXISTS user_id VARCHAR(36)",
            "ALTER TABLE meetings ADD COLUMN IF NOT EXISTS user_id VARCHAR(36)",
            "ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS user_id VARCHAR(36)",
            "CREATE INDEX IF NOT EXISTS ix_video_assets_user_id ON video_assets(user_id)",
            "CREATE INDEX IF NOT EXISTS ix_meetings_user_id ON meetings(user_id)",
            "CREATE INDEX IF NOT EXISTS ix_api_keys_user_id ON api_keys(user_id)",
        ):
            conn.execute(text(statement))


def _backfill_owner(session: Session, owner_id: str) -> None:
    """把无归属的历史数据（视频/会议/密钥）挂给指定用户（首位管理员）。"""
    for table in ("video_assets", "meetings", "api_keys"):
        session.execute(text(f"UPDATE {table} SET user_id = :uid WHERE user_id IS NULL"), {"uid": owner_id})


def _bootstrap_first_admin() -> None:
    """users 为空时：设置了 ADMIN_PASSWORD 就建管理员（平滑承接原 env 登录），
    否则等首位注册用户成为 admin（register 端点负责回填历史数据）。"""
    from .auth import hash_password

    with SessionLocal() as session:
        if session.scalar(select(func.count()).select_from(User)) > 0:
            return
        password = os.getenv("ADMIN_PASSWORD", "")
        if not password or password == "change-me":
            return
        admin = User(
            username=os.getenv("ADMIN_USERNAME", "admin"),
            password_hash=hash_password(password),
            display_name="管理员",
            role="admin",
        )
        session.add(admin)
        session.flush()
        _backfill_owner(session, admin.id)
        session.commit()


_bootstrap_first_admin()


class ParseRequest(BaseModel):
    url: str = Field(min_length=3)


class ZipDownloadRequest(BaseModel):
    variant_ids: list[str] = Field(default_factory=list)


class JobRequest(BaseModel):
    video_id: str
    job_type: str = Field(pattern="^(download|transcribe|summarize)$")
    model: str | None = None
    template_id: str | None = None


class TranscriptUpdateRequest(BaseModel):
    text: str | None = None
    segments: list[dict[str, Any]] | None = None
    speaker_names: dict[str, str] = Field(default_factory=dict)


class SummaryUpdateRequest(BaseModel):
    content_markdown: str = Field(min_length=1)


class TemplateRequest(BaseModel):
    name: str | None = None
    description: str | None = None
    sections: list[dict[str, Any]] | None = None
    show_on_home: bool | None = None
    is_default: bool | None = None


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


def video_scope(user: Any) -> list:
    """数据隔离：admin 看全部，普通用户只看自己的。返回附加 where 条件列表。"""
    return [] if user.role == "admin" else [VideoAsset.user_id == user.id]


def get_owned_video(session: Session, user: Any, video_id: str) -> VideoAsset:
    """取视频并校验归属；不存在或越权一律 404（不暴露存在性）。"""
    video = session.get(VideoAsset, video_id)
    if video is None or (user.role != "admin" and video.user_id != user.id):
        raise HTTPException(404, "视频不存在")
    return video


def get_owned_job(session: Session, user: Any, job_id: str) -> ProcessingJob:
    job = session.get(ProcessingJob, job_id)
    if job is None:
        raise HTTPException(404, "任务不存在")
    get_owned_video(session, user, job.video_asset_id)
    return job


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


def _strip_instruction_echo(text: str, section_titles: list[str]) -> str:
    """小模型常把 prompt 里的章节指令复述一遍再写正文；第一个章节标题出现两次时，从第二次出现处截取正文。"""
    for title in section_titles:
        needle = str(title or "").strip()
        if not needle:
            continue
        pattern = rf"(?m)^#{{0,3}}\s*{re.escape(needle)}\s*$"
        positions = [m.start() for m in re.finditer(pattern, text)]
        if len(positions) >= 2:
            return text[positions[1]:].strip()
    return text


def clean_summary_text(raw: str | None, section_titles: list[str] | None = None) -> str:
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
    if section_titles:
        text = _strip_instruction_echo(text, section_titles)
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
        # thumbnail_path（服务器文件系统绝对路径）不对外暴露；封面统一走上面的代理路由。
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
    {"id": "zhihu", "name": "知乎", "logo": "zhihu.svg", "featured": True},
    {"id": "twitter", "name": "Twitter", "logo": "twitter.png", "featured": False},
    {"id": "pixiv", "name": "Pixiv", "logo": "pixiv.png", "featured": False},
    {"id": "xiaoheihe", "name": "小黑盒", "logo": "xiaoheihe.svg", "featured": False},
    {"id": "shipinhao", "name": "微信视频号", "logo": "shipinhao.svg", "featured": False},
    {"id": "qzone", "name": "QQ空间", "logo": "qzone.svg", "featured": False},
]

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


def safe_filename(title: str, ext: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in " ._-" else "_" for ch in title).strip() or "video"
    return f"{cleaned[:80].rstrip('.')}.{ext.lstrip('.')}"


def cookie_status(parser_id: str, cookies: str) -> str:
    """静态检测 Cookie 健康度：missing=缺失 / weak=疑似游客或关键字段缺失 / ok=已有。"""
    if not cookies:
        return "missing"
    if parser_id == "youtube":
        names = {part.strip().split("=", 1)[0] for part in cookies.split(";") if "=" in part}
        if "LOGIN_INFO" in names and "SAPISID" in names:
            return "ok"
        return "weak"
    return "ok"


def mask_settings(group: str, data: dict[str, Any]) -> dict[str, Any]:
    payload = copy.deepcopy(data)
    if group == "transcription":
        secret = payload.get("api_key") or ""
        payload["api_key"] = ""
        payload["api_key_masked"] = f"{secret[:3]}****{secret[-2:]}" if len(secret) > 5 else ("已配置" if secret else "")
    for parser_id, parser in (payload.get("parsers") or {}).items():
        cookies = parser.get("cookies") or ""
        if cookies:
            parser["cookies"] = ""
            parser["cookies_masked"] = "已保存"
        parser["cookie_status"] = cookie_status(parser_id, cookies)
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
        "is_default": bool(item.is_default),
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
        _ensure_single_default_template(session)


def _ensure_single_default_template(session: Session) -> None:
    """保证「有且仅有一个默认模板」：没有默认时挑第一个可用模板补位，多个时保留首个。

    空库/删掉默认模板后由这里自动补位，前端预选永远有据可依。
    """
    rows = list(session.scalars(
        select(SummaryTemplate).where(SummaryTemplate.is_active.is_(True)).order_by(SummaryTemplate.created_at)
    ).all())
    defaults = [row for row in rows if row.is_default]
    if not defaults and rows:
        rows[0].is_default = True
    for extra in defaults[1:]:
        extra.is_default = False
    session.commit()


seed_builtin_templates()
persist_transcription_defaults()


def current_transcription() -> dict[str, Any]:
    with SessionLocal() as session:
        record = session.get(ConfigRecord, "transcription")
        return merge_settings("transcription", record.value_json if record else {})


def current_parser_settings(session: Session | None = None) -> dict[str, Any]:
    if session is not None:
        record = session.get(ConfigRecord, "parser")
        return merge_settings("parser", record.value_json if record else {})
    with SessionLocal() as local_session:
        record = local_session.get(ConfigRecord, "parser")
        return merge_settings("parser", record.value_json if record else {})


def _download_options(session: Session | None = None) -> dict[str, Any]:
    settings = current_parser_settings(session)
    return {
        "max_bytes": max(1, int(settings.get("source_max_size") or 90)) * 1024 * 1024,
        "timeout": max(3.0, min(float(settings.get("download_timeout") or 280), 600.0)),
        "retries": max(0, min(int(settings.get("download_retry_times") or 2), 5)),
    }


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
        "summary_model": transcription["summary_model"],
        "summary_model_path": transcription["summary_model_path"],
        # 与 moss_model_found 同理：本地总结其实是 MOSS 用 llama-cli 跑的，真正知道
        # 「能不能用」的只有 MOSS。API 进程（尤其跑在宿主机时）看到的路径未必和容器
        # 内一致，所以 MOSS 可达且上报了该字段时以它为准；没上报（老版本）才退回
        # 本地路径检查，避免把「查不到」误报成「不可用」。
        "summary_model_found": bool(
            moss.get("summary_model_found")
            if moss.get("reachable") and moss.get("summary_model_found") is not None
            else transcription["summary_model_found"]
        ),
        "summary_ready": moss.get("summary_ready"),
        "summary_engine": moss.get("summary_engine"),
        "summary_cli": moss.get("summary_cli"),
        "summary_cli_found": moss.get("summary_cli_found"),
        "default_language": "zh",
    }


@app.post("/api/system/moss/warmup")
def warmup_moss(user: Any = Depends(get_current_user)):
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
def parse(request: ParseRequest, user: Any = Depends(get_current_user), session: Session = Depends(db_session)):
    now = datetime.now(timezone.utc)
    try:
        parser_settings = merge_settings(
            "parser",
            (session.get(ConfigRecord, "parser") or ConfigRecord(key="parser", value_json={})).value_json,
        )
        parsed = parse_media(request.url, settings=parser_settings)
    except ParseError as exc:
        raise HTTPException(502, str(exc)) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(502, "解析服务访问来源网站失败，请稍后重试") from exc
    except Exception as exc:
        traceback.print_exc()
        raise HTTPException(502, "解析器处理链接时发生异常，请稍后重试") from exc
    # 数据隔离：重复资产只复用本人（或 admin 视角）的，不碰别人的视频。
    related = [item for item in _related_videos(session, parsed) if user.role == "admin" or item.user_id == user.id]
    existing = related[0] if related else None
    video = existing or VideoAsset(source_url=parsed.source_url, normalized_url=parsed.normalized_url, user_id=user.id)
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
    existing_variants: dict[tuple[str, str, str], list[MediaVariant]] = {}
    for old in session.scalars(select(MediaVariant).where(MediaVariant.video_asset_id == video.id)).all():
        key = (old.media_type or "", old.format or "", old.quality or "")
        existing_variants.setdefault(key, []).append(old)
    for item in parsed.variants:
        key = (item.media_type or "", item.format or "", item.quality or "")
        matches = existing_variants.get(key) or []
        variant = matches.pop(0) if matches else MediaVariant(video_asset_id=video.id)
        variant.format = item.format
        variant.quality = item.quality
        variant.media_type = item.media_type
        variant.source_url = item.source_url
        variant.file_size = item.file_size or variant.file_size
        variant.extra_json = dict(item.extra) if item.extra else None
        variant.download_status = "downloaded" if variant.local_path else "available"
        session.add(variant)
    for stale_items in existing_variants.values():
        for stale in stale_items:
            for preview in PREVIEW_DIR.glob(f"{stale.id}.*"):
                preview.unlink(missing_ok=True)
            session.delete(stale)
    # 不再自动删除其它重复资产：它们可能包含用户已经校对的转录或纪要。
    session.add(ProcessingJob(video_asset_id=video.id, job_type="parse", status="completed", current_stage="parsed", started_at=now, finished_at=now, progress=1))
    session.commit()
    session.refresh(video)
    return video_dict(video, session)


def _store_cover(video_id: str, url: str | None, referer: str) -> Path | None:
    if not url:
        return None
    try:
        payload = download_bytes(url, referer=cover_referer(url, referer), max_bytes=10 * 1024 * 1024, timeout=30, retries=1)
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
async def upload_media(file: UploadFile = File(...), user: Any = Depends(get_current_user), session: Session = Depends(db_session)):
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
        # 去重只在本人的资产内进行；别人上传过同一文件 → 为本人新建一份。
        existing = session.scalars(
            select(VideoAsset).where(VideoAsset.normalized_url == source_url, VideoAsset.user_id == user.id)
        ).first()
        if existing:
            temp_path.unlink(missing_ok=True)
            return {"video": _video_detail(existing, session), "duplicate": True}
        duration = _uploaded_media_info(temp_path, media_type)
        final_path = UPLOAD_DIR / f"{sha256}{extension}"
        os.replace(temp_path, final_path)
        now = datetime.now(timezone.utc)
        video = VideoAsset(source_url=source_url, normalized_url=source_url, platform="upload", source_platform="upload", platform_video_id=sha256, title=Path(original_name).stem[:500] or "未命名上传", author="本地上传", duration_seconds=duration, parse_status="uploaded", available_formats=[{"format": extension[1:], "quality": "原始文件"}], last_processed_at=now, user_id=user.id)
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
def list_videos(user: Any = Depends(get_current_user), session: Session = Depends(db_session), q: str | None = None):
    query = (
        select(VideoAsset)
        .where(VideoAsset.platform != "meeting", *video_scope(user))
        .order_by(VideoAsset.updated_at.desc())
    )
    if q:
        query = query.where(VideoAsset.title.ilike(f"%{q}%"))
    return [video_dict(v, session) for v in session.scalars(query).all()]


def _is_image_record(item: dict[str, Any]) -> bool:
    variants = item.get("variants") or []
    kinds = {variant.get("media_type") for variant in variants}
    return "image" in kinds and not ({"video", "audio"} & kinds)


@app.get("/api/videos/recent")
def recent_videos(user: Any = Depends(get_current_user), session: Session = Depends(db_session), media: str | None = None):
    videos = session.scalars(
        select(VideoAsset)
        .where(VideoAsset.platform != "meeting", *video_scope(user))
        .order_by(func.coalesce(VideoAsset.last_processed_at, VideoAsset.updated_at).desc())
        .limit(80 if media in {"video", "image"} else 6)
    ).all()
    rows = []
    for video in videos:
        item = video_dict(video, session)
        if media == "image" and not _is_image_record(item):
            continue
        if media == "video" and _is_image_record(item):
            continue
        rows.append(item)
        if len(rows) >= 6:
            break
    return rows


@app.get("/api/history")
def history(
    user: Any = Depends(get_current_user),
    session: Session = Depends(db_session),
    q: str | None = None,
    platform: str | None = None,
    job_type: str | None = None,
    status: str | None = None,
):
    query = select(VideoAsset).where(VideoAsset.platform != "meeting", *video_scope(user)).order_by(VideoAsset.updated_at.desc())
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
def history_detail(video_id: str, user: Any = Depends(get_current_user), session: Session = Depends(db_session)):
    video = get_owned_video(session, user, video_id)
    return _video_detail(video, session)


@app.get("/api/videos/{video_id}/variants")
def variants(video_id: str, user: Any = Depends(get_current_user), session: Session = Depends(db_session)):
    get_owned_video(session, user, video_id)
    return [{"id": x.id, "format": x.format, "quality": x.quality, "media_type": x.media_type, "download_status": x.download_status, "file_size": x.file_size} for x in session.scalars(select(MediaVariant).where(MediaVariant.video_asset_id == video_id)).all()]


def _format_ts(seconds: float) -> str:
    total = int(seconds)
    h, remainder = divmod(max(0, total), 3600)
    m, s = divmod(remainder, 60)
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def _persist_result_files(session: Session, video_id: str, folder: Path | None) -> None:
    """把数据库里的转录/纪要落盘成文档，让任务文件夹自包含（幂等覆盖）。"""
    if folder is None or not folder.is_dir():
        return
    transcript = session.scalars(
        select(Transcript).where(Transcript.video_asset_id == video_id).order_by(Transcript.created_at.desc())
    ).first()
    if transcript is not None:
        segments = transcript.segments_json or []
        if segments:
            lines = []
            for seg in segments:
                start = seg.get("start") or seg.get("begin") or 0
                text_part = (seg.get("text") or "").strip()
                if text_part:
                    lines.append(f"[{_format_ts(float(start))}] {text_part}")
            body = "\n".join(lines) or transcript.text
        else:
            body = transcript.text
        header = f"# 转录全文\n\n- 模型：{transcript.model}\n- 语言：{transcript.language}\n\n"
        try:
            (folder / "转录全文.txt").write_text(header + body + "\n", encoding="utf-8")
        except OSError:
            pass
    video = session.get(VideoAsset, video_id)
    summary = None
    if video is not None and video.latest_summary_id:
        summary = session.get(Summary, video.latest_summary_id)
    if summary is None:
        summary = session.scalars(
            select(Summary).where(Summary.video_asset_id == video_id).order_by(Summary.created_at.desc())
        ).first()
    if summary is not None:
        try:
            (folder / "总结纪要.md").write_text(clean_summary_text(summary.content_markdown) + "\n", encoding="utf-8")
        except OSError:
            pass


def _video_file_paths(session: Session, video_id: str) -> list[tuple[str, Path]]:
    """该任务在磁盘上的候选文件：会议录音 + 各媒体变体，去重保序。"""
    candidates: list[tuple[str, Path]] = []
    meeting = session.scalars(select(Meeting).where(Meeting.video_asset_id == video_id)).first()
    if meeting is not None and meeting.audio_path:
        candidates.append(("录音文件", Path(meeting.audio_path)))
        folder = Path(meeting.audio_path).parent
        for name, label in (("转录全文.txt", "转录结果"), ("总结纪要.md", "总结结果")):
            candidate = folder / name
            if candidate.is_file():
                candidates.append((label, candidate))
    for variant in session.scalars(select(MediaVariant).where(MediaVariant.video_asset_id == video_id)).all():
        if variant.local_path and (variant.local_path != (meeting.audio_path if meeting else None)):
            label = variant.quality or variant.format or "媒体文件"
            candidates.append((f"媒体文件（{label}）", Path(variant.local_path)))
    files: list[tuple[str, Path]] = []
    seen: set[str] = set()
    for kind, path in candidates:
        try:
            resolved = str(path.resolve())
            if resolved in seen or not path.is_file():
                continue
            seen.add(resolved)
            files.append((kind, path))
        except OSError:
            continue
    return files


def _resolve_task_folder(session: Session, video_id: str) -> Path | None:
    """确定该任务的落盘目录（录音/媒体所在目录），并懒生成转录/纪要文档。"""
    entries = _video_file_paths(session, video_id)
    folder = Path(entries[0][1].parent) if entries else None
    if folder is not None:
        _persist_result_files(session, video_id, folder)
    return folder


@app.get("/api/videos/{video_id}/files")
def video_files(video_id: str, user: Any = Depends(get_current_user), session: Session = Depends(db_session)):
    """列出该任务的落盘文件与所在目录；打开前把转录/纪要文档同步落盘。"""
    get_owned_video(session, user, video_id)
    folder = _resolve_task_folder(session, video_id)
    entries = _video_file_paths(session, video_id)
    folder_openable = bool(folder) and Path(folder).is_dir() and os.name == "nt"
    return {
        "video_id": video_id,
        "folder": str(folder) if folder else None,
        # 只有 API 跑在本机（Windows 宿主机）时才能帮忙拉起资源管理器；容器/远程部署只能给路径。
        "folder_openable": folder_openable,
        "files": [
            {"kind": kind, "name": path.name, "path": str(path), "size": path.stat().st_size}
            for kind, path in entries
        ],
    }


@app.post("/api/videos/{video_id}/files/open")
def video_open_folder(video_id: str, user: Any = Depends(get_current_user), session: Session = Depends(db_session)):
    """在本机资源管理器中打开任务文件夹；仅 API 与用户同机时可行。"""
    get_owned_video(session, user, video_id)
    folder = _resolve_task_folder(session, video_id)
    if not folder or not Path(folder).is_dir():
        raise HTTPException(404, "该任务没有落盘文件")
    opener = getattr(os, "startfile", None)
    if opener is None:
        raise HTTPException(501, f"当前部署形态无法代开文件夹（服务端为 {os.name}），请手动访问：{folder}")
    opener(folder)
    return {"ok": True, "folder": folder}


@app.get("/api/videos/{video_id}")
def get_video(video_id: str, user: Any = Depends(get_current_user), session: Session = Depends(db_session)):
    video = get_owned_video(session, user, video_id)
    return _video_detail(video, session)


@app.get("/api/videos/{video_id}/thumbnail")
def thumbnail(video_id: str, user: Any = Depends(get_current_user_flex), session: Session = Depends(db_session)):
    # <img> 不能带 Authorization header，token 走 ?token=（get_current_user_flex）。
    video = get_owned_video(session, user, video_id)
    if not video.thumbnail_path:
        raise HTTPException(404, "封面不存在")
    path = Path(video.thumbnail_path)
    if not path.exists():
        raise HTTPException(404, "封面不存在")
    return FileResponse(path, media_type="image/jpeg")


def _load_variant_bytes(video: VideoAsset, variant: MediaVariant) -> bytes:
    if variant.local_path and Path(variant.local_path).exists():
        return Path(variant.local_path).read_bytes()
    if not variant.source_url:
        raise HTTPException(409, "解析器尚未提供可下载文件，无法直接保存到浏览器。")
    play_url = variant.source_url
    try:
        return download_bytes(
            play_url,
            referer=cover_referer(play_url, video.source_url),
            max_bytes=25 * 1024 * 1024,
            timeout=60,
            retries=1,
        )
    except Exception:
        refreshed = None
        if (video.platform or video.source_platform) == "bilibili" and (video.platform_video_id or "").startswith("BV"):
            refreshed = bili_play_url(video.platform_video_id, variant.quality)
        if not refreshed:
            raise HTTPException(502, "下载失败：媒体地址已失效或无法访问")
        try:
            payload = download_bytes(refreshed, referer="https://www.bilibili.com/", max_bytes=25 * 1024 * 1024, timeout=60, retries=1)
        except Exception:
            raise HTTPException(502, "下载失败：媒体地址已失效或无法访问")
        variant.source_url = refreshed
        return payload


def _load_variant_file(video: VideoAsset, variant: MediaVariant, session: Session) -> tuple[Path, bool]:
    if variant.local_path and Path(variant.local_path).exists():
        return Path(variant.local_path), False
    if not variant.source_url:
        raise HTTPException(409, "解析器尚未提供可下载文件，无法直接保存到浏览器。")
    extension = re.sub(r"[^a-z0-9]", "", (variant.format or "mp4").lower()) or "mp4"
    destination = TEMP_DIR / f"download-{uuid.uuid4()}.{extension}"
    options = _download_options(session)
    play_url = variant.source_url
    try:
        return download_to_path(
            play_url,
            destination,
            referer=cover_referer(play_url, video.source_url),
            **options,
        ), True
    except Exception:
        destination.unlink(missing_ok=True)
        refreshed = None
        if (video.platform or video.source_platform) == "bilibili" and (video.platform_video_id or "").startswith("BV"):
            refreshed = bili_play_url(video.platform_video_id, variant.quality)
        if not refreshed:
            try:
                parsed = parse_media(video.source_url, settings=current_parser_settings(session))
                candidates = [
                    item for item in parsed.variants
                    if item.media_type == variant.media_type and (
                        (item.format == variant.format and item.quality == variant.quality)
                        or item.format == variant.format
                    )
                ]
                refreshed = candidates[0].source_url if candidates else None
            except Exception:
                refreshed = None
        if not refreshed:
            raise HTTPException(502, "下载失败：媒体地址已失效、文件过大或无法访问")
        try:
            path = download_to_path(refreshed, destination, referer="https://www.bilibili.com/", **options)
        except Exception as exc:
            destination.unlink(missing_ok=True)
            raise HTTPException(502, "下载失败：媒体地址已失效、文件过大或无法访问") from exc
        variant.source_url = refreshed
        return path, True


def _attachment(filename: str, content: bytes, media_type: str) -> Response:
    return Response(
        content=content,
        media_type=media_type,
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"},
    )


@app.get("/api/videos/{video_id}/preview/{variant_id}")
def preview_variant(video_id: str, variant_id: str, user: Any = Depends(get_current_user_flex), session: Session = Depends(db_session)):
    """图片预览走 ?token=，浏览器 img 带不上 Authorization。"""
    video = get_owned_video(session, user, video_id)
    variant = session.get(MediaVariant, variant_id)
    if not variant or variant.video_asset_id != video_id:
        raise HTTPException(404, "媒体资源不存在")
    if variant.media_type != "image":
        raise HTTPException(400, "这个文件不是图片")
    ext = "jpg" if (variant.format or "").lower() == "jpeg" else (variant.format or "jpg").lower()
    path = PREVIEW_DIR / f"{variant.id}.{ext}"
    if not path.exists():
        path.write_bytes(_load_variant_bytes(video, variant))
        session.commit()
    return FileResponse(path, media_type=IMAGE_MEDIA_TYPES.get(ext, "application/octet-stream"))


@app.get("/api/videos/{video_id}/download/{variant_id}")
def download_variant(video_id: str, variant_id: str, user: Any = Depends(get_current_user), session: Session = Depends(db_session)):
    video = get_owned_video(session, user, video_id)
    variant = session.get(MediaVariant, variant_id)
    if not video or not variant or variant.video_asset_id != video_id:
        raise HTTPException(404, "媒体资源不存在")
    now = datetime.now(timezone.utc)
    job = ProcessingJob(video_asset_id=video_id, job_type="download", status="running", current_stage="download", started_at=now)
    session.add(job)
    video.last_processed_at = now
    try:
        path, temporary = _load_variant_file(video, variant, session)
    except HTTPException as exc:
        job.status = "failed"
        job.finished_at = datetime.now(timezone.utc)
        job.error_code = "DOWNLOAD_FAILED"
        job.error_message = str(exc.detail)
        variant.download_status = "failed"
        session.commit()
        raise
    # Pixiv 动图：帧包 zip 自动合成为 GIF（有 ffmpeg 时优先 MP4），失败则回退原始 zip。
    output_path, output_ext = path, (variant.format or "mp4")
    if (variant.media_type or "") == "file" and (variant.format or "").lower() == "zip" and (video.platform or video.source_platform) == "pixiv":
        frames = ((variant.extra_json or {}).get("frames")) or []
        try:
            composed, ext = compose_ugoira(path, frames, TEMP_DIR)
            output_path, output_ext = composed, ext
            if temporary:
                path.unlink(missing_ok=True)
                temporary = False  # 原始 zip 已删；合成产物由下方 BackgroundTask 清理
        except Exception as exc:
            logger.warning("Pixiv 动图合成失败，回退原始帧包: %s", exc)
    job.status = "completed"
    job.current_stage = "completed"
    job.finished_at = datetime.now(timezone.utc)
    job.progress = 1
    variant.download_status = "downloaded"
    variant.file_size = output_path.stat().st_size
    session.commit()
    filename = safe_filename(video.title, output_ext)
    media_type = IMAGE_MEDIA_TYPES.get(output_ext.lower(), "video/mp4" if output_ext == "mp4" else "application/octet-stream")
    return FileResponse(
        output_path,
        filename=filename,
        media_type=media_type,
        background=BackgroundTask(output_path.unlink, missing_ok=True) if (temporary or output_path != path) else None,
    )


@app.post("/api/videos/{video_id}/download-zip")
def download_zip(video_id: str, request: ZipDownloadRequest, user: Any = Depends(get_current_user), session: Session = Depends(db_session)):
    video = get_owned_video(session, user, video_id)
    chosen = list(session.scalars(select(MediaVariant).where(MediaVariant.video_asset_id == video_id)).all())
    if request.variant_ids:
        wanted = set(request.variant_ids)
        chosen = [item for item in chosen if item.id in wanted]
    chosen = [item for item in chosen if item.media_type == "image"]
    if not chosen:
        raise HTTPException(400, "没有可打包的图片")
    if len(chosen) > 40:
        raise HTTPException(400, "一次最多打包 40 张图片")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_STORED) as archive:
        for index, variant in enumerate(chosen, start=1):
            ext = "jpg" if (variant.format or "").lower() == "jpeg" else (variant.format or "jpg").lower()
            archive.writestr(f"{index:02d}.{ext}", _load_variant_bytes(video, variant))
    session.commit()
    return _attachment(safe_filename(video.title or "images", "zip"), buffer.getvalue(), "application/zip")


_MEDIA_JOB_CREATE_LOCK = threading.RLock()


def _create_media_job(
    session: Session,
    video: VideoAsset,
    job_type: str,
    message: str,
    context: dict[str, Any] | None = None,
) -> ProcessingJob:
    # PostgreSQL 行锁负责多 worker；进程锁兼容 SQLite 和本机单进程开发。
    with _MEDIA_JOB_CREATE_LOCK:
        if session.bind and session.bind.dialect.name == "postgresql":
            session.scalar(select(VideoAsset).where(VideoAsset.id == video.id).with_for_update())
        if _has_active_job(session, video.id):
            raise HTTPException(409, "该媒体已有处理任务进行中，请勿重复提交")
        now = datetime.now(timezone.utc)
        job = ProcessingJob(
            video_asset_id=video.id,
            job_type=job_type,
            status="running",
            input_media_type="audio",
            current_stage="queued",
            started_at=now,
            progress=0.04,
            progress_json=dict(context or {}),
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


def _cancel_moss_transcription(moss_url: str, moss_id: str | None) -> bool:
    """尽力取消 MOSS 侧转录任务，把全局推理锁放掉。

    本地放弃（超时 / 连续轮询失败 / 取消）时 MOSS 往往还在推理，不取消的话
    它会一直占着唯一的推理锁，之后所有任务——包括本任务的重试——都被
    busy 挡在门外（正是「失败后重试仍提示正忙于另一个任务」的根因）。
    """
    if not moss_id:
        return False
    try:
        with httpx.Client(timeout=10.0) as client:
            client.delete(f"{moss_url}/v1/transcriptions/{moss_id}")
        return True
    except Exception:  # noqa: BLE001 - MOSS 不可用时不能阻塞主流程
        return False


def _reclaim_stale_moss_lock(moss: dict[str, Any]) -> bool:
    """MOSS 报 busy 时，若忙的其实是本平台已终态任务的僵尸锁，就取消它把锁抢回。

    判定依据：busy_job 能在本平台已失败/已取消任务的 progress_json.moss_job_id
    里找到。属主已死但 MOSS 还在推理（典型：转录超时），这个锁纯属僵尸。
    抢不到（真是别人的在途任务或身份不明）时返回 False，由调用方继续报 409。
    """
    busy_job = moss.get("busy_job")
    if not busy_job:
        return False
    with SessionLocal() as session:
        stale = False
        for job in session.scalars(
            select(ProcessingJob).where(ProcessingJob.status.in_(("failed", "cancelled")))
        ).all():
            if (job.progress_json or {}).get("moss_job_id") == busy_job:
                stale = True
                break
    if not stale:
        return False
    return _cancel_moss_transcription(moss_service_url(current_transcription()), busy_job)


def ensure_moss_available(settings: dict[str, Any] | None = None) -> dict[str, Any]:
    """转录前置检查：MOSS 可达、模型就绪、推理锁没被本平台的僵尸任务占着。"""
    config = settings or current_transcription()
    url = moss_service_url(config)
    moss = probe_moss(url)
    if not moss.get("reachable"):
        raise HTTPException(503, "转录服务未连接，请先启动本机 MOSS")
    if not moss.get("model_loaded"):
        detail = moss.get("warmup_error") or ("模型正在加载，请稍后再试" if moss.get("model_loading") else "转录模型尚未加载")
        raise HTTPException(409, str(detail))
    if moss.get("busy") and _reclaim_stale_moss_lock(moss):
        # DELETE 后 MOSS 的 job 线程要 unwind 才清 _busy_job，轮询几次再放行。
        for _ in range(6):
            time.sleep(1.5)
            moss = probe_moss(url)
            if not moss.get("busy"):
                break
    if moss.get("busy"):
        raise HTTPException(409, f"转录服务正忙于另一个任务（{moss.get('busy_job') or '未知任务'}），请等它完成或取消后再开始")
    return config


@app.delete("/api/jobs/{job_id}")
def cancel_job(job_id: str, user: Any = Depends(get_current_user), session: Session = Depends(db_session)):
    """取消在途任务：MOSS 侧会 kill 掉推理子进程，立即释放推理锁。"""
    job = get_owned_job(session, user, job_id)
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
    meeting = session.scalars(select(Meeting).where(Meeting.video_asset_id == job.video_asset_id)).first()
    if meeting is not None:
        # 取消总结不应抹掉可用转录；取消转录则进入可重试失败态。
        if job.job_type == "summarize" and _latest_transcript(session, job.video_asset_id):
            meeting.status = "ready"
            meeting.error_message = "纪要生成已取消"
        else:
            meeting.status = "failed"
            meeting.error_message = "转录任务已取消"
        meeting.updated_at = datetime.now(timezone.utc)
    session.commit()
    return job_dict(job)


@app.post("/api/videos/{video_id}/transcribe")
def transcribe(video_id: str, payload: dict[str, Any] | None = None, user: Any = Depends(get_current_user), session: Session = Depends(db_session)):
    video = get_owned_video(session, user, video_id)
    if _has_active_job(session, video_id, "transcribe"):
        raise HTTPException(409, "该任务正在转录，请勿重复提交")
    body = payload or {}
    settings = current_transcription()
    ensure_moss_available(settings)
    language = (body.get("transcription") or {}).get("language") or settings.get("default_language") or "zh"
    if language not in {"zh", "auto", "en"}:
        raise HTTPException(400, "不支持的转录语言")
    job = _create_media_job(session, video, "transcribe", "转录任务已创建", {"language": language})
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
        job.progress_json = {**(job.progress_json or {}), **detail}
    if job.status == "queued":
        job.status = "running"
        job.started_at = job.started_at or datetime.now(timezone.utc)
    session.commit()


def _transcribe_progress(payload: dict[str, Any], waited: int, audio_seconds: float | None) -> tuple[float, dict[str, Any], str]:
    elapsed = int(payload.get("elapsed_seconds") or waited * 2)
    audio = payload.get("audio_seconds")
    if audio is None:
        audio = audio_seconds
    audio = float(audio or 0) or None
    stage = str(payload.get("stage") or "transcribing")
    running = str(payload.get("status") or "running") == "running"
    # 真实分块进度优先：done/total 片单调且准确。MOSS 心跳的 progress 字段是
    # 墙钟估算（基于 RTF 假设），机器慢于假设时会停滞甚至倒退，只作兜底。
    done = payload.get("done_chunks")
    total = payload.get("total_chunks")
    try:
        chunk_ratio = float(done) / float(total) if done is not None and float(total or 0) > 0 else None
    except (TypeError, ValueError):
        chunk_ratio = None
    raw = float(payload.get("progress") or 0)
    wall_ratio = raw / 100 if raw > 1 else raw
    if chunk_ratio is not None:
        moss_ratio = chunk_ratio
    else:
        moss_ratio = wall_ratio
    # MOSS 已改用 fp32（CPU 上比 bf16 快约 4~5 倍），实时倍数约 2.5；
    # 可用 MOSS_RTF_HINT 覆盖。估时只影响进度条，不影响实际完成判断。
    rtf = float(os.environ.get("MOSS_RTF_HINT") or 2.5) if stage == "transcribing" else 1.0
    estimate = 90 if stage == "loading_model" else (max(audio * rtf, 30) if audio else 160)
    time_ratio = min(0.97, elapsed / estimate) if estimate else 0
    from_time = 0.30 + 0.62 * time_ratio
    from_moss = 0.28 + 0.64 * moss_ratio
    if chunk_ratio is not None:
        overall = from_moss
    else:
        overall = from_moss if moss_ratio >= 0.15 else max(from_moss, from_time)
        if running and elapsed > estimate:
            overall = min(0.94, max(overall, 0.80 + 0.14 * min(1.0, (elapsed - estimate) / max(estimate, 1))))
    overall = min(0.95, max(0.28, overall))
    eta = payload.get("eta_seconds")
    if eta is None and audio:
        eta = max(0, int(estimate - elapsed))
    if running and eta is not None and int(eta) <= 0:
        eta = max(30, int(max(estimate - elapsed, elapsed * 0.25)))
    # MOSS 的消息已含「已用/预计剩余/已完成 x/y 片」，直接透传；
    # 只有它没给消息时才退回本地的墙钟描述。
    message = str(payload.get("message") or "")
    if running and stage == "transcribing" and not message:
        message = f"正在转录 {int(audio or 0)} 秒音频，已用 {elapsed} 秒（进度为估算值）"
    detail = {
        "stage": stage,
        "message": message or "正在转录",
        "percent": int(round(overall * 100)),
        "elapsed_seconds": elapsed,
        "audio_seconds": audio,
        "eta_seconds": eta,
        "done_chunks": done,
        "total_chunks": total,
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
    playable = [
        item for item in variants
        if item.media_type in {"audio", "video"} and (item.source_url or item.local_path)
    ]

    def media_rank(item: MediaVariant) -> tuple[int, int]:
        is_local = bool(item.local_path and Path(item.local_path).exists())
        is_hls = ".m3u8" in str(item.source_url or "").lower() or (item.format or "").lower() == "m3u8"
        if item.media_type == "audio":
            return (0 if is_local else 1, 0)
        if is_local:
            return (2, 0)
        return (4 if is_hls else 3, 0)

    media = min(playable, key=media_rank) if playable else None
    _append_job_log(session, job, f"开始处理「{video.title}」", "info", "extracting_audio", 0.08)
    if not media:
        _fail_job(session, job, "当前解析结果没有可下载媒体，无法提取音频", "extracting_audio")
        return None
    label = media.quality or media.format or "媒体文件"
    _append_job_log(session, job, f"找到媒体：{label}", "info", "extracting_audio", 0.16)
    local_media_path = path_for_moss(media.local_path) if media.local_path and Path(media.local_path).exists() else None
    media_url = None if local_media_path else media.source_url
    referer = cover_referer(media_url or video.source_url, video.source_url)
    download_options = _download_options(session)
    if media_url and ".m3u8" in media_url.lower():
        # MOSS 只收文件或直链。m3u8 先在 API 侧用 yt-dlp 收成 mp4，再交给转录。
        saved = TEMP_DIR / f"hls-{job.id}.mp4"
        def cancelled() -> bool:
            with SessionLocal() as check_session:
                current = check_session.get(ProcessingJob, job.id)
                return current is None or current.status == "cancelled"

        download_to_path(media_url, saved, referer=referer, cancel_check=cancelled, **download_options)
        if cancelled():
            saved.unlink(missing_ok=True)
            return None
        local_media_path = path_for_moss(str(saved))
        media_url = None
    _append_job_log(session, job, "正在提交转录任务", "info", "transcribe_queued", 0.28)
    retries = max(0, min(int(settings.get("retry_times") or 0), 5))
    created: httpx.Response | None = None
    try:
        with httpx.Client(timeout=20.0) as client:
            for attempt in range(retries + 1):
                created = client.post(
                    f"{moss_url}/v1/transcriptions",
                    json={
                        "media_url": media_url,
                        "media_path": local_media_path,
                        "language": language or "zh",
                        "duration_seconds": video.duration_seconds,
                        "referer": referer,
                        "max_source_bytes": download_options["max_bytes"],
                    },
                )
                payload = created.json() if created.headers.get("content-type", "").startswith("application/json") else {}
                if created.status_code != 503 or not isinstance(payload, dict) or not payload.get("busy") or attempt >= retries:
                    break
                delay = max(1.0, min(float(payload.get("retry_after") or 2), 15.0))
                _append_job_log(
                    session,
                    job,
                    f"转录服务繁忙，{delay:g} 秒后重试（{attempt + 1}/{retries}）",
                    "warning",
                    "transcribe_queued",
                    0.28,
                )
                time.sleep(delay)
                current = session.get(ProcessingJob, job.id)
                if not current or current.status == "cancelled":
                    return None
    except (httpx.RequestError, ValueError) as exc:
        _fail_job(session, job, f"无法连接转录服务 {moss_url}：{exc}", "transcribe_queued")
        return None
    if created is None:
        _fail_job(session, job, "转录服务没有返回任务", "transcribe_queued")
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
    session.refresh(job)
    job.progress_json = {**(job.progress_json or {}), "moss_job_id": moss_id}
    session.commit()
    if job.status == "cancelled":
        _cancel_moss_transcription(moss_url, moss_id)
        return None
    _append_job_log(session, job, "转录服务已接收", "info", "transcribing", 0.3, {
        "stage": "queued",
        "message": "转录服务已接收",
        "percent": 30,
        "moss_job_id": moss_id,
    })
    result = None
    last_stage = ""
    # 轮询上限：至少 2 小时，或按音频时长 15x 估算。
    # 旧下限 450×2s=15 分钟隐含「video 一定带时长」的假设，但录音导入的会议
    # duration_seconds 为 NULL，预算直接落到 15 分钟——CPU 推理一段 45 分钟的
    # 录音要 ~20 分钟，会在快完成时被平台误判超时（本次事故根因）。
    # 下限提到 2 小时；时长缺失时在轮询里用 MOSS 回报的 audio_seconds 回填
    # video 并按 15x 放大预算（range 固定长度，因此循环用 while）。
    max_polls = max(3600, int((video.duration_seconds or 0) * 15))
    consecutive_errors = 0
    waited = 0
    while waited < max_polls:
        waited += 1
        time.sleep(2)
        try:
            job = session.get(ProcessingJob, job.id)
            if not job or job.status == "cancelled":
                _cancel_moss_transcription(moss_url, moss_id)
                return None
            with httpx.Client(timeout=15.0) as client:
                status = client.get(f"{moss_url}/v1/transcriptions/{moss_id}")
            if status.status_code >= 400:
                raise RuntimeError(f"转录任务查询返回 HTTP {status.status_code}")
            payload = status.json()
            moss_audio = payload.get("audio_seconds")
            if moss_audio and not video.duration_seconds:
                # 时长缺失（录音导入/实时录制）：用 MOSS 实测值回填，
                # 让本轮预算和前端进度估算都基于真实时长。
                video.duration_seconds = int(float(moss_audio))
                session.commit()
                max_polls = max(max_polls, int((video.duration_seconds or 0) * 15))
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
                        # 同样可能只是查不动状态而 MOSS 仍在推理，取消放锁后再判失败。
                        _cancel_moss_transcription(moss_url, moss_id)
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
        # 超时放弃时 MOSS 侧往往还在推理：必须先取消把全局推理锁放掉，
        # 否则之后所有任务（包括本任务的重试）都被 busy 挡在门外。
        _cancel_moss_transcription(moss_url, moss_id)
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


def _run_summary_model(
    settings: dict[str, Any],
    prompt: str,
    title: str,
    template: SummaryTemplate | None = None,
    max_tokens: int = 1024,
) -> tuple[str, str]:
    provider = str(settings.get("default_provider") or "local")
    if provider in {"openai", "ollama"} or settings.get("api_base_url"):
        return _chat_summary(settings, prompt)
    return _local_summary(settings, {
        "prompt": prompt,
        "text": "",
        "title": title,
        "template_name": template.name if template else None,
        "sections": template.sections_json if template else [],
        "max_tokens": max_tokens,
    })


def _wait_summary(session: Session, job: ProcessingJob, settings: dict[str, Any], template: SummaryTemplate | None, transcript: Transcript, title: str) -> tuple[str, str]:
    box: dict[str, Any] = {
        "done": False,
        "result": None,
        "error": None,
        "message": "正在准备转写内容",
        "done_units": 0,
        "total_units": 1,
    }

    def worker() -> None:
        try:
            source = _transcript_summary_source(transcript)
            chunk_limit = max(1000, min(int(settings.get("chunk_size") or 4000), 5000))
            context_limit = max(chunk_limit, min(int((settings.get("max_context") or 8000) * 0.65), 6000))
            chunks = _split_summary_text(source, chunk_limit)
            box["total_units"] = len(chunks) + 1
            notes: list[str] = []
            if len(chunks) > 1:
                for index, chunk in enumerate(chunks, start=1):
                    box["message"] = f"正在提取第 {index}/{len(chunks)} 段的决策与行动项"
                    prompt = "\n".join([
                        f"这是会议「{title}」的第 {index}/{len(chunks)} 段转写。",
                        "提取本段明确出现的事实、议题、结论、分歧、行动项、负责人、截止时间和原始时间戳。",
                        "用紧凑 Markdown 列表输出；未提及的信息不要猜测，不要写套话。",
                        "",
                        chunk,
                    ])
                    note, _model = _run_summary_model(settings, prompt, title, max_tokens=768)
                    notes.append(note)
                    box["done_units"] = index
                source = "\n\n".join(
                    f"### 分段记录 {index}\n{note}" for index, note in enumerate(notes, start=1)
                )
                reduction_round = 1
                while len(source) > context_limit:
                    groups = _split_summary_text(source, chunk_limit)
                    reduced: list[str] = []
                    box["total_units"] += len(groups)
                    for index, group in enumerate(groups, start=1):
                        box["message"] = f"正在合并长会议笔记（第 {reduction_round} 轮 {index}/{len(groups)}）"
                        compact, _model = _run_summary_model(
                            settings,
                            "合并下面的会议分段笔记，去重但保留所有明确决策、行动项、负责人、日期和时间戳。禁止推测。\n\n" + group,
                            title,
                            max_tokens=896,
                        )
                        reduced.append(compact)
                        box["done_units"] += 1
                    source = "\n\n".join(reduced)
                    reduction_round += 1
            box["message"] = "正在生成最终会议纪要"
            box["result"] = _run_summary_model(
                settings,
                _summary_user_prompt(template, source, title),
                title,
                template,
                max_tokens=1536,
            )
            box["done_units"] = box["total_units"]
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
        unit_ratio = float(box["done_units"]) / max(1, int(box["total_units"]))
        progress = min(0.96, max(0.82 + elapsed / 500, 0.82 + unit_ratio * 0.13))
        message = str(box.get("message") or "正在生成总结")
        _append_job_log(
            session,
            current,
            message,
            "info",
            "summarizing",
            progress,
            {
                "stage": "summarizing",
                "message": f"{message}，已用 {elapsed} 秒",
                "percent": int(progress * 100),
                "elapsed_seconds": elapsed,
                "done_chunks": box["done_units"],
                "total_chunks": box["total_units"],
            },
        )
    if box["error"]:
        raise box["error"]
    return box["result"]


def _fmt_summary_clock(value_ms: Any) -> str:
    try:
        seconds = max(0, int(float(value_ms or 0) / 1000))
    except (TypeError, ValueError):
        seconds = 0
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes:02d}:{seconds:02d}"


def _transcript_summary_source(transcript: Transcript) -> str:
    """给总结模型保留时间戳与说话人，避免模板要求引用时只能编造。"""
    lines: list[str] = []
    for segment in transcript.segments_json or []:
        text = str(segment.get("text") or "").strip()
        if not text:
            continue
        if segment.get("start_ms") is not None:
            start_ms = segment.get("start_ms")
        else:
            try:
                raw_start = float(segment.get("start") or 0)
            except (TypeError, ValueError):
                raw_start = 0
            start_ms = raw_start if raw_start > 1000 else raw_start * 1000
        stamp = _fmt_summary_clock(start_ms)
        speaker = str(segment.get("speaker") or "发言人未标注").strip()
        lines.append(f"[{stamp}] {speaker}：{text}")
    return "\n".join(lines) if lines else transcript.text.strip()


def _split_summary_text(text: str, limit: int = 4500) -> list[str]:
    """按行切块并限制上下文；超长单行再硬切，保证任何录音都不会只取开头。"""
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for raw_line in text.splitlines() or [text]:
        line = raw_line.strip()
        if not line:
            continue
        parts = [line[index:index + limit] for index in range(0, len(line), limit)]
        for part in parts:
            if current and size + len(part) + 1 > limit:
                chunks.append("\n".join(current))
                current, size = [], 0
            current.append(part)
            size += len(part) + 1
    if current:
        chunks.append("\n".join(current))
    return chunks or [""]


def _summary_user_prompt(template: SummaryTemplate | None, text: str, title: str) -> str:
    sections = (template.sections_json if template else None) or [{"title": "摘要", "instruction": "概括主要内容"}]
    lines = [
        f"请总结「{title}」的转写。按以下章节输出 Markdown；章节列表只是格式要求，直接输出正文，禁止复述说明文字。",
        "只能依据转写内容；负责人、日期或结论未明确时写“未提及”，禁止推测。",
        "引用行动项或关键决策时尽量保留输入中的时间戳。",
    ]
    for item in sections:
        lines.append(f"## {item.get('title')}\n{item.get('instruction') or ''}")
    lines.extend(["", "转写：", text])
    return "\n".join(lines)


def _generate_summary(session: Session, job: ProcessingJob, video: VideoAsset, transcript: Transcript, template_id: str | None) -> None:
    settings = merge_settings("transcription", (session.get(ConfigRecord, "transcription") or ConfigRecord(key="transcription", value_json={})).value_json)
    template = session.get(SummaryTemplate, template_id) if template_id else None
    section_titles = [str(item.get("title") or "") for item in ((template.sections_json if template else None) or [{"title": "摘要"}])]
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
        content = clean_summary_text(content, section_titles)
        if not content:
            raise RuntimeError("总结模型没有返回可用结果，请重试")
    except Exception as exc:
        current = session.get(ProcessingJob, job.id)
        if not current or current.status == "cancelled":
            return
        _fail_job(session, job, f"总结失败：{exc}", "summarizing")
        return
    job = session.get(ProcessingJob, job.id)
    video = session.get(VideoAsset, video.id)
    if not job or not video or job.status == "cancelled":
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
    try:
        with SessionLocal() as session:
            try:
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
                if job and job.status != "cancelled":
                    _complete_job(session, job, "转录完成")
            except Exception as exc:  # noqa: BLE001 - 后台线程不能留下永久 running 任务
                session.rollback()
                current = session.get(ProcessingJob, job_id)
                if current and current.status not in {"completed", "failed", "cancelled"}:
                    _fail_job(session, current, f"转录异常中断：{exc}", "transcribing")
    finally:
        (TEMP_DIR / f"hls-{job_id}.mp4").unlink(missing_ok=True)


def execute_summary_job(job_id: str, template_id: str | None, language: str, reuse_transcript: bool = True) -> None:
    try:
        with SessionLocal() as session:
            try:
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
                if not job or not video or job.status == "cancelled":
                    return
                _append_job_log(session, job, "准备生成总结", "info", "summarize_queued", 0.8)
                _generate_summary(session, job, video, transcript, template_id)
            except Exception as exc:  # noqa: BLE001 - 保证总结任务一定进入终态
                session.rollback()
                current = session.get(ProcessingJob, job_id)
                if current and current.status not in {"completed", "failed", "cancelled"}:
                    _fail_job(session, current, f"总结异常中断：{exc}", "summarizing")
    finally:
        (TEMP_DIR / f"hls-{job_id}.mp4").unlink(missing_ok=True)


def _summary_ready(settings: dict[str, Any]) -> bool:
    """总结任务的前置检查。

    本地总结由 MOSS 的 /v1/summaries 用 llama-cli 加载 summary_model_path 指向的
    GGUF 完成，所以「内置本地模型」是否可用取决于那个文件在不在；远程 provider
    则必须有 api_base_url，否则 _chat_summary 里必然抛「未配置 API Base URL」，
    这里提前拦住，避免创建一个注定失败的任务。
    """
    provider = str(settings.get("default_provider") or "local")
    if provider == "local":
        return bool(settings.get("summary_model_found"))
    return bool(settings.get("api_base_url"))


@app.post("/api/videos/{video_id}/summary")
def summary(video_id: str, payload: dict[str, Any] | None = None, user: Any = Depends(get_current_user), session: Session = Depends(db_session)):
    video = get_owned_video(session, user, video_id)
    if _has_active_job(session, video_id):
        raise HTTPException(409, "该媒体已有处理任务进行中，请等待完成")
    settings = current_transcription()
    if not _summary_ready(settings):
        raise HTTPException(400, "总结模型未配置")
    body = payload or {}
    language = (body.get("transcription") or {}).get("language") or settings.get("default_language") or "zh"
    job = _create_media_job(session, video, "summarize", "总结任务已创建", {
        "language": language,
        "template_id": body.get("template_id"),
        "reuse_transcript": bool(body.get("reuse_transcript", True)),
    })
    threading.Thread(
        target=execute_summary_job,
        args=(job.id, body.get("template_id"), language, bool(body.get("reuse_transcript", True))),
        daemon=True,
    ).start()
    return job_dict(job)


@app.post("/api/videos/{video_id}/summaries")
def summary_from_transcript(video_id: str, payload: dict[str, Any] | None = None, user: Any = Depends(get_current_user), session: Session = Depends(db_session)):
    video = get_owned_video(session, user, video_id)
    if _has_active_job(session, video_id):
        raise HTTPException(409, "该媒体已有处理任务进行中，请等待完成")
    settings = current_transcription()
    if not _summary_ready(settings):
        raise HTTPException(400, "总结模型未配置")
    if not _latest_transcript(session, video_id):
        raise HTTPException(409, "还没有转录结果，请先转录")
    body = payload or {}
    language = settings.get("default_language") or "zh"
    job = _create_media_job(session, video, "summarize", "根据已有转录生成总结", {
        "language": language,
        "template_id": body.get("template_id"),
        "reuse_transcript": True,
    })
    threading.Thread(target=execute_summary_job, args=(job.id, body.get("template_id"), language, True), daemon=True).start()
    return job_dict(job)


@app.patch("/api/transcripts/{transcript_id}")
def update_transcript(
    transcript_id: str,
    payload: TranscriptUpdateRequest,
    user: Any = Depends(get_current_user),
    session: Session = Depends(db_session),
):
    """校对结果另存为新版本，保留原始 ASR 结果用于回退和审计。"""
    source = session.get(Transcript, transcript_id)
    if source is None:
        raise HTTPException(404, "转录不存在")
    get_owned_video(session, user, source.video_asset_id)
    segments = [dict(item) for item in (payload.segments if payload.segments is not None else source.segments_json or [])]
    names = {str(key): str(value).strip()[:80] for key, value in payload.speaker_names.items() if str(value).strip()}
    for item in segments:
        speaker = str(item.get("speaker") or "")
        if speaker in names:
            item["speaker"] = names[speaker]
        item["text"] = str(item.get("text") or "").strip()
    text = (payload.text or "").strip()
    if not text and segments:
        text = "\n".join(str(item.get("text") or "") for item in segments if str(item.get("text") or "").strip())
    if not text:
        raise HTTPException(400, "转录内容不能为空")
    revised = Transcript(
        job_id=source.job_id,
        video_asset_id=source.video_asset_id,
        model=f"{source.model} (校对版)",
        language=source.language,
        text=text,
        segments_json=segments,
        duration_seconds=source.duration_seconds,
    )
    session.add(revised)
    session.commit()
    session.refresh(revised)
    return {
        "id": revised.id,
        "model": revised.model,
        "language": revised.language,
        "text": revised.text,
        "segments": revised.segments_json,
        "created_at": revised.created_at.isoformat(),
    }


@app.patch("/api/summaries/{summary_id}")
def update_summary(
    summary_id: str,
    payload: SummaryUpdateRequest,
    user: Any = Depends(get_current_user),
    session: Session = Depends(db_session),
):
    """人工编辑纪要时创建新版本，不覆盖模型原稿。"""
    source = session.get(Summary, summary_id)
    if source is None:
        raise HTTPException(404, "纪要不存在")
    video = get_owned_video(session, user, source.video_asset_id)
    content = payload.content_markdown.strip()
    revised = Summary(
        job_id=source.job_id,
        video_asset_id=source.video_asset_id,
        template_id=source.template_id,
        provider="manual",
        model="人工编辑",
        content_markdown=content,
        source_transcript_id=source.source_transcript_id,
    )
    session.add(revised)
    session.flush()
    video.latest_summary_id = revised.id
    session.commit()
    session.refresh(revised)
    return summary_dict(revised)


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str, user: Any = Depends(get_current_user), session: Session = Depends(db_session)):
    job = get_owned_job(session, user, job_id)
    return job_dict(job)


@app.post("/api/jobs/{job_id}/retry")
def retry_job(job_id: str, user: Any = Depends(get_current_user), session: Session = Depends(db_session)):
    previous = get_owned_job(session, user, job_id)
    if previous.status not in {"failed", "cancelled"}:
        raise HTTPException(409, "只有失败或已取消的任务可以重试")
    if _has_active_job(session, previous.video_asset_id):
        raise HTTPException(409, "该媒体已有处理任务进行中")
    video = get_owned_video(session, user, previous.video_asset_id)
    settings = current_transcription()
    previous_context = previous.progress_json or {}
    if previous.job_type == "transcribe":
        ensure_moss_available(settings)
        language = previous_context.get("language") or settings.get("default_language") or "zh"
        created = _create_media_job(
            session,
            video,
            "transcribe",
            f"重试转录任务 {previous.id}",
            {"language": language, "retry_of": previous.id},
        )
        threading.Thread(target=execute_transcribe_job, args=(created.id, language), daemon=True).start()
    elif previous.job_type == "summarize":
        if not _summary_ready(settings):
            raise HTTPException(400, "总结模型未配置")
        template = session.scalars(
            select(SummaryTemplate).where(SummaryTemplate.is_active.is_(True), SummaryTemplate.is_default.is_(True))
        ).first()
        template_id = previous_context.get("template_id") or (template.id if template else None)
        language = previous_context.get("language") or settings.get("default_language") or "zh"
        reuse_transcript = bool(previous_context.get("reuse_transcript", True))
        created = _create_media_job(session, video, "summarize", f"重试总结任务 {previous.id}", {
            "language": language,
            "template_id": template_id,
            "reuse_transcript": reuse_transcript,
            "retry_of": previous.id,
        })
        threading.Thread(
            target=execute_summary_job,
            args=(created.id, template_id, language, reuse_transcript),
            daemon=True,
        ).start()
    else:
        raise HTTPException(400, "下载任务缺少原始格式信息，请在下载面板重新下载")
    return job_dict(created)


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job_compat(job_id: str, user: Any = Depends(get_current_user), session: Session = Depends(db_session)):
    """兼容旧入口：与 DELETE /api/jobs/{id} 等价，同样要放掉 MOSS 推理锁。"""
    return cancel_job(job_id, user, session)


@app.get("/api/jobs/{job_id}/events")
def job_events(job_id: str, user: Any = Depends(get_current_user_flex), session: Session = Depends(db_session)):
    get_owned_job(session, user, job_id)
    def stream():
        last_payload = ""
        while True:
            with SessionLocal() as event_session:
                current = event_session.get(ProcessingJob, job_id)
                if current is None:
                    yield f"event: error\ndata: {json.dumps({'detail': '任务不存在'}, ensure_ascii=False)}\n\n"
                    return
                payload = json.dumps(job_dict(current), ensure_ascii=False)
                terminal = current.status in {"completed", "failed", "cancelled"}
            if payload != last_payload:
                yield f"event: status\ndata: {payload}\n\n"
                last_payload = payload
            else:
                yield ": keep-alive\n\n"
            if terminal:
                return
            time.sleep(1)
    return StreamingResponse(stream(), media_type="text/event-stream")


@app.get("/api/templates")
def list_templates(listed: bool | None = Query(None), user: Any = Depends(get_current_user), session: Session = Depends(db_session)):
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
                item.pop("cookies_masked", None)
                item.pop("cookie_status", None)
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
def get_settings(group: str, user: Any = Depends(get_current_user), session: Session = Depends(db_session)):
    if group not in SETTING_DEFAULTS:
        raise HTTPException(404, "设置分组不存在")
    return config_endpoint(group, session)


@app.put("/api/settings/{group}")
def put_settings(group: str, payload: dict[str, Any], _: Any = Depends(require_admin), session: Session = Depends(db_session)):
    if group not in SETTING_DEFAULTS:
        raise HTTPException(404, "设置分组不存在")
    return config_endpoint(group, session, payload)


def _make_default_template(session: Session, template: SummaryTemplate) -> None:
    """设为默认并清掉其它模板的默认标记（全局唯一默认）。"""
    template.is_default = True
    for other in session.scalars(
        select(SummaryTemplate).where(SummaryTemplate.id != template.id, SummaryTemplate.is_default.is_(True))
    ).all():
        other.is_default = False


@app.post("/api/templates")
def create_template(request: TemplateRequest, _: Any = Depends(require_admin), session: Session = Depends(db_session)):
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
    if request.is_default:
        _make_default_template(session, template)
        session.commit()
    else:
        _ensure_single_default_template(session)
    session.refresh(template)
    return template_dict(template)


@app.put("/api/templates/{template_id}")
def update_template(template_id: str, request: TemplateRequest, _: Any = Depends(require_admin), session: Session = Depends(db_session)):
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
    if request.is_default is not None:
        if request.is_default:
            _make_default_template(session, template)
        else:
            template.is_default = False
            _ensure_single_default_template(session)
    session.commit()
    return template_dict(template)


@app.post("/api/templates/{template_id}/restore")
def restore_template(template_id: str, _: Any = Depends(require_admin), session: Session = Depends(db_session)):
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
def delete_template(template_id: str, _: Any = Depends(require_admin), session: Session = Depends(db_session)):
    template = session.get(SummaryTemplate, template_id)
    if not template:
        raise HTTPException(404, "模板不存在")
    if template.origin == "builtin":
        raise HTTPException(400, "内置模板不可删除，可以关闭首页显示或恢复默认")
    was_default = bool(template.is_default)
    template.is_active = False
    if was_default:
        template.is_default = False
    session.commit()
    if was_default:
        _ensure_single_default_template(session)
    return {"deleted": template_id}


def _purge_video_files(upload_paths: list[str], preview_paths: list[Path], cover: Path | None) -> None:
    """后台清理媒体文件：DB 记录已先行删除，文件删失败只记日志不阻塞响应。"""
    for raw in upload_paths:
        try:
            Path(raw).unlink(missing_ok=True)
        except OSError as exc:
            logging.getLogger(__name__).warning("删除上传文件失败 %s: %s", raw, exc)
    for preview in preview_paths:
        preview.unlink(missing_ok=True)
    if cover is not None:
        cover.unlink(missing_ok=True)


@app.delete("/api/videos/{video_id}")
def delete_video(video_id: str, background_tasks: BackgroundTasks, user: Any = Depends(get_current_user), session: Session = Depends(db_session)):
    video = get_owned_video(session, user, video_id)
    active_job = session.scalars(
        select(ProcessingJob).where(
            ProcessingJob.video_asset_id == video_id,
            ProcessingJob.status.in_(("queued", "running")),
        ).limit(1)
    ).first()
    if active_job:
        raise HTTPException(409, "处理任务仍在运行，请先取消或等待任务完成")
    variants = list(session.scalars(select(MediaVariant).where(MediaVariant.video_asset_id == video_id)).all())
    upload_paths: list[str] = []
    if (video.platform or video.source_platform) == "upload":
        upload_root = UPLOAD_DIR.resolve()
        for variant in variants:
            if not variant.local_path:
                continue
            path = Path(variant.local_path).resolve()
            if not path.is_relative_to(upload_root):
                raise HTTPException(500, "拒绝删除受管上传目录以外的文件")
            upload_paths.append(str(path))
    previews = [preview for variant in variants for preview in PREVIEW_DIR.glob(f"{variant.id}.*")]
    cover: Path | None = None
    if video.thumbnail_path:
        candidate = Path(video.thumbnail_path).resolve()
        if candidate.is_relative_to(COVER_DIR.resolve()):
            cover = candidate
    # 文件清理放后台：大文件 unlink 是删除接口慢的主因，DB 记录先删、立即返回。
    background_tasks.add_task(_purge_video_files, upload_paths, previews, cover)
    session.delete(video); session.commit()
    return {"deleted": video_id}


from .meetings import register_meeting_routes
from .auth import register_auth_routes

register_meeting_routes(app)
register_auth_routes(app)


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


def _has_active_job(session: Session, video_asset_id: str | None, job_type: str | None = None) -> bool:
    if not video_asset_id:
        return False
    query = select(ProcessingJob.id).where(
        ProcessingJob.video_asset_id == video_asset_id,
        ProcessingJob.status.in_(("running", "queued")),
    )
    if job_type:
        query = query.where(ProcessingJob.job_type == job_type)
    return session.scalar(query.limit(1)) is not None


def _reconcile_meetings(session: Session, now: datetime, include_recording: bool) -> None:
    """把卡死的会议状态收敛掉：跟随最新任务的终态落地。

    只在「确实没有对应活动」时动手，永不误伤正在录音/正在转录的会议。
    """
    from .meetings import finalize_live_meeting

    statuses = ("recording", "processing", "summarizing") if include_recording else ("processing", "summarizing")
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
            transcript = _latest_transcript(session, meeting.video_asset_id)
            if job.status == "completed" and transcript:
                meeting.status = "ready"
                meeting.error_message = None
                meeting.updated_at = now
                continue
            if job.status in {"failed", "cancelled"}:
                # 纪要失败不应让已经成功的终稿消失，也不能阻止用户再次生成纪要。
                meeting.status = "ready" if job.job_type == "summarize" and transcript else "failed"
                fallback = "纪要生成已取消" if job.job_type == "summarize" else "转录任务已取消"
                meeting.error_message = job.error_message or fallback
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


def _cleanup_stale_temp_files() -> None:
    settings = current_parser_settings()
    retain_seconds = max(1, int(settings.get("temp_retain_hours") or 24)) * 3600
    cutoff = time.time() - retain_seconds
    for path in TEMP_DIR.iterdir():
        try:
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink(missing_ok=True)
        except OSError:
            continue


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
        _cleanup_stale_temp_files()
    except Exception:
        traceback.print_exc()
    threading.Thread(target=_sweep_stale_meetings, daemon=True, name="meeting-sweeper").start()

