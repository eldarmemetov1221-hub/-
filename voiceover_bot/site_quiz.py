"""
Авто-запись билета ПРЯМО С САЙТА pdd-exam.ru — картинки и вид родные.

В отличие от auto_quiz.py (рисует свою страницу), этот скрипт открывает
настоящий билет на pdd-exam.ru, сам проходит его в браузере и записывает —
вопрос, картинка, варианты, зелёный правильный ответ, таймер — всё как в твоих
готовых билетах 1-5. Плюс накладывает озвучку голосом Дмитрия.

Как работает:
  1) берёт твой текст билета (вопрос, варианты, правильный ответ, пояснение);
  2) озвучивает каждый вопрос двумя кусками:
        — «вопрос + варианты» (звучит, пока читаем условие),
        — «правильный ответ + пояснение» (звучит, когда загорелся зелёный);
  3) открывает https://pdd-exam.ru/bilet/<N>/, прячет рекламу/шапку/подвал,
     проходит 20 вопросов: держит вопрос под озвучку, ведёт к правильному
     варианту и жмёт его (сайт сам подсвечивает зелёным), даёт зелёному
     повисеть, листает дальше;
  4) пишет видео экрана и накладывает озвучку -> готовый .mp4.

Запуск (на твоём компе, с VPN для edge-tts):
    pip install -r requirements-auto.txt && python -m playwright install chromium
    python site_quiz.py 6 bilet6.txt bilet6.mp4

Первый прогон лучше глянуть глазами (--inspect), чтобы я точно подогнал под
сайт:
    python site_quiz.py 6 bilet6.txt --inspect

Правильный ответ жмётся по СОВПАДЕНИЮ ТЕКСТА варианта из твоего текста с
кнопкой на сайте (порядок вариантов на сайте может отличаться — это ок). Если
совпадения нет — вопрос просто покажется под озвучку без нажатия (чтобы не
поставить сайту неверный ответ). Тогда поправь формулировку в тексте.
"""

import argparse
import asyncio
import json
import re
import subprocess
import tempfile
from pathlib import Path

import imageio_ffmpeg

import auto_quiz
from auto_quiz import concat_audio, media_duration, parse_questions
from make_video import make_cached_synth, read_text_any, synth_edge

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()

BILET_URL = "https://pdd-exam.ru/bilet/{n}/"

# CSS: прячем всё лишнее, оставляем только сам билет — чтобы запись была чистой,
# без рекламы, шапки, подвала, кнопок и водяного знака.
CLEAN_CSS = """
  .main-upline, .main-header, .main-header__menu, .main-footer,
  .main__tg-zone, .main__help-us-row, .bilet__crosslink-zone,
  .cookie-consent, .bilet__img-watermark, .bilet__comments-wrapper,
  .bilet__down-btns, .bilet__hint-wrapper, .bilet__theme-zone,
  .bilet__fullscreen-btn, .bilet__dop-zone, .bilet__feed-zone,
  [id^="yandex_rtb"], [id^="adfox"], ins, .adsbygoogle,
  .bilet__rezult-ads-zone {
      display: none !important;
  }
  html, body { background: #eef2f7 !important; }
  .bilet__wrapper { margin: 0 auto !important; padding-top: 18px !important; }
  .bilet { box-shadow: none !important; }
"""

# JS: гасит рекламные всплывашки Яндекса и отключает авто-перелистывание сайта,
# чтобы зелёный ответ успевал повисеть, а мы сами решали, когда листать.
INIT_JS = """
  window.yaContextCb = { push: function(){} };
  try {
    Object.defineProperty(window, 'PDD_USER_SETTINGS', {
      configurable: true,
      get() { return this.__pdd || {}; },
      set(v) { v = v || {}; v.auto_next_on_correct = false; this.__pdd = v; },
    });
  } catch (e) {}
"""


def _norm(s: str) -> str:
    """Нормализует текст ответа для сравнения: нижний регистр, без лишних
    пробелов, ё=е, без хвостовой пунктуации."""
    s = (s or "").lower().replace("ё", "е")
    s = re.sub(r"\s+", " ", s).strip()
    return s.strip(" .;,:!?«»\"'()")


