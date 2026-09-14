"""Наложение озвучки на видео по сценам.

Находит моменты смены сцены в видео (ffmpeg), делит текст на абзацы и ставит
абзац №1 к сцене №1, абзац №2 к сцене №2 и т.д. Озвучка синтезируется переданной
функцией (edge-tts или клон). Работает через ffmpeg (imageio-ffmpeg), без
пересжатия видео (-c:v copy) — быстро.
"""

import asyncio
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Awaitable, Callable

SynthFn = Callable[[str], Awaitable[bytes]]  # текст -> mp3 (bytes)


def _ffmpeg() -> str:
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def _run(args: list[str]) -> str:
    """Запустить ffmpeg, вернуть stderr (там весь его вывод)."""
    proc = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace")
    return proc.stderr


def media_duration(path: str) -> float:
    """Длительность аудио/видео в секундах (из строки Duration ffmpeg)."""
    out = _run([_ffmpeg(), "-hide_banner", "-i", path])
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.?\d*)", out)
    if not m:
        return 0.0
    h, mnt, s = m.groups()
    return int(h) * 3600 + int(mnt) * 60 + float(s)


def detect_scenes(video_path: str, threshold: float = 0.3) -> list[float]:
    """Времена смены сцены (в секундах), всегда включая 0.0."""
    out = _run([
        _ffmpeg(), "-hide_banner", "-i", video_path,
        "-filter_complex", f"select='gt(scene,{threshold})',showinfo",
        "-an", "-f", "null", "-",
    ])
    times = sorted({float(t) for t in re.findall(r"pts_time:([\d.]+)", out)})
    return [0.0] + times


def split_paragraphs(text: str) -> list[str]:
    """Текст -> список абзацев (разделитель — пустая строка; если её нет — по строкам)."""
    parts = [p.strip() for p in re.split(r"\n\s*\n", text.strip()) if p.strip()]
    if len(parts) <= 1:
        parts = [ln.strip() for ln in text.strip().splitlines() if ln.strip()]
    return parts or [text.strip()]


def _assemble(video_path: str, clips: list[tuple[str, float]], out_path: str) -> None:
    """Склеить видео с аудиодорожкой, где каждый клип начинается в своё время."""
    args = [_ffmpeg(), "-hide_banner", "-y", "-i", video_path]
    for clip, _ in clips:
        args += ["-i", clip]

    filt = []
    for i, (_, start) in enumerate(clips):
        delay = max(0, int(start * 1000))
        filt.append(f"[{i + 1}:a]adelay={delay}|{delay}[a{i}]")
    labels = "".join(f"[a{i}]" for i in range(len(clips)))
    filt.append(f"{labels}amix=inputs={len(clips)}:normalize=0[aout]")

    args += [
        "-filter_complex", ";".join(filt),
        "-map", "0:v", "-map", "[aout]",
        "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart",
        out_path,
    ]
    proc = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0 or not os.path.exists(out_path):
        raise RuntimeError(proc.stderr[-1500:] or "ffmpeg не смог собрать видео")


async def build_voiced_video(
    video_path: str,
    text: str,
    synth: SynthFn,
    out_path: str,
    scene_threshold: float = 0.3,
    segment_times: list[float] | None = None,
) -> dict:
    """Собрать видео с озвучкой по сценам. Возвращает статистику (сцены/абзацы).

    Если передан `segment_times` (готовые моменты смены вопроса от умного
    детектора), используем их. Иначе — обычный поиск склеек ffmpeg.
    """
    paragraphs = split_paragraphs(text)
    if segment_times is not None:
        scenes = sorted(set(segment_times) | {0.0})
    else:
        scenes = await asyncio.to_thread(detect_scenes, video_path, scene_threshold)

    with tempfile.TemporaryDirectory() as tmp:
        clips: list[tuple[str, float]] = []
        prev_end = 0.0
        for i, para in enumerate(paragraphs):
            data = await synth(para)
            clip = os.path.join(tmp, f"p{i}.mp3")
            Path(clip).write_bytes(data)
            dur = await asyncio.to_thread(media_duration, clip)
            anchor = scenes[i] if i < len(scenes) else prev_end
            start = max(anchor, prev_end)  # не даём абзацам наезжать друг на друга
            clips.append((clip, start))
            prev_end = start + dur

        await asyncio.to_thread(_assemble, video_path, clips, out_path)

    return {"scenes": len(scenes), "paragraphs": len(paragraphs)}
