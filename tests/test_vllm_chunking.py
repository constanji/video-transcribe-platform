"""vLLM 分块转写纯函数单测：切分 / 重叠去重 / 说话人对齐 / 标注解析。"""
from __future__ import annotations

import struct
import sys
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from apps.api.main import (  # noqa: E402
    _align_chunk_speakers,
    _drop_overlap_duplicates,
    _segments_from_moss_text,
    _split_wav16k,
    _vllm_chunk_settings,
    _vllm_speaker_label,
)


def _make_wav(path: Path, total_sec: float, rate: int = 16000, silence_at: list[int] | None = None) -> Path:
    """生成 16bit 单声道测试 wav；silence_at 指定的整秒处写入 1s 静音。"""
    silence_at = silence_at or []
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        for sec in range(int(total_sec) + 1):
            if sec in silence_at:
                w.writeframes(b"\x00\x00" * rate)
            else:
                amp = 12000
                one = struct.pack("<h", amp)
                w.writeframes((one + struct.pack("<h", -amp)) * (rate // 2))
    return path


def test_split_short_returns_same_file(tmp: Path):
    wav = _make_wav(tmp / "short.wav", 60)
    chunks = _split_wav16k(wav, chunk_seconds=600, overlap_seconds=20)
    assert len(chunks) == 1
    assert chunks[0][0] == wav and chunks[0][1] == 0.0


def test_split_long_aligns_silence_with_true_overlap(tmp: Path):
    # 35s 音频、块长 10s、重叠 2s → 期望 5 块左右；在 10s/20s/30s 处留静音供收口
    wav = _make_wav(tmp / "long.wav", 35, silence_at=[10, 20, 30])
    chunks = _split_wav16k(wav, chunk_seconds=10, overlap_seconds=2)
    assert len(chunks) >= 3
    offsets = [off for _, off in chunks]
    assert offsets == sorted(offsets) and offsets[0] == 0.0
    # 真重叠：相邻块步距 = 块长 - 重叠（静音对齐允许 ±窗口浮动）
    stride = 10 - 2
    for a, b in zip(offsets, offsets[1:]):
        assert stride - 10 <= (b - a) <= stride + 10
        assert b - a > 0
    # 每块时长 ≈ 块长（±静音窗口），最后一块收在音频末尾
    for path, off in chunks:
        with wave.open(str(path), "rb") as w:
            dur = w.getnframes() / w.getframerate()
        assert 2 <= dur <= 10 + 10 + 1
    assert offsets[-1] + 10 >= 35 - 10  # 覆盖到末尾（容差一个窗口）


def test_drop_overlap_duplicates():
    prev = [
        {"text": "今天我们讨论一下项目进度", "start_ms": 595000.0, "end_ms": 598000.0, "speaker": 1, "is_final": True},
        {"text": "好的没问题", "start_ms": 598500.0, "end_ms": 600000.0, "speaker": 2, "is_final": True},
    ]
    curr = [
        # 与 prev[0] 时间相近、文本相同 → 应去重
        {"text": "今天我们讨论一下项目进度", "start_ms": 595200.0, "end_ms": 598100.0, "speaker": 1, "is_final": True},
        # 新内容 → 保留
        {"text": "接下来看预算", "start_ms": 601000.0, "end_ms": 603000.0, "speaker": 1, "is_final": True},
    ]
    kept = _drop_overlap_duplicates(prev, curr, overlap_start_ms=590000.0, overlap_end_ms=610000.0)
    assert [s["text"] for s in kept] == ["接下来看预算"]


def test_align_chunk_speakers_matching():
    prev = [{"text": "方案确认了吗大家", "start_ms": 595000.0, "end_ms": 599000.0, "speaker": 1, "is_final": True}]
    curr = [
        {"text": "方案确认了吗大家", "start_ms": 1000.0, "end_ms": 5000.0, "speaker": 2, "is_final": True},   # 重叠区，同一人 → S?→1
        {"text": "新说话人插话内容很长很长", "start_ms": 11000.0, "end_ms": 15000.0, "speaker": 3, "is_final": True},
    ]
    mapping, next_global = _align_chunk_speakers(prev, curr, 0.0, 10000.0, next_global=2)
    assert mapping[2] == 1          # 重叠相似 → 复用全局 1
    assert mapping[3] == 2          # 新说话人 → 新全局 2
    assert next_global == 3


def test_segments_raw_speaker():
    raw = "[0.5][S1]你好[1.2][1.8][S2]在的[3.0]"
    segs = _segments_from_moss_text(raw, 4000.0, raw_speaker=True)
    assert [(s["speaker"], s["text"]) for s in segs] == [(1, "你好"), (2, "在的")]
    assert segs[0]["start_ms"] == 500.0 and segs[1]["end_ms"] == 3000.0
    labeled = _segments_from_moss_text(raw, 4000.0)
    assert labeled[0]["speaker"] == _vllm_speaker_label(1)


def test_chunk_settings_clamped():
    assert _vllm_chunk_settings({}) == (600, 20, 4)
    assert _vllm_chunk_settings({"vllm_chunk_minutes": 999}) == (1200, 20, 4)
    assert _vllm_chunk_settings({"vllm_chunk_concurrency": 99}) == (600, 20, 8)
    assert _vllm_chunk_settings({"vllm_chunk_overlap_seconds": -5}) == (600, 0, 4)


if __name__ == "__main__":
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        test_split_short_returns_same_file(tmp)
        test_split_long_aligns_silence_with_true_overlap(tmp)
        test_drop_overlap_duplicates()
        test_align_chunk_speakers_matching()
        test_segments_raw_speaker()
        test_chunk_settings_clamped()
    print("ALL CHUNKING TESTS PASSED")