def _best_answer_index(correct: str, buttons: list[str]) -> int:
    """Ищет на сайте кнопку, совпадающую с правильным вариантом из текста.
    Точное совпадение -> по вхождению -> по доле общих слов. -1 если не уверены."""
    target = _norm(correct)
    if not target:
        return -1
    norm = [_norm(b) for b in buttons]
    for i, b in enumerate(norm):
        if b == target:
            return i
    for i, b in enumerate(norm):
        if target and (target in b or b in target):
            return i
    # По доле общих слов.
    tw = set(target.split())
    best, best_score = -1, 0.0
    for i, b in enumerate(norm):
        bw = set(b.split())
        if not bw:
            continue
        score = len(tw & bw) / max(len(tw), 1)
        if score > best_score:
            best, best_score = i, score
    return best if best_score >= 0.6 else -1


# --------------------------------------------------------------------------- #
#  Работа со страницей билета
# --------------------------------------------------------------------------- #

async def _prepare_page(page, url: str) -> None:
    await page.add_init_script(INIT_JS)
    await page.goto(url, wait_until="domcontentloaded", timeout=60000)
    # Куки-баннер.
    try:
        await page.click("[data-cookie-consent-accept]", timeout=2500)
    except Exception:
        pass
    await page.add_style_tag(content=CLEAN_CSS)
    # Ждём, пока движок билета отрисует вопрос и варианты.
    await page.wait_for_selector(".bilet__answer-btn", timeout=30000)


async def _read_current(page) -> dict:
    """Читает текущий вопрос со страницы: номер, текст, варианты."""
    return await page.evaluate("""() => {
      const num = document.querySelector('.bilet__qs-num');
      const q = document.querySelector('.bilet__question');
      const btns = [...document.querySelectorAll('.bilet__answer-list .bilet__answer-btn')];
      return {
        num: num ? parseInt(num.textContent.trim(), 10) : null,
        question: q ? q.textContent.trim() : '',
        answers: btns.map(b => b.textContent.trim()),
      };
    }""")


async def _click_answer(page, index: int) -> bool:
    ok = await page.evaluate("""(i) => {
      const btns = [...document.querySelectorAll('.bilet__answer-list .bilet__answer-btn')];
      if (i < 0 || i >= btns.length) return false;
      btns[i].scrollIntoView({block:'center'});
      btns[i].click();
      return true;
    }""", index)
    return bool(ok)


async def _goto_next(page) -> None:
    await page.evaluate("""() => {
      const b = document.querySelector('.bilet__next-btn');
      if (b) b.click();
    }""")


async def inspect(url: str, questions, executable_path: str | None,
                  width: int, height: int, shot_dir: str) -> None:
    """Разведка: открыть сайт, показать структуру и сохранить пару скриншотов —
    чтобы точно подогнать селекторы/тайминги под сайт. Отвечает НЕ жмёт (кроме
    первого вопроса — по правильному ответу из текста, чтобы увидеть, как
    подсвечивается зелёный и перелистывает ли сайт сам)."""
    from playwright.async_api import async_playwright

    exe = executable_path or auto_quiz._find_chromium()
    launch_kw = {"args": ["--no-sandbox"]}
    if exe:
        launch_kw["executable_path"] = exe

    Path(shot_dir).mkdir(parents=True, exist_ok=True)
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(**launch_kw)
        ctx = await browser.new_context(viewport={"width": width, "height": height})
        page = await ctx.new_page()
        await _prepare_page(page, url)

        cur = await _read_current(page)
        print("── Разведка сайта ──")
        print("Вопрос №:", cur["num"])
        print("Текст:", cur["question"][:90])
        print("Варианты на сайте:")
        for i, a in enumerate(cur["answers"], 1):
            print(f"   {i}. {a[:80]}")
        await page.screenshot(path=str(Path(shot_dir) / "01_вопрос.png"))

        # Пробуем нажать правильный ответ 1-го вопроса (по тексту).
        idx = -1
        if questions:
            idx = _best_answer_index(questions[0].correct_text(), cur["answers"])
        print(f"\nПравильный (из текста): «{questions[0].correct_text() if questions else ''}» "
              f"-> кнопка №{idx + 1 if idx >= 0 else '?'}")
        if idx >= 0:
            await _click_answer(page, idx)
            await asyncio.sleep(1.2)
            after = await page.evaluate("""() => {
              const items = [...document.querySelectorAll('.bilet__answer-item')];
              const num = document.querySelector('.bilet__qs-num');
              return {
                num: num ? num.textContent.trim() : null,
                classes: items.map(el => el.className),
                btnClasses: items.map(el => (el.querySelector('.bilet__answer-btn')||{}).className||''),
              };
            }""")
            print("После клика номер вопроса:", after["num"], "(если сменился — сайт листает сам)")
            print("Классы вариантов (ищем 'зелёный'):")
            for c in after["classes"]:
                print("   li:", c)
            for c in after["btnClasses"]:
                if c:
                    print("   btn:", c)
            await page.screenshot(path=str(Path(shot_dir) / "02_после_клика.png"))
        print(f"\n🖼 Скриншоты: {shot_dir}")
        await ctx.close()
        await browser.close()


