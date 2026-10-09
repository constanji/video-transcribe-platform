from __future__ import annotations

import base64
import multiprocessing as mp
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
import wave
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from queue import Empty, Queue
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
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
# 好处：① 任务可以被真正 kill 取消（原来没有任何取消手段，孤儿推理会一直占着推理锁）
#      ② worker 崩了不会拖垮 HTTP 服务，且模型只需要常驻一份。
# worker_parent/child 之间用 Pipe 通信。
_worker: dict[str, Any] = {"proc": None, "conn": None}
_worker_lock = threading.Lock()
_worker_state: dict[str, Any] = {"loaded": False, "loading": False, "error": None}
# 推理全部放在一个常驻 worker 子进程：父进程只对外提供 HTTP，模型只常驻一份。
# 好处：① 长任务可被真正 kill 取消（原来没有任何取消手段，孤儿推理会一直占着推理锁）
#      ② worker 崩溃/OOM 不会拖垮 HTTP 服务，下次请求自动拉起。
_worker: dict[str, Any] = {"proc": None, "conn": None}
_worker_lock = threading.Lock()
_worker_state: dict[str, Any] = {"loaded": False, "loading": False, "error": None}
# 父进程必须有一个常驻线程消费 worker 事件，否则事件堆在管道里没人处理，
# model_loaded 状态永远更新不了（预加载会永远显示 loading）。
_worker_events: dict[str, Any] = {"queue": None}
_load_lock = threading.Lock()
_funasr_error: str | None = None
_warmup_error: str | None = None
_infer_lock = threading.Lock()  # 语义不变：同一时刻只有一个请求能用 worker
_busy_job: str | None = None


class InferBusy(Exception):
    """推理锁在限定时间内拿不到：让实时同步请求尽早失败并排队重试，而不是被长任务阻塞。"""

    def __init__(self, retry_after: float = 2.0):
        self.retry_after = retry_after
        super().__init__("infer busy")


class InferCancelled(RuntimeError):
    """任务被 DELETE 接口取消，worker 已被杀死。"""


def _check_cancelled(job: dict[str, Any] | None) -> None:
    """在耗时阶段之间检查取消标记。

    少了这一步，「转码/加载模型期间被取消」的任务会在之后把推理重新跑一遍——
    既浪费几小时 CPU，还让 busy 状态一直挂着。
    """
    if job is None:
        return
    if job.get("cancelled") or str(job.get("status") or "") == "cancelled":
        raise InferCancelled("任务已取消")


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _lock_wait() -> float:
    """实时同步请求等待推理锁的最长时间（秒）。超时即返回 busy，由客户端排队重试。"""
    return _env_float("MOSS_LOCK_WAIT", 3.0)


def _busy_retry_after() -> float:
    return _env_float("MOSS_BUSY_RETRY_AFTER", 2.0)


def _chunk_seconds() -> float:
    """超过该时长（秒）才切片并行推理。<=0 表示关闭切片。"""
    return _env_float("MOSS_CHUNK_SECONDS", 300.0)


def _chunk_workers() -> int:
    """切片并行推理的线程数。1 = 走原来的整段推理（说话人标签最连贯）。"""
    return max(1, _env_int("MOSS_CHUNK_WORKERS", 4))


def _worker_spawn_context():
    return mp.get_context("spawn")


def _model_load_timeout() -> float:
    """等待 worker 加载模型的最长时间（秒）。"""
    return _env_float("MOSS_LOAD_TIMEOUT", 900.0)


def _sync_infer_timeout() -> float:
    """实时同步单次推理的最长等待（秒）。"""
    return _env_float("MOSS_SYNC_INFER_TIMEOUT", 60.0)


def _jobs_ttl() -> float:
    """已完成/失败任务的保留时长。必须 >= 平台对批量任务的最大轮询窗口，否则会误删。"""
    return _env_float("MOSS_JOB_TTL", 7200.0)


def _jobs_max() -> int:
    return _env_int("MOSS_JOBS_MAX", 200)


def _should_preload() -> bool:
    return os.getenv("MOSS_PRELOAD", "1").strip().lower() not in {"0", "false", "no", "off"}


