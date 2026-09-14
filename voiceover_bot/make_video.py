"""
Озвучка видео по сценам — для видео ЛЮБОГО размера (без Telegram).

Использование:
    python make_video.py видео.mp4 текст.txt [итог.mp4] [--voice ru-RU-DmitryNeural] [--rate +0%] [--pitch +0Hz]

Текст в .txt дели на абзацы (пустой строкой) — один абзац на одну сцену/задание.
Бот найдёт смены сцен и поставит абзац №1 к сцене №1, №2 к №2 и т.д.
"""

import argparse
import asyncio
import tempfile
from pathlib import Path

import edge_tts

import smartscenes
import videovoice


async def synth_edge(text: str, voice: str, rate: str, pitch: str) -> bytes:
    c = edge_tts.Communicate(text, voice, rate=rate, pitch=pitch)
    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
        path = tmp.name
    try:
        await c.save(path)
        return Path(path).read_bytes()
    finally:
        Path(path).unlink(missing_ok=True)


async def main() -> None:
    ap = argparse.ArgumentParser(description="Озвучка видео по сценам (edge-tts)")
    ap.add_argument("video", help="исходное видео (любой размер)")
    ap.add_argument("script", help=".txt с текстом (абзацы = сцены)")
    ap.add_argument("output", nargs="?", default=None, help="итоговый файл (по умолчанию <видео>_voiced.mp4)")
    ap.add_argument("--voice", default="ru-RU-DmitryNeural", help="голос edge-tts")
    ap.add_argument("--rate", default="+0%", help="скорость, напр. +10%%")
    ap.add_argument("--pitch", default="+0Hz", help="тон, напр. -10Hz")
    ap.add_argument("--threshold", type=float, default=0.03,
                    help="чувствительность смены вопроса (меньше = больше сцен, обычно 0.03–0.08)")
    ap.add_argument("--interval", type=float, default=1.0, help="шаг проверки кадров, сек")
    ap.add_argument("--min-gap", type=float, default=2.0, help="мин. пауза между вопросами, сек")
    ap.add_argument("--preview", action="store_true",
                    help="только показать найденные моменты, без озвучки")
    args = ap.parse_args()

    video = Path(args.video)
    out = Path(args.output) if args.output else video.with_name(video.stem + "_voiced.mp4")
    text = Path(args.script).read_text(encoding="utf-8")

    print(f"🎬 Видео: {video}\n📝 Текст: {args.script}\n🎙 Голос: {args.voice}")
    print("🔎 Определяю смену вопросов на экране…")
    segments = smartscenes.detect_changes(
        str(video), interval=args.interval, threshold=args.threshold, min_gap=args.min_gap
    )
    paras = videovoice.split_paragraphs(text)
    print(f"   OCR (чтение номера вопроса): {'да' if smartscenes.ocr_available() else 'нет (по разнице кадров)'}")
    print(f"   Найдено моментов: {len(segments)} | абзацев в тексте: {len(paras)}")
    print("   Моменты (сек):", ", ".join(f"{t:.0f}" for t in segments))
    if len(segments) != len(paras):
        print("   ⚠️ Число моментов и абзацев не совпадает — проверь текст или подбери --threshold.")

    if args.preview:
        print("👁 Предпросмотр — озвучка не создавалась. Подбери --threshold и запусти без --preview.")
        return

    print("⏳ Накладываю озвучку…")

    async def synth(t: str) -> bytes:
        return await synth_edge(t, args.voice, args.rate, args.pitch)

    stats = await videovoice.build_voiced_video(
        str(video), text, synth, str(out), segment_times=segments
    )
    print(f"✅ Готово: {out}")
    print(f"   Сцен: {stats['scenes']}, абзацев озвучено: {stats['paragraphs']}")


if __name__ == "__main__":
    asyncio.run(main())