async def record(url: str, questions, schedule: list[dict], out_dir: str,
                 executable_path: str | None, width: int, height: int,
                 pad: float) -> str:
    """Проходит билет под расписание и пишет видео. Возвращает путь к .webm."""
    from playwright.async_api import async_playwright

    exe = executable_path or auto_quiz._find_chromium()
    launch_kw = {"args": ["--no-sandbox"]}
    if exe:
        launch_kw["executable_path"] = exe

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(**launch_kw)
        ctx = await browser.new_context(
            viewport={"width": width, "height": height},
            record_video_dir=out_dir,
            record_video_size={"width": width, "height": height},
        )
        page = await ctx.new_page()
        await _prepare_page(page, url)

        for i, (q, seg) in enumerate(zip(questions, schedule)):
            cur = await _read_current(page)
            expected = i + 1
            # Читаем «вопрос + варианты».
            await asyncio.sleep(seg["intro"])
            # Зажигаем зелёный: жмём правильный вариант (по совпадению текста).
            idx = _best_answer_index(q.correct_text(), cur["answers"])
            clicked = False
            if idx >= 0:
                clicked = await _click_answer(page, idx)
            else:
                print(f"   ⚠️ Вопрос {expected}: не нашёл на сайте вариант «{q.correct_text()[:40]}» "
                      f"— показываю без нажатия. Поправь формулировку в тексте.")
            # Читаем «правильный ответ + пояснение», зелёный висит.
            await asyncio.sleep(seg["answer"] + pad)
            # Листаем дальше (если не последний и сайт не перелистнул сам).
            if i < len(questions) - 1:
                now = await _read_current(page)
                if now["num"] == expected or now["num"] is None:
                    await _goto_next(page)
                    # Ждём смену вопроса.
                    try:
                        await page.wait_for_function(
                            "(n) => { const e=document.querySelector('.bilet__qs-num');"
                            " return e && parseInt(e.textContent)!==n; }",
                            arg=expected, timeout=6000)
                    except Exception:
                        pass
                _ = clicked

        await asyncio.sleep(0.4)
        video = page.video
        await ctx.close()
        await browser.close()
        return await video.path()


def mux(webm: str, audio: str, out: str, total_dur: float) -> None:
    subprocess.run(
        [FFMPEG, "-hide_banner", "-y", "-i", webm, "-i", audio,
         "-map", "0:v:0", "-map", "1:a:0",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-b:a", "192k", "-t", f"{total_dur:.3f}",
         "-movflags", "+faststart", out],
        capture_output=True, check=True,
    )


# --------------------------------------------------------------------------- #
#  Оркестратор
# --------------------------------------------------------------------------- #

