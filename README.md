# 视频解析与转录总结平台

一个面向单管理员、自部署场景的视频工作台。输入视频链接或上传本地音视频后，可以查看媒体信息，下载视频，调用 MOSS/FunASR 完成转录，并使用本地小模型生成总结。

当前项目包含 FastAPI API、Next.js 工作台、PostgreSQL 数据库，以及可独立运行的 MOSS 转录服务。未配置真实解析器、转录模型或总结模型时，系统会显示明确的未配置状态，不会伪造结果。

## 功能概览

- 解析视频链接并保存标题、作者、封面和媒体版本
- 上传本地 MP3、WAV、M4A、MP4、WebM、MOV 文件并复用现有转录与历史工作流（最大 500MB）
- 支持视频下载和历史记录查看
- 通过 MOSS/FunASR 进行异步转录，支持进度日志和说话人信息
- 根据转录结果生成 Markdown 总结
- 管理总结模板
- 查看、复制和导出转录结果与总结结果
- 支持 Docker Compose 部署，也支持 API、Web、数据库和转录服务分别运行

## 系统要求

- Python 3.11+
- Node.js 18+
- Docker Desktop（使用 Compose 部署时）
- PostgreSQL 16（本地开发时可由 Compose 启动）
- MOSS 转录模型和总结模型（需要真实转录/总结时）

## 快速启动

```bash
cp .env.example .env
docker compose -f deploy/docker-compose.yml up --build
```

默认地址：

| 服务 | 地址 |
| --- | --- |
| Web 工作台 | http://localhost:3000 |
| API 文档 | http://localhost:8000/docs |
| API 健康检查 | http://localhost:8000/api/system/health |
| MOSS 服务 | http://localhost:9001/health |

Compose 默认启动 Web、API、PostgreSQL 和 MOSS 四个本机容器，API 与 MOSS 共用受管媒体卷以处理上传文件。模型挂载路径默认面向本机 Meetily 安装目录；Linux 或其他目录布局需要按实际路径修改 `deploy/docker-compose.yml`。

停止服务：

```bash
docker compose -f deploy/docker-compose.yml down
```

## 本地开发

### 启动数据库

```bash
docker compose -f deploy/docker-compose.yml up -d postgres
```

### 启动 API

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
uvicorn apps.api.main:app --reload --host 0.0.0.0 --port 8000
```

### 启动 Web

```bash
cd apps/web
npm ci
npm run dev
```

开发服务器默认运行在 http://localhost:3000。API 地址可以通过 `NEXT_PUBLIC_API_URL` 修改：

```bash
NEXT_PUBLIC_API_URL=http://localhost:8000 npm run dev
```

### 启动本机 MOSS 服务

```bash
./services/moss-transcriber/run_local.sh
```

脚本默认复用 Meetily FunASR 环境：

- Python 环境：`$HOME/Desktop/Meetily/frontend/funasr-sidecar/.venv`
- MOSS 模型：`$HOME/Library/Application Support/com.meetily.ai/funasr-models/OpenMOSS-Team/MOSS-Transcribe-Diarize`
- 总结模型：`$HOME/Library/Application Support/com.meetily.ai/models/summary/Qwen3.5-4B-Q4_K_M.gguf`

路径不同可以覆盖变量：

```bash
MEETILY_FUNASR_VENV=/path/to/.venv \
MOSS_MODEL_PATH=/path/to/MOSS-Transcribe-Diarize \
SUMMARY_MODEL_PATH=/path/to/Qwen3.5-4B-Q4_K_M.gguf \
./services/moss-transcriber/run_local.sh
```

检查转录服务：

```bash
curl http://127.0.0.1:9000/health
```

重点查看 `model_found`、`model_loading`、`model_loaded` 和 `device`。详细接口见 [services/moss-transcriber/README.md](services/moss-transcriber/README.md)。

## 配置

根目录 `.env` 由 `.env.example` 复制而来：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DATABASE_URL` | `postgresql+psycopg://media:media@localhost:5432/media_platform` | API 数据库连接串 |
| `ADMIN_USERNAME` | `admin` | 管理员用户名 |
| `ADMIN_PASSWORD` | `change-me` | 管理员密码，部署前应修改 |
| `API_SECRET` | `change-me` | API 签名密钥，部署前应修改 |
| `COOKIE_ENCRYPTION_KEY` | 空（回退使用 `API_SECRET`） | 视频号 Cookie 加密密钥，生产环境应设置为独立的高强度随机值 |
| `MOSS_URL` | `http://localhost:9000` | MOSS 服务地址 |
| `MOSS_PRELOAD` | `1` | 是否启动后预热模型 |
| `STORAGE_ROOT` | `./storage` | 下载媒体和中间文件目录 |
| `JWT_ACCESS_SECRET` | 空（从 `API_SECRET` 派生） | 访问令牌密钥，弱值（如 `change-me`）会导致拒绝启动 |
| `JWT_REFRESH_SECRET` | 空（从 `API_SECRET` 派生） | 刷新令牌密钥，轮换会使全部会话失效 |
| `REGISTRATION_MODE` | `open` | `open` 开放注册；`admin_only` 仅管理员建用户（首位用户永远开放） |
| `AUTH_LOCAL_BYPASS` | 空（关） | 设为 `1` 时本地浏览器无凭证可直连 API，仅用于本地开发 |

