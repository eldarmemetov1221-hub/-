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


def apply_tempo(src: str, dst: str, tempo: float) -> None:
    """Ускорить/замедлить аудио без изменения тона (ffmpeg atempo, 0.5–2.0)."""
    tempo = max(0.5, min(2.0, tempo))
    subprocess.run(
        [_ffmpeg(), "-hide_banner", "-y", "-i", src,
         "-filter:a", f"atempo={tempo:.4f}", "-b:a", "192k", dst],
        capture_output=True,
    )


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


def _assemble_trim(video_path: str, segments: list[tuple[float, float]],
                   clips: list[tuple[str, float]], out_path: str) -> None:
    """Собрать видео, оставив только куски `segments` (start,end) и склеив их,
    с озвучкой в новой шкале времени. Видео пересжимается (нужно вырезание)."""
    args = [_ffmpeg(), "-hide_banner", "-y", "-i", video_path]
    for clip, _ in clips:
        args += ["-i", clip]

    filt = []
    for i, (s, e) in enumerate(segments):
        filt.append(f"[0:v]trim=start={s:.3f}:end={e:.3f},setpts=PTS-STARTPTS[v{i}]")
    vlabels = "".join(f"[v{i}]" for i in range(len(segments)))
    filt.append(f"{vlabels}concat=n={len(segments)}:v=1:a=0[vout]")

    for i, (_, start) in enumerate(clips):
        delay = max(0, int(start * 1000))
        filt.append(f"[{i + 1}:a]adelay={delay}|{delay}[a{i}]")
    alabels = "".join(f"[a{i}]" for i in range(len(clips)))
    filt.append(f"{alabels}amix=inputs={len(clips)}:normalize=0[aout]")

    args += [
        "-filter_complex", ";".join(filt),
        "-map", "[vout]", "-map", "[aout]",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart",
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
    fit_to_scenes: bool = True,
    max_tempo: float = 1.6,
    trim_idle: bool = False,
    pad: float = 1.2,
    min_keep: float = 1.5,
) -> dict:
    """Собрать видео с озвучкой по сценам. Возвращает статистику.

    `fit_to_scenes` — если голос не успевает в сцену, ускоряем под тайминг.
    `trim_idle` — вырезать «мёртвые» паузы: держим вопрос на экране только пока
    идёт озвучка + `pad` секунд, лишнее ожидание убираем. Тогда голос читается
    в естественном темпе, а видео становится короче.
    """
    paragraphs = split_paragraphs(text)
    if segment_times is not None:
        scenes = sorted(set(segment_times) | {0.0})
    else:
        scenes = await asyncio.to_thread(detect_scenes, video_path, scene_threshold)

    video_dur = await asyncio.to_thread(media_duration, video_path)
    speedups = 0

    with tempfile.TemporaryDirectory() as tmp:
        clips: list[tuple[str, float]] = []
        segments: list[tuple[float, float]] = []
        prev_end = 0.0
        for i, para in enumerate(paragraphs):
            data = await synth(para)
            clip = os.path.join(tmp, f"p{i}.mp3")
            Path(clip).write_bytes(data)
            dur = await asyncio.to_thread(media_duration, clip)

            if i < len(scenes):
                s_start = scenes[i]
                s_end = scenes[i + 1] if i + 1 < len(scenes) else video_dur
                slot = s_end - s_start
            else:
                s_start, s_end, slot = prev_end, None, None

            if trim_idle and slot and slot > 0.3:
                # Держим сцену ровно под озвучку + pad, лишнее вырезаем.
                keep = max(min_keep, min(slot, dur + pad))
                if dur > keep + 0.05:  # даже урезанной сцены мало -> чуть ускорим
                    tempo = min(dur / keep, max_tempo)
                    fitted = os.path.join(tmp, f"f{i}.mp3")
                    await asyncio.to_thread(apply_tempo, clip, fitted, tempo)
                    clip, dur, speedups = fitted, dur / tempo, speedups + 1
                segments.append((s_start, s_start + keep))
                clips.append((clip, prev_end))
                prev_end += keep
            else:
                # Обычный режим: озвучка привязана к моменту сцены.
                start = s_start if slot else prev_end
                if fit_to_scenes and slot and slot > 0.3 and dur > slot:
                    tempo = min(dur / slot, max_tempo)
                    if tempo > 1.01:
                        fitted = os.path.join(tmp, f"f{i}.mp3")
                        await asyncio.to_thread(apply_tempo, clip, fitted, tempo)
                        clip, dur, speedups = fitted, dur / tempo, speedups + 1
                start = max(start, prev_end)
                clips.append((clip, start))
                prev_end = start + dur

        if trim_idle and segments:
            await asyncio.to_thread(_assemble_trim, video_path, segments, clips, out_path)
        else:
            await asyncio.to_thread(_assemble, video_path, clips, out_path)

    return {"scenes": len(scenes), "paragraphs": len(paragraphs),
            "speedups": speedups, "trimmed": bool(trim_idle and segments)}
