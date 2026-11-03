"""MCP ASR 中间层：把会议转录/纪要能力以 MCP 工具形式暴露给 goodeep-agent。

设计文档：docs/mcp-asr-integration-design.md（阶段 1，2026-11-02 定稿）
- 音频传递：agent 平台 file_id → MinIO 预签名 GET URL → 工具入参 → 本模块下载落盘 storage/meetings/
- task_id = meeting_id，复用平台 Meeting/ProcessingJob/Summary 全套状态机，不新建任务表
- 认证：/mcp 入口 ASGI 中间件校验 X-API-Key（复用 api_keys 表，与 meetings 面同一套签发/吊销）
- 纪要：提交时带 template_id，转录完成后自动触发 start_summary（agent 轮询 result 一次取全）
"""

from __future__ import annotations

import contextlib
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import httpx
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_http_headers
from sqlalchemy import select
from sqlalchemy.orm import Session

mcp = FastMCP("asr")

# 与 meetings.MEETING_FORMATS 对齐的白名单（设计决策：仅放开这 6 个扩展名）
AGENT_AUDIO_FORMATS = {
    ".mp3": "audio",
    ".wav": "audio",
    ".m4a": "audio",
    ".webm": "audio",
    ".mp4": "video",
    ".mov": "video",
}
AGENT_AUDIO_MAX_BYTES = 500 * 1024 * 1024  # 与会议上传上限一致
_DOWNLOAD_MAX_SECONDS = 600  # 跨机下载预算：500MB 内网带宽通常分钟级
_POLL_INTERVAL = 2.0

# 下载进度（内存态，进程重启丢失可接受：阶段名仍准确）
_download_progress: dict[str, str] = {}
_download_progress_guard = threading.Lock()


def _now():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# /mcp 入口认证（ASGI 层，FastMCP 挂载前包装）
# ---------------------------------------------------------------------------

def _auth_error_response(send) -> None:
    body = b'{"detail":"X-API-Key invalid or missing"}'
    headers = [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]
    send({"type": "http.response.start", "status": 401, "headers": headers})
    send({"type": "http.response.body", "body": body})


def _check_api_key(raw_key: str) -> bool:
    """与 meetings.require_meeting_key 同一套规则：有效 key 放行；全新空库与本地 bypass 放行。"""
    from .main import ApiKey, SessionLocal, User

    with SessionLocal() as session:
        if raw_key:
            from .meetings import hash_api_key

            match = session.scalar(
                select(ApiKey).where(ApiKey.hashed == hash_api_key(raw_key), ApiKey.revoked.is_(False))
            )
            if match is not None:
                match.last_used_at = _now()
                session.commit()
                return True
            return False
        # 过渡期放行：无任何 ApiKey 且无任何用户（全新库）。
        if session.scalar(select(ApiKey.id).limit(1)) is None and session.scalar(select(User.id).limit(1)) is None:
            return True
        # 本地调试放行：显式 AUTH_LOCAL_BYPASS=1（服务器不设即关）。
        return os.getenv("AUTH_LOCAL_BYPASS") == "1"


def _api_key_auth_wrapper(inner):
    async def wrapped(scope, receive, send):
        if scope["type"] == "http":
            headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers") or []}
            raw = (headers.get("x-api-key") or headers.get("authorization") or "").strip()
            if raw.lower().startswith("bearer "):
                raw = raw[7:].strip()
            if not _check_api_key(raw):
                await _auth_error_response(send)
                return
        await inner(scope, receive, send)

    return wrapped


# ---------------------------------------------------------------------------
# 归属：MCP 调用方（ApiKey）→ 平台用户
# ---------------------------------------------------------------------------

def _key_principal(session: Session) -> tuple[Any | None, Any | None]:
    """从工具调用的 HTTP header 反查 (ApiKey, User)；查不到返回 (None, None)。"""
    from .main import ApiKey, User

    headers = get_http_headers() or {}
    raw = (headers.get("x-api-key") or "").strip()
    auth = (headers.get("authorization") or "").strip()
    if not raw and auth.lower().startswith("bearer "):
        raw = auth[7:].strip()
    if not raw:
        return None, None
    from .meetings import hash_api_key

    match = session.scalar(select(ApiKey).where(ApiKey.hashed == hash_api_key(raw), ApiKey.revoked.is_(False)))
    if match is None:
        return None, None
    user = session.get(User, match.user_id) if match.user_id else None
    return match, user


