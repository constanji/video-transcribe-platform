from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import shutil
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Header,
    HTTPException,
    Query,
    Request,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from pydantic import BaseModel, Field
from sqlalchemy import String, cast, or_, select
from sqlalchemy.orm import Session

router = APIRouter()

MEETING_FORMATS = {".mp3": "audio", ".wav": "audio", ".m4a": "audio", ".webm": "audio", ".mp4": "video", ".mov": "video"}


class CreateMeetingRequest(BaseModel):
    title: str | None = None
    mode: str = Field(pattern="^(live|recording)$")
    language: str = "zh"
    audio_source: str = Field(default="mic", pattern="^(mic|system|both)$")


class CreateApiKeyRequest(BaseModel):
    name: str | None = None


class SummaryMeetingRequest(BaseModel):
    template_id: str | None = None


class RenameMeetingRequest(BaseModel):
    title: str = Field(min_length=1, max_length=500)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def hash_api_key(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def mask_key(prefix: str) -> str:
    return f"{prefix}****"


def _origin_host(value: str | None) -> str:
    if not value:
        return ""
    parsed = urlparse(value)
    return (parsed.hostname or "").lower()


def is_local_browser(request: Any) -> bool:
    headers = getattr(request, "headers", None)
    getter = getattr(headers, "get", None)
    origin = _origin_host(getter("origin") if getter else None)
    referer = _origin_host(getter("referer") if getter else None)
    url = getattr(request, "url", None)
    host = (getattr(url, "hostname", None) or "").lower()
    allowed = {"localhost", "127.0.0.1", host}
    return origin in allowed or referer in allowed


def extract_api_key(authorization: str | None, x_api_key: str | None, query_key: str | None = None) -> str:
    if x_api_key:
        return x_api_key.strip()
    if query_key:
        return query_key.strip()
    if authorization:
        scheme, _, remainder = authorization.partition(" ")
        if scheme.lower() == "bearer" and remainder:
            return remainder.strip()
        return authorization.strip()
    return ""


def require_meeting_key(
    request: Request,
    session: Session,
    raw_key: str,
) -> Any | None:
    from .main import ApiKey, User

    keys = list(session.scalars(select(ApiKey).where(ApiKey.revoked.is_(False))).all())
    if raw_key:
        hashed = hash_api_key(raw_key)
        match = next((item for item in keys if item.hashed == hashed), None)
        if not match:
            raise HTTPException(401, "API Key 无效")
        match.last_used_at = _now()
        session.commit()
        return match
    # 过渡期放行：系统既没有任何 API Key、也没有任何用户（全新库首次启动）。
    if not keys and session.scalar(select(User.id).limit(1)) is None:
        return None
    # 本地开发放行：必须显式设置 AUTH_LOCAL_BYPASS=1（服务器不设即关）。
    if os.getenv("AUTH_LOCAL_BYPASS") == "1" and is_local_browser(request):
        return None
    raise HTTPException(401, "请提供 API Key 或登录（Authorization: Bearer 或 X-API-Key）")


def get_meeting_or_404(session: Session, meeting_id: str):
    from .main import Meeting

    meeting = session.get(Meeting, meeting_id)
    if not meeting:
        raise HTTPException(404, "会议不存在")
    return meeting


def ensure_meeting_video(session: Session, meeting) -> Any:
    from .main import MediaVariant, VideoAsset

    if meeting.video_asset_id:
        video = session.get(VideoAsset, meeting.video_asset_id)
        if video:
            return video
    video = VideoAsset(
        source_url=f"meeting://{meeting.id}",
        normalized_url=f"meeting://{meeting.id}",
        platform="meeting",
        source_platform="meeting",
        platform_video_id=meeting.id,
        title=meeting.title,
        author="会议纪要",
        parse_status="uploaded",
        last_processed_at=_now(),
        user_id=meeting.user_id,
    )
    session.add(video)
    session.flush()
    session.add(MediaVariant(
        video_asset_id=video.id,
        format="webm",
        quality="会议录音",
        media_type="audio",
        download_status="available",
    ))
    meeting.video_asset_id = video.id
    session.commit()
    session.refresh(video)
    return video


def meeting_dict(session: Session, meeting) -> dict[str, Any]:
    from .main import ProcessingJob, Summary, Transcript, _latest_transcript, _video_detail, job_dict, summary_dict

    video = None
    if meeting.video_asset_id:
        from .main import VideoAsset
        video = session.get(VideoAsset, meeting.video_asset_id)
    transcript = _latest_transcript(session, meeting.video_asset_id) if meeting.video_asset_id else None
    summary = None
    job = None
    if meeting.video_asset_id:
        summary = session.scalars(
            select(Summary).where(Summary.video_asset_id == meeting.video_asset_id).order_by(Summary.created_at.desc())
        ).first()
        job = session.scalars(
            select(ProcessingJob).where(ProcessingJob.video_asset_id == meeting.video_asset_id).order_by(ProcessingJob.created_at.desc())
        ).first()
    live_segments = list(meeting.live_segments_json or [])
    transcript_payload = None
    # 旧终稿在重新转录、纪要失败或取消时仍然可用，不应随会议状态被隐藏。
    if transcript and transcript.text.strip():
        transcript_payload = {
            "id": transcript.id,
            "model": transcript.model,
            "language": transcript.language,
            "text": transcript.text,
            "segments": transcript.segments_json,
            "created_at": transcript.created_at.isoformat(),
        }
    elif live_segments:
        text = "\n".join(str(item.get("text") or "") for item in live_segments).strip()
        transcript_payload = {
            "id": None,
            "model": "MOSS-live",
            "language": meeting.language,
            "text": text,
            "segments": live_segments,
            "created_at": meeting.updated_at.isoformat() if meeting.updated_at else None,
        }
    return {
        "id": meeting.id,
        "title": meeting.title,
        "mode": meeting.mode,
        "status": meeting.status,
        "language": meeting.language,
        "audio_source": meeting.audio_source,
        "video_id": meeting.video_asset_id,
        "error_message": meeting.error_message,
        "live_segments": live_segments,
        "transcript": transcript_payload,
        "summary": summary_dict(summary) if summary else None,
        "job": job_dict(job) if job else None,
        "video": _video_detail(video, session) if video else None,
        "created_at": meeting.created_at.isoformat(),
        "updated_at": meeting.updated_at.isoformat() if meeting.updated_at else meeting.created_at.isoformat(),
        "ws_url": f"/api/v1/meetings/{meeting.id}/live",
    }


def require_moss_ready() -> dict[str, Any]:
    from .main import ensure_moss_available

    # 统一前置检查里带僵尸锁回收：本平台已失败/已取消的任务若还在 MOSS 侧
    # 占着推理锁（典型：转录超时），直接取消抢回，别让用户的重试永远被
    # 「正忙于另一个任务」挡住。
    return ensure_moss_available()


def start_final_transcribe(session: Session, meeting) -> Any:
    from .main import _create_media_job, _has_active_job

    video = ensure_meeting_video(session, meeting)
    if _has_active_job(session, video.id, "transcribe"):
        raise HTTPException(409, "终稿转录正在进行中")
    job = _create_media_job(session, video, "transcribe", "会议终稿转录已创建")
    meeting.status = "processing"
    meeting.error_message = None
    meeting.updated_at = _now()
    session.commit()
    threading_start_final(meeting.id, job.id, meeting.language)
    return job


def threading_start_final(meeting_id: str, job_id: str, language: str) -> None:
    import threading

    from .main import Meeting, ProcessingJob, SessionLocal, execute_transcribe_job

    def worker() -> None:
        execute_transcribe_job(job_id, language)
        with SessionLocal() as session:
            meeting = session.get(Meeting, meeting_id)
            job = session.get(ProcessingJob, job_id)
            if not meeting or not job:
                return
            if job.status == "completed":
                meeting.status = "ready"
                meeting.error_message = None
            elif job.status == "failed":
                meeting.status = "failed"
                meeting.error_message = job.error_message
            meeting.updated_at = _now()
            session.commit()

    threading.Thread(target=worker, daemon=True, name=f"meeting-final-{meeting_id[:8]}").start()


def finalize_live_meeting(session: Session, meeting) -> None:
    """live 会议没有落盘音频时，用已收的 live_segments 直接归档为 ready。

    用于 /stop、启动对账与僵尸清扫，避免会议永久停留在 processing。
    """
    from .main import _complete_job, _create_media_job, _save_transcript

    segments = [item for item in (meeting.live_segments_json or []) if str(item.get("text") or "").strip()]
    if not segments:
        meeting.status = "failed"
        meeting.error_message = "实时会议没有可归档的片段"
        meeting.updated_at = _now()
        session.commit()
        return
    video = ensure_meeting_video(session, meeting)
    job = _create_media_job(session, video, "transcribe", "实时会议片段已归档")
    text = "\n".join(str(item.get("text") or "") for item in segments).strip()
    _save_transcript(
        session,
        job,
        {"text": text, "segments": segments, "model": "MOSS-live", "audio_seconds": None},
        meeting.language,
    )
    _complete_job(session, job, "实时片段已归档")
    meeting.status = "ready"
    meeting.error_message = None
    meeting.updated_at = _now()
    session.commit()


def start_summary(session: Session, meeting, template_id: str | None) -> Any:
    from .main import _create_media_job, _has_active_job, _latest_transcript, _summary_ready, current_transcription, execute_summary_job

    settings = current_transcription()
    if not _summary_ready(settings):
        raise HTTPException(400, "总结模型未配置")
    if meeting.status != "ready":
        raise HTTPException(409, "请等待终稿转录完成后再总结")
    if not meeting.video_asset_id or not _latest_transcript(session, meeting.video_asset_id):
        raise HTTPException(409, "还没有转录结果，请先完成转录")
    video = ensure_meeting_video(session, meeting)
    if _has_active_job(session, video.id, "summarize"):
        raise HTTPException(409, "会议纪要正在生成中")
    job = _create_media_job(session, video, "summarize", "根据会议转录生成纪要")
    meeting.status = "summarizing"
    meeting.error_message = None
    meeting.updated_at = _now()
    session.commit()

    import threading

    from .main import Meeting, ProcessingJob, SessionLocal

    def worker() -> None:
        execute_summary_job(job.id, template_id, meeting.language, True)
        with SessionLocal() as session_inner:
            current = session_inner.get(Meeting, meeting.id)
            stored = session_inner.get(ProcessingJob, job.id)
            if not current or not stored:
                return
            if stored.status == "completed":
                current.status = "ready"
                current.error_message = None
            elif stored.status == "failed":
                # 纪要失败不影响已经完成的终稿，保留 ready 才能直接重试总结。
                current.status = "ready"
                current.error_message = stored.error_message
            elif stored.status == "cancelled":
                current.status = "ready"
                current.error_message = "纪要生成已取消"
            current.updated_at = _now()
            session_inner.commit()

    threading.Thread(target=worker, daemon=True, name=f"meeting-summary-{meeting.id[:8]}").start()
    return job


class MossBusy(RuntimeError):
    """MOSS 推理锁被长任务占住，未能及时返回结果。可退避重试。"""

    def __init__(self, retry_after: float = 2.0):
        self.retry_after = retry_after
        super().__init__("moss busy")


def moss_sync(pcm16: str, sample_rate: int, language: str, offset_ms: float) -> dict[str, Any]:
    from .main import moss_service_url

    url = moss_service_url()
    # 实时块不该等太久：MOSS 侧抢不到锁会在 ~3s 内回 503 busy，60s 作为真实推理的上限足够。
    timeout = float(os.getenv("MOSS_SYNC_TIMEOUT", "60"))
    with httpx.Client(timeout=timeout) as client:
        response = client.post(
            f"{url}/v1/transcriptions/sync",
            json={
                "pcm16": pcm16,
                "sample_rate": sample_rate,
                "language": language,
                "offset_ms": offset_ms,
            },
        )
    if response.status_code == 503:
        try:
            payload = response.json()
        except Exception:
            payload = {}
        if isinstance(payload, dict) and payload.get("busy"):
            raise MossBusy(float(payload.get("retry_after") or 2.0))
    if response.status_code >= 400:
        detail = response.text
        try:
            detail = response.json().get("detail") or detail
        except Exception:
            pass
        raise RuntimeError(str(detail))
    payload = response.json()
    return payload if isinstance(payload, dict) else {}


_live_locks: dict[str, threading.Lock] = {}
_live_locks_guard = threading.Lock()


def _live_lock(meeting_id: str) -> threading.Lock:
    with _live_locks_guard:
        return _live_locks.setdefault(meeting_id, threading.Lock())


def append_live_segments(meeting_id: str, segments: list[dict[str, Any]]) -> None:
    from .main import Meeting, SessionLocal

    if not segments:
        return
    # 同一会议串行写入，避免「读-改-写」在并发下丢片段。
    with _live_lock(meeting_id):
        with SessionLocal() as session:
            meeting = session.get(Meeting, meeting_id)
            if not meeting:
                return
            current = list(meeting.live_segments_json or [])
            current.extend(segments)
            meeting.live_segments_json = current
            meeting.updated_at = _now()
            session.commit()


def touch_meeting(meeting_id: str) -> None:
    """仅刷新 updated_at，用于 WS 心跳，避免长时间静默的会议被僵尸清扫误判。"""
    from .main import Meeting, SessionLocal

    with SessionLocal() as session:
        meeting = session.get(Meeting, meeting_id)
        if meeting:
            meeting.updated_at = _now()
            session.commit()


def get_db():
    from .main import SessionLocal
    with SessionLocal() as session:
        yield session


def meeting_auth(
    request: Request,
    session: Session = Depends(get_db),
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    token: str | None = Query(default=None),
):
    """双通道认证：先 JWT（Bearer header 或 ?token=），再 ApiKey，最后过渡期放行。

    返回 (kind, principal)：('user', User) / ('api_key', ApiKey) / ('anon', None)。
    """
    from .auth import resolve_principal

    if not authorization and token:
        authorization = f"Bearer {token}"
    return resolve_principal(request, session, authorization, x_api_key)


def _principal_user(session: Session, kind: str, principal: Any) -> Any | None:
    """把认证结果折算成用户：api_key 取其归属者；anon（过渡期）返回 None 表示不限。"""
    if kind == "user":
        return principal
    if kind == "api_key" and getattr(principal, "user_id", None):
        from .main import User

        return session.get(User, principal.user_id)
    return None


def _meeting_scope(session: Session, auth: tuple[str, Any]) -> list:
    """会议数据隔离：admin 全见；普通用户（含 API Key 归属者）只看自己；anon 过渡期全见。"""
    from .main import Meeting

    user = _principal_user(session, *auth)
    if user is None or user.role == "admin":
        return []
    return [Meeting.user_id == user.id]


def get_owned_meeting(session: Session, auth: tuple[str, Any], meeting_id: str):
    """取会议并校验归属；不存在或越权一律 404（不暴露存在性）。"""
    from .main import Meeting

    meeting = session.get(Meeting, meeting_id)
    user = _principal_user(session, *auth)
    if meeting is None or (user is not None and user.role != "admin" and meeting.user_id != user.id):
        raise HTTPException(404, "会议不存在")
    return meeting


@router.post("/api/v1/meetings")
def create_meeting(payload: CreateMeetingRequest, request: Request, session: Session = Depends(get_db), auth=Depends(meeting_auth)):
    from .main import Meeting

    language = payload.language or "zh"
    if language not in {"zh", "auto", "en"}:
        raise HTTPException(400, "不支持的转录语言")
    stamp = _now().strftime("会议 %Y-%m-%d %H:%M")
    owner = _principal_user(session, *auth)
    meeting = Meeting(
        title=(payload.title or "").strip() or stamp,
        mode=payload.mode,
        language=language,
        audio_source=payload.audio_source,
        status="created",
        live_segments_json=[],
        user_id=owner.id if owner else None,
    )
    session.add(meeting)
    session.flush()
    ensure_meeting_video(session, meeting)
    session.refresh(meeting)
    return meeting_dict(session, meeting)


@router.get("/api/v1/meetings")
def list_meetings(request: Request, q: str = "", session: Session = Depends(get_db), auth=Depends(meeting_auth)):
    from .main import Meeting, Summary, Transcript, VideoAsset, _latest_transcript

    scope = _meeting_scope(session, auth)
    # 检索：标题 / 转录正文 / 纪要正文（含实时分段），任一命中即返回。
    keyword = (q or "").strip()
    if keyword:
        like = f"%{keyword}%"
        transcript_hit = select(Transcript.video_asset_id).where(Transcript.text.ilike(like))
        summary_hit = select(Summary.video_asset_id).where(Summary.content_markdown.ilike(like))
        scope = [
            *scope,
            or_(
                Meeting.title.ilike(like),
                Meeting.video_asset_id.in_(transcript_hit),
                Meeting.video_asset_id.in_(summary_hit),
                cast(Meeting.live_segments_json, String).ilike(like),
            ),
        ]
    meetings = session.scalars(select(Meeting).where(*scope).order_by(Meeting.updated_at.desc())).all()
    items: list[dict[str, Any]] = []
    for meeting in meetings:
        video = session.get(VideoAsset, meeting.video_asset_id) if meeting.video_asset_id else None
        transcript = _latest_transcript(session, meeting.video_asset_id) if meeting.video_asset_id else None
        has_summary = False
        if meeting.video_asset_id:
            has_summary = session.scalars(
                select(Summary.id).where(Summary.video_asset_id == meeting.video_asset_id).limit(1)
            ).first() is not None
        live_segments = list(meeting.live_segments_json or [])
        has_transcript = bool((transcript and transcript.text and transcript.text.strip()) or live_segments)
        duration = None
        if video is not None and video.duration_seconds:
            duration = video.duration_seconds
        elif transcript is not None and transcript.duration_seconds:
            duration = transcript.duration_seconds
        items.append(
            {
                "id": meeting.id,
                "title": meeting.title,
                "status": meeting.status,
                "mode": meeting.mode,
                "language": meeting.language,
                # 会议的结果面板（转录+纪要工作室）直接读 video asset，列表带上 id 免得逐个回查。
                "video_id": meeting.video_asset_id,
                "duration_seconds": duration,
                "has_transcript": has_transcript,
                "has_summary": has_summary,
                "error_message": meeting.error_message,
                "created_at": meeting.created_at.isoformat(),
                "updated_at": meeting.updated_at.isoformat(),
            }
        )
    return items


@router.get("/api/v1/meetings/{meeting_id}")
def get_meeting(meeting_id: str, request: Request, session: Session = Depends(get_db), auth=Depends(meeting_auth)):
    meeting = get_owned_meeting(session, auth, meeting_id)
    return meeting_dict(session, meeting)


@router.patch("/api/v1/meetings/{meeting_id}")
def rename_meeting(meeting_id: str, payload: RenameMeetingRequest, request: Request, session: Session = Depends(get_db), auth=Depends(meeting_auth)):
    from .main import VideoAsset

    meeting = get_owned_meeting(session, auth, meeting_id)
    title = payload.title.strip()
    if not title:
        raise HTTPException(400, "会议名称不能为空")
    meeting.title = title
    meeting.updated_at = _now()
    # 关联的视频资产同步改名：历史任务列表/文件弹窗/纪要落盘标题保持一致。
    if meeting.video_asset_id:
        video = session.get(VideoAsset, meeting.video_asset_id)
        if video is not None:
            video.title = title
    session.commit()
    return meeting_dict(session, meeting)


@router.post("/api/v1/meetings/{meeting_id}/audio")
async def upload_meeting_audio(
    meeting_id: str,
    request: Request,
    file: UploadFile = File(...),
    transcribe: bool = Query(True),
    session: Session = Depends(get_db),
    auth=Depends(meeting_auth),
):
    from .main import MEETING_DIR, MediaVariant, _uploaded_media_info, job_dict

    meeting = get_owned_meeting(session, auth, meeting_id)
    original_name = Path((file.filename or "recording.webm").replace("\\", "/")).name
    extension = Path(original_name).suffix.lower() or ".webm"
    media_type = MEETING_FORMATS.get(extension)
    if not media_type:
        raise HTTPException(400, "仅支持 MP3、WAV、M4A、MP4、WebM 和 MOV 文件")
    folder = MEETING_DIR / meeting.id
    folder.mkdir(parents=True, exist_ok=True)
    dest = folder / f"audio{extension}"
    temp_dest = folder / f".upload-{secrets.token_hex(6)}.part"
    size = 0
    try:
        with temp_dest.open("wb") as target:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > 500 * 1024 * 1024:
                    raise HTTPException(413, "单个文件不能超过 500MB")
                target.write(chunk)
    except Exception:
        temp_dest.unlink(missing_ok=True)
        raise
    finally:
        await file.close()
    if size == 0:
        temp_dest.unlink(missing_ok=True)
        raise HTTPException(400, "不能上传空文件")
    previous_path = Path(meeting.audio_path) if meeting.audio_path else None
    os.replace(temp_dest, dest)
    if previous_path and previous_path != dest:
        previous_path.unlink(missing_ok=True)
    duration = _uploaded_media_info(dest, media_type)
    video = ensure_meeting_video(session, meeting)
    video.title = meeting.title
    video.duration_seconds = duration
    video.parse_status = "uploaded"
    video.last_processed_at = _now()
    variant = session.scalars(select(MediaVariant).where(MediaVariant.video_asset_id == video.id)).first()
    if variant is None:
        variant = MediaVariant(video_asset_id=video.id, download_status="downloaded")
        session.add(variant)
    variant.format = extension[1:]
    variant.quality = "会议录音"
    variant.media_type = media_type
    variant.local_path = str(dest)
    variant.file_size = size
    variant.download_status = "downloaded"
    meeting.audio_path = str(dest)
    meeting.updated_at = _now()
    session.commit()
    job = None
    if transcribe:
        require_moss_ready()
        job = start_final_transcribe(session, meeting)
    session.refresh(meeting)
    payload = meeting_dict(session, meeting)
    if job:
        payload["job"] = job_dict(job)
    return payload


@router.post("/api/v1/meetings/{meeting_id}/stop")
def stop_meeting(meeting_id: str, request: Request, session: Session = Depends(get_db), auth=Depends(meeting_auth)):
    meeting = get_owned_meeting(session, auth, meeting_id)
    if meeting.status == "ready":
        return meeting_dict(session, meeting)
    if meeting.audio_path and Path(meeting.audio_path).exists():
        require_moss_ready()
        start_final_transcribe(session, meeting)
    else:
        # 没有落盘音频（例如实时会议中途放弃上传）：用已收片段归档，避免永久停在 processing。
        finalize_live_meeting(session, meeting)
    session.refresh(meeting)
    return meeting_dict(session, meeting)


@router.post("/api/v1/meetings/{meeting_id}/retry-transcribe")
def retry_transcribe(meeting_id: str, request: Request, session: Session = Depends(get_db), auth=Depends(meeting_auth)):
    """重跑终稿转录：供任务失败/中断后由前端重试，不必重录。"""
    from .main import MediaVariant, _has_active_job, job_dict

    meeting = get_owned_meeting(session, auth, meeting_id)
    if _has_active_job(session, meeting.video_asset_id):
        raise HTTPException(409, "终稿转录正在进行中")
    has_audio = bool(meeting.audio_path and Path(meeting.audio_path).exists())
    if not has_audio:
        variant = session.scalars(select(MediaVariant).where(MediaVariant.video_asset_id == meeting.video_asset_id)).first()
        has_audio = bool(
            variant
            and (variant.source_url or (variant.local_path and Path(variant.local_path).exists()))
        )
    if not has_audio:
        raise HTTPException(400, "找不到可转录的录音文件，请重新导入音频")
    require_moss_ready()
    job = start_final_transcribe(session, meeting)
    session.refresh(meeting)
    payload = meeting_dict(session, meeting)
    payload["job"] = job_dict(job)
    return payload


def _purge_meeting_files(local_paths: list[str], folder: Path) -> None:
    """后台清理会议媒体文件与目录：DB 记录已先行删除，失败不阻塞响应。"""
    import logging

    for raw in local_paths:
        try:
            Path(raw).unlink(missing_ok=True)
        except OSError as exc:
            logging.getLogger(__name__).warning("删除会议媒体文件失败 %s: %s", raw, exc)
    shutil.rmtree(folder, ignore_errors=True)


@router.delete("/api/v1/meetings/{meeting_id}")
def delete_meeting(meeting_id: str, request: Request, background_tasks: BackgroundTasks, session: Session = Depends(get_db), auth=Depends(meeting_auth)):
    """删除会议及其绑定的视频资产（转录/纪要/媒体文件同属这场会议）。"""
    from .main import MEETING_DIR, MediaVariant, ProcessingJob, Summary, Transcript, VideoAsset, _has_active_job

    meeting = get_owned_meeting(session, auth, meeting_id)
    if _has_active_job(session, meeting.video_asset_id):
        raise HTTPException(409, "转录任务仍在运行，请完成或取消后再删除")
    video = session.get(VideoAsset, meeting.video_asset_id) if meeting.video_asset_id else None
    pending_paths: list[str] = []
    if video is not None:
        video_id = video.id
        session.query(ProcessingJob).filter(ProcessingJob.video_asset_id == video_id).delete(synchronize_session=False)
        session.query(Transcript).filter(Transcript.video_asset_id == video_id).delete(synchronize_session=False)
        session.query(Summary).filter(Summary.video_asset_id == video_id).delete(synchronize_session=False)
        for variant in session.scalars(select(MediaVariant).where(MediaVariant.video_asset_id == video_id)).all():
            if variant.local_path:
                pending_paths.append(variant.local_path)
            session.delete(variant)
        session.delete(video)
    folder = MEETING_DIR / meeting.id
    # 文件清理放后台：大音频/纪要目录 rmtree 是删除慢的主因，DB 记录先删、立即返回。
    background_tasks.add_task(_purge_meeting_files, pending_paths, folder)
    session.delete(meeting)
    session.commit()
    return {"deleted": meeting_id}


@router.post("/api/v1/meetings/{meeting_id}/summaries")
def summarize_meeting(meeting_id: str, payload: SummaryMeetingRequest | None = None, request: Request = None, session: Session = Depends(get_db), auth=Depends(meeting_auth)):
    from .main import job_dict

    meeting = get_owned_meeting(session, auth, meeting_id)
    body = payload or SummaryMeetingRequest()
    job = start_summary(session, meeting, body.template_id)
    session.refresh(meeting)
    result = meeting_dict(session, meeting)
    result["job"] = job_dict(job)
    return result


@router.get("/api/v1/api-keys")
def list_api_keys(request: Request, session: Session = Depends(get_db), auth=Depends(meeting_auth)):
    from .main import ApiKey

    owner = _principal_user(session, *auth)
    query = select(ApiKey).where(ApiKey.revoked.is_(False))
    if owner is not None and owner.role != "admin":
        query = query.where(ApiKey.user_id == owner.id)
    items = session.scalars(query.order_by(ApiKey.created_at.desc())).all()
    return [
        {
            "id": item.id,
            "name": item.name,
            "prefix": item.prefix,
            "masked": mask_key(item.prefix),
            "created_at": item.created_at.isoformat(),
            "last_used_at": item.last_used_at.isoformat() if item.last_used_at else None,
        }
        for item in items
    ]


@router.post("/api/v1/api-keys")
def create_api_key(payload: CreateApiKeyRequest, request: Request, session: Session = Depends(get_db), auth=Depends(meeting_auth)):
    from .main import ApiKey

    plaintext = f"mk_{secrets.token_urlsafe(24)}"
    owner = _principal_user(session, *auth)
    item = ApiKey(
        name=(payload.name or "").strip() or "会议纪要 API Key",
        prefix=plaintext[:12],
        hashed=hash_api_key(plaintext),
        user_id=owner.id if owner else None,
    )
    session.add(item)
    session.commit()
    session.refresh(item)
    return {
        "id": item.id,
        "name": item.name,
        "prefix": item.prefix,
        "masked": mask_key(item.prefix),
        "key": plaintext,
        "created_at": item.created_at.isoformat(),
        "hint": "请立即复制，明文只展示一次",
    }


@router.delete("/api/v1/api-keys/{key_id}")
def revoke_api_key(key_id: str, request: Request, session: Session = Depends(get_db), auth=Depends(meeting_auth)):
    from .main import ApiKey

    owner = _principal_user(session, *auth)
    item = session.get(ApiKey, key_id)
    if not item or item.revoked:
        raise HTTPException(404, "密钥不存在")
    if owner is not None and owner.role != "admin" and item.user_id != owner.id:
        raise HTTPException(404, "密钥不存在")
    item.revoked = True
    session.commit()
    return {"deleted": key_id}


@router.websocket("/api/v1/meetings/{meeting_id}/live")
async def live_meeting(websocket: WebSocket, meeting_id: str, api_key: str | None = Query(default=None), token: str | None = Query(default=None)):
    from .auth import resolve_principal
    from .main import Meeting, SessionLocal

    await websocket.accept()
    with SessionLocal() as session:
        meeting = session.get(Meeting, meeting_id)
        if not meeting:
            await websocket.send_json({"type": "error", "message": "会议不存在"})
            await websocket.close(code=1008)
            return
        authorization = websocket.headers.get("authorization")
        if not authorization and token:
            authorization = f"Bearer {token}"
        try:
            class WsRequest:
                headers = websocket.headers
                url = websocket.url
            kind, principal = resolve_principal(WsRequest(), session, authorization, websocket.headers.get("x-api-key") or api_key)
            owner = _principal_user(session, kind, principal)
            if owner is not None and owner.role != "admin" and meeting.user_id != owner.id:
                raise HTTPException(404, "会议不存在")
        except HTTPException as exc:
            await websocket.send_json({"type": "error", "message": exc.detail})
            await websocket.close(code=1008)
            return
        if meeting.mode != "live":
            await websocket.send_json({"type": "error", "message": "当前会议不是实时模式"})
            await websocket.close(code=1008)
            return
        meeting.status = "recording"
        meeting.updated_at = _now()
        session.commit()
        language = meeting.language
        # 已入库的 seq 集合：客户端重连后重发未确认块时用于幂等去重。
        processed = {int(item.get("seq") or 0) for item in (meeting.live_segments_json or [])}
    await websocket.send_json({"type": "ready", "meeting_id": meeting_id})
    pending: list[dict[str, Any]] = []
    last_flush = time.time()

    async def flush_live() -> None:
        nonlocal pending, last_flush
        if not pending:
            last_flush = time.time()
            return
        batch, pending = pending, []
        last_flush = time.time()
        error: Exception | None = None
        for attempt in range(3):
            try:
                await asyncio.to_thread(append_live_segments, meeting_id, batch)
                return
            except Exception as exc:
                error = exc
                await asyncio.sleep(0.2 * (attempt + 1))
        # 不确认未落库的片段：恢复 pending 并断开，让客户端用同一 seq 重连重发。
        pending = batch + pending
        raise RuntimeError(f"实时转写片段保存失败：{error}")

    try:
        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                break
            data = message.get("text")
            if not data:
                continue
            try:
                payload = json.loads(data)
            except json.JSONDecodeError:
                await websocket.send_json({"type": "error", "message": "消息不是合法 JSON"})
                continue
            kind = payload.get("type")
            if kind == "ping":
                await websocket.send_json({"type": "pong"})
                await asyncio.to_thread(touch_meeting, meeting_id)
                continue
            if kind == "stop":
                await flush_live()
                await websocket.send_json({"type": "stopped"})
                break
            if kind != "audio":
                continue
            pcm16 = str(payload.get("pcm16") or "")
            if not pcm16:
                continue
            seq = int(payload.get("seq") or 0)
            offset_ms = float(payload.get("offset_ms") or 0)
            sample_rate = int(payload.get("sample_rate") or 16000)
            if seq and seq in processed:
                # 该块此前已处理过（重连重发）：回空确认，让客户端继续推进。
                await websocket.send_json({"type": "segment", "seq": seq, "text": "", "segments": []})
                continue
            try:
                result = await asyncio.to_thread(moss_sync, pcm16, sample_rate, language, offset_ms)
            except MossBusy as exc:
                # 推理锁被长任务占住：让客户端退避重试，而不是卡住整条串行循环。
                await websocket.send_json({"type": "busy", "seq": seq, "retry_after": exc.retry_after})
                continue
            except Exception as exc:
                await websocket.send_json({"type": "error", "seq": seq, "message": str(exc)})
                continue
            segments = [item for item in (result.get("segments") or []) if str(item.get("text") or "").strip()]
            if not segments and str(result.get("text") or "").strip():
                segments = [{
                    "text": result["text"].strip(),
                    "start_ms": offset_ms,
                    "end_ms": offset_ms,
                    "speaker": None,
                    "is_final": True,
                }]
            for item in segments:
                item["seq"] = seq
                item["is_partial"] = False
            if seq:
                processed.add(seq)
            if segments:
                pending.extend(segments)
                # 即时下发字幕；落库做节流，避免每个块都全量重写 JSON 列。
                if len(pending) >= 4 or (time.time() - last_flush) > 1.5:
                    await flush_live()
                await websocket.send_json({
                    "type": "segment",
                    "seq": seq,
                    "text": " ".join(item["text"] for item in segments),
                    "segments": segments,
                })
            else:
                await websocket.send_json({"type": "segment", "seq": seq, "text": "", "segments": []})
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        try:
            await websocket.send_json({"type": "error", "message": str(exc)})
        except Exception:
            pass
    finally:
        await flush_live()
        with SessionLocal() as session:
            meeting = session.get(Meeting, meeting_id)
            if meeting and meeting.status == "recording":
                meeting.status = "processing"
                meeting.updated_at = _now()
                session.commit()


def register_meeting_routes(app) -> None:
    app.include_router(router)
