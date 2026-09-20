from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

HUB_ID = "OpenMOSS-Team/MOSS-Transcribe-Diarize"
MODEL_ROOT = Path(os.getenv(
    "MEETILY_FUNASR_MODEL_DIR",
    str(Path.home() / "Library/Application Support/com.meetily.ai/funasr-models"),
))
MOSS_MODEL_PATH = Path(os.getenv("MOSS_MODEL_PATH", str(MODEL_ROOT / HUB_ID)))
SUMMARY_MODEL_PATH = Path(os.getenv(
    "SUMMARY_MODEL_PATH",
    str(Path.home() / "Library/Application Support/com.meetily.ai/models/summary/Qwen3.5-4B-Q4_K_M.gguf"),
))
LLAMA_CLI = os.getenv("LLAMA_CLI", "llama-cli")
jobs: dict[str, dict[str, Any]] = {}
_model = None
_device_cache: str | None = None
_infer_lock = threading.Lock()
_load_lock = threading.Lock()
_funasr_error: str | None = None
_loading = False
_warmup_error: str | None = None


def _should_preload() -> bool:
    return os.getenv("MOSS_PRELOAD", "1").strip().lower() not in {"0", "false", "no", "off"}


def _preload() -> None:
    global _warmup_error
    try:
        _load_model()
    except Exception as exc:
        _warmup_error = str(getattr(exc, "detail", None) or exc)


@asynccontextmanager
async def lifespan(_: FastAPI):
    if _should_preload():
        threading.Thread(target=_preload, daemon=True, name="moss-preload").start()
    yield


app = FastAPI(title="MOSS Transcriber", version="0.2.0", lifespan=lifespan)


class TranscriptionRequest(BaseModel):
    media_url: str | None = None
    media_path: str | None = None
    language: str = "zh"
    model: str | None = None
    output_format: str = "json"
    duration_seconds: float | None = None
    referer: str | None = None


class SummaryRequest(BaseModel):
    text: str
    title: str | None = None
    template_name: str | None = None
    sections: list[dict[str, Any]] = []
    max_tokens: int = 1024


def _device() -> str:
    global _device_cache
    if _device_cache:
        return _device_cache
    env = os.environ.get("MEETILY_FUNASR_DEVICE") or os.environ.get("MOSS_DEVICE")
    if env:
        _device_cache = env
        return _device_cache
    try:
        import torch
        if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
            _device_cache = "mps"
            return _device_cache
        if torch.cuda.is_available():
            _device_cache = "cuda"
            return _device_cache
    except Exception:
        pass
    _device_cache = "cpu"
    return _device_cache


def _funasr_available() -> bool:
    global _funasr_error
    try:
        import importlib.util
        if importlib.util.find_spec("funasr") is None:
            _funasr_error = "未安装 funasr"
            return False
        return True
    except Exception as exc:
        _funasr_error = str(exc)
        return False


def _model_ready() -> bool:
    if not MOSS_MODEL_PATH.is_dir():
        return False
    has_config = (MOSS_MODEL_PATH / "config.json").exists() or (MOSS_MODEL_PATH / "configuration.json").exists()
    has_weight = any(
        child.is_file() and child.suffix in {".safetensors", ".bin", ".pt"}
        for child in MOSS_MODEL_PATH.rglob("*")
    )
    return has_config and has_weight


def _speaker_label(spk: Any) -> str | None:
    if spk is None or spk == "":
        return None
    if isinstance(spk, str):
        digits = "".join(ch for ch in spk if ch.isdigit())
        if not digits:
            return spk if spk.startswith("说话人") else f"说话人 {spk}"
        idx = int(digits)
        if idx >= 1:
            idx -= 1
    else:
        idx = int(spk)
    return f"说话人 {chr(ord('A') + (idx % 26))}"


def _load_model():
    global _model, _loading, _warmup_error
    if _model is not None:
        return _model
    _loading = True
    with _load_lock:
        try:
            if _model is not None:
                return _model
            if not _funasr_available():
                raise HTTPException(503, f"本机未安装 FunASR：{_funasr_error or 'import failed'}")
            if not _model_ready():
                raise HTTPException(503, f"未找到 MOSS 模型文件：{MOSS_MODEL_PATH}")
            from funasr import AutoModel

            _model = AutoModel(
                model=HUB_ID,
                model_path=str(MOSS_MODEL_PATH),
                model_conf={},
                device=_device(),
                disable_update=True,
                trust_remote_code=True,
            )
            _warmup_error = None
            return _model
        except HTTPException as exc:
            _warmup_error = str(exc.detail)
            raise
        except Exception as exc:
            _warmup_error = str(exc)
            raise
        finally:
            _loading = False