def _key_owner(session: Session):
    """兼容辅助：只取归属用户。"""
    return _key_principal(session)[1]


def _get_agent_meeting(session: Session, task_id: str):
    """取会议并按 ApiKey 归属校验（与 get_owned_meeting 同语义）。"""
    from .main import Meeting

    meeting = session.get(Meeting, task_id)
    user = _key_owner(session)
    if meeting is None or (user is not None and user.role != "admin" and meeting.user_id != user.id):
        raise ToolError("任务不存在")
    return meeting


# ---------------------------------------------------------------------------
# 状态映射：平台状态 → agent 侧状态
# ---------------------------------------------------------------------------

def _agent_status(session: Session, meeting) -> dict[str, Any]:
    from .main import ProcessingJob

    status = (meeting.status or "").lower()
    job = None
    if meeting.video_asset_id:
        job = session.scalars(
            select(ProcessingJob)
            .where(ProcessingJob.video_asset_id == meeting.video_asset_id)
            .order_by(ProcessingJob.created_at.desc())
        ).first()
    if status == "downloading":
        with _download_progress_guard:
            progress = _download_progress.get(meeting.id)
        return {"status": "downloading", "progress": None, "stage": progress or "下载音频中", "error": None}
    if status in {"created", "recorded", "processing"}:
        return {"status": "transcribing", "progress": getattr(job, "progress", None), "stage": getattr(job, "current_stage", None), "error": None}
    if status == "summarizing":
        return {"status": "summarizing", "progress": getattr(job, "progress", None), "stage": getattr(job, "current_stage", None), "error": None}
    if status == "ready":
        return {"status": "completed", "progress": 100, "stage": None, "error": None}
    if status == "failed":
        return {"status": "failed", "progress": None, "stage": None, "error": meeting.error_message or "转录失败"}
    # cancelled / 其他未知态
    return {"status": "cancelled" if status == "cancelled" else status, "progress": None, "stage": None, "error": meeting.error_message}


# ---------------------------------------------------------------------------
# 后台流水线：下载 → 登记 → 终稿转录 → （可选）自动纪要
# ---------------------------------------------------------------------------

def _ext_from_url(audio_url: str, content_type: str) -> str:
    path = unquote(urlparse(audio_url).path)
    ext = Path(path).suffix.lower()
    if ext in AGENT_AUDIO_FORMATS:
        return ext
    mapping = {"audio/mpeg": ".mp3", "audio/wav": ".wav", "audio/x-wav": ".wav", "audio/x-m4a": ".m4a", "audio/mp4": ".m4a", "audio/webm": ".webm", "video/mp4": ".mp4", "video/quicktime": ".mov"}
    return mapping.get((content_type or "").split(";")[0].strip().lower(), "")


def _download_to(session: Session, meeting, audio_url: str) -> tuple[Path, int, float | None]:
    """流式下载到会议音频目录；500MB 上限 + 扩展名白名单。返回最终落盘路径。"""
    from .main import MEETING_DIR, TEMP_DIR, _uploaded_media_info

    dest_ext = ""
    size = 0
    temp_dest = TEMP_DIR / f"agent-{secrets.token_hex(6)}.part"
    try:
        with httpx.stream("GET", audio_url, timeout=httpx.Timeout(30, read=120), follow_redirects=True) as response:
            if response.status_code >= 400:
                raise ToolError(f"音频下载失败：HTTP {response.status_code}")
            content_type = response.headers.get("content-type", "")
            declared = response.headers.get("content-length")
            if declared and int(declared) > AGENT_AUDIO_MAX_BYTES:
                raise ToolError("音频超过 500MB 上限")
            dest_ext = _ext_from_url(audio_url, content_type)
            if not dest_ext:
                raise ToolError("无法识别音频格式：URL 扩展名与 Content-Type 均不在支持列表（mp3/wav/m4a/webm/mp4/mov）")
            with temp_dest.open("wb") as target:
                for chunk in response.iter_bytes(1024 * 1024):
                    size += len(chunk)
                    if size > AGENT_AUDIO_MAX_BYTES:
                        raise ToolError("音频超过 500MB 上限")
                    target.write(chunk)
    except httpx.HTTPError as exc:
        temp_dest.unlink(missing_ok=True)
        raise ToolError(f"音频下载失败：{exc.__class__.__name__}: {exc}") from exc
    except Exception:
        temp_dest.unlink(missing_ok=True)
        raise
    if size == 0:
        temp_dest.unlink(missing_ok=True)
        raise ToolError("音频文件为空")
    folder = MEETING_DIR / meeting.id
    folder.mkdir(parents=True, exist_ok=True)
    dest = folder / f"audio{dest_ext}"
    os.replace(temp_dest, dest)
    with _download_progress_guard:
        _download_progress[meeting.id] = f"下载完成 {size // (1024 * 1024)}MB，探测时长"
    duration = _uploaded_media_info(dest, AGENT_AUDIO_FORMATS[dest_ext])
    return dest, size, duration