def _preload() -> None:
    """预热：拉起 worker 并等它在子进程里加载模型（父进程不再持有模型副本）。"""
    global _warmup_error
    try:
        _ensure_worker(load=True, wait_seconds=_model_load_timeout())
    except Exception as exc:
        _warmup_error = str(getattr(exc, "detail", None) or exc)


def _worker_alive() -> bool:
    proc = _worker.get("proc")
    return bool(proc and proc.is_alive())


def _worker_reader(conn) -> None:
    """常驻读取线程：把状态事件落到 _worker_state，把请求事件转给当前请求队列。"""
    while True:
        try:
            payload = conn.recv()
        except (EOFError, OSError):
            break
        kind = payload.get("event")
        if kind == "loading":
            _worker_state.update({"loading": True, "error": None})
        elif kind == "loaded":
            _worker_state.update({
                "loaded": True,
                "loading": False,
                "error": None,
                "device": payload.get("device") or _worker_state.get("device"),
                "rtf": payload.get("rtf") or _worker_state.get("rtf"),
            })
        elif kind == "error" and payload.get("fatal"):
            _worker_state.update({"loaded": False, "loading": False, "error": str(payload.get("error"))})
        target = _worker_events.get("queue")
        if target is not None:
            target.put(payload)
    _worker_state.update({"loaded": False, "loading": False})
    target = _worker_events.get("queue")
    if target is not None:
        target.put({"event": "error", "error": "推理子进程已退出", "fatal": True})


def _ensure_worker(load: bool = False, wait_seconds: float = 0.0) -> Any:
    """确保 worker 子进程存在（必要时让它加载模型），返回父端连接。"""
    with _worker_lock:
        if not _worker_alive():
            _worker_state.update({"loaded": False, "loading": False, "error": None})
            _worker_events["queue"] = None
            ctx = _worker_spawn_context()
            parent_conn, child_conn = ctx.Pipe(duplex=True)
            proc = ctx.Process(target=_worker_main, args=(child_conn,), daemon=True, name="moss-infer-worker")
            proc.start()
            child_conn.close()
            _worker.update({"proc": proc, "conn": parent_conn})
            threading.Thread(target=_worker_reader, args=(parent_conn,), daemon=True, name="moss-worker-reader").start()
        conn = _worker["conn"]
        if load and not _worker_state["loaded"] and not _worker_state["loading"]:
            _worker_state["loading"] = True
            conn.send({"cmd": "load"})
    if load and wait_seconds:
        # 等 loaded/error 事件（由 _worker_reader 落到 _worker_state）
        countdown = wait_seconds
        while countdown > 0 and not _worker_state["loaded"] and _worker_state["error"] is None and _worker_alive():
            time.sleep(0.2)
            countdown -= 0.2
        if not _worker_state["loaded"] and _worker_state["error"] is None:
            raise RuntimeError("等待模型加载超时")
    return conn


def _stop_worker() -> None:
    """杀掉 worker（用于取消任务）。下次请求会自动重新拉起。"""
    with _worker_lock:
        proc = _worker.get("proc")
        conn = _worker.get("conn")
        if proc and proc.is_alive():
            try:
                proc.kill()
                proc.join(timeout=5)
            except Exception:
                pass
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        _worker.update({"proc": None, "conn": None})
        _worker_state.update({"loaded": False, "loading": False, "error": None})
        if _should_preload():
            # 取消后必须立刻在后台把 worker 拉起来重载模型：
            # 否则下一个请求会看到 model_loaded=false，被平台的就绪检查直接拒掉。
            threading.Thread(target=_ensure_worker, kwargs={"load": True}, daemon=True, name="moss-worker-restart").start()


