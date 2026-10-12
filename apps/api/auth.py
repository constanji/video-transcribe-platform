r"""用户认证：注册/登录/双 JWT 会话/权限依赖。

设计参考（D:\Goodeep 三个项目）：
- LibreChat-main / Because：双 JWT（短 access + 长 refresh）、Session 表存 refresh
  哈希可撤销、bcrypt、JWT payload 最小化且每次请求查库、防枚举通用响应。
- goodeep-agent：refresh 一次性轮换（旧 token 重用即失效）、越权 404 不暴露
  存在性、内存限流。

令牌策略为纯 Bearer（web 与 api 跨端口且内网 http 部署，SameSite=None 的
cookie 会被浏览器拒绝，故选 header 承载，零部署拓扑变更）：
- access 30 分钟（payload: sub/username/role/sid/type）
- refresh 14 天（payload: sub/sid/type），PG 的 user_sessions 表存 SHA-256
  哈希；刷新即轮换（旧行标记 revoked，重用视为重放 → 撤销该用户全部会话）。
- 改密/禁用/重置密码 → 删除该用户全部会话（全端踢下线）。
"""

from __future__ import annotations

import hashlib
import os
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import bcrypt
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from jose import JWTError, jwt
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

ACCESS_TOKEN_EXPIRE_MINUTES = 30
REFRESH_TOKEN_EXPIRE_DAYS = 14

# ------------------------------------------------------------------ 密钥


def _jwt_secret(kind: str) -> str:
    """每个令牌类型一个 secret。未设置时从 API_SECRET 派生（升级不漂移）；
    显式设置弱值则拒绝启动。"""
    raw = os.environ.get(f"JWT_{kind.upper()}_SECRET")
    if raw and raw in {"change-me", "local-dev-token", "secret", "changeme"}:
        raise RuntimeError(f"JWT_{kind.upper()}_SECRET 是弱值，请换成足够长的随机串")
    if raw:
        return raw
    base = os.getenv("API_SECRET", "local-dev-token")
    return hashlib.sha256(f"{base}:{kind}".encode("utf-8")).hexdigest()


ACCESS_SECRET = _jwt_secret("access")
REFRESH_SECRET = _jwt_secret("refresh")

# ------------------------------------------------------------------ 密码

# bcrypt 5 对超过 72 字节的密码直接报错，统一截断（与 bcrypt 算法本身的限制一致）。
_BCRYPT_MAX_BYTES = 72


def _pw_bytes(plain: str) -> bytes:
    return plain.encode("utf-8")[:_BCRYPT_MAX_BYTES]


def hash_password(plain: str) -> str:
    return bcrypt.hashpw(_pw_bytes(plain), bcrypt.gensalt(rounds=12)).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(_pw_bytes(plain), hashed.encode("utf-8"))
    except ValueError:
        return False


# 用户不存在时也对它跑一次校验，让登录耗时与真实用户对齐（防用户名枚举）。
DUMMY_HASH = hash_password(uuid.uuid4().hex)


# ------------------------------------------------------------------ JWT


