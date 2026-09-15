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


def _extract_frames(video_path: str, outdir: str, interval: float,
                    scale: str = "256:144", gray: bool = True) -> list[tuple[float, str]]:
    """Достаёт по одному кадру каждые `interval` секунд.

    По умолчанию 256x144 серый — для детекции смены по разнице пикселей.
    Для OCR передают крупный масштаб (например 720:-1), чтобы текст читался.
    """
    fps = 1.0 / interval
    pattern = os.path.join(outdir, "f%05d.png")
    vf = f"fps={fps},scale={scale}"
    cmd = [_ffmpeg(), "-hide_banner", "-y", "-i", video_path, "-vf", vf]
    if gray:
        cmd += ["-pix_fmt", "gray"]
    cmd.append(pattern)
    subprocess.run(cmd, capture_output=True)
    frames = []
    for name in sorted(os.listdir(outdir)):
        if name.endswith(".png"):
            idx = int(re.search(r"(\d+)", name).group(1))
            frames.append(((idx - 1) * interval, os.path.join(outdir, name)))
    return frames


def _setup_tesseract() -> None:
    """На Windows pytesseract часто не видит tesseract.exe в PATH — пропишем
    стандартный путь установки, если он есть."""
    try:
        import pytesseract
    except Exception:
        return
    import shutil
    if shutil.which("tesseract"):
        return
    for p in (r"C:\Program Files\Tesseract-OCR\tesseract.exe",
              r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe"):
        if os.path.exists(p):
            pytesseract.pytesseract.tesseract_cmd = p
            return


_easyocr_reader = None


def _easyocr():
    """Ленивая инициализация EasyOCR (ставится через pip, использует torch,
    отдельная программа не нужна)."""
    global _easyocr_reader
    if _easyocr_reader is None:
        import easyocr
        _easyocr_reader = easyocr.Reader(["ru"], gpu=False, verbose=False)
    return _easyocr_reader


def _has_tesseract() -> bool:
    try:
        import pytesseract
        _setup_tesseract()
        pytesseract.get_tesseract_version()
        return True
    except Exception:
        return False


def _has_easyocr() -> bool:
    import importlib.util
    return importlib.util.find_spec("easyocr") is not None


def ocr_available() -> bool:
    return _has_tesseract() or _has_easyocr()


def _ocr_text(png_path: str) -> str:
    """Текст с кадра — через Tesseract или EasyOCR (что установлено)."""
    if _has_tesseract():
        try:
            import pytesseract
            return pytesseract.image_to_string(Image.open(png_path), lang="rus+eng")
        except Exception:
            pass
    if _has_easyocr():
        try:
            return " ".join(_easyocr().readtext(png_path, detail=0))
        except Exception:
            pass
    return ""


def _ocr_number(png_path: str) -> int | None:
    """Читает номер вопроса на кадре: ищет «Вопрос N»."""
    txt = _ocr_text(png_path).lower()
    m = re.search(r"вопрос\W{0,4}(\d{1,2})", txt)
    if m:
        return int(m.group(1))
    return None


def detect_by_ocr(video_path: str, interval: float = 2.0, min_gap: float = 4.0) -> list[float]:
    """Определяет старты вопросов, читая на экране «Билет 1, Вопрос N».

    Возвращает время первого появления каждого номера по возрастанию — то есть
    реальные смены вопросов. Подсветка ответа игнорируется (номер не меняется).
    """
    _setup_tesseract()
    with tempfile.TemporaryDirectory() as tmp:
        frames = _extract_frames(video_path, tmp, interval, scale="720:-1", gray=False)
        seen: dict[int, float] = {}
        for t, path in frames:
            num = _ocr_number(path)
            if num is not None and 1 <= num <= 60 and num not in seen:
                seen[num] = t
        if not seen:
            return []
        times = [seen[n] for n in sorted(seen)]
        # склеиваем слишком близкие (случайные двойные чтения)
        out = [0.0]
        for t in times:
            if t > 0 and t - out[-1] >= min_gap:
                out.append(t)
        return out


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


def _load_region(path: str, y0: float, y1: float) -> "np.ndarray":
    """Грузит кадр и обрезает по вертикали к области вопроса (без верхней шапки
    и без нижних кнопок ответов, где загорается зелёный)."""
    arr = np.asarray(Image.open(path), dtype=np.int16)
    h = arr.shape[0]
    return arr[int(h * y0):int(h * y1), :]


def _scored_changes(video_path: str, interval: float, pixel_delta: int,
                    y0: float = 0.28, y1: float = 0.66) -> list[tuple[float, float]]:
    """Для каждого момента — насколько изменилась ОБЛАСТЬ ВОПРОСА (картинка +
    текст вопроса). Нижние кнопки ответов в расчёт не берём — поэтому подсветка
    ответа не считается сменой вопроса."""
    with tempfile.TemporaryDirectory() as tmp:
        frames = _extract_frames(video_path, tmp, interval)
        scores = []
        prev = None
        for t, path in frames:
            arr = _load_region(path, y0, y1)
            if prev is not None and t > 0:
                scores.append((t, float(np.mean(np.abs(arr - prev) > pixel_delta))))
            prev = arr
        return scores


def detect_n_changes(video_path: str, n: int, interval: float = 1.0,
                     min_gap: float = 6.0, pixel_delta: int = 30,
                     y0: float = 0.28, y1: float = 0.66) -> list[float]:
    """Найти ровно `n` сцен: берём `n-1` САМЫХ СИЛЬНЫХ смен ОБЛАСТИ ВОПРОСА
    (картинка + текст вопроса), разнесённых не ближе min_gap. Плюс старт 0.0.

    Нижние кнопки ответов в область не входят, поэтому подсветка зелёного ответа
    не считается новым вопросом. `y0`/`y1` — границы области по высоте (доли).
    """
    if n <= 1:
        return [0.0]
    scores = _scored_changes(video_path, interval, pixel_delta, y0, y1)
    picked: list[float] = []
    for t, frac in sorted(scores, key=lambda x: -x[1]):
        if frac < 0.008:
            break
        if all(abs(t - p) >= min_gap for p in picked):
            picked.append(t)
        if len(picked) >= n - 1:
            break
    return sorted([0.0] + picked)