def _worker_request(command: dict[str, Any], job: dict[str, Any] | None = None, timeout: float | None = None) -> dict[str, Any]:
    """向 worker 发命令并等待终态事件。事件都由 _worker_reader 线程投递。

    job 不为 None 时按 worker 上报的分片进度更新 job["done_chunks"]/["total_chunks"]，
    并检查 job["status"] == "cancelled" —— 命中说明被 DELETE 取消，立即终止。
    """
    _check_cancelled(job)
    conn = _ensure_worker()
    box: Queue = Queue()
    previous = _worker_events.get("queue")
    _worker_events["queue"] = box
    deadline = None if timeout is None else time.time() + timeout
    try:
        conn.send(command)
    except (BrokenPipeError, OSError) as exc:
        raise RuntimeError(f"推理子进程不可用：{exc}") from exc
    try:
        while True:
            remaining = None if deadline is None else max(0.0, deadline - time.time())
            wait_step = 0.5 if remaining is None else min(0.5, remaining)
            try:
                payload = box.get(timeout=wait_step or 0.05)
            except Empty:
                if deadline is not None and time.time() > deadline:
                    raise RuntimeError("推理子进程响应超时")
                continue
            if job is not None and (job.get("cancelled") or str(job.get("status") or "") == "cancelled"):
                raise InferCancelled("任务已取消")
            kind = payload.get("event")
            if kind == "loaded":
                continue
            if kind == "loading":
                continue
            if kind == "progress":
                if job is not None:
                    job["done_chunks"] = int(payload.get("done") or 0)
                    job["total_chunks"] = int(payload.get("total") or 0)
                continue
            if kind == "error":
                if payload.get("fatal"):
                    _stop_worker()
                raise RuntimeError(str(payload.get("error") or "推理失败"))
            if kind == "done":
                if job is not None:
                    job["done_chunks"] = int(payload.get("total") or job.get("total_chunks") or 0)
                return payload
            # 其余事件忽略，继续等待终态
    finally:
        _worker_events["queue"] = previous


def _jobs_janitor() -> None:
    """定期清理内存里的任务，避免 jobs 字典无界增长。

    只淘汰「已结束(completed/failed)且超过 TTL」或「总数超上限」的最旧任务，
    不会动仍在 running/queued 的任务，因此不会影响平台正在轮询的批量任务。
    """
    while True:
        time.sleep(60)
        try:
            now = time.time()
            ttl = _jobs_ttl()
            for key, job in list(jobs.items()):
                status = str(job.get("status") or "")
                if status in {"completed", "failed", "cancelled"} and now - float(job.get("updated_at") or 0) > ttl:
                    jobs.pop(key, None)
            cap = _jobs_max()
            if len(jobs) > cap:
                oldest = sorted(jobs, key=lambda k: float(jobs[k].get("updated_at") or 0))
                for key in oldest[: len(jobs) - cap]:
                    jobs.pop(key, None)
        except Exception:
            pass


@asynccontextmanager
async def lifespan(_: FastAPI):
    if _should_preload():
        threading.Thread(target=_preload, daemon=True, name="moss-preload").start()
    threading.Thread(target=_jobs_janitor, daemon=True, name="moss-jobs-janitor").start()
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
    # 切片并行参数：不传就用环境变量 MOSS_CHUNK_SECONDS / MOSS_CHUNK_WORKERS。
    chunk_seconds: float | None = None
    chunk_workers: int | None = None


class SummaryRequest(BaseModel):
    text: str
    title: str | None = None
    template_name: str | None = None
    sections: list[dict[str, Any]] = []
    max_tokens: int = 1024


class SyncTranscriptionRequest(BaseModel):
    language: str = "zh"
    sample_rate: int = 16000
    pcm16: str | None = None
    media_path: str | None = None
    offset_ms: float = 0


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


def _dtype() -> str:
    """推理精度。

    CPU/MPS 上必须用 fp32：torch 的 CPU 后端对 bfloat16 走的是低效回退内核，
    同一段音频实测 bf16 比 fp32 慢约 4~5 倍。只有 CUDA 上 bf16 才更快。
    可通过环境变量 MOSS_DTYPE 覆盖（如 MOSS_DTYPE=fp16）。
    """
    override = os.environ.get("MOSS_DTYPE")
    if override:
        return override.strip()
    return "bf16" if _device() == "cuda" else "fp32"


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
    global _model, _warmup_error
    if _model is not None:
        return _model
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
                dtype=_dtype(),
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
        # finally: _loading = False 由发送事件的一方维护，这里不再动全局 flag


def _rtf_hint() -> float:
    """worker 侧实测实时倍数：CPU(MPS) 上 bf16 约 7~8 倍，fp32 约 1.5~2.5 倍，GPU 约 1.7。"""
    if (_device_cache or "") == "cpu":
        return 8.0 if _dtype() == "bf16" else 2.5
    return 1.7


