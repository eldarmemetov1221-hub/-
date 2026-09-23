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


async def _show_answer(page) -> bool:
    """Жмёт кнопку сайта «Показать ответ» — открывает текстовый комментарий."""
    return bool(await page.evaluate("""() => {
      const b = document.querySelector('.bilet__hint-btn');
      if (b) { b.click(); return true; }
      return false;
    }"""))


async def _read_hint_and_options(page) -> dict:
    """Читает СКРЫТЫЙ комментарий (ГИБДД/сайта) к текущему вопросу и варианты.
    В комментарии правильный ответ описан почти дословно — по нему и находим
    правильный вариант, ничего не нажимая наугад."""
    return await page.evaluate("""() => {
      const t = el => el ? el.textContent.trim() : '';
      const hint = (t(document.querySelector('.bilet__hint')) + ' ' +
                    t(document.querySelector('.bilet__expl-hint-text'))).trim();
      const opts = [...document.querySelectorAll('.bilet__answer-list .bilet__answer-btn')]
                     .map(b => b.textContent.trim());
      const numEl = document.querySelector('.bilet__qs-num');
      return {hint, opts, num: numEl ? parseInt(numEl.textContent) : null};
    }""")


def _correct_from_hint(hint: str, options: list[str]) -> int:
    """Правильный вариант = тот, чьи слова сильнее всего встречаются в
    комментарии к вопросу (комментарий цитирует верный ответ). -1 если неясно."""
    hint_words = set(_norm(hint).split())
    if not hint_words or not options:
        return -1
    best, best_score = -1, 0.0
    for i, opt in enumerate(options):
        ow = set(_norm(opt).split())
        ow = {w for w in ow if len(w) > 2}   # выкидываем короткие слова-связки
        if not ow:
            continue
        score = len(ow & hint_words) / len(ow)
        if score > best_score:
            best, best_score = i, score
    return best if best_score >= 0.5 else -1


async def _wait_question_ready(page, timeout: float = 15.0) -> None:
    """Ждёт полной отрисовки вопроса (текст, варианты, комментарий) — чтобы не
    поймать кадр со спиннером загрузки."""
    try:
        await page.wait_for_function(
            """() => {
              const q = document.querySelector('.bilet__question');
              const btns = document.querySelectorAll('.bilet__answer-btn');
              const spin = document.querySelector('.waiting__zone:not(.visually-hidden)');
              return q && q.textContent.trim().length > 3 && btns.length >= 2 && !spin;
            }""",
            timeout=int(timeout * 1000))
    except Exception:
        pass
    # Дать картинке дорисоваться.
    await asyncio.sleep(0.6)


async def _goto_next(page) -> None:
    await page.evaluate("""() => {
      const b = document.querySelector('.bilet__next-btn');
      if (b) b.click();
    }""")


