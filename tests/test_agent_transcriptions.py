"""agent_transcriptions REST 契约测试（B1：上传期转写）。

覆盖 32 platform-service 将调用的三个端点：
- POST /api/v1/agent/transcriptions（按 audio_url 提交，X-API-Key 认证）
- GET  /api/v1/agent/transcriptions/{task_id}（进度透传 + duration_seconds）
- GET  /api/v1/agent/transcriptions/{task_id}/result（结果组装）

真实 app + 临时 SQLite；后台流水线 monkeypatch 为 no-op（下载/转录不在本测试范围，
由 E2E 覆盖）。运行方式：仓库根目录下 `.venv/Scripts/python.exe -m pytest tests/test_agent_transcriptions.py -q`
（或 unittest）。
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# 环境变量必须在 import apps.api.main 之前设置（main.py 在 import 期建 engine/建表）
_TMP = tempfile.mkdtemp(prefix="agent-transcribe-test-")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP}/test.db"
os.environ["STORAGE_ROOT"] = str(Path(_TMP) / "storage")
os.environ.pop("AUTH_LOCAL_BYPASS", None)  # 走真实 X-API-Key 认证路径（32 Java 同款）

from fastapi.testclient import TestClient  # noqa: E402

from apps.api import main as api_main  # noqa: E402
from apps.api import mcp_asr  # noqa: E402
from apps.api.meetings import hash_api_key  # noqa: E402

API_KEY_RAW = "test-agent-key-0123456789abcdef"
OWNER = "plat-user-e2e"
SESSION_KEY = "sess-e2e-1"


def _seed():
    from apps.api.main import ApiKey, BusinessContext, SessionLocal

    with SessionLocal() as session:
        key = ApiKey(name="contract-test", prefix=API_KEY_RAW[:8], hashed=hash_api_key(API_KEY_RAW), revoked=False)
        session.add(key)
        # 会话级快照（带热词）：模拟前端此前经 Java 代理保存的业务背景
        session.add(
            BusinessContext(
                user_id=OWNER,
                session_key=SESSION_KEY,
                content="测试业务背景",
                hotwords="歌尔、领元",
                source="user",
                updated_at=datetime.now(timezone.utc),
            )
        )
        # 用户级长期默认（不同 session_key，验证会话级优先）
        session.add(
            BusinessContext(
                user_id=OWNER,
                session_key="__user__",
                content="长期背景",
                hotwords="长期热词",
                source="user",
                updated_at=datetime.now(timezone.utc),
            )
        )
        session.commit()
        return key.id


class AgentTranscriptionsContractTest(unittest.TestCase):
    client: TestClient
    key_id: str

    @classmethod
    def setUpClass(cls):
        cls.key_id = _seed()

    def setUp(self):
        # 每个用例独立 client（避免 lifespan 状态串扰）
        self._ctx = TestClient(api_main.app)
        self.client = self._ctx.__enter__()
        self.headers = {"X-API-Key": API_KEY_RAW}

    def tearDown(self):
        self._ctx.__exit__(None, None, None)

    # ---- POST 提交 ----

    def test_submit_creates_meeting_with_key_and_context_hotwords(self):
        with patch.object(mcp_asr, "_run_agent_pipeline") as pipeline:
            resp = self.client.post(
                "/api/v1/agent/transcriptions",
                json={
                    "audio_url": "http://10.0.0.32:9101/goodeep/tenants/t/attachment/x.webm?sig=1",
                    "title": "契约测试会议",
                    "owner": OWNER,
                    "session_key": SESSION_KEY,
                },
                headers=self.headers,
            )
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(body["status"], "downloading")
        self.assertTrue(body["task_id"])
        pipeline.assert_called_once()

        from apps.api.main import Meeting, SessionLocal

        with SessionLocal() as session:
            meeting = session.get(Meeting, body["task_id"])
            self.assertIsNotNone(meeting)
            self.assertEqual(meeting.title, "契约测试会议")
            self.assertEqual(meeting.status, "downloading")
            # agent 来源标记（api_key_id）与归属用户落库
            self.assertEqual(meeting.api_key_id, self.key_id)
            self.assertIsNone(meeting.user_id)  # ApiKey 未绑定平台用户
            # 热词来自 business_contexts 会话级快照（normalize 后顿号转逗号）
            self.assertEqual(meeting.hotwords, "歌尔,领元")

    def test_submit_hotwords_fallback_user_level(self):
        with patch.object(mcp_asr, "_run_agent_pipeline"):
            resp = self.client.post(
                "/api/v1/agent/transcriptions",
                json={
                    "audio_url": "http://10.0.0.32:9101/x.webm",
                    "owner": OWNER,
                    "session_key": "sess-not-exists",  # 会话级无行 → 回退用户级
                },
                headers=self.headers,
            )
        self.assertEqual(resp.status_code, 200)
        from apps.api.main import Meeting, SessionLocal

        with SessionLocal() as session:
            meeting = session.get(Meeting, resp.json()["task_id"])
            self.assertEqual(meeting.hotwords, "长期热词")

    def test_submit_explicit_hotwords_override(self):
        with patch.object(mcp_asr, "_run_agent_pipeline"):
            resp = self.client.post(
                "/api/v1/agent/transcriptions",
                json={
                    "audio_url": "http://10.0.0.32:9101/x.mp3",
                    "owner": OWNER,
                    "hotwords": "显式热词",
                },
                headers=self.headers,
            )
        self.assertEqual(resp.status_code, 200)
        from apps.api.main import Meeting, SessionLocal

        with SessionLocal() as session:
            meeting = session.get(Meeting, resp.json()["task_id"])
            self.assertEqual(meeting.hotwords, "显式热词")

    def test_submit_invalid_url_400(self):
        resp = self.client.post(
            "/api/v1/agent/transcriptions",
            json={"audio_url": "ftp://not-http/x.webm"},
            headers=self.headers,
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("http(s)", resp.json()["detail"])

    def test_submit_requires_key_401(self):
        resp = self.client.post(
            "/api/v1/agent/transcriptions",
            json={"audio_url": "http://10.0.0.32:9101/x.webm"},
        )
        self.assertEqual(resp.status_code, 401)

    # ---- GET 进度 / 结果 ----

    def _submit(self) -> str:
        with patch.object(mcp_asr, "_run_agent_pipeline"):
            resp = self.client.post(
                "/api/v1/agent/transcriptions",
                json={"audio_url": "http://10.0.0.32:9101/x.webm", "title": "状态测试"},
                headers=self.headers,
            )
        assert resp.status_code == 200, resp.text
        return resp.json()["task_id"]

    def test_status_downloading_with_duration_none(self):
        task_id = self._submit()
        resp = self.client.get(f"/api/v1/agent/transcriptions/{task_id}", headers=self.headers)
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["task_id"], task_id)
        self.assertEqual(body["status"], "downloading")
        self.assertIsNone(body["duration_seconds"])

    def test_status_unknown_404(self):
        resp = self.client.get("/api/v1/agent/transcriptions/no-such-task", headers=self.headers)
        self.assertEqual(resp.status_code, 404)

    def test_result_progress_then_completed(self):
        task_id = self._submit()
        # 未到终态：返回进度而非全文
        resp = self.client.get(f"/api/v1/agent/transcriptions/{task_id}/result", headers=self.headers)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "downloading")
        self.assertNotIn("transcript", resp.json())  # 未终态只回进度，不给全文（与 MCP 工具一致）

        # 置为终态 ready → completed + transcript（此处无 Transcript 行 → None，结构仍在）
        from apps.api.main import Meeting, SessionLocal

        with SessionLocal() as session:
            meeting = session.get(Meeting, task_id)
            meeting.status = "ready"
            session.commit()
        resp = self.client.get(f"/api/v1/agent/transcriptions/{task_id}/result", headers=self.headers)
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["status"], "completed")
        self.assertIn("transcript", body)
        self.assertIn("segments", body)


if __name__ == "__main__":
    unittest.main()