def _slice_wav(wav_path: str, chunk_seconds: float) -> list[tuple[str, float]]:
    """按时长切片，返回 [(切片路径, 起始秒)]。

    只有一片时直接返回原文件，避免无谓的拷贝。
    """
    items: list[tuple[str, float]] = []
    with wave.open(wav_path, "rb") as source:
        rate = max(1, source.getframerate())
        frame_size = max(1, source.getnchannels() * source.getsampwidth())
        per_chunk = max(1, int(chunk_seconds * rate))
        offset_frames = 0
        while True:
            frames = source.readframes(per_chunk)
            if not frames:
                break
            handle = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
            handle.close()
            with wave.open(handle.name, "wb") as target:
                target.setnchannels(source.getnchannels())
                target.setsampwidth(source.getsampwidth())
                target.setframerate(rate)
                target.writeframes(frames)
            items.append((handle.name, offset_frames / rate))
            offset_frames += len(frames) // frame_size
    if len(items) <= 1:
        for path, _offset in items:
            try:
                if path != wav_path:
                    os.unlink(path)
            except OSError:
                pass
        return [(wav_path, 0.0)]
    return items


def _set_cpu_threads(workers: int) -> None:
    """多线程并行推理时必须压住每线程的 intra-op 线程数，否则抢同一批核心反而更慢。"""
    try:
        import torch

        cores = os.cpu_count() or 4
        torch.set_num_threads(max(1, cores // max(workers, 1)))
    except Exception:
        pass


def _infer_file(runtime: Any, wav_path: str, audio_seconds: float | None, progress=None, chunk_seconds: float | None = None, chunk_workers: int | None = None) -> tuple[list[dict[str, Any]], bool]:
    """返回 (segments, 是否走了切片并行)。

    切片并行时说话人标签是「分片内编号」，跨分片不保证同一个代号对应同一个人；
    需要最连贯的说话人编号就设 MOSS_CHUNK_WORKERS=1 走整段推理。
    """
    chunk_seconds = float(chunk_seconds) if chunk_seconds else _chunk_seconds()
    workers = max(1, int(chunk_workers)) if chunk_workers else _chunk_workers()
    total = audio_seconds or 0
    use_chunks = workers > 1 and chunk_seconds > 0 and total > chunk_seconds * 1.2
    if not use_chunks:
        result = runtime.generate(input=wav_path, cache={}, disable_pbar=True, batch_size=1)
        return _segments_from_funasr(result, round(total * 1000, 1)), False
    chunks = _slice_wav(wav_path, chunk_seconds)
    if len(chunks) <= 1:
        result = runtime.generate(input=wav_path, cache={}, disable_pbar=True, batch_size=1)
        return _segments_from_funasr(result, round(total * 1000, 1)), False
    _set_cpu_threads(workers)
    parts: list[Any] = [None] * len(chunks)
    done = 0

    def run(index: int) -> None:
        nonlocal done
        parts[index] = runtime.generate(input=chunks[index][0], cache={}, disable_pbar=True, batch_size=1)
        done += 1
        if progress:
            progress(done, len(chunks))

    try:
        with ThreadPoolExecutor(max_workers=min(workers, len(chunks))) as pool:
            list(pool.map(run, range(len(chunks))))
        merged: list[dict[str, Any]] = []
        for index, ((_path, offset), part) in enumerate(zip(chunks, parts)):
            if part is None:
                continue
            next_offset = chunks[index + 1][1] if index + 1 < len(chunks) else total
            chunk_ms = max(0.0, (next_offset - offset) * 1000)
            for item in _segments_from_funasr(part, round(chunk_ms, 1)):
                item["start_ms"] = round(float(item.get("start_ms") or 0) + offset * 1000, 1)
                item["end_ms"] = round(float(item.get("end_ms") or 0) + offset * 1000, 1)
                merged.append(item)
        merged.sort(key=lambda item: item["start_ms"])
        return merged, True
    finally:
        for path, _offset in chunks:
            try:
                os.unlink(path)
            except OSError:
                pass


def _worker_main(conn) -> None:
    """worker 子进程主循环：模型只在这里加载，行为完全由父进程发来的命令驱动。"""
    runtime = None
    try:
        while True:
            try:
                command = conn.recv()
            except (EOFError, KeyboardInterrupt):
                return
            kind = command.get("cmd")
            if kind == "stop":
                return
            if kind == "load" or runtime is None:
                try:
                    conn.send({"event": "loading"})
                    runtime = _load_model()
                    conn.send({"event": "loaded", "device": _device_cache or "cpu", "rtf": _rtf_hint()})
                except Exception as exc:
                    detail = str(getattr(exc, "detail", None) or exc)
                    conn.send({"event": "error", "error": detail, "fatal": True})
                    runtime = None
                if kind == "load":
                    continue
            if kind == "infer":
                try:
                    segments, parallel = _infer_file(
                        runtime,
                        command["wav"],
                        command.get("audio_seconds"),
                        lambda done, total: conn.send({"event": "progress", "done": done, "total": total}),
                        command.get("chunk_seconds"),
                        command.get("chunk_workers"),
                    )
                    conn.send({"event": "done", "segments": segments, "parallel": parallel})
                except Exception as exc:
                    conn.send({"event": "error", "error": str(getattr(exc, "detail", None) or exc)})
    except Exception as exc:  # noqa: BLE001 - worker 崩溃必须通知父进程
        try:
            conn.send({"event": "error", "error": str(exc), "fatal": True})
        except Exception:
            pass
    finally:
        try:
            conn.close()
        except Exception:
            pass


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
    rtf = float(_worker_state.get("rtf") or 2.5)
    estimate = max((audio_seconds or 0) * rtf, 30) if audio_seconds else 120
    while not stop.wait(1.0):
        elapsed = time.time() - t0
        # 实际比预估慢时缓慢放宽预估，避免剩余时间瞬间归零
        if elapsed > estimate:
            estimate = elapsed * 1.15
        done = int(job.get("done_chunks") or 0)
        total = int(job.get("total_chunks") or 0)
        if stage == "transcribing" and done and total and done < total:
            # 切片并行时有真实进度：按已完成分片推算剩余，比纯墙钟估算准得多。
            ratio = min(0.97, done / total)
            remain = max(1.0, (total - done) * (elapsed / done))
            suffix = f"，已完成 {done}/{total} 片"
        else:
            ratio = min(0.97, elapsed / estimate)
            remain = max(1.0, estimate - elapsed)
            suffix = ""
        progress = start + int((end - start) * ratio)
        if audio_seconds and stage == "transcribing":
            message = f"{label} {_fmt_clock(audio_seconds)} 音频，已用 {_fmt_clock(elapsed)}，预计剩余 {_fmt_clock(remain)}{suffix}"
        else:
            message = f"{label}，已用 {_fmt_clock(elapsed)}{suffix}"
        # 取消必须「粘住」：heartbeat 每秒写一次，不判断就会把 cancelled 刷回 running，
        # 让已经停掉的任务看上去还在跑、推理锁也永远释放不了。
        if job.get("cancelled"):
            return
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


def _write_pcm16_wav(path: str, pcm: bytes, sample_rate: int) -> None:
    rate = max(8000, min(int(sample_rate or 16000), 48000))
    with wave.open(path, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(pcm)


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


def _transcribe(request: TranscriptionRequest, job: dict[str, Any], fail_fast: bool = False) -> dict[str, Any]:
    wav_path, cleanup = _resolve_media(request, job)
    _check_cancelled(job)  # 转码期间可能已经被取消，别再往下跑
    stop = threading.Event()
    beater: threading.Thread | None = None
    is_sync = str(job.get("id") or "") == "sync"
    try:
        audio_seconds = _audio_seconds(wav_path, request.duration_seconds)
        job["audio_seconds"] = audio_seconds
        if not _worker_state["loaded"]:
            _touch(job, stage="loading_model", progress=30, message="正在加载 MOSS 模型，首次可能需要几分钟")
            if not is_sync:
                beater = threading.Thread(
                    target=_heartbeat,
                    args=(job, stop, 30, 40, None, "loading_model", "正在加载模型"),
                    daemon=True,
                )
                beater.start()
            _ensure_worker(load=True, wait_seconds=_model_load_timeout())
            # 模型加载失败：worker 会被停掉，这里把原因抛出来
            if not _worker_state["loaded"]:
                stop.set()
                if beater:
                    beater.join(timeout=1)
                raise HTTPException(503, _worker_state.get("error") or "转录模型加载失败")
            stop.set()
            if beater:
                beater.join(timeout=1)
            stop = threading.Event()
        _check_cancelled(job)
        _touch(job, stage="transcribing", progress=42, message="开始转录推理")
        if not is_sync:
            beater = threading.Thread(
                target=_heartbeat,
                args=(job, stop, 42, 94, audio_seconds, "transcribing", "正在转录"),
                daemon=True,
            )
            beater.start()
        chunk_options = {
            "chunk_seconds": request.chunk_seconds,
            "chunk_workers": request.chunk_workers,
        }
        if fail_fast:
            # 实时同步：限时抢锁，抢不到立刻抛 InferBusy，由客户端排队重试。
            if not _infer_lock.acquire(timeout=_lock_wait()):
                raise InferBusy(_busy_retry_after())
            try:
                payload = _worker_request(
                    {"cmd": "infer", "wav": wav_path, "audio_seconds": audio_seconds, **chunk_options},
                    timeout=_sync_infer_timeout(),
                )
            finally:
                _infer_lock.release()
        else:
            with _infer_lock:
                payload = _worker_request(
                    {"cmd": "infer", "wav": wav_path, "audio_seconds": audio_seconds, **chunk_options},
                    job=job,
                )
        stop.set()
        segments = list(payload.get("segments") or [])
        parallel = bool(payload.get("parallel"))
        fallback_end_ms = round((audio_seconds or 0) * 1000, 1)
        if not segments and audio_seconds:
            segments = _segments_from_funasr(None, fallback_end_ms)
        text = "\n".join(segment["text"] for segment in segments).strip() if len(segments) > 1 else "".join(segment["text"] for segment in segments).strip()
        return {
            "text": text,
            "segments": segments,
            "model": "MOSS-Transcribe-Diarize",
            "language": request.language or "zh",
            "device": _worker_state.get("device") or "cpu",
            "audio_seconds": audio_seconds,
            "parallel": parallel,
            "chunk_workers": _chunk_workers() if parallel else 1,
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
    global _busy_job
    job = jobs[job_id]
    _touch(job, status="running", stage="queued", progress=4, message="转录任务开始")
    _busy_job = job_id
    try:
        result = _transcribe(request, job)
        _touch(job, status="completed", stage="completed", progress=100, message="转录完成", result=result, eta_seconds=0)
    except InferCancelled:
        _touch(job, status="cancelled", stage="cancelled", progress=100, message="任务已取消", eta_seconds=0)
    except HTTPException as exc:
        _touch(job, status="failed", stage="failed", progress=100, error=exc.detail, message=str(exc.detail))
    except Exception as exc:
        _touch(job, status="failed", stage="failed", progress=100, error=str(exc), message=str(exc))
    finally:
        if _busy_job == job_id:
            _busy_job = None


@app.get("/health")
def health():
    found = _model_ready()
    available = _funasr_available()
    loaded = bool(_worker_state["loaded"])
    loading = bool(_worker_state["loading"])
    return {
        "status": "ok" if found and loaded else ("loading" if loading else "degraded"),
        "engine": "funasr" if available else "stub",
        "model": "MOSS-Transcribe-Diarize",
        "model_path": str(MOSS_MODEL_PATH),
        "model_found": found,
        "model_loaded": loaded,
        "model_loading": loading,
        "preload": _should_preload(),
        "warmup_error": _warmup_error,
        # 平台用来做「下单前 busy 预检」：有批量任务正在推理时为 True。
        "busy": _busy_job is not None,
        "busy_job": _busy_job,
        "gpu": (_worker_state.get("device") or "") in {"mps", "cuda"},
        "device": _worker_state.get("device") or "cpu",
        "default_language": "zh",
        "funasr_available": available,
        "funasr_error": None if available else _funasr_error,
        "chunk": {"enabled": _chunk_workers() > 1, "seconds": _chunk_seconds(), "workers": _chunk_workers()},
    }


@app.get("/v1/models")
def models():
    found = _model_ready()
    return {
        "data": [{
            "id": "MOSS-Transcribe-Diarize",
            "path": str(MOSS_MODEL_PATH),
            "available": found,
            "loaded": bool(_worker_state["loaded"]),
            "engine": "funasr" if _funasr_available() else "stub",
        }] if found else [],
        "warning": None if found else "MOSS 模型文件未找到",
    }


@app.post("/v1/models/load")
def load():
    _ensure_worker(load=True, wait_seconds=_model_load_timeout())
    if not _worker_state["loaded"]:
        raise HTTPException(503, _worker_state.get("error") or "转录模型加载失败")
    return {
        "loaded": True,
        "device": _worker_state.get("device") or "cpu",
        "model_path": str(MOSS_MODEL_PATH),
        "model_loaded": True,
    }


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


@app.post("/v1/transcriptions/sync")
def transcribe_sync(request: SyncTranscriptionRequest):
    if not _funasr_available():
        raise HTTPException(503, "当前进程没有 FunASR。请用 Meetily 的 Python 环境启动本机 moss 服务。")
    cleanup: list[str] = []
    media_path = request.media_path
    if request.pcm16:
        try:
            pcm = base64.b64decode(request.pcm16)
        except Exception as exc:
            raise HTTPException(400, "pcm16 不是合法的 base64") from exc
        if len(pcm) < 3200:
            return {"text": "", "segments": [], "model": "MOSS-Transcribe-Diarize", "language": request.language or "zh", "audio_seconds": 0}
        handle = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        handle.close()
        _write_pcm16_wav(handle.name, pcm, request.sample_rate)
        media_path = handle.name
        cleanup.append(handle.name)
    if not media_path:
        raise HTTPException(400, "需要提供 pcm16 或 media_path")
    job = {"id": "sync", "status": "running", "progress": 0, "stage": "queued", "message": "同步转录"}
    try:
        result = _transcribe(
            TranscriptionRequest(media_path=media_path, language=request.language or "zh"),
            job,
            fail_fast=True,
        )
        offset = float(request.offset_ms or 0)
        for segment in result.get("segments") or []:
            segment["start_ms"] = round(float(segment.get("start_ms") or 0) + offset, 1)
            segment["end_ms"] = round(float(segment.get("end_ms") or 0) + offset, 1)
        return result
    except InferBusy as exc:
        # 503 + busy 标记：与「未装 FunASR / 模型未就绪」的普通 503 区分开。
        return JSONResponse(
            status_code=503,
            content={
                "busy": True,
                "retry_after": exc.retry_after,
                "detail": "推理忙，请稍后重试",
            },
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(500, str(exc)) from exc
    finally:
        for path in cleanup:
            try:
                os.unlink(path)
            except OSError:
                pass


@app.post("/v1/transcriptions", status_code=202)
def create(request: TranscriptionRequest):
    if not _funasr_available():
        raise HTTPException(503, "当前进程没有 FunASR。请用 Meetily 的 Python 环境启动本机 moss 服务。")
    if _busy_job is not None:
        # 以前这里会默默排队，平台侧一个 59 分钟的任务能把后续所有请求拖上数小时。
        # 直接回 busy：调用方可以立刻给用户明确反馈，或者先取消旧任务再来。
        return JSONResponse(
            status_code=503,
            content={
                "busy": True,
                "retry_after": _busy_retry_after(),
                "busy_job": _busy_job,
                "detail": f"转录服务正忙于任务 {_busy_job}，请先等待或取消该任务",
            },
        )
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


@app.delete("/v1/transcriptions/{job_id}")
def cancel(job_id: str):
    """取消任务：直接杀掉正在推理的 worker 子进程，立刻释放推理锁。

    模型需要下次请求时重新加载（约 30 秒），但相比让一个跑不完的任务占用数小时要划算。
    """
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, "任务不存在")
    status = str(job.get("status") or "")
    if status in {"completed", "failed", "cancelled"}:
        return {"id": job_id, "status": status, "cancelled": False}
    # cancelled 标记必须与 status 一起打，供 heartbeat / 请求循环做「粘性取消」判断。
    job["cancelled"] = True
    job["status"] = "cancelled"
    _touch(job, message="正在取消…")
    if _busy_job == job_id:
        _stop_worker()
    return {"id": job_id, "status": "cancelled", "cancelled": True}


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