async def inspect(url: str, bilet_hint: int, questions, executable_path: str | None,
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

        print("── Разведка сайта: прохожу все 20 вопросов, определяю правильные ответы ──")
        n = len(questions) or 20
        table = []
        for i in range(n):
            await _wait_question_ready(page)
            info = await _read_hint_and_options(page)
            idx = _correct_from_hint(info["hint"], info["opts"])
            table.append({"num": info["num"], "idx": idx,
                          "opts": info["opts"], "hint": info["hint"]})
            cur_num = info["num"]
            if i == 0:
                # На 1-м вопросе жмём найденный правильный вариант — проверяем,
                # что сайт красит его зелёным (это увидим на скриншоте).
                await page.screenshot(path=str(Path(shot_dir) / "01_вопрос.png"))
                if idx >= 0:
                    await _click_answer(page, idx)
                    await asyncio.sleep(1.3)
                await page.screenshot(path=str(Path(shot_dir) / "02_зелёный.png"))
            # Листаем дальше.
            if i < n - 1:
                await _goto_next(page)
                try:
                    await page.wait_for_function(
                        "(k)=>{const e=document.querySelector('.bilet__qs-num');"
                        "return e && parseInt(e.textContent)!==k;}",
                        arg=cur_num, timeout=6000)
                except Exception:
                    pass

        print(f"\nОпределено правильных ответов ({sum(1 for r in table if r['idx']>=0)}/{n}):")
        for r in table:
            mark = f"№{r['idx']+1}" if r["idx"] >= 0 else "❓ НЕ ОПРЕДЕЛЁН"
            opt = r["opts"][r["idx"]][:60] if r["idx"] >= 0 else ""
            print(f"   Вопрос {r['num']}: правильный {mark}  {opt}")

        print(f"\n🖼 Скриншоты в папке: {shot_dir}")
        print("   01_вопрос.png — вопрос 1, 02_зелёный.png — после нажатия (тут виден зелёный).")
        print("   Если ответы верные и на 02 горит зелёный — запускай БЕЗ --inspect, будет видео.")
        await ctx.close()
        await browser.close()


async def record(url: str, questions, schedule: list[dict], out_dir: str,
                 executable_path: str | None, width: int, height: int,
                 pad: float, reveal_mode: str) -> str:
    """Проходит билет под расписание и пишет видео. Возвращает путь к .webm.

    reveal_mode:
      'hint'  — зелёный через кнопку сайта «Показать ответ» (по умолчанию;
                не нужен список ответов, не ставит «ошибку» в аккаунт);
      'click' — жмём правильный вариант по совпадению текста (нужен структурный
                текст с вариантами и «Ответ: N»).
    """
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
            expected = i + 1
            await _wait_question_ready(page)
            info = await _read_hint_and_options(page)
            # Находим правильный вариант: 'text' — по твоему тексту (если есть
            # «Ответ: N»); иначе (auto) — по скрытому комментарию сайта.
            idx = -1
            if reveal_mode == "text":
                idx = _best_answer_index(q.correct_text(), info["opts"])
            if idx < 0:
                idx = _correct_from_hint(info["hint"], info["opts"])
            # Читаем вопрос — держим до момента показа ответа.
            await asyncio.sleep(seg["reveal_at"])
            # Зажигаем зелёный: жмём найденный правильный вариант.
            if idx >= 0:
                await _click_answer(page, idx)
            else:
                print(f"   ⚠️ Вопрос {expected}: не смог уверенно определить правильный "
                      f"вариант — показываю без зелёного.")
            # Дочитываем пояснение, зелёный висит.
            await asyncio.sleep(seg["dur"] - seg["reveal_at"] + pad)
            # Листаем дальше (если не последний и сайт не перелистнул сам).
            if i < len(questions) - 1:
                now = await _read_current(page)
                if now["num"] == expected or now["num"] is None:
                    await _goto_next(page)
                    try:
                        await page.wait_for_function(
                            "(n) => { const e=document.querySelector('.bilet__qs-num');"
                            " return e && parseInt(e.textContent)!==n; }",
                            arg=expected, timeout=6000)
                    except Exception:
                        pass

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
                engine: str, pad: float, reveal_frac: float, reveal_mode: str,
                width: int, height: int,
                chromium_path: str | None, do_inspect: bool, shot_dir: str) -> dict:
    questions = parse_questions(text)
    if not questions:
        raise SystemExit("❌ Не удалось разобрать вопросы из текста.")
    url = BILET_URL.format(n=bilet)
    print(f"🌐 Сайт: {url}\n📋 Вопросов в тексте: {len(questions)}")

    if do_inspect:
        await inspect(url, bilet, questions, chromium_path, width, height, shot_dir)
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

    synth = make_cached_synth(base_synth, engine, voice, rate, pitch, len(questions))

    print("⏳ Озвучиваю вопросы голосом Дмитрия…")
    tmp = Path(tempfile.mkdtemp(prefix="sitequiz_"))
    clips: list[tuple[str, float]] = []
    schedule: list[dict] = []
    for q in questions:
        audio = await synth(q.narration())
        p = tmp / f"q{q.number:02d}.mp3"; p.write_bytes(audio)
        dur = media_duration(str(p))
        clips.append((str(p), dur))
        # Зелёный показываем на доле reveal_frac от озвучки.
        schedule.append({"dur": round(dur, 3),
                         "reveal_at": round(min(dur, dur * reveal_frac), 3)})

    total_dur = sum(s["dur"] + pad for s in schedule)
    print(f"🎞 Общая длительность: {int(total_dur // 60)}:{int(total_dur % 60):02d}")

    print("🎥 Записываю прохождение билета на сайте…")
    vid_dir = str(tmp / "vid"); Path(vid_dir).mkdir(exist_ok=True)
    webm = await record(url, questions, schedule, vid_dir, chromium_path, width, height,
                        pad, reveal_mode)

    print("🔊 Склеиваю озвучку под тайминг…")
    gaps = [pad] * len(clips)
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
    ap.add_argument("--reveal", type=float, default=0.55,
                    help="в какой доле озвучки показать зелёный (0.55 = чуть за серединой)")
    ap.add_argument("--reveal-mode", choices=["hint", "click"], default="hint",
                    help="hint = кнопкой сайта «Показать ответ» (по умолчанию); "
                         "click = жать правильный вариант (нужен текст с «Ответ: N»)")
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
        engine=args.engine, pad=args.pad, reveal_frac=args.reveal,
        reveal_mode=args.reveal_mode, width=args.width, height=args.height,
        chromium_path=args.chromium_path, do_inspect=args.inspect, shot_dir=args.shots,
    ))


if __name__ == "__main__":
    main()
