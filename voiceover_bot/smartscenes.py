"""Умное определение смены вопроса/сцены на записи экрана.

Смотрит на СОДЕРЖИМОЕ кадров (а не только на резкие склейки): раз в секунду
берёт маленький кадр и ловит момент, когда картинка заметно изменилась —
именно так меняется вопрос в билетах ПДД (сменился текст/картинка вопроса).

Опционально, если установлен OCR (pytesseract + Tesseract), дополнительно читает
номер вопроса на экране («Вопрос 5») и определяет смену по номеру — самый
точный вариант. Без OCR работает по разнице кадров (никаких доп. установок,
кроме numpy+Pillow).
"""

import os
import re
import subprocess
import tempfile

import numpy as np
from PIL import Image


def _ffmpeg() -> str:
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def _extract_frames(video_path: str, outdir: str, interval: float) -> list[tuple[float, str]]:
    """Достаёт по одному серому кадру каждые `interval` секунд.

    Разрешение среднее (256x144), чтобы текст вопроса не «замывался» — тогда
    смена вопроса даёт заметную долю изменившихся пикселей.
    """
    fps = 1.0 / interval
    pattern = os.path.join(outdir, "f%05d.png")
    subprocess.run(
        [_ffmpeg(), "-hide_banner", "-y", "-i", video_path,
         "-vf", f"fps={fps},scale=256:144", "-pix_fmt", "gray", pattern],
        capture_output=True,
    )
    frames = []
    for name in sorted(os.listdir(outdir)):
        if name.endswith(".png"):
            idx = int(re.search(r"(\d+)", name).group(1))
            frames.append(((idx - 1) * interval, os.path.join(outdir, name)))
    return frames


def ocr_available() -> bool:
    try:
        import pytesseract  # noqa
        pytesseract.get_tesseract_version()
        return True
    except Exception:
        return False


def _ocr_number(png_path: str) -> int | None:
    """Пытается прочитать номер вопроса на кадре (если есть OCR)."""
    try:
        import pytesseract
        txt = pytesseract.image_to_string(Image.open(png_path), lang="rus+eng")
    except Exception:
        return None
    m = re.search(r"вопрос\D{0,3}(\d{1,2})", txt.lower())
    return int(m.group(1)) if m else None


def detect_changes(
    video_path: str,
    interval: float = 1.0,
    threshold: float = 0.03,
    min_gap: float = 2.0,
    use_ocr: bool | None = None,
    pixel_delta: int = 30,
) -> list[float]:
    """Времена (сек), когда сменился вопрос/сцена. Всегда включает 0.0.

    `threshold` — доля изменившихся пикселей (0.06 = 6%), при которой считаем,
    что вопрос сменился. `pixel_delta` — насколько должен измениться пиксель
    (0–255), чтобы считать его «изменившимся».
    """
    with tempfile.TemporaryDirectory() as tmp:
        frames = _extract_frames(video_path, tmp, interval)
        if len(frames) < 2:
            return [0.0]

        want_ocr = ocr_available() if use_ocr is None else use_ocr

        boundaries = [0.0]
        last_kept = 0.0

        if want_ocr:
            # По номеру вопроса: смена номера = новый вопрос.
            prev_num = None
            for t, path in frames:
                num = _ocr_number(path)
                if num is not None and num != prev_num:
                    if t - last_kept >= min_gap and t > 0:
                        boundaries.append(t)
                        last_kept = t
                    prev_num = num
            if len(boundaries) > 1:
                return boundaries

        # По доле изменившихся пикселей (устойчиво к статичному фону).
        prev = None
        for t, path in frames:
            arr = np.asarray(Image.open(path), dtype=np.int16)
            if prev is not None:
                changed = np.mean(np.abs(arr - prev) > pixel_delta)
                if changed > threshold and t - last_kept >= min_gap and t > 0:
                    boundaries.append(t)
                    last_kept = t
            prev = arr
        return boundaries


def _scored_changes(video_path: str, interval: float, pixel_delta: int) -> list[tuple[float, float]]:
    """Для каждого момента — насколько сильно изменился кадр (доля пикселей)."""
    with tempfile.TemporaryDirectory() as tmp:
        frames = _extract_frames(video_path, tmp, interval)
        scores = []
        prev = None
        for t, path in frames:
            arr = np.asarray(Image.open(path), dtype=np.int16)
            if prev is not None and t > 0:
                scores.append((t, float(np.mean(np.abs(arr - prev) > pixel_delta))))
            prev = arr
        return scores


def detect_n_changes(video_path: str, n: int, interval: float = 1.0,
                     min_gap: float = 6.0, pixel_delta: int = 30) -> list[float]:
    """Найти ровно `n` сцен: берём `n-1` САМЫХ СИЛЬНЫХ смен картинки (полная
    смена вопроса меняет весь экран сильнее, чем подсветка зелёного ответа),
    разнесённых не ближе min_gap. Плюс старт 0.0.

    Так число сцен = числу вопросов (абзацев), а мелкие изменения внутри вопроса
    (подсветка ответа) игнорируются.
    """
    if n <= 1:
        return [0.0]
    scores = _scored_changes(video_path, interval, pixel_delta)
    picked: list[float] = []
    for t, frac in sorted(scores, key=lambda x: -x[1]):
        if frac < 0.008:
            break
        if all(abs(t - p) >= min_gap for p in picked):
            picked.append(t)
        if len(picked) >= n - 1:
            break
    return sorted([0.0] + picked)