def _segments_from_funasr(result: Any, fallback_end_ms: float) -> list[dict[str, Any]]:
    if not result:
        return []
    payload = result[0] if isinstance(result, list) else result
    sentence_info = payload.get("sentence_info") if isinstance(payload, dict) else None
    if sentence_info:
        segments = []
        for item in sentence_info:
            text = str(item.get("text", "")).strip()
            if not text:
                continue
            start = item.get("start", 0)
            end = item.get("end", start)
            start_ms = float(start) if float(start) > 1000 or float(end) > 1000 else float(start) * 1000
            end_ms = float(end) if float(end) > 1000 or float(start) > 1000 else float(end) * 1000
            segments.append({
                "text": text,
                "start_ms": round(start_ms, 1),
                "end_ms": round(end_ms, 1),
                "speaker": _speaker_label(item.get("spk", item.get("speaker"))),
                "is_final": True,
            })
        if segments:
            return segments
    text = str(payload.get("text", "")).strip() if isinstance(payload, dict) else str(payload).strip()
    if not text:
        return []
    return [{"text": text, "start_ms": 0.0, "end_ms": fallback_end_ms, "speaker": None, "is_final": True}]


def _fmt_clock(seconds: float | None) -> str:
    value = max(0, int(seconds or 0))
    minutes, rest = divmod(value, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{rest:02d}"
    return f"{minutes}:{rest:02d}"


def _touch(job: dict[str, Any], **fields: Any) -> None:
    job.update(fields)
    job["updated_at"] = time.time()


def _heartbeat(job: dict[str, Any], stop: threading.Event, start: int, end: int, audio_seconds: float | None, stage: str, label: str) -> None:
    t0 = time.time()
    rtf = 2.4 if (_device_cache or "") == "cpu" else 1.7
    estimate = max((audio_seconds or 0) * rtf, 25) if audio_seconds else 120
    while not stop.wait(1.0):
        elapsed = time.time() - t0
        ratio = min(0.94, elapsed / estimate)
        progress = start + int((end - start) * ratio)
        remain = max(0, estimate - elapsed)
        if audio_seconds and stage == "transcribing":
            message = f"{label} {_fmt_clock(audio_seconds)} 音频，已用 {_fmt_clock(elapsed)}，预计剩余 {_fmt_clock(remain)}"
        else:
            message = f"{label}，已用 {_fmt_clock(elapsed)}"
        _touch(
            job,
            status="running",
            stage=stage,
            progress=progress,
            message=message,
            elapsed_seconds=round(elapsed),
            eta_seconds=round(remain),
            audio_seconds=audio_seconds,
        )


def _download_url(url: str, referer: str | None, job: dict[str, Any]) -> tuple[str, str]:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
    }
    if referer:
        headers["Referer"] = referer
    elif any(token in url for token in ("douyin", "byte", "iesdouyin")):
        headers["Referer"] = "https://www.iesdouyin.com/"
    elif any(token in url for token in ("bili", "hdslb")):
        headers["Referer"] = "https://www.bilibili.com/"
    suffix = ".mp4"
    path_part = url.split("?", 1)[0].lower()
    for item in (".wav", ".mp3", ".m4a", ".aac", ".flac", ".mp4", ".webm"):
        if path_part.endswith(item):
            suffix = item
            break
    handle = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    handle.close()
    request = urllib.request.Request(url, headers=headers)
    _touch(job, stage="downloading", progress=6, message="正在下载媒体")
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            total = int(response.headers.get("Content-Length") or 0)
            received = 0
            with open(handle.name, "wb") as output:
                while True:
                    chunk = response.read(1024 * 256)
                    if not chunk:
                        break
                    output.write(chunk)
                    received += len(chunk)
                    if total:
                        ratio = min(1.0, received / total)
                        _touch(job, stage="downloading", progress=6 + int(16 * ratio), message=f"正在下载媒体 {int(ratio * 100)}%")
                    else:
                        _touch(job, stage="downloading", progress=12, message=f"正在下载媒体 {received // (1024 * 1024)} MB")
    except urllib.error.URLError as exc:
        os.unlink(handle.name)
        raise HTTPException(400, f"下载媒体失败：{exc}") from exc
    if not Path(handle.name).stat().st_size:
        os.unlink(handle.name)
        raise HTTPException(400, "下载的媒体文件为空")
    return handle.name, handle.name