def _register_audio(session: Session, meeting, dest: Path, size: int, duration: float | None) -> None:
    """复刻 meetings.upload_meeting_audio 的落库段：video/variant/audio_path。"""
    from .main import MediaVariant
    from .meetings import MEETING_FORMATS, ensure_meeting_video

    media_type = MEETING_FORMATS[dest.suffix.lower()]
    from .meetings import ensure_meeting_video

    video = ensure_meeting_video(session, meeting)
    video.title = meeting.title
    video.duration_seconds = duration
    video.parse_status = "uploaded"
    video.last_processed_at = _now()
    variant = session.scalars(select(MediaVariant).where(MediaVariant.video_asset_id == video.id)).first()
    if variant is None:
        variant = MediaVariant(video_asset_id=video.id, download_status="downloaded")
        session.add(variant)
    variant.format = dest.suffix[1:]
    variant.quality = "会议录音"
    variant.media_type = media_type
    variant.local_path = str(dest)
    variant.file_size = size
    variant.download_status = "downloaded"
    meeting.audio_path = str(dest)
    meeting.updated_at = _now()


def _wait_transcribe(session: Session, meeting, timeout_seconds: float) -> str:
    """轮询最新 transcribe job 直到终态；返回终态字符串。"""
    from .main import ProcessingJob

    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        session.expire_all()
        job = session.scalars(
            select(ProcessingJob)
            .where(ProcessingJob.video_asset_id == meeting.video_asset_id, ProcessingJob.job_type == "transcribe")
            .order_by(ProcessingJob.created_at.desc())
        ).first()
        if job is not None and job.status in {"completed", "failed", "cancelled"}:
            return job.status
        time.sleep(_POLL_INTERVAL)
    return "timeout"


def _run_agent_pipeline(meeting_id: str, audio_url: str, template_id: str | None, business_context: str | None) -> None:
    """后台线程：下载 → 登记 → 终稿转录 → 等完成 → 自动纪要。任何失败落 meeting.error_message。"""
    from .main import Meeting, SessionLocal

    with SessionLocal() as session:
        meeting = session.get(Meeting, meeting_id)
        if meeting is None:
            return
        try:
            dest, size, duration = _download_to(session, meeting, audio_url)
            _register_audio(session, meeting, dest, size, duration)
            meeting.status = "recorded"
            meeting.updated_at = _now()
            session.commit()

            from .meetings import require_moss_ready, start_final_transcribe

            require_moss_ready()
            start_final_transcribe(session, meeting)
            session.commit()

            # 转录时长预算与 main.py 一致：至少 2h，长音频按 15 倍放宽
            budget = max(7200.0, 15.0 * (duration or 0.0))
            final = _wait_transcribe(session, meeting, budget)
            session.expire_all()
            meeting = session.get(Meeting, meeting_id)
            if final != "completed":
                if meeting.status not in {"failed", "cancelled"}:
                    meeting.status = "failed"
                    meeting.error_message = f"转录未完成（{final}）"
                    meeting.updated_at = _now()
                    session.commit()
                return
            # 转录完成 → 自动纪要（start_summary 内部自起线程并把 meeting 置 summarizing→ready）
            if template_id:
                from .meetings import start_summary

                start_summary(session, meeting, template_id, business_context)
        except ToolError as exc:
            meeting.status = "failed"
            meeting.error_message = str(exc)
            meeting.updated_at = _now()
            session.commit()
        except Exception as exc:  # 兜底：不让流水线异常静默丢状态
            meeting.status = "failed"
            meeting.error_message = f"agent 转录流水线异常：{exc.__class__.__name__}: {exc}"
            meeting.updated_at = _now()
            session.commit()
        finally:
            with _download_progress_guard:
                _download_progress.pop(meeting_id, None)


# ---------------------------------------------------------------------------
# MCP 工具（3 个，契约见设计文档 §3）
# ---------------------------------------------------------------------------