def _utcnow() -> datetime:
    """数据库 DateTime 列不带时区（存 UTC），必须用 naive UTC 比较。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def create_access_token(user: Any, session_id: str | None = None) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user.id,
        "username": user.username,
        "role": user.role,
        "sid": session_id,
        "type": "access",
        "iat": now,
        "exp": now + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES),
    }
    return jwt.encode(payload, ACCESS_SECRET, algorithm="HS256")


def create_refresh_token(user_id: str, session_id: str) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user_id,
        "sid": session_id,
        "type": "refresh",
        "iat": now,
        "exp": now + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS),
    }
    return jwt.encode(payload, REFRESH_SECRET, algorithm="HS256")


def decode_token(token: str, expected_type: str) -> dict[str, Any]:
    secret = ACCESS_SECRET if expected_type == "access" else REFRESH_SECRET
    try:
        payload = jwt.decode(token, secret, algorithms=["HS256"])
    except JWTError:
        raise HTTPException(401, "登录状态已失效，请重新登录")
    if payload.get("type") != expected_type:
        raise HTTPException(401, "令牌类型不符")
    return payload


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


# ------------------------------------------------------------------ 依赖


def get_db():
    from .main import SessionLocal

    with SessionLocal() as session:
        yield session


def get_current_user(
    authorization: str | None = Header(default=None),
    session: Session = Depends(get_db),
) -> Any:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "未登录")
    payload = decode_token(authorization[7:].strip(), "access")
    from .main import User

    user = session.get(User, str(payload.get("sub") or ""))
    if user is None or user.status != "active":
        raise HTTPException(401, "账号不存在或已禁用")
    return user


def get_current_user_flex(
    authorization: str | None = Header(default=None),
    token: str | None = Query(default=None),
    session: Session = Depends(get_db),
) -> Any:
    """<img>/SSE 等无法携带 Authorization header 的通道，允许 ?token= 传 JWT。"""
    if not authorization and token:
        authorization = f"Bearer {token}"
    return get_current_user(authorization, session)


def require_admin(user: Any = Depends(get_current_user)) -> Any:
    if user.role != "admin":
        raise HTTPException(403, "需要管理员权限")
    return user


def resolve_principal(
    request: Request,
    session: Session = Depends(get_db),
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> tuple[str, Any]:
    """meetings 侧双通道：先 JWT，再 ApiKey（含原有放行逻辑）。

    返回 (kind, principal)：('user', User) / ('api_key', ApiKey) / ('anon', None)。
    """
    if authorization and authorization.lower().startswith("bearer "):
        raw = authorization[7:].strip()
        try:
            payload = decode_token(raw, "access")
        except HTTPException:
            payload = None
        if payload is not None:
            from .main import User

            user = session.get(User, str(payload.get("sub") or ""))
            if user is not None and user.status == "active":
                return ("user", user)
    from .meetings import extract_api_key, require_meeting_key

    key = require_meeting_key(request, session, extract_api_key(authorization, x_api_key))
    if key is not None:
        return ("api_key", key)
    return ("anon", None)


# ------------------------------------------------------------------ 限流


class MemoryRateLimiter:
    """进程内滑动窗口限流（单机工具够用；重启清零）。"""

    def __init__(self, max_hits: int, window_seconds: int) -> None:
        self.max_hits = max_hits
        self.window = window_seconds
        self._hits: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            hits = [t for t in self._hits.get(key, []) if now - t < self.window]
            if len(hits) >= self.max_hits:
                self._hits[key] = hits
                return False
            hits.append(now)
            self._hits[key] = hits
            return True


login_ip_limiter = MemoryRateLimiter(10, 60)       # 每 IP 10 次/分钟
login_user_limiter = MemoryRateLimiter(5, 300)     # 每用户名 5 次/5 分钟
register_limiter = MemoryRateLimiter(5, 3600)      # 每 IP 5 次/小时


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


# ------------------------------------------------------------------ 模型


class RegisterRequest(BaseModel):
    username: str = Field(min_length=3, max_length=80, pattern=r"^[a-zA-Z0-9_.-]+$")
    password: str = Field(min_length=8, max_length=128)
    display_name: str | None = Field(default=None, max_length=120)


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=128)


class RefreshRequest(BaseModel):
    refresh_token: str


class ChangePasswordRequest(BaseModel):
    old_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=8, max_length=128)


class AdminUpdateUserRequest(BaseModel):
    role: str | None = Field(default=None, pattern="^(admin|user)$")
    status: str | None = Field(default=None, pattern="^(active|disabled)$")


class AdminResetPasswordRequest(BaseModel):
    new_password: str = Field(min_length=8, max_length=128)


def user_dict(user: Any) -> dict[str, Any]:
    return {
        "id": user.id,
        "username": user.username,
        "display_name": user.display_name,
        "role": user.role,
        "status": user.status,
        "last_login_at": user.last_login_at.isoformat() if user.last_login_at else None,
        "created_at": user.created_at.isoformat() if user.created_at else None,
    }


def issue_tokens(session: Session, user: Any, request: Request | None = None) -> dict[str, Any]:
    """建会话行并签发双令牌。"""
    from .main import UserSession

    row = UserSession(
        user_id=user.id,
        refresh_token_hash=_sha256(uuid.uuid4().hex),  # 占位，flush 后用真值覆盖
        expires_at=_utcnow() + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS),
        user_agent=(request.headers.get("user-agent", "")[:255] if request else None),
        ip=(_client_ip(request) if request else None),
    )
    session.add(row)
    session.flush()
    refresh = create_refresh_token(user.id, row.id)
    row.refresh_token_hash = _sha256(refresh)
    session.commit()
    return {
        "access_token": create_access_token(user, row.id),
        "refresh_token": refresh,
        "user": user_dict(user),
    }


# ------------------------------------------------------------------ 路由

router = APIRouter()


@router.get("/api/auth/public-config")
def public_config(session: Session = Depends(get_db)):
    from .main import User

    has_users = session.scalar(select(func.count()).select_from(User)) > 0
    mode = os.getenv("REGISTRATION_MODE", "open").strip() or "open"
    return {"first_user": not has_users, "registration_open": (not has_users) or mode == "open"}


@router.post("/api/auth/register")
def register(payload: RegisterRequest, request: Request, session: Session = Depends(get_db)):
    from .main import User, _backfill_owner

    if not register_limiter.allow(_client_ip(request)):
        raise HTTPException(429, "注册过于频繁，请稍后再试")
    if session.scalar(select(User).where(User.username == payload.username).limit(1)) is not None:
        raise HTTPException(409, "用户名已被注册")
    has_users = session.scalar(select(func.count()).select_from(User)) > 0
    if has_users and os.getenv("REGISTRATION_MODE", "open") == "admin_only":
        raise HTTPException(403, "注册已关闭，请联系管理员创建账号")
    # 首位注册者成为管理员，历史数据全部回填给他。
    user = User(
        username=payload.username,
        password_hash=hash_password(payload.password),
        display_name=(payload.display_name or "").strip() or None,
        role="user" if has_users else "admin",
    )
    session.add(user)
    session.flush()
    if not has_users:
        _backfill_owner(session, user.id)
    session.commit()
    return issue_tokens(session, user, request)


@router.post("/api/auth/login")
def login(payload: LoginRequest, request: Request, session: Session = Depends(get_db)):
    from .main import User

    if not login_ip_limiter.allow(_client_ip(request)) or not login_user_limiter.allow(payload.username):
        raise HTTPException(429, "尝试过于频繁，请稍后再试")
    user = session.scalar(select(User).where(User.username == payload.username).limit(1))
    if user is None:
        verify_password(payload.password, DUMMY_HASH)  # 耗时对齐，防枚举
        raise HTTPException(401, "用户名或密码错误")
    if not verify_password(payload.password, user.password_hash):
        raise HTTPException(401, "用户名或密码错误")
    if user.status != "active":
        raise HTTPException(401, "用户名或密码错误")
    user.last_login_at = _utcnow()
    session.commit()
    return issue_tokens(session, user, request)


@router.post("/api/auth/refresh")
def refresh(payload: RefreshRequest, session: Session = Depends(get_db)):
    from .main import User, UserSession

    decode_token(payload.refresh_token, "refresh")
    row = session.scalar(
        select(UserSession).where(UserSession.refresh_token_hash == _sha256(payload.refresh_token)).limit(1)
    )
    now = _utcnow()
    if row is None or row.expires_at < now:
        raise HTTPException(401, "刷新令牌无效")
    if row.revoked:
        # 已轮换过的 refresh 再次使用 = 重放：撤销该用户全部会话。
        session.execute(delete(UserSession).where(UserSession.user_id == row.user_id))
        session.commit()
        raise HTTPException(401, "刷新令牌已失效，请重新登录")
    user = session.get(User, row.user_id)
    if user is None or user.status != "active":
        raise HTTPException(401, "账号不可用")
    row.revoked = True  # 轮换：旧行作废（保留到 expires_at 用于重放检测）
    row.last_used_at = now
    session.commit()
    return issue_tokens(session, user)


@router.post("/api/auth/logout")
def logout(
    authorization: str | None = Header(default=None),
    session: Session = Depends(get_db),
):
    from .main import UserSession

    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "未登录")
    payload = decode_token(authorization[7:].strip(), "access")
    sid = payload.get("sid")
    if sid:
        session.execute(delete(UserSession).where(UserSession.id == sid, UserSession.user_id == payload["sub"]))
    else:
        session.execute(delete(UserSession).where(UserSession.user_id == payload["sub"]))
    session.commit()
    return {"ok": True}


@router.get("/api/auth/me")
def me(user: Any = Depends(get_current_user)):
    return user_dict(user)


@router.put("/api/auth/me/password")
def change_password(
    payload: ChangePasswordRequest,
    user: Any = Depends(get_current_user),
    session: Session = Depends(get_db),
):
    from .main import UserSession

    if not verify_password(payload.old_password, user.password_hash):
        raise HTTPException(400, "原密码错误")
    user.password_hash = hash_password(payload.new_password)
    # 改密后全端下线，避免旧会话继续有效。
    session.execute(delete(UserSession).where(UserSession.user_id == user.id))
    session.commit()
    return {"ok": True}


@router.get("/api/admin/users")
def list_users(admin: Any = Depends(require_admin), session: Session = Depends(get_db)):
    from .main import User

    rows = session.scalars(select(User).order_by(User.created_at)).all()
    return [user_dict(item) for item in rows]


def _get_user_or_404(session: Session, user_id: str) -> Any:
    from .main import User

    user = session.get(User, user_id)
    if user is None:
        raise HTTPException(404, "用户不存在")
    return user


def _kick_sessions(session: Session, user_id: str) -> None:
    from .main import UserSession

    session.execute(delete(UserSession).where(UserSession.user_id == user_id))


def _admin_count(session: Session) -> int:
    from .main import User

    return session.scalar(
        select(func.count()).select_from(User).where(User.role == "admin", User.status == "active")
    )


@router.put("/api/admin/users/{user_id}")
def update_user(
    user_id: str,
    payload: AdminUpdateUserRequest,
    admin: Any = Depends(require_admin),
    session: Session = Depends(get_db),
):
    user = _get_user_or_404(session, user_id)
    demoting = payload.role == "user" or payload.status == "disabled"
    if user.id == admin.id and demoting:
        raise HTTPException(400, "不能禁用或降级自己")
    if demoting and user.role == "admin" and _admin_count(session) <= 1:
        raise HTTPException(400, "系统至少需要一名管理员")
    if payload.role:
        user.role = payload.role
    if payload.status:
        user.status = payload.status
    if payload.status == "disabled":
        _kick_sessions(session, user.id)
    session.commit()
    return user_dict(user)


@router.post("/api/admin/users/{user_id}/reset-password")
def admin_reset_password(
    user_id: str,
    payload: AdminResetPasswordRequest,
    admin: Any = Depends(require_admin),
    session: Session = Depends(get_db),
):
    user = _get_user_or_404(session, user_id)
    user.password_hash = hash_password(payload.new_password)
    _kick_sessions(session, user.id)
    session.commit()
    return {"ok": True}


@router.delete("/api/admin/users/{user_id}")
def delete_user(user_id: str, admin: Any = Depends(require_admin), session: Session = Depends(get_db)):
    from .main import Meeting, UserSession, VideoAsset

    user = _get_user_or_404(session, user_id)
    if user.id == admin.id:
        raise HTTPException(400, "不能删除自己")
    if user.role == "admin" and _admin_count(session) <= 1:
        raise HTTPException(400, "系统至少需要一名管理员")
    # 有数据的用户先清理数据再删号，避免误删视频/会议文件。
    if session.scalar(select(VideoAsset).where(VideoAsset.user_id == user_id).limit(1)) is not None:
        raise HTTPException(400, "该用户名下还有视频资产，请先删除后再删号")
    if session.scalar(select(Meeting).where(Meeting.user_id == user_id).limit(1)) is not None:
        raise HTTPException(400, "该用户名下还有会议记录，请先删除后再删号")
    _kick_sessions(session, user.id)
    session.delete(user)
    session.commit()
    return {"deleted": user_id}


def register_auth_routes(app: Any) -> None:
    app.include_router(router)