def _to_wav(source: Path, job: dict[str, Any] | None = None) -> tuple[str, str | None]:
    if source.suffix.lower() in {".wav", ".flac"}:
        return str(source), None
    if job is not None:
        _touch(job, stage="converting", progress=24, message="正在提取 16kHz 音频")
    handle = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    handle.close()
    command = [
        "ffmpeg", "-y", "-i", str(source),
        "-ac", "1", "-ar", "16000", "-f", "wav", handle.name,
    ]
    completed = subprocess.run(command, capture_output=True, text=True)
    if completed.returncode != 0:
        os.unlink(handle.name)
        raise HTTPException(400, completed.stderr.strip() or "音频转码失败")
    return handle.name, handle.name


def _audio_seconds(wav_path: str, fallback: float | None = None) -> float | None:
    try:
        import soundfile as sf
        info = sf.info(wav_path)
        return info.frames / max(info.samplerate, 1)
    except Exception:
        return fallback


def _resolve_media(request: TranscriptionRequest, job: dict[str, Any]) -> tuple[str, list[str]]:
    cleanup: list[str] = []
    if request.media_path:
        path = Path(request.media_path).expanduser()
        if not path.is_file():
            raise HTTPException(400, "找不到音频文件")
        wav, temp = _to_wav(path, job)
        if temp:
            cleanup.append(temp)
        return wav, cleanup
    if request.media_url:
        downloaded, temp = _download_url(request.media_url, request.referer, job)
        cleanup.append(temp)
        wav, converted = _to_wav(Path(downloaded), job)
        if converted:
            cleanup.append(converted)
        return wav, cleanup
    raise HTTPException(400, "需要提供 media_path 或 media_url")


def _transcribe(request: TranscriptionRequest, job: dict[str, Any]) -> dict[str, Any]:
    wav_path, cleanup = _resolve_media(request, job)
    stop = threading.Event()
    beater: threading.Thread | None = None
    try:
        audio_seconds = _audio_seconds(wav_path, request.duration_seconds)
        job["audio_seconds"] = audio_seconds
        if _model is None:
            _touch(job, stage="loading_model", progress=30, message="正在加载 MOSS 模型，首次可能需要几分钟")
            beater = threading.Thread(
                target=_heartbeat,
                args=(job, stop, 30, 40, None, "loading_model", "正在加载模型"),
                daemon=True,
            )
            beater.start()
            _load_model()
            stop.set()
            if beater:
                beater.join(timeout=1)
            stop = threading.Event()
        _touch(job, stage="transcribing", progress=42, message="开始转录推理")
        beater = threading.Thread(
            target=_heartbeat,
            args=(job, stop, 42, 94, audio_seconds, "transcribing", "正在转录"),
            daemon=True,
        )
        beater.start()
        with _infer_lock:
            runtime = _load_model()
            result = runtime.generate(input=wav_path, cache={}, disable_pbar=True, batch_size=1)
        stop.set()
        fallback_end_ms = round((audio_seconds or 0) * 1000, 1)
        segments = _segments_from_funasr(result, fallback_end_ms)
        text = "\n".join(segment["text"] for segment in segments).strip() if len(segments) > 1 else "".join(segment["text"] for segment in segments).strip()
        return {
            "text": text,
            "segments": segments,
            "model": "MOSS-Transcribe-Diarize",
            "language": request.language or "zh",
            "device": _device_cache or "not-loaded",
            "audio_seconds": audio_seconds,
        }
    finally:
        stop.set()
        if beater:
            beater.join(timeout=1)
        for path in cleanup:
            try:
                os.unlink(path)
            except OSError:
                pass


