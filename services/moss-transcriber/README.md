# MOSS Transcriber

本机用 FunASR 加载 `MOSS-Transcribe-Diarize`。Mac 上不要放进 Linux Docker 小容器：没有 MPS，torch 镜像也太大。

Meetily 已经装好环境和模型，直接复用：

```bash
./services/moss-transcriber/run_local.sh
```

进程起来后会在后台预热模型。HTTP 立即可用，模型加载完成前 `health.model_loading` 为 `true`。

```text
GET  /health
GET  /v1/models
POST /v1/models/load
POST /v1/summaries
POST /v1/transcriptions
GET  /v1/transcriptions/{job_id}
GET  /v1/transcriptions/{job_id}/result
```

检查状态：

```bash
curl http://127.0.0.1:9000/health
```

关注 `model_found`、`model_loading`、`model_loaded`、`device`。手动预热（会等到加载完成）：

```bash
curl -X POST http://127.0.0.1:9000/v1/models/load
```

关闭启动预热：`MOSS_PRELOAD=0 ./services/moss-transcriber/run_local.sh`
