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


def read_text_any(path: str) -> str:
    """Читает .txt в любой распространённой кодировке (UTF-8/16, Windows-1251).

    UTF-16 определяем только по метке BOM (иначе он декодирует что угодно в
    мусор), затем строгий UTF-8, затем cp1251 (обычный ANSI-Блокнот).
    """
    raw = Path(path).read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw.decode("utf-8-sig")
    if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
        return raw.decode("utf-16")  # сам уберёт метку BOM
    for enc in ("utf-8", "cp1251", "cp1252"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


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
    ap.add_argument("--engine", choices=["edge", "silero"], default="edge",
                    help="движок озвучки: edge (онлайн Microsoft) или silero (офлайн, если edge блокируется)")
    ap.add_argument("--voice", default="ru-RU-DmitryNeural",
                    help="голос: для edge — ru-RU-DmitryNeural; для silero — eugene/aidar (муж.)")
    ap.add_argument("--rate", default="+0%", help="скорость, напр. +10%%")
    ap.add_argument("--pitch", default="+0Hz", help="тон, напр. -10Hz")
    ap.add_argument("--threshold", type=float, default=0.03,
                    help="чувствительность смены вопроса (меньше = больше сцен, обычно 0.03–0.08)")
    ap.add_argument("--interval", type=float, default=1.0, help="шаг проверки кадров, сек")
    ap.add_argument("--min-gap", type=float, default=2.0, help="мин. пауза между вопросами, сек")
    ap.add_argument("--preview", action="store_true",
                    help="только показать найденные моменты, без озвучки")
    ap.add_argument("--max-tempo", type=float, default=1.6,
                    help="макс. ускорение голоса, чтобы влезть в сцену (1.6 = до +60%)")
    ap.add_argument("--no-fit", action="store_true",
                    help="не ускорять под тайминг сцены (читать в обычном темпе)")
    ap.add_argument("--trim", action="store_true",
                    help="вырезать мёртвые паузы: держать вопрос только пока идёт озвучка + пауза")
    ap.add_argument("--pad", type=float, default=1.2,
                    help="сколько секунд оставлять после озвучки перед вырезкой (с --trim)")
    args = ap.parse_args()

    video = Path(args.video)
    out = Path(args.output) if args.output else video.with_name(video.stem + "_voiced.mp4")
    text = read_text_any(args.script)

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

    print(f"⏳ Накладываю озвучку (движок: {args.engine})…")

    if args.engine == "silero":
        import silero_tts
        if not silero_tts.available():
            print("❌ Для silero нужен torch и soundfile. Установи: pip install soundfile")
            return
        speaker = args.voice if args.voice in silero_tts.SPEAKERS else silero_tts.MALE_DEFAULT
        print(f"   Голос Silero: {speaker} (первый запуск скачает модель ~50 МБ)")

        async def synth(t: str) -> bytes:
            return await asyncio.to_thread(silero_tts.synth, t, speaker)
    else:
        async def synth(t: str) -> bytes:
            return await synth_edge(t, args.voice, args.rate, args.pitch)

    if args.trim:
        print("   ✂️ Режим обрезки пауз включён (видео пересжимается, это дольше)")
    stats = await videovoice.build_voiced_video(
        str(video), text, synth, str(out), segment_times=segments,
        fit_to_scenes=not args.no_fit, max_tempo=args.max_tempo,
        trim_idle=args.trim, pad=args.pad,
    )
    print(f"✅ Готово: {out}")
    print(f"   Сцен: {stats['scenes']}, абзацев озвучено: {stats['paragraphs']}, "
          f"ускорено под тайминг: {stats['speedups']}, обрезка пауз: {'да' if stats['trimmed'] else 'нет'}")


if __name__ == "__main__":
    asyncio.run(main())