不要提交 `.env`、数据库文件、模型文件或本地运行数据；仓库的 `.gitignore` 已排除这些内容。

## 用户系统

平台内置账号体系，浏览器端统一使用 JWT（Bearer），API Key 保留给编程访问：

- **注册/登录**：`POST /api/auth/register`、`POST /api/auth/login`。系统没有任何用户时，首位注册者成为管理员，历史数据自动归属其名下
- **令牌**：access 30 分钟 + refresh 14 天，一次性轮换（旧 refresh 再用会撤销该会话并拒绝）；刷新失败即跳登录页
- **数据隔离**：普通用户只能看到自己的视频/会议/任务，越权一律返回 404；管理员可见全部数据
- **会话管理**：改密、禁用、重置密码会踢掉该用户全部设备；`设置 → 用户管理`（仅管理员）可禁用/启用、改角色、重置密码、删号
- **登出**：右上角用户菜单 → 退出登录

公开端点仅 `/api/auth/*`、`/api/system/health`、`/api/platforms`，其余全部需要登录。

## 项目结构

```text
apps/
  api/                    FastAPI API、数据库模型和任务编排
  web/                    Next.js 工作台
services/
  moss-transcriber/       MOSS/FunASR HTTP 服务和启动脚本
deploy/
  docker-compose.yml      PostgreSQL、API、Web、MOSS 编排
  *.Dockerfile            各服务镜像定义
docs/
  PRD-v1.0.md             产品设计和验收边界
pyproject.toml            Python API 依赖
.env.example              环境变量模板
```

## 主要 API

启动 API 后可访问 Swagger：`http://localhost:8000/docs`。

- `POST /api/parse`：解析视频链接
- `POST /api/uploads`：流式上传并校验本地音视频，按 SHA-256 复用重复资产
- `GET /api/videos`、`GET /api/videos/recent`：视频和最近记录
- `POST /api/videos/{video_id}/transcribe`：创建转录任务
- `POST /api/videos/{video_id}/summary`：创建转录并总结任务
- `POST /api/videos/{video_id}/summaries`：根据已有转录创建总结
- `GET /api/jobs/{job_id}`：查询任务进度
- `GET /api/jobs/{job_id}/events`：读取任务日志
- `GET /api/system/health`：检查 API、MOSS 和模型状态
- `GET/PUT /api/settings/{group}`：读取和保存设置
- `GET/POST/PUT/DELETE /api/templates...`：管理总结模板
- `POST /api/auth/register|login|refresh|logout`、`GET /api/auth/me`：账号与会话
- `GET/PUT/DELETE /api/admin/users...`：用户管理（管理员）

## 开发检查

```bash
cd apps/web
npm run build
```

运行中的服务可以这样检查：

```bash
curl http://localhost:8000/api/system/health
curl http://localhost:9001/health
```

## 当前边界

- 媒体解析依赖 `apps/api/parser_runtime.py` 中的适配器和外部解析能力
- 转录依赖 MOSS/FunASR 模型及其运行环境
- 总结依赖 `llama-cli` 和本地 GGUF 模型，或已配置的远程 Provider
- Compose 中的 GPU、模型目录和宿主机路径需要按部署机器调整
- 用户系统覆盖注册/登录/会话/数据隔离/用户管理；不包含邮箱验证、2FA、社交登录和多租户能力

## 相关文档

- [产品设计 PRD](docs/PRD-v1.0.md)
- [MOSS 转录服务说明](services/moss-transcriber/README.md)
