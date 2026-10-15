"""Pixiv ugoira 动图帧包合成：zip 帧包 → GIF（或 MP4，需 ffmpeg）。"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

from .common import ParseError

MAX_SIDE = 720


def compose_ugoira(zip_path: Path, frames: list[dict], out_dir: Path) -> tuple[Path, str]:
    """解压帧包并按 ugoira_meta 的帧延迟合成动图，返回 (文件, 扩展名)。

    本机有 ffmpeg 时优先合成 MP4（画质/体积更优），否则用 Pillow 合成 GIF；
    任意一步失败向上抛 ParseError，由调用方决定回退原始 zip。
    """
    carry_root = Path(tempfile.mkdtemp(prefix="ugoira-"))
    try:
        ordered = _extract_frames(zip_path, frames, carry_root)
        if shutil.which("ffmpeg"):
            try:
                return _compose_mp4(ordered, out_dir), "mp4"
            except Exception:
                pass  # ffmpeg 失败不阻塞下载，回退 GIF
        return _compose_gif(ordered, out_dir), "gif"
    finally:
        shutil.rmtree(carry_root, ignore_errors=True)


def _extract_frames(zip_path: Path, frames: list[dict], carry_root: Path) -> list[tuple[Path, int]]:
    with tempfile.TemporaryDirectory() as tmp:
        frame_dir = Path(tmp) / "frames"
        with zipfile.ZipFile(zip_path) as bundle:
            bundle.extractall(frame_dir)
        ordered: list[tuple[Path, int]] = []
        for frame in frames:
            name = str((frame or {}).get("file") or "")
            if not name:
                continue
            path = frame_dir / name
            if not path.exists():
                continue
            try:
                delay = max(10, int((frame or {}).get("delay") or 100))
            except (TypeError, ValueError):
                delay = 100
            ordered.append((path, delay))
        if not ordered:
            entries = sorted(path for path in frame_dir.rglob("*") if path.is_file())
            ordered = [(path, 100) for path in entries]
        if not ordered:
            raise ParseError("动图帧包中没有可用帧")
        # 临时目录随 with 退出销毁，把帧挪到独立目录供合成阶段读取。
        result: list[tuple[Path, int]] = []
        for index, (path, delay) in enumerate(ordered):
            target = carry_root / f"{index:05d}{path.suffix.lower() or '.png'}"
            shutil.copyfile(path, target)
            result.append((target, delay))
        return result


def _compose_gif(ordered: list[tuple[Path, int]], out_dir: Path) -> Path:
    from PIL import Image

    images: list[Image.Image] = []
    durations: list[int] = []
    for path, delay in ordered:
        with Image.open(path) as handle:
            frame = handle.convert("RGBA")
            frame = _shrink(frame)
            images.append(frame.copy())
        durations.append(delay)
    if not images:
        raise ParseError("动图帧包中没有可用帧")
    output = out_dir / f"ugoira-{abs(hash(tuple(p.name for p, _ in ordered))) % 10**10}.gif"
    images[0].save(
        output,
        save_all=True,
        append_images=images[1:],
        duration=durations,
        loop=0,
        disposal=2,
        optimize=True,
    )
    return output


def _compose_mp4(ordered: list[tuple[Path, int]], out_dir: Path) -> Path:
    listing = ordered[0][0].parent / "list.txt"
    with listing.open("w", encoding="utf-8") as handle:
        for path, delay in ordered:
            handle.write(f"file '{path.as_posix()}'\nduration {delay / 1000:.3f}\n")
        # concat demuxer 要求最后再写一次首帧收尾。
        handle.write(f"file '{ordered[-1][0].as_posix()}'\n")
    output = out_dir / f"ugoira-{abs(hash(tuple(p.name for p, _ in ordered))) % 10**10}.mp4"
    command = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "concat", "-safe", "0", "-i", str(listing),
        "-vf", f"scale='min({MAX_SIDE},iw)':-2:flags=lanczos",
        "-vsync", "vfr", "-pix_fmt", "yuv420p", "-movflags", "+faststart",
        str(output),
    ]
    subprocess.run(command, check=True, capture_output=True, timeout=300)
    if not output.exists() or not output.stat().st_size:
        raise ParseError("MP4 合成失败")
    return output


def _shrink(image):
    """GIF 体积控制：最长边超过 MAX_SIDE 时等比缩小。"""
    longest = max(image.width, image.height)
    if longest <= MAX_SIDE:
        return image
    ratio = MAX_SIDE / longest
    size = (max(1, round(image.width * ratio)), max(1, round(image.height * ratio)))
    return image.resize(size)