def _run_job(job_id: str, request: TranscriptionRequest) -> None:
    job = jobs[job_id]
    _touch(job, status="running", stage="queued", progress=4, message="转录任务开始")
    try:
        result = _transcribe(request, job)
        _touch(job, status="completed", stage="completed", progress=100, message="转录完成", result=result, eta_seconds=0)
    except HTTPException as exc:
        _touch(job, status="failed", stage="failed", progress=100, error=exc.detail, message=str(exc.detail))
    except Exception as exc:
        _touch(job, status="failed", stage="failed", progress=100, error=str(exc), message=str(exc))


@app.get("/health")
def health():
    found = _model_ready()
    available = _funasr_available()
    return {
        "status": "ok" if found and _model is not None else ("loading" if _loading else "degraded"),
        "engine": "funasr" if available else "stub",
        "model": "MOSS-Transcribe-Diarize",
        "model_path": str(MOSS_MODEL_PATH),
        "model_found": found,
        "model_loaded": _model is not None,
        "model_loading": _loading,
        "preload": _should_preload(),
        "warmup_error": _warmup_error,
        "gpu": (_device_cache or "") in {"mps", "cuda"},
        "device": _device() if available else (_device_cache or "cpu"),
        "default_language": "zh",
        "funasr_available": available,
        "funasr_error": None if available else _funasr_error,
    }


@app.get("/v1/models")
def models():
    found = _model_ready()
    return {
        "data": [{
            "id": "MOSS-Transcribe-Diarize",
            "path": str(MOSS_MODEL_PATH),
            "available": found,
            "loaded": _model is not None,
            "engine": "funasr" if _funasr_available() else "stub",
        }] if found else [],
        "warning": None if found else "MOSS 模型文件未找到",
    }


@app.post("/v1/models/load")
def load():
    _load_model()
    return {"loaded": True, "device": _device(), "model_path": str(MOSS_MODEL_PATH), "model_loaded": True}


def _summary_prompt(request: SummaryRequest) -> str:
    sections = request.sections or [{"title": "摘要", "instruction": "概括视频的主要内容和结论。"}]
    lines = [
        "你是视频总结助手。根据转写用简体中文输出 Markdown，不要解释写作过程。",
        f"视频标题：{request.title or '未命名视频'}",
        f"模板：{request.template_name or '通用摘要'}",
        "必须按下面的章节输出：",
    ]
    for item in sections:
        title = str(item.get("title") or "小节").strip()
        instruction = str(item.get("instruction") or "").strip()
        fmt = str(item.get("format") or "")
        lines.append(f"## {title}")
        if instruction:
            lines.append(instruction)
        if fmt == "list":
            lines.append("请用项目符号列表。")
    lines.extend(["", "转写文本：", request.text[:14000]])
    return "\n".join(lines)


_THINK_RE = re.compile(
    r"(?is)<think>.*?</think>|<thinking>.*?</thinking>|"
    r"\[start thinking\].*?\[end thinking\]|"
    r"\[start thinking\].*?(?=\n## |\n# |\Z)|"
    r"thinking process:.*?(?=\n## |\n# |\Z)"
)
_NOISE_LINE_RE = re.compile(
    r"(?ix)^(loading\s+model\b.*|build\s*:.*|model\s*:.*|ftype\s*:.*|modalities\s*:.*|"
    r"available\s+commands:.*|/exit\b.*|/regen\b.*|/clear\b.*|/read\b.*|/glob\b.*|"
    r".*stop or exit.*|.*regenerate the last.*|.*clear the chat.*|.*add a text file.*|"
    r".*globbing pattern.*|exiting\.{0,3})$"
)
_BLOCK_ART_RE = re.compile(r"^[\s█░▒▓▄▀━─│|/\\-]+$")


def _strip_noise_lines(text: str) -> str:
    keep: list[str] = []
    for line in text.split("\n"):
        value = line.strip()
        if not value or (not _BLOCK_ART_RE.fullmatch(value) and not _NOISE_LINE_RE.fullmatch(value)):
            keep.append(line)
    return "\n".join(keep)


def _after_prompt(text: str) -> str:
    last = -1
    marker = ""
    for item in ("转写文本：", "转写："):
        index = text.rfind(item)
        if index > last:
            last = index
            marker = item
    if last < 0:
        return text
    return text[last + len(marker):].lstrip()


