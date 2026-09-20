# 视频解析与转录总结平台

面向单管理员自部署的媒体解析、MOSS 转录与 Meetily 风格总结工作台。

## 快速启动

```bash
cp .env.example .env
docker compose -f deploy/docker-compose.yml up --build
```

API 文档启动后位于 `http://localhost:8000/docs`，Web 位于 `http://localhost:3000`。

## 目录

- `apps/api`：FastAPI API、PostgreSQL 模型和异步任务编排
- `apps/web`：Next.js 工作台
- `services/moss-transcriber`：独立 MOSS HTTP 服务契约与适配器
- `packages`：共享类型、解析器和摘要 Provider
- `database`：迁移和种子数据
- `deploy`：PostgreSQL、API、Web、MOSS 的 Compose 配置

当前适配器在没有真实服务配置时返回明确的“未配置”状态，不会伪造媒体或转录结果。
