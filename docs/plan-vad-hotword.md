# 转写链路增强计划：VAD 前置 + 热词到 ASR 层

> 目标：对齐豆包 ASR 链路中两个可低成本补齐的能力（预处理 VAD、识别层热词），
> 降低垃圾识别与同音错字。先做 P1-VAD，验收后再做 P2-热词，之后评估效果再定后续。
> 基线现状（2026-10-21 核对）：MOSS-Transcribe-Diarize 单模型端到端（ASR+说话人一体），
> 无任何前处理；热词仅存在于 LLM 总结层（business_context）。

## P1：VAD 前置（先做）

### 背景
- 现状：音频块（直播分块 / 文件 300s 分片）不加分拣直接进模型，纯噪声/静音块产出
  `[0.00][S01][0.50]` 标签行垃圾（已修过一次兜底过滤，但算力照烧）。
- 会议音频静音占比通常 40%+，VAD 前置预计省一半推理 CPU，同时从源头消灭垃圾段。

### 方案：能量门限 + FSMN-VAD 两级
- L1 能量门限（便宜，先挡）：对输入块算 RMS，低于阈值且持续整块 → 直接返回空结果。
  零模型、零依赖，收益立竿见影。
- L2 FSMN-VAD（准确，后挂）：FunASR 官方 `speech_fsmn_vad_zh-cn-16k-common`（约 1MB，
  CPU 亚毫秒级），挂在 moss 服务内作为独立 AutoModel 实例，对整段音频先切出人声区间，
  只把人声段送主模型。
- 阈值/开关走环境变量：`MOSS_VAD=energy|fsmn|off`（默认 energy，灰度可回退）。

### 改动点
| 文件 | 改动 |
|---|---|
| `services/moss-transcriber/main.py` | 新增 `_vad_gate(pcm)`；直播 `/transcribe-live` 与文件 `_infer_file` 入口各挂一层；FSMN 模型懒加载复用现有 `_load_lock` 模式 |
| 容器模型目录 | 下载数据 fsmn-vad 模型到 `/data/moss-cache/vad/`（随 volume 持久化，烤镜像丢失也不怕） |
| `deployment-assets` | compose 侧无改动（模型在数据卷） |

### 验收标准
1. 单测：静音块/纯噪声块 → 空结果（不再进主模型）；有人声块 → 照常出段。
2. 实测：录一场 2 分钟含停顿的会议，直播无垃圾段；日志统计「VAD 拦截块数/总块数」。
3. 性能：同一录音前后对比推理耗时，预期下降 ≥30%（视静音占比）。
4. 回归：终稿转录带说话人分离结果与基线一致（不因 VAD 切分丢失段）。

## P2：热词到 ASR 层（VAD 验收后）

### 背景
- 现状：`business_context`（参会人/公司名）只注入 LLM 总结 prompt；ASR 认错的字总结救不回。
- 探针结论：FunASR `AutoModel.generate` 的 kwargs 经 `cfg` 透传（含 `hotword` 通道），
  但 MOSS-Transcribe-Diarize 模型代码是否消费该参数**需 spike 验证**——这是 P2 的第一件事。

### 步骤
1. **Spike（半小时级）**：容器内用同一段含人名音频对比 `generate(..., hotword="怀古 张三")`
   与不带热词的输出，确认模型是否吃该参数。不吃 → P2 降级为「不做」，热词维持总结层。
2. **数据链路**（若 spike 通过）：
   - 前端 `BusinessContextInput` 增加「热词」字段（逗号分隔，参会人名自动带入可编辑），
     随 localStorage 预设一起存。
   - 后端：live WS 消息、`retry-transcribe`、文件转写任务的请求体加 `hotwords`；
     meetings.py → moss 请求透传。
   - moss：`_infer_file` / 直播解码处把热词拼进每次 `generate` 调用。
3. **总结层继续保留** business_context（两层各司其职：ASR 管字对，LLM 管文顺）。

### 验收标准
1. Spike 有正向差异（或确认不支持而止步）。
2. 同一段录音，带热词后人名错字率下降（人工对比文稿）。
3. 不带热词的请求行为与基线完全一致（回归零影响）。

## P3：效果评估（两者落地后）
- 用最近三场真实会议重跑转录+纪要，对比：垃圾段数、人名正确率、推理耗时。
- 输出一页对比结论，再决定是否继续追（降噪/真流式等豆包其余能力）。

## 排期与提交节奏
| 阶段 | 内容 | 交付 |
|---|---|---|
| P1 | VAD 前置（energy → fsmn 两级） | commit + 容器部署 + 回归验证 |
| P2 | 热词 spike →（通过则）全链路透传 | spike 结论或完整功能 |
| P3 | 三场会议效果评估 | 对比结论 |

风险备注：moss 容器代码是 docker cp 热部署（镜像重建会丢），P1/P2 每步都要 commit 入库；
VAD 模型文件放数据卷不走镜像。
