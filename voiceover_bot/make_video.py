"""
Озвучка видео по сценам — для видео ЛЮБОГО размера (без Telegram).

Использование:
    python make_video.py видео.mp4 текст.txt [итог.mp4] [--voice ru-RU-DmitryNeural] [--rate +0%] [--pitch +0Hz]

Текст в .txt дели на абзацы (пустой строкой) — один абзац на одну сцену/задание.
Бот найдёт смены сцен и поставит абзац №1 к сцене №1, №2 к №2 и т.д.
"""

import argparse
import asyncio
import hashlib
import tempfile
from pathlib import Path

import edge_tts

import smartscenes
import videovoice

CACHE_DIR = Path(__file__).parent / ".tts_cache"


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


def _load_scene_cache(path):
    """Читает кэш моментов; при пустом/битом файле возвращает None."""
    import json
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, list) and len(data) >= 2:
            return [float(x) for x in data]
    except Exception:
        pass
    return None


def parse_timings(raw: str) -> list[float]:
    """Разбирает тайминги: строки вида 0:00, 1:12, 2:05 или просто секунды 37.
    Можно несколько в строке через пробел/запятую. Возвращает секунды со стартом 0."""
    import re
    times = []
    for tok in re.split(r"[\s,;]+", raw.strip()):
        if not tok:
            continue
        if ":" in tok:
            parts = tok.split(":")
            try:
                nums = [float(p) for p in parts]
            except ValueError:
                continue
            sec = 0.0
            for p in nums:
                sec = sec * 60 + p
            times.append(sec)
        else:
            try:
                times.append(float(tok))
            except ValueError:
                continue
    return sorted(set(times) | {0.0})


async def synth_edge(text: str, voice: str, rate: str, pitch: str, retries: int = 4) -> bytes:
    last = None
    for attempt in range(retries):
        try:
            c = edge_tts.Communicate(text, voice, rate=rate, pitch=pitch)
            with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
                path = tmp.name
            try:
                await c.save(path)
                data = Path(path).read_bytes()
                if data:
                    return data
                raise RuntimeError("пустой ответ")
            finally:
                Path(path).unlink(missing_ok=True)
        except Exception as e:  # noqa: BLE001 — сеть/таймаут/NoAudio: повторяем
            last = e
            if attempt < retries - 1:
                print(f"      ⚠️ сеть моргнула ({type(e).__name__}), повтор {attempt + 2}/{retries}…")
                await asyncio.sleep(2 * (attempt + 1))
    raise last


def _cache_key(engine: str, voice: str, rate: str, pitch: str, text: str) -> str:
    raw = f"{engine}|{voice}|{rate}|{pitch}|{text}".encode("utf-8")
    return hashlib.sha1(raw).hexdigest()