def _clean_summary(raw: str) -> str:
    original = (raw or "").replace("\r\n", "\n").strip()
    if not original:
        return ""
    text = _THINK_RE.sub("", _strip_noise_lines(original))
    text = _THINK_RE.sub("", _after_prompt(text)).strip()
    heading = re.search(r"(?m)^(?:##|#)\s+", text)
    if heading:
        text = text[heading.start():].strip()
    elif re.search(r"(?i)loading model|available commands:|\[start thinking\]|thinking process:", original):
        return ""
    return text.strip()


@app.post("/v1/summaries")
def summarize(request: SummaryRequest):
    if not request.text.strip():
        raise HTTPException(400, "转写文本为空")
    if not SUMMARY_MODEL_PATH.is_file():
        raise HTTPException(503, f"未找到总结模型文件：{SUMMARY_MODEL_PATH}")
    binary = LLAMA_CLI if Path(LLAMA_CLI).is_file() else (shutil.which(LLAMA_CLI) or "")
    if not binary:
        raise HTTPException(503, f"未找到 llama-cli：{LLAMA_CLI}")
    prompt_file = tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8")
    prompt_file.write(_summary_prompt(request))
    prompt_file.close()
    command = [
        binary,
        "-m", str(SUMMARY_MODEL_PATH),
        "-f", prompt_file.name,
        "-n", str(max(256, min(request.max_tokens, 2048))),
        "-c", "8192",
        "-st",
        "-ngl", "99",
        "--no-display-prompt",
        "--log-disable",
        "--simple-io",
        "--no-warmup",
        "--no-show-timings",
        "--color", "off",
        "--reasoning", "off",
        "--reasoning-budget", "0",
        "--chat-template-kwargs", '{"enable_thinking":false}',
    ]
    out_file = tempfile.NamedTemporaryFile("w+", suffix=".out", delete=False, encoding="utf-8")
    err_file = tempfile.NamedTemporaryFile("w+", suffix=".err", delete=False, encoding="utf-8")
    out_file.close()
    err_file.close()
    stdout_text = ""
    stderr_text = ""
    try:
        with open(out_file.name, "w", encoding="utf-8") as stdout, open(err_file.name, "w", encoding="utf-8") as stderr:
            completed = subprocess.run(command, stdout=stdout, stderr=stderr, timeout=600)
        stdout_text = Path(out_file.name).read_text(encoding="utf-8", errors="ignore")
        stderr_text = Path(err_file.name).read_text(encoding="utf-8", errors="ignore")
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(504, "本地总结超时") from exc
    finally:
        for path in (prompt_file.name, out_file.name, err_file.name):
            try:
                os.unlink(path)
            except OSError:
                pass
    if completed.returncode != 0:
        detail = (stderr_text or stdout_text or "llama-cli 失败").strip()[-800:]
        raise HTTPException(500, detail)
    text = _clean_summary(stdout_text or "")
    if not text:
        raise HTTPException(500, "总结模型没有返回内容")
    return {"text": text, "model": SUMMARY_MODEL_PATH.name, "engine": "llama-cli"}


@app.post("/v1/transcriptions", status_code=202)
def create(request: TranscriptionRequest):
    if not _funasr_available():
        raise HTTPException(503, "当前进程没有 FunASR。请用 Meetily 的 Python 环境启动本机 moss 服务。")
    job_id = str(uuid4())
    jobs[job_id] = {
        "id": job_id,
        "status": "queued",
        "progress": 0,
        "stage": "queued",
        "message": "已排队",
        "request": request.model_dump(),
        "updated_at": time.time(),
    }
    threading.Thread(target=_run_job, args=(job_id, request), daemon=True).start()
    return jobs[job_id]


@app.get("/v1/transcriptions/{job_id}")
def get(job_id: str):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, "任务不存在")
    return job


@app.get("/v1/transcriptions/{job_id}/result")
def result(job_id: str):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, "任务不存在")
    if job.get("status") != "completed":
        raise HTTPException(409, job.get("error") or "任务尚未完成")
    return job.get("result") or {}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=os.getenv("MOSS_HOST", "127.0.0.1"), port=int(os.getenv("MOSS_PORT", "9000")))
