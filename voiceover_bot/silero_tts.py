"""Офлайн-озвучка на Silero TTS (бесплатно, локально, без интернета после
первой загрузки модели). Отличные русские голоса, работает на процессоре.

Не зависит от серверов Microsoft — спасение, если edge-tts выдаёт
`No audio was received` (сеть/провайдер блокирует эндпоинт Microsoft).

Голоса (speaker): eugene, aidar (мужские), baya, kseniya, xenia (женские).
"""

import io
import re

MALE_DEFAULT = "eugene"
SPEAKERS = {"eugene", "aidar", "baya", "kseniya", "xenia", "random"}

_model = None


def available() -> bool:
    import importlib.util
    return all(importlib.util.find_spec(m) is not None for m in ("torch", "soundfile"))


def _load():
    global _model
    if _model is None:
        import torch
        torch.set_num_threads(max(1, (torch.get_num_threads() or 4)))
        _model, _ = torch.hub.load(
            repo_or_dir="snakers4/silero-models",
            model="silero_tts",
            language="ru",
            speaker="v4_ru",
        )
        _model.to("cpu")
    return _model


def is_loaded() -> bool:
    return _model is not None


def _split(text: str, limit: int = 800) -> list[str]:
    """Silero держит ~1000 символов за раз — режем по предложениям."""
    sents = re.split(r"(?<=[.!?…])\s+", text.strip())
    chunks, cur = [], ""
    for s in sents:
        if len(cur) + len(s) + 1 <= limit:
            cur = (cur + " " + s).strip()
        else:
            if cur:
                chunks.append(cur)
            cur = s
    if cur:
        chunks.append(cur)
    return chunks or [text.strip() or "."]


def synth(text: str, speaker: str = MALE_DEFAULT, sample_rate: int = 48000) -> bytes:
    """Озвучить текст -> wav (bytes)."""
    import numpy as np
    import soundfile as sf

    model = _load()
    if speaker not in SPEAKERS:
        speaker = MALE_DEFAULT

    parts = []
    for chunk in _split(text):
        audio = model.apply_tts(
            text=chunk, speaker=speaker, sample_rate=sample_rate,
            put_accent=True, put_yo=True,
        )
        parts.append(audio.numpy())
        parts.append(np.zeros(int(sample_rate * 0.15), dtype=parts[-1].dtype))  # пауза

    data = np.concatenate(parts) if len(parts) > 1 else parts[0]
    buf = io.BytesIO()
    sf.write(buf, data, sample_rate, format="WAV")
    return buf.getvalue()
