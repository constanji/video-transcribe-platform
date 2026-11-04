"""Agent 平台转写 REST：按 audio_url 提交（替代 multipart 重传），供 32 platform-service
在文件 commit 时异步调用，实现「上传即转写」。

与 /mcp 面的关系：流水线/归属/状态机完全复用 mcp_asr（_submit_transcription /
_agent_status / _agent_result_payload），这里只是补一个 REST 入口——MCP 工具
transcribe/status/result 保留不动（旧指令路径 + 超长全文 fallback）。

差异点（有意设计）：
- 认证走 meeting_auth（JWT 或 X-API-Key，与 meetings 面同套）；32 Java 持 ApiKey。
- 热词：请求未显式给 hotwords 时，按 owner/session_key 从 business_contexts 解析
  （会话级优先，回退用户级），语义对齐 MCP get_context——模型不再参与转写提交，
  热词必须由服务端自己带上。
- owner 透传限制与 REST context 语义一致：仅 ApiKey / admin 可指定他人归属域。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .meetings import (
    CTX_ANON_SCOPE,
    CTX_KEY_MAX,
    CTX_USER_SCOPE,
    _find_context,
    _principal_user,
    _scope_token,
    get_db,
    meeting_auth,
    normalize_hotwords,
)

router = APIRouter()


class AgentTranscriptionRequest(BaseModel):
    audio_url: str
    title: str | None = None
    language: str = "zh"
    hotwords: str | None = Field(default=None, max_length=500)
    # business_context 仅在自动纪要（template_id）路径生效；本 REST 不收 template_id，
    # 纪要由平台模型生成，故不透传（与 2026-11-04 拍板一致）。
    owner: str | None = Field(default=None, max_length=CTX_KEY_MAX)
    session_key: str | None = Field(default=None, max_length=CTX_KEY_MAX)


def _resolve_user_scope(session: Session, kind: str, principal: Any, owner: str | None) -> str:
    """归属域解析（对齐 meetings._context_scope_user：透传归属仅限服务端凭证）。"""
    if owner:
        if kind == "api_key" or (kind == "user" and getattr(principal, "role", "") == "admin"):
            return _scope_token(owner, CTX_ANON_SCOPE)
        raise HTTPException(403, "无权指定 owner（归属域透传仅限服务端凭证）")
    user = _principal_user(session, kind, principal)
    return user.id if user else CTX_ANON_SCOPE


def _resolve_hotwords(session: Session, kind: str, principal: Any, payload: AgentTranscriptionRequest) -> str | None:
    if payload.hotwords is not None:
        return normalize_hotwords(payload.hotwords)
    user_scope = _resolve_user_scope(session, kind, principal, payload.owner)
    key = _scope_token(payload.session_key, "")[:CTX_KEY_MAX]
    row = _find_context(session, user_scope, key) if key else None
    if row is None:
        row = _find_context(session, user_scope, CTX_USER_SCOPE)
    return row.hotwords if row else None


def _meeting_for_auth(session: Session, kind: str, principal: Any, task_id: str):
    """按认证主体取会议并校验归属（admin 全见 / 归属用户自见 / anon 过渡期全见）。"""
    from .main import Meeting

    meeting = session.get(Meeting, task_id)
    user = _principal_user(session, kind, principal)
    if meeting is None or (user is not None and user.role != "admin" and meeting.user_id != user.id):
        raise HTTPException(404, "任务不存在")
    return meeting


@router.post("/api/v1/agent/transcriptions")
def submit_agent_transcription(
    payload: AgentTranscriptionRequest,
    request: Request,
    session: Session = Depends(get_db),
    auth=Depends(meeting_auth),
):
    """按预签名 audio_url 提交转写（异步）。返回 task_id 后轮询 GET .../{task_id}。"""
    from .mcp_asr import _submit_transcription

    kind, principal = auth
    api_key = principal if kind == "api_key" else None
    owner_user = _principal_user(session, kind, principal)
    hotwords = _resolve_hotwords(session, kind, principal, payload)
    try:
        task_id = _submit_transcription(
            session,
            audio_url=payload.audio_url,
            title=payload.title,
            language=payload.language,
            template_id=None,
            business_context=None,
            hotwords=hotwords,
            api_key=api_key,
            owner=owner_user,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"task_id": task_id, "status": "downloading"}


@router.get("/api/v1/agent/transcriptions/{task_id}")
def agent_transcription_status(
    task_id: str,
    request: Request,
    session: Session = Depends(get_db),
    auth=Depends(meeting_auth),
):
    """任务进度：status/progress/stage/error + duration_seconds（下载登记后可得）。"""
    from .main import VideoAsset
    from .mcp_asr import _agent_status

    kind, principal = auth
    meeting = _meeting_for_auth(session, kind, principal, task_id)
    payload = _agent_status(session, meeting)
    payload["task_id"] = task_id
    duration = None
    if meeting.video_asset_id:
        video = session.get(VideoAsset, meeting.video_asset_id)
        duration = getattr(video, "duration_seconds", None)
    payload["duration_seconds"] = duration
    return payload


@router.get("/api/v1/agent/transcriptions/{task_id}/result")
def agent_transcription_result(
    task_id: str,
    request: Request,
    include_segments: bool = True,
    session: Session = Depends(get_db),
    auth=Depends(meeting_auth),
):
    """转写结果（须终态）：transcript 全文 + segments；未完成时返回进度。"""
    from .mcp_asr import _agent_result_payload

    kind, principal = auth
    meeting = _meeting_for_auth(session, kind, principal, task_id)
    return _agent_result_payload(session, meeting, include_segments)


def register_agent_transcription_routes(app) -> None:
    app.include_router(router)
