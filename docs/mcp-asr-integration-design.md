# MCP-ASR 转录能力接入 goodeep-agent — 阶段 1 接口设计

> 状态：**已定稿**（2026-11-02 四项决策确认：跨机部署 / 同 app mount / 提交带模板自动纪要 / 仅放开音频 6 格式 + 500MB）
> 日期：2026-11-02

## 1. 结论先行

- **中间层**：不新建服务。在 video-transcribe-platform 的 FastAPI（8310/8010）内挂载 MCP streamable-http 端点（`/mcp`），复用现有 Meeting/ProcessingJob/模板/热词全套状态机，MOSS→vLLM 算力切换对 agent 侧完全透明。
- **音频传递**：agent 平台 `file_id` → agent-adapt 调 `FileService.presignGet` 生成 GET 预签名 URL → 作为 MCP 工具入参 → 中间层收到后**立即下载**到 `storage/meetings/`，之后走平台既有转录流水线。不做跨容器文件搬运。
- **异步模型**：两段式（提交返回 task_id + 轮询工具），符合 agent-adapt `async_tool_record` 的既有模式，不阻塞 MCP 调用。
- **task_id = meeting_id**：不新建任务表，直接复用平台 `GET /api/v1/meetings/{id}` 的状态与产物。

## 2. 架构与调用时序

```
用户对话                    agent-adapt(8081)              video-transcribe-platform(FastAPI)        MOSS/vLLM
  │  "转写这场会议"             │                                │                                     │
  │──────────────────────────>│  1. presignGet(file_id)        │                                     │
  │                           │     → presigned GET URL(5min)  │                                     │
  │                           │  2. MCP: asr.transcribe(url,   │                                     │
  │                           │     template_id, hotwords...)  │                                     │
  │                           │───────────────────────────────>│  3. 202 {task_id=meeting_id}        │
  │                           │  4. 记 async_tool_record       │  后台: 下载→storage/meetings/       │
  │                           │                                │      → 建 Meeting(recording)        │
  │                           │                                │      → start_final_transcribe ─────>│
  │                           │  5. MCP: asr.status(task_id)   │                                     │
  │                           │───────────────────────────────>│  → ProcessingJob 状态/进度          │
  │                           │  6. MCP: asr.result(task_id)   │                                     │
  │                           │───────────────────────────────>│  → transcript 分段 + 纪要           │
```

## 3. MCP 工具契约（3 个工具）

挂载方式：FastMCP `streamable_http_app()` mount 到现有 ASGI app，路径 `/mcp`。认证复用 `require_meeting_key`（`X-API-Key` header，`mk_` 前缀 ApiKey 已有签发/吊销端点）。

### asr.transcribe — 提交转录任务
```jsonc
// 入参
{
  "audio_url": "string, 必填, MinIO 预签名 GET URL",
  "title": "string, 可选, 默认取 URL 对象名",
  "language": "zh|auto|en, 默认 zh",
  "template_id": "int, 可选, 纪要模板; 提供则转录完成后自动生成纪要",
  "business_context": "string, 可选, 纪要生成的业务语境",
  "hotwords": "string, 可选, 逗号/顿号分隔(≤500字符, 透传 normalize_hotwords)"
}
// 返回 202
{ "task_id": "<meeting_id>", "status": "submitted" }
```
后台流程：下载（限时 + 500MB 上限 + 格式白名单校验）→ `create_meeting(mode=recording, hotwords=...)` → 上传音频登记 → `start_final_transcribe`。下载失败返回 4xx 带原因（URL 过期/格式不符/超限）。

### asr.status — 查询进度
```jsonc
// 入参 { "task_id": "..." }
// 返回（映射 meeting_dict 内的 job）
{ "status": "downloading|transcribing|summarizing|completed|failed|cancelled",
  "progress": 42, "error": "仅失败时" }
```

