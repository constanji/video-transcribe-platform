# 冒烟测试：业务背景落库 REST + MCP 工具（临时 DB，跑完即删）
# 覆盖：CRUD、merged 合并、哨兵去重（唯一约束）、热词保护（None 保留/空串清空）、
#       X-Owner-Id 归属透传与隔离、MCP owner 参数、长度护栏。
import os
import sys
import tempfile
from pathlib import Path

tmp_db = Path(tempfile.mkdtemp()) / "smoke-ctx.db"
os.environ["DATABASE_URL"] = f"sqlite:///{tmp_db.as_posix()}"
os.environ["STORAGE_ROOT"] = str(tmp_db.parent / "storage")
os.environ["AUTH_LOCAL_BYPASS"] = ""  # 走全新空库过渡期放行

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi.testclient import TestClient  # noqa: E402

from apps.api.main import SessionLocal, app  # noqa: E402

client = TestClient(app)


def context_rows() -> int:
    from sqlalchemy import func, select

    from apps.api.main import BusinessContext

    with SessionLocal() as session:
        return session.scalar(select(func.count()).select_from(BusinessContext))


# ---- Phase A：匿名（全新空库过渡期放行）----
r = client.get("/api/v1/recording-context")
assert r.status_code == 200, r.text
assert r.json() == {"user": None, "session": None, "merged": None}, r.json()

r = client.put("/api/v1/recording-context", json={"content": "用户级背景", "hotwords": "领元、全哥"})
assert r.status_code == 200, r.text
assert r.json()["content"] == "用户级背景"

r = client.put(
    "/api/v1/recording-context",
    json={"content": "会话级背景", "session_key": "sess-1", "scope": "session"},
)
assert r.status_code == 200, r.text

r = client.get("/api/v1/recording-context", params={"session_key": "sess-1"})
body = r.json()
assert body["user"]["content"] == "用户级背景", body
assert body["session"]["content"] == "会话级背景", body
assert body["merged"]["content"] == "会话级背景" and body["merged"]["scope"] == "session", body

r = client.get("/api/v1/recording-context", params={"session_key": "sess-2"})
assert r.json()["merged"]["content"] == "用户级背景" and r.json()["merged"]["scope"] == "user", r.json()

r = client.put("/api/v1/recording-context", json={"content": "x", "scope": "session"})
assert r.status_code == 400, r.text  # scope=session 缺 session_key

# 热词保护：只改 content（hotwords 缺省）→ 保留；hotwords="" → 清空
r = client.put("/api/v1/recording-context", json={"content": "用户级背景 v2"})
assert r.json()["hotwords"] == "领元,全哥", r.json()
r = client.put("/api/v1/recording-context", json={"content": "用户级背景 v3", "hotwords": ""})
assert r.json()["hotwords"] is None, r.json()

# 哨兵去重：同键多次 PUT 仍只有一行
r = client.put("/api/v1/recording-context", json={"content": "用户级背景 v4"})
assert r.status_code == 200
assert context_rows() == 2, context_rows()  # 匿名用户级 1 行 + sess-1 会话级 1 行

r = client.delete("/api/v1/recording-context", params={"session_key": "sess-1"})
assert r.json() == {"deleted": True}, r.text
assert context_rows() == 1, context_rows()
r = client.get("/api/v1/recording-context", params={"session_key": "sess-1"})
assert r.json()["merged"]["content"] == "用户级背景 v4", r.json()
print("Phase A (匿名 CRUD/去重/热词保护): ok")

# ---- Phase B：ApiKey 服务端凭证 + X-Owner-Id 透传 ----
r = client.post("/api/v1/api-keys", json={"name": "java-proxy"})
assert r.status_code == 200, r.text
raw_key = r.json()["key"]
key_headers = {"X-API-Key": raw_key}

# 有 ApiKey 后匿名通道关闭
r = client.get("/api/v1/recording-context")
assert r.status_code == 401, (r.status_code, r.text)

owner_headers = {**key_headers, "X-Owner-Id": "platform-user-A"}
r = client.put(
    "/api/v1/recording-context",
    headers=owner_headers,
    json={"content": "A 的长期背景", "hotwords": "Bid Agent"},
)
assert r.status_code == 200, r.text
r = client.put(
    "/api/v1/recording-context",
    headers=owner_headers,
    json={"content": "A 的会话背景", "session_key": "sess-A1", "scope": "session"},
)
assert r.status_code == 200, r.text

# 隔离：带 owner 读到 A 的；不带 owner（回退 anon 域）读不到 A 的数据
r = client.get("/api/v1/recording-context", headers=owner_headers, params={"session_key": "sess-A1"})
body = r.json()
assert body["merged"]["content"] == "A 的会话背景" and body["merged"]["scope"] == "session", body
r = client.get("/api/v1/recording-context", headers=key_headers)
assert r.json()["merged"]["content"] == "用户级背景 v4", r.json()  # anon 域数据，非 A 的
assert context_rows() == 3, context_rows()  # anon 用户级 + A 用户级 + A 会话级
print("Phase B (X-Owner-Id 透传/隔离): ok")

# ---- Phase C：MCP 工具 ----
import asyncio  # noqa: E402

from fastmcp import Client  # noqa: E402

from apps.api.mcp_asr import mcp  # noqa: E402


async def mcp_checks() -> None:
    async with Client(mcp) as mcp_client:
        tools = {t.name for t in await mcp_client.list_tools()}
        assert {"transcribe", "status", "result", "get_context", "save_context"} <= tools, tools

        # owner + session 双写
        res = await mcp_client.call_tool(
            "save_context",
            {"content": "模型修正版 A", "hotwords": "领元Agent", "session_key": "sess-A1", "owner": "platform-user-A"},
        )
        assert res.data == {"saved": True, "scopes": ["session", "user"]}, res.data

        res = await mcp_client.call_tool("get_context", {"session_key": "sess-A1", "owner": "platform-user-A"})
        assert res.data["content"] == "模型修正版 A" and res.data["scope"] == "session", res.data

        # 回退用户级（模型双写已沉淀）
        res = await mcp_client.call_tool("get_context", {"session_key": "other", "owner": "platform-user-A"})
        assert res.data["content"] == "模型修正版 A" and res.data["scope"] == "user", res.data

        # owner 隔离：B 域读不到 A 的
        res = await mcp_client.call_tool("get_context", {"owner": "platform-user-B"})
        assert res.data["content"] is None, res.data

        # 热词保护：二次保存不传 hotwords → 保留
        await mcp_client.call_tool(
            "save_context", {"content": "模型修正版 A2", "session_key": "sess-A1", "owner": "platform-user-A"}
        )
        res = await mcp_client.call_tool("get_context", {"session_key": "sess-A1", "owner": "platform-user-A"})
        assert res.data["hotwords"] == "领元Agent", res.data

        # 长度护栏
        try:
            await mcp_client.call_tool("save_context", {"content": "x" * 20001, "owner": "platform-user-A"})
            raise AssertionError("超长 content 应报 ToolError")
        except Exception:
            pass
        try:
            await mcp_client.call_tool("save_context", {"content": "  ", "owner": "platform-user-A"})
            raise AssertionError("空 content 应报 ToolError")
        except Exception:
            pass


asyncio.run(mcp_checks())
print("Phase C (MCP owner/双写/热词保护/护栏): ok")
print("ALL PASS")