async def build(bilet: int, text: str, out: str, *, voice: str, rate: str, pitch: str,
                engine: str, pad: float, width: int, height: int,
                chromium_path: str | None, do_inspect: bool, shot_dir: str) -> dict:
    questions = parse_questions(text)
    if not questions:
        raise SystemExit("❌ Не удалось разобрать вопросы из текста.")
    url = BILET_URL.format(n=bilet)
    print(f"🌐 Сайт: {url}\n📋 Вопросов в тексте: {len(questions)}")

    if do_inspect:
        await inspect(url, questions, chromium_path, width, height, shot_dir)
        return {"inspect": True}

    # Озвучка двумя кусками на вопрос (с кэшем).
    if engine == "silero":
        import silero_tts
        speaker = voice if voice in silero_tts.SPEAKERS else silero_tts.MALE_DEFAULT

        async def base_synth(t: str) -> bytes:
            return await asyncio.to_thread(silero_tts.synth, t, speaker)
    else:
        async def base_synth(t: str) -> bytes:
            return await synth_edge(t, voice, rate, pitch)

    synth = make_cached_synth(base_synth, engine, voice, rate, pitch, len(questions) * 2)

    print("⏳ Озвучиваю вопросы голосом Дмитрия…")
    tmp = Path(tempfile.mkdtemp(prefix="sitequiz_"))
    clips: list[tuple[str, float]] = []
    schedule: list[dict] = []
    for q in questions:
        intro_audio = await synth(q.narration_intro())
        pa = tmp / f"q{q.number:02d}_a.mp3"; pa.write_bytes(intro_audio)
        da = media_duration(str(pa))

        ans_text = q.narration_answer()
        if ans_text:
            ans_audio = await synth(ans_text)
            pb = tmp / f"q{q.number:02d}_b.mp3"; pb.write_bytes(ans_audio)
            db = media_duration(str(pb))
        else:
            pb, db = None, 0.0

        clips.append((str(pa), da))
        if pb:
            clips.append((str(pb), db))
        schedule.append({"intro": round(da, 3), "answer": round(db, 3)})

    total_dur = sum(s["intro"] + s["answer"] + pad for s in schedule)
    print(f"🎞 Общая длительность: {int(total_dur // 60)}:{int(total_dur % 60):02d}")

    print("🎥 Записываю прохождение билета на сайте…")
    vid_dir = str(tmp / "vid"); Path(vid_dir).mkdir(exist_ok=True)
    webm = await record(url, questions, schedule, vid_dir, chromium_path, width, height, pad)

    print("🔊 Склеиваю озвучку под тайминг…")
    # Тишина (pad) после КАЖДОГО «ответа». clips идут парами (intro, answer) —
    # паузу вставляем после каждой второй.
    gaps = []
    ci = 0
    for s in schedule:
        gaps.append(0.0)            # после intro — сразу answer
        if s["answer"] > 0:
            gaps.append(pad)        # после answer — пауза
        else:
            gaps[-1] = pad          # ответа нет: пауза после intro
    audio_track = str(tmp / "track.m4a")
    concat_audio(clips, gaps, audio_track)

    print("🎬 Собираю финальное видео…")
    mux(webm, audio_track, out, total_dur)
    print(f"✅ Готово: {out}")
    return {"questions": len(questions), "duration": total_dur, "video": out}


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Авто-запись билета прямо с сайта pdd-exam.ru + озвучка Дмитрия")
    ap.add_argument("bilet", type=int, help="номер билета (например 6)")
    ap.add_argument("script", help=".txt с 20 вопросами (вопрос, варианты, ответ, пояснение)")
    ap.add_argument("output", nargs="?", default=None, help="итоговый .mp4")
    ap.add_argument("--engine", choices=["edge", "silero"], default="edge")
    ap.add_argument("--voice", default="ru-RU-DmitryNeural")
    ap.add_argument("--rate", default="+0%", help="скорость речи, напр. +8%% (средний темп)")
    ap.add_argument("--pitch", default="+0Hz")
    ap.add_argument("--pad", type=float, default=1.4,
                    help="сколько секунд держать зелёный ответ после озвучки")
    ap.add_argument("--width", type=int, default=1280)
    ap.add_argument("--height", type=int, default=720)
    ap.add_argument("--chromium-path", default=None,
                    help="путь к chrome.exe/chromium (обычно не нужен)")
    ap.add_argument("--inspect", action="store_true",
                    help="разведка: показать, как устроен сайт, и сохранить скриншоты (без видео)")
    ap.add_argument("--shots", default="разведка_кадры",
                    help="папка для скриншотов разведки")
    args = ap.parse_args()

    text = read_text_any(args.script)
    out = args.output or f"bilet{args.bilet}_auto.mp4"

    asyncio.run(build(
        args.bilet, text, out, voice=args.voice, rate=args.rate, pitch=args.pitch,
        engine=args.engine, pad=args.pad, width=args.width, height=args.height,
        chromium_path=args.chromium_path, do_inspect=args.inspect, shot_dir=args.shots,
    ))


if __name__ == "__main__":
    main()