def make_cached_synth(base_synth, engine, voice, rate, pitch, total):
    """Оборачивает синтез в кэш: озвученные куски сохраняются на диск и при
    повторном/упавшем запуске берутся готовыми (не переозвучиваются)."""
    CACHE_DIR.mkdir(exist_ok=True)
    counter = {"n": 0}

    async def synth(text: str) -> bytes:
        counter["n"] += 1
        f = CACHE_DIR / (_cache_key(engine, voice, rate, pitch, text) + ".audio")
        if f.exists() and f.stat().st_size > 0:
            print(f"   🎙 {counter['n']}/{total} (из кэша)")
            return f.read_bytes()
        print(f"   🎙 {counter['n']}/{total} озвучиваю…")
        data = await base_synth(text)
        f.write_bytes(data)
        return data

    return synth


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
    ap.add_argument("--smart", action="store_true",
                    help="умный режим: естественный темп; удлиняет сцену (заморозка кадра), "
                         "если текста больше; вырезает простой, если меньше; хранит зелёный ответ")
    ap.add_argument("--pad", type=float, default=1.2,
                    help="пауза после озвучки в секундах")
    ap.add_argument("--no-auto-count", dest="auto_count", action="store_false",
                    help="не привязываться к числу абзацев, искать смены по порогу")
    ap.add_argument("--region-top", type=float, default=0.28,
                    help="верхняя граница области вопроса (доля высоты), чтобы не ловить шапку")
    ap.add_argument("--region-bottom", type=float, default=0.66,
                    help="нижняя граница области вопроса (доля высоты), чтобы не ловить кнопки ответов")
    ap.add_argument("--timings", default=None,
                    help="файл с временами начала сцен (по строке: 0:00, 0:37, 1:12 …) — 100% точно, без авто-детекта")
    args = ap.parse_args()

    video = Path(args.video)
    out = Path(args.output) if args.output else video.with_name(video.stem + "_voiced.mp4")
    text = read_text_any(args.script)

    print(f"🎬 Видео: {video}\n📝 Текст: {args.script}\n🎙 Голос: {args.voice}")
    paras = videovoice.split_paragraphs(text)

    # Кэш найденных моментов: определяем один раз, дальше берём готовое (мгновенно).
    scene_cache = None
    try:
        st = video.stat()
        ckey = hashlib.sha1(f"{video}|{st.st_size}|{int(st.st_mtime)}|ocr".encode()).hexdigest()
        scene_cache = CACHE_DIR / f"scenes_{ckey}.json"
    except OSError:
        pass

    if args.timings:
        segments = parse_timings(read_text_any(args.timings))
        print(f"⏱ Тайминги заданы вручную: {len(segments)} сцен")
    elif scene_cache and scene_cache.exists() and not args.preview and _load_scene_cache(scene_cache):
        segments = _load_scene_cache(scene_cache)
        print(f"   💾 Моменты из кэша: {len(segments)} (OCR не повторяю)")
    elif smartscenes.ocr_available():
        # Читаем номер «Вопрос N», но OCR только на кадрах смены картинки — легко.
        print("   📖 Читаю номер вопроса только на сменах кадра (лёгкая нагрузка). Это разово.")
        segments = smartscenes.detect_by_ocr(str(video), min_gap=4.0)
        if len(segments) < max(2, len(paras) // 2):
            print(f"   ⚠️ OCR нашёл мало вопросов ({len(segments)}) — откатываюсь на разницу кадров")
            segments = smartscenes.detect_n_changes(str(video), len(paras), interval=args.interval, min_gap=args.min_gap)
        else:
            CACHE_DIR.mkdir(exist_ok=True)
            if scene_cache:
                import json
                scene_cache.write_text(json.dumps(segments), encoding="utf-8")
    elif args.auto_count:
        # Знаем число вопросов (= число абзацев): берём столько же самых сильных
        # смен ОБЛАСТИ ВОПРОСА (без нижних кнопок ответов).
        print("   🎯 Ищу 20 самых сильных смен в области вопроса (подсветка ответа игнорируется)")
        segments = smartscenes.detect_n_changes(
            str(video), len(paras), interval=args.interval, min_gap=args.min_gap,
            y0=args.region_top, y1=args.region_bottom,
        )
    else:
        segments = smartscenes.detect_changes(
            str(video), interval=args.interval, threshold=args.threshold, min_gap=args.min_gap
        )
    print(f"   OCR (чтение номера вопроса): {'да' if smartscenes.ocr_available() else 'нет (по разнице кадров)'}")
    print(f"   Найдено моментов: {len(segments)} | абзацев в тексте: {len(paras)}")
    print("   Моменты (сек):", ", ".join(f"{t:.0f}" for t in segments))
    if len(segments) != len(paras):
        print("   ⚠️ Число моментов и абзацев не совпадает — проверь текст или подбери --threshold.")

    if args.preview:
        # Сохраняем по скриншоту на каждый найденный момент — чтобы глазами
        # проверить, что момент №N = вопрос №N.
        import subprocess
        import imageio_ffmpeg
        ff = imageio_ffmpeg.get_ffmpeg_exe()
        shots = video.with_name("preview_кадры")
        shots.mkdir(exist_ok=True)
        for f in shots.glob("*.jpg"):
            f.unlink()
        for i, t in enumerate(segments, 1):
            out_img = shots / f"{i:02d}_вопрос_{int(t)}сек.jpg"
            subprocess.run([ff, "-hide_banner", "-y", "-ss", f"{t:.3f}", "-i", str(video),
                            "-frames:v", "1", "-q:v", "3", str(out_img)], capture_output=True)
        print(f"🖼  Скриншоты моментов сохранены в папку: {shots}")
        print("   Открой её и проверь: 01 = 1-й вопрос, 02 = 2-й и т.д.")
        print("👁 Предпросмотр — озвучка не создавалась.")
        return

    print(f"⏳ Накладываю озвучку (движок: {args.engine})…")

    if args.engine == "silero":
        import silero_tts
        if not silero_tts.available():
            print("❌ Для silero нужен torch и soundfile. Установи: pip install soundfile")
            return
        speaker = args.voice if args.voice in silero_tts.SPEAKERS else silero_tts.MALE_DEFAULT
        print(f"   Голос Silero: {speaker} (первый запуск скачает модель ~50 МБ)")

        async def base_synth(t: str) -> bytes:
            return await asyncio.to_thread(silero_tts.synth, t, speaker)
    else:
        async def base_synth(t: str) -> bytes:
            return await synth_edge(t, args.voice, args.rate, args.pitch)

    synth = make_cached_synth(base_synth, args.engine, args.voice, args.rate, args.pitch, len(paras))

    if args.smart or args.trim:
        # Оба режима теперь режут «мёртвую» задержку в середине вопроса, но
        # ОСТАВЛЯЮТ зелёный ответ в конце. Разница: --smart может удлинять сцену
        # заморозкой (если текста больше), --trim секунды не добавляет (ускоряет).
        extend = bool(args.smart)
        print("   ✂️ Режу задержки, зелёный ответ сохраняю" +
              (", длинный текст удлиняет сцену" if extend else ", длинный текст ускоряю"))
        stats = await videovoice.build_smart_video(
            str(video), text, synth, str(out), segment_times=segments,
            pad=min(args.pad, 1.2), extend=extend, max_tempo=args.max_tempo,
        )
        print(f"✅ Готово: {out}")
        print(f"   Сцен: {stats['scenes']}, абзацев: {stats['paragraphs']}, "
              f"обрезано (задержка): {stats['trimmed']}, "
              f"удлинено: {stats['extended']}, ускорено: {stats.get('spedup', 0)}")
        return

    stats = await videovoice.build_voiced_video(
        str(video), text, synth, str(out), segment_times=segments,
        fit_to_scenes=not args.no_fit, max_tempo=args.max_tempo,
        trim_idle=False, pad=args.pad,
    )
    print(f"✅ Готово: {out}")
    print(f"   Сцен: {stats['scenes']}, абзацев: {stats['paragraphs']}, "
          f"ускорено: {stats['speedups']}, замедлено: {stats.get('slowdowns', 0)}")


if __name__ == "__main__":
    asyncio.run(main())
