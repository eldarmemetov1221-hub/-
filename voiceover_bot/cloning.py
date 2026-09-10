"""Клонирование голоса на XTTS v2 (Coqui TTS).

Бесплатно, локально, поддерживает русский. Тяжёлый модуль: требует torch и
модель ~2 ГБ (скачивается автоматически при первом запуске). Базовый бот
работает и без него — клонирование просто «выключено», пока не доустановлены
пакеты (см. requirements-clone.txt).
"""

import asyncio
import importlib.util
import os

# Согласие с лицензией Coqui, чтобы модель качалась без интерактивного вопроса.
os.environ.setdefault("COQUI_TOS_AGREED", "1")

MODEL_NAME = "tts_models/multilingual/multi-dataset/xtts_v2"

_tts = None
_load_error: Exception | None = None


def available() -> bool:
    """Установлены ли все зависимости для клонирования."""
    return all(
        importlib.util.find_spec(m) is not None
        for m in ("torch", "TTS", "imageio_ffmpeg")
    )


def is_loaded() -> bool:
    return _tts is not None


def _load() -> None:
    global _tts, _load_error
    if _tts is not None or _load_error is not None:
        return
    try:
        import torch
        from TTS.api import TTS

        device = "cuda" if torch.cuda.is_available() else "cpu"
        _tts = TTS(MODEL_NAME).to(device)
    except Exception as e:  # noqa: BLE001
        _load_error = e


def _synth(text: str, speaker_wav: str, language: str, out_path: str) -> None:
    _load()
    if _tts is None:
        raise RuntimeError(f"Не удалось загрузить модель XTTS: {_load_error}")
    _tts.tts_to_file(
        text=text,
        speaker_wav=speaker_wav,
        language=language,
        file_path=out_path,
        split_sentences=True,
    )


async def synthesize_clone(text: str, speaker_wav: str, language: str, out_path: str) -> None:
    """Озвучить текст клонированным голосом. Тяжёлая операция — уводим в поток,
    чтобы не блокировать бота."""
    await asyncio.to_thread(_synth, text, speaker_wav, language, out_path)
