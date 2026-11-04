# 计划：业务背景/热词落库（33 侧 CRUD + 模型工具读写 + 前端接口拉取）

> 2026-11-04 起草，待批准后实施。
> 背景：业务背景目前只存 agent-web 浏览器 localStorage（`recording.biz_context` / `recording.hotwords`），
> 换设备/换浏览器即丢；模型对业务背景的修正也无法沉淀。本计划把它落到 33 侧数据库，
> 打通「前端拉初始值 → 录音转写 → 模型修正 → 写回落库 → 下次拉到新版」的闭环。

## 一、存储模型（33 侧 video-transcribe-platform）

新表 `business_contexts`（main.py 的 `Base` + `create_all` 自动建表，迁移写法参考 `meetings.hotwords` 列的 sqlite/postgres 双兼容先例）：

| 列 | 类型 | 说明 |
|---|---|---|
| id | str pk (uuid) | |
| user_id | str nullable FK users.id, index | ApiKey 归属用户（复用 `_key_principal` 反查）；NULL = 匿名/服务账号 |
| session_key | str nullable, index | 会话维度标识（透传 Java 会话 ID 的不透明字符串）；NULL = 用户级默认 |
| content | Text | 业务背景正文 |
| hotwords | Text nullable | 热词与业务背景同存同取 |
| source | str | `user`（前端保存）/ `model`（模型工具写入） |
| created_at / updated_at | datetime | |

- upsert 语义：`(user_id, session_key)` 定位一行；session 级存在则覆盖 session 级，否则覆盖 user 级（由调用方指定 scope）。
- 合并读取语义：session 级 > user 级（session 有值返回 session 值，否则回退 user 值）。
- 并发：转录场景写入频率极低（一次会议一两次），SQLite 单写锁足够，无需乐观锁。

## 二、REST CRUD（33 侧，meetings.py 追加，`meeting_auth` 双通道鉴权）

> 2026-11-04 已实施，实际路径对齐现有 `/api/v1/` 前缀：

```
GET    /api/v1/recording-context?session_key=<可选>
  → { "user": {content, hotwords, source, updated_at} | null,
      "session": {...} | null,                               # 传了 session_key 才有
      "merged": {...} }                                      # session > user

PUT    /api/v1/recording-context
  body: { content, hotwords?, session_key?, scope: "user"|"session" }
  → upsert，source 记 "user"

DELETE /api/v1/recording-context?session_key=<可选>   # 不传=清用户级，传=清会话级
```

## 三、MCP 工具 +2（33 侧 mcp_asr.py，复用现有 ApiKey 中间件与归属反查）

> 2026-11-04 已实施：

```python
@mcp.tool
def get_context(session_key: str | None = None) -> dict:
    """读取当前生效的业务背景与热词（会话级优先，回退用户级）。转写前可调用确认背景。"""

@mcp.tool
def save_context(content: str, hotwords: str | None = None, session_key: str | None = None) -> dict:
    """保存业务背景（含修正）。提供 session_key 时同时写会话级快照 + 用户级长期版本（跨会话沉淀），未提供只更新用户级。"""
```

- 归属：user_id 从 ApiKey 反查（与 transcribe 落 Meeting.user_id 同一逻辑）。
- `save_context` 的 source 记 `model`。
- transcribe 工具本身不动：模型要么用前端注入的背景，要么先 `get_context`。

## 四、Java 代理（platform-service，前端拉取的中转）

> 2026-11-04 已实施（`platform-service/src/main/java/com/goodeep/agent/platform/asr/`，编译通过）：

- `AsrProperties`：`platform.asr.base-url` / `api-key`（env `ASR_BASE_URL`/`ASR_API_KEY`，**空默认值**——漏配时上下文照常启动，调用时 503 `ASR_NOT_CONFIGURED(50002)`，绝不降级放行）
- `AsrClient`：java.net.http，连接 3s / 请求 10s 超时；上游非 2xx → BizException；密钥只进请求头不落日志
- `RecordingContextController`：`GET/PUT/DELETE /api/v1/recording-context`（ApiResult 包装，`session_key` query 参数）；**归属域固定 `{tenantId}:{userId}`**（userId 跨租户可能重复，拼 tenantId 规避），经 `X-Owner-Id` 透传
- 不进 whitelist（JwtAuthFilter 默认要求登录）；nginx/vite 零配置改动；platform-service 不建表不迁移
- **归属格式契约**：agent-web 注入录音指令的 MCP `owner` 参数必须用同一 `{tenantId}:{userId}` 格式（前端从登录态取），与 REST 字节级同源
- 33 侧的两个调用方（agent-adapt 的 MCP `X-API-Key`、platform-service 的 `asr.api-key`）必须是**同一把 key**，轮换时一起改