@mcp.tool
def transcribe(
    audio_url: str,
    title: str | None = None,
    language: str = "zh",
    template_id: str | None = None,
    business_context: str | None = None,
    hotwords: str | None = None,
) -> dict[str, Any]:
    """提交会议音频转写任务（异步）。audio_url 为可内网访问的预签名 GET URL；返回 task_id 后用 asr_status 轮询、asr_result 取结果。提供 template_id 时转录完成后自动生成纪要。"""
    from .main import Meeting, SessionLocal, SummaryTemplate
    from .meetings import ensure_meeting_video, normalize_hotwords

    parsed = urlparse(audio_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ToolError("audio_url 必须是 http(s) 预签名 GET URL")
    if language not in {"zh", "auto", "en"}:
        raise ToolError("language 仅支持 zh / auto / en")

    with SessionLocal() as session:
        if template_id is not None and session.get(SummaryTemplate, template_id) is None:
            raise ToolError(f"纪要模板不存在：{template_id}")
        api_key, owner = _key_principal(session)
        stamp = _now().astimezone().strftime("会议 %Y-%m-%d %H:%M")
        meeting = Meeting(
            title=(title or "").strip() or stamp,
            mode="recording",
            language=language,
            audio_source="mic",
            status="downloading",
            live_segments_json=[],
            hotwords=normalize_hotwords(hotwords),
            user_id=owner.id if owner else None,
            # agent 来源标记：api_key_id 非空即 agent 发起（预留字段复用，零迁移）
            api_key_id=api_key.id if api_key else None,
        )
        session.add(meeting)
        session.flush()
        ensure_meeting_video(session, meeting)
        session.refresh(meeting)
        task_id = meeting.id

    threading.Thread(
        target=_run_agent_pipeline,
        args=(task_id, audio_url, template_id, business_context),
        daemon=True,
        name=f"agent-asr-{task_id[:8]}",
    ).start()
    return {"task_id": task_id, "status": "downloading"}


@mcp.tool
def status(task_id: str) -> dict[str, Any]:
    """查询转写任务进度。status: downloading/transcribing/summarizing/completed/failed/cancelled。"""
    from .main import SessionLocal

    with SessionLocal() as session:
        meeting = _get_agent_meeting(session, task_id)
        payload = _agent_status(session, meeting)
    payload["task_id"] = task_id
    return payload


@mcp.tool
def result(task_id: str, include_segments: bool = True) -> dict[str, Any]:
    """取转写结果（须任务完成）。返回转写全文；include_segments 时带分段；提供过模板时带纪要 markdown。"""
    from .main import SessionLocal, Summary, _latest_transcript, summary_dict

    with SessionLocal() as session:
        meeting = _get_agent_meeting(session, task_id)
        state = _agent_status(session, meeting)
        if state["status"] not in {"completed", "failed", "cancelled"}:
            return {"task_id": task_id, "status": state["status"], "progress": state["progress"], "error": None}
        transcript = _latest_transcript(session, meeting.video_asset_id) if meeting.video_asset_id else None
        summary = None
        if meeting.video_asset_id:
            summary = session.scalars(
                select(Summary).where(Summary.video_asset_id == meeting.video_asset_id).order_by(Summary.created_at.desc())
            ).first()
        payload: dict[str, Any] = {
            "task_id": task_id,
            "status": state["status"],
            "error": state["error"],
            "transcript": transcript.text if transcript else None,
            "segments": transcript.segments_json if (transcript and include_segments) else None,
            "summary": summary_dict(summary)["content_markdown"] if summary else None,
        }
        return payload


# ---------------------------------------------------------------------------
# 挂载：/mcp → streamable-http app（带 API Key 中间件）
# ---------------------------------------------------------------------------

mcp_app = mcp.http_app(transport="streamable-http", path="/")
mcp_asgi = _api_key_auth_wrapper(mcp_app)


@contextlib.asynccontextmanager
async def mcp_lifespan(app):  # noqa: ANN001
    """合并 FastMCP session manager 的 lifespan（mount 子应用的生命周期不会自动执行）。"""
    async with contextlib.AsyncExitStack() as stack:
        await stack.enter_async_context(mcp_app.lifespan(mcp_app))
        yield


def mount_asr_mcp(parent_app) -> None:
    """在主 FastAPI 上挂载 /mcp 端点（main.py 启动时调用一次）。"""
    parent_app.mount("/mcp", mcp_asgi)