### asr.result — 取结果
```jsonc
// 入参 { "task_id": "...", "include_segments": true }
// 返回
{ "status": "completed",
  "transcript": "全文纯文本",
  "segments": [{ "start": 0.0, "end": 3.2, "speaker": "SPEAKER_00", "text": "..." }],
  "summary": "纪要 markdown（template_id 提供时才有）" }
```

## 4. goodeep-agent 侧改动（Java）

| # | 改动 | 位置 | 说明 |
|---|------|------|------|
| J1 | **扩音频白名单**（必须） | `file/FileRules.java` L18-23 | `ALLOWED_EXT` 追加 `mp3/wav/m4a/mp4/webm/mov`；新增"音频"类别上限 500MB（对齐平台 `MEETING_FORMATS`） |
| J2 | **注册 MCP Server**（免改码） | 前端 MCP 页或 `POST /api/v1/agents/{id}/mcp-servers` | `{name:"asr", transport:"streamable-http", url:"http://<host>:<port>/mcp", headers:{"X-API-Key":"mk_..."}}`；纯数据库配置（`a_agent.mcp_json` + `tools_json` 镜像） |
| J3 | **presignGet 过期参数化**（跨机部署 → 必做） | `file/FileService.java` L406-417 | 现固定 5 分钟；两平台不同机器，下载耗时受内网带宽影响，改为可配 TTL（默认仍 300s，ASR 场景配 900s），并确保 MinIO endpoint 地址对转录平台内网可达 |

## 5. 平台侧改动（Python）

| # | 改动 | 位置 | 说明 |
|---|------|------|------|
| P1 | MCP 工具层 | 新文件 `apps/api/mcp_asr.py` | 3 个工具实现 + FastMCP mount；下载器（httpx 流式落盘 `storage/meetings/agent/`） |
| P2 | 认证 | 复用 `require_meeting_key` / ApiKey 表 | 为 agent 平台签发专用 ApiKey，不改认证代码 |
| P3 | 任务串联 | 复用 `main.py` `_run_moss_transcription` + `start_summary` | template_id 提供时转录完成回调里追加 `start_summary`；新增产物 `source=agent` 标记便于列表过滤 |
| P4 | vLLM 池对接（阶段 2） | 仅换 `_run_moss_transcription` 内部实现 | 工具契约零变动——这正是选中间层而非直连 vLLM 的收益 |

## 6. 已确认决策（2026-11-02）

| 决策点 | 结论 |
|--------|------|
| 网络拓扑 | **两平台不同机器**：presigned URL / MinIO endpoint 需内网跨机可达；J3 参数化 TTL 为必做；中间层下载器加超时与重试 |
| MCP 挂载方式 | **B：同 app mount**（FastMCP mount 到现有 FastAPI，零新增容器） |
| 纪要触发时机 | **提交时带 template_id，转录完成自动生成纪要**，result 一次取全 |
| 白名单范围 | **仅放开 mp3/wav/m4a/mp4/webm/mov 6 个扩展名，音频类别上限 500MB**，其他类别与大文件策略不动 |

跨机部署补充风险注记：① MinIO 预签名 URL 中 host 为 agent 平台配置的 MinIO endpoint，转录平台侧需配置可达（必要时 hosts/内网 DNS）；② `/mcp` 端点需对 agent 平台（agent-adapt 出口）网络放通，认证仅靠 `X-API-Key`，建议内网安全组进一步限源；③ 下载 500MB 音频跨机耗时需计入任务预算（下载阶段单独计 progress）。

## 7. 阶段计划

- **阶段 1（本设计）**：接口契约 + J1/J2/P1-P3 落地 → 本地 E2E（手工 curl MCP 端点模拟 agent 调用）
- **阶段 2**：33 上 vLLM 池（隔离压测通过后）替换 MOSS 后端，P4
- **阶段 3**：agent-adapt 侧 async_tool_record 与 SSE 进度联动（代码在 agent-adapt 仓库，本 checkout 不可见，需另行对接）