## 五、agent-web 改动

1. **TextInput.tsx**：
   - 初始值：先读 localStorage 立即渲染（不阻塞输入），同时异步拉 `GET /recording-context`，
     拉到内容比本地新则覆盖并更新 localStorage（按 updated_at 比较）。
   - 保存：输入时仍即时写 localStorage（防抖保留现状）；开录瞬间（或失焦）额外 `PUT /recording-context` 落库；
     落库失败不阻断录音，toast 提示即可。
2. **ChatViewport.tsx 指令**：把上一轮的「条件式记忆指令」替换为明确工具调用：
   - 未提供业务背景时：先调 `asr get_context` 取背景再用；
   - 纪要 + 「业务背景修正」小节生成后：调 `asr save_context` 写回（session_key 传当前会话 ID）。
3. **mock（javaHandlers.ts）**：mockAsrScenario 事件流补一个 `save_context` 调用示例，让 mock 演示完整闭环。

## 六、实施顺序（每步可独立验证）

1. **33 侧**：表 + REST CRUD + 2 个 MCP 工具 → 本地起 8010，curl 验证 REST，MCP inspector / 直接工具调用验证 get/save。
2. **Java 侧**：代理端点 → 本地起 platform-service，带登录态 curl 验证转发。
3. **agent-web**：TextInput 拉取/回写 + ChatViewport 指令 + mock → mock 演示 + 真链路 E2E。
4. **部署**：33 走 release.sh 发版（18443 会议页回归）；platform-service 发版；agent-web 前端构建发版。

## 七、评审修复记录（2026-11-04 第二轮，全部已实施）

评审发现 6 项问题，修复如下（冒烟测试 `scripts/smoke_business_context.py` 三阶段全过）：

| 级别 | 问题 | 修复 |
|---|---|---|
| P0 | 匿名共享行跨用户串号 | 归属域透传：REST `X-Owner-Id` header（仅 ApiKey/admin 凭证可指定，普通登录用户 403）；MCP 工具加 `owner` 参数（前端录音指令注入平台用户标识）。两者均缺省时回退 ApiKey 归属用户 → `__anon__` |
| P0 | NULL 不参与唯一索引判重 → 并发脏行 | `user_id`/`session_key` 改 NOT NULL 哨兵（`__anon__` / `__user__`）+ `UniqueConstraint(user_id, session_key)`（唯一索引兼作复合查询索引，不再建单列索引）；upsert 撞约束回查转更新重试一次，仍冲突 409/ToolError |
| P1 | PUT 全量替换静默清空热词 | 热词语义统一（REST/MCP 一致）：`None`=保留现有，空串=清空，其余规范化覆盖 |
| P1 | MCP save_context 无长度上限 | content ≤20000、session_key/owner ≤128（与 REST 对齐），超限 ToolError |
| P2 | REST/MCP 会话语义不一致 | 有意设计并写死：MCP `save_context` 带 session_key 时双写（会话快照+用户沉淀），REST PUT 精确单层。已在 mcp_asr.py 注释与本文档标明 |
| P2 | 时区/复合索引/merged 来源 | `DateTime(timezone=True)`；唯一约束即复合索引；GET `merged` 带 `scope: session\|user` |

**Java 代理对接要求（更新）**：
- 必传 `X-Owner-Id: <平台用户标识>`（33 侧已按 P0 加了归属透传，单服务账号共享行的问题从代理层根治）
- `asr.api-key` 只留服务端；匿名通道在有任一 ApiKey 后自动关闭
- 模型侧 `save_context`/`get_context` 的 `owner` 参数由 agent-web 录音指令文本注入（前端知道当前登录用户标识）
