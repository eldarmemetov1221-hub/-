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
      const q = document.querySelector('.bilet__question');
      return {hint, opts, question: q ? q.textContent.trim() : '',
              num: numEl ? parseInt(numEl.textContent) : null};
    }""")


async def _find_green_option(page) -> int:
    """Индекс варианта, который сайт подсветил ЗЕЛЁНЫМ (правильный) — по зелёному
    фону или по классу. -1 если зелёного нет."""
    return await page.evaluate("""() => {
      const items = [...document.querySelectorAll('.bilet__answer-item')];
      for (let i = 0; i < items.length; i++) {
        const btn = items[i].querySelector('.bilet__answer-btn') || items[i];
        for (const el of [items[i], btn]) {
          const m = getComputedStyle(el).backgroundColor.match(/(\\d+),\\s*(\\d+),\\s*(\\d+)/);
          if (m) { const r=+m[1],g=+m[2],b=+m[3];
                   if (g > 90 && g > r + 25 && g > b + 25) return i; }
        }
        if (/right|correct|true|success|green|vern|prav/i.test(items[i].className +
            ' ' + (btn.className || ''))) return i;
      }
      return -1;
    }""")


def _stems(text: str) -> set[str]:
    """Грубые «основы» слов: без окончаний (первые 6 букв), короткие/служебные
    слова выкидываем. Так «обязан» и «обязаны» совпадут (русская морфология)."""
    out = set()
    for w in _norm(text).split():
        if len(w) <= 3:
            continue
        out.add(w[:6])
    return out


def _correct_from_hint(hint: str, options: list[str], extra: str = "") -> int:
    """Правильный вариант = тот, чьи ОСНОВЫ слов сильнее всего встречаются в
    комментарии к вопросу (он цитирует верный ответ). `extra` — доп. текст
    (твоё пояснение), чтобы добить короткие варианты. -1 если неясно."""
    hint_stems = _stems(hint) | _stems(extra)
    if not hint_stems or not options:
        return -1
    scores = []
    for i, opt in enumerate(options):
        os_ = _stems(opt)
        if not os_:
            scores.append((0.0, i))
            continue
        scores.append((len(os_ & hint_stems) / len(os_), i))
    scores.sort(reverse=True)
    best_score, best = scores[0]
    second = scores[1][0] if len(scores) > 1 else 0.0
    # Берём, если уверенно (>=0.5) или заметно лучше второго варианта.
    if best_score >= 0.5 or (best_score >= 0.34 and best_score - second >= 0.2):
        return best
    return -1


async def _wait_question_ready(page, timeout: float = 10.0) -> None:
    """Ждёт отрисовки вопроса: непустой текст вопроса, минимум 2 варианта с
    текстом и подгруженный комментарий. Быстро — как только всё на месте."""
    try:
        await page.wait_for_function(
            """() => {
              const q = document.querySelector('.bilet__question');
              const btns = [...document.querySelectorAll('.bilet__answer-btn')];
              const okBtns = btns.length >= 2 && btns.every(b => b.textContent.trim().length > 0);
              const hint = document.querySelector('.bilet__hint');
              const okHint = hint && hint.textContent.trim().length > 0;
              return q && q.textContent.trim().length > 3 && okBtns && okHint;
            }""",
            timeout=int(timeout * 1000))
    except Exception:
        pass
    # Дать картинке дорисоваться.
    await asyncio.sleep(0.5)


async def _goto_next(page) -> None:
    await page.evaluate("""() => {
      const b = document.querySelector('.bilet__next-btn');
      if (b) b.click();
    }""")


async def save_images(url: str, n: int, out_dir: str, executable_path: str | None,
                      width: int, height: int) -> None:
    """Один заход на сайт: скачивает картинку каждого вопроса в out_dir/<номер>.jpg.
    Картинки-заглушки («вопрос без изображения») отсеиваются (у них одинаковые
    байты) — их auto_quiz просто не покажет."""
    import hashlib
    from playwright.async_api import async_playwright

    exe = executable_path or auto_quiz._find_chromium()
    launch_kw = {"args": ["--no-sandbox"]}
    if exe:
        launch_kw["executable_path"] = exe

    Path(out_dir).mkdir(parents=True, exist_ok=True)
    saved: dict[int, tuple[str, str]] = {}   # номер -> (путь, md5)
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(**launch_kw)
        ctx = await browser.new_context(viewport={"width": width, "height": height})
        page = await ctx.new_page()
        await _prepare_page(page, url)
        for i in range(n):
            await _wait_question_ready(page)
            info = await _read_hint_and_options(page)
            num = info["num"] or (i + 1)
            src = await page.evaluate(
                "() => { const im=document.querySelector('.bilet__img'); return im?im.src:''; }")
            if src and not src.startswith("data:"):
                try:
                    resp = await page.request.get(src)
                    data = await resp.body()
                    ext = ".png" if src.lower().split("?")[0].endswith(".png") else ".jpg"
                    p = Path(out_dir) / f"{num}{ext}"
                    p.write_bytes(data)
                    saved[num] = (str(p), hashlib.md5(data).hexdigest())
                    print(f"   Вопрос {num}: картинка сохранена", flush=True)
                except Exception as e:  # noqa: BLE001
                    print(f"   Вопрос {num}: не скачалась ({e})", flush=True)
            else:
                print(f"   Вопрос {num}: без картинки", flush=True)
            if i < n - 1:
                cur = info["num"]
                await _goto_next(page)
                try:
                    await page.wait_for_function(
                        "(k)=>{const e=document.querySelector('.bilet__qs-num');"
                        "return e && parseInt(e.textContent)!==k;}",
                        arg=cur, timeout=6000)
                except Exception:
                    pass
        await ctx.close()
        await browser.close()

    # Отсеиваем заглушки (одинаковые картинки у нескольких вопросов).
    from collections import Counter
    counts = Counter(h for _, h in saved.values())
    removed = 0
    for num, (path, h) in list(saved.items()):
        if counts[h] > 1:
            Path(path).unlink(missing_ok=True)
            removed += 1
    real = len(saved) - removed
    print(f"\n💾 Картинки в папке: {out_dir} (реальных: {real}, заглушек убрано: {removed})")
    print("   Теперь: python auto_quiz.py bilet6.txt bilet6.mp4 --speak твой.txt --images "
          f'"{out_dir}"')


async def discover_answers(page, questions, shot_dir: str | None = None) -> list[dict]:
    """Надёжно узнаёт правильные ответы: на каждом вопросе жмёт вариант (по
    подсказке-догадке, чтобы реже мазать) и читает, КАКОЙ загорелся зелёным —
    это ответ самого сайта, а значит 100% верный (числа и перемешивание не
    важны). Возвращает [{num, question, answer}] по порядку вопросов."""
    n = len(questions) or 20
    out: list[dict] = []
    for i in range(n):
        await _wait_question_ready(page)
        info = await _read_hint_and_options(page)
        extra = questions[i].narration() if i < len(questions) else ""
        guess = _correct_from_hint(info["hint"], info["opts"], extra)
        cur_num = info["num"]
        # Кликаем догадку (или первый вариант), потом смотрим, что зелёное.
        await _click_answer(page, guess if guess >= 0 else 0)
        await asyncio.sleep(0.8)
        green = await _find_green_option(page)
        if shot_dir and i == 0:
            await page.screenshot(path=str(Path(shot_dir) / "02_зелёный.png"))
        answer = info["opts"][green] if 0 <= green < len(info["opts"]) else (
                 info["opts"][guess] if guess >= 0 else "")
        out.append({"num": cur_num, "question": info["question"], "answer": answer})
        mark = f"№{green+1}" if green >= 0 else ("№%d?" % (guess+1) if guess >= 0 else "❓")
        print(f"   Вопрос {cur_num}: правильный {mark}  {answer[:55]}", flush=True)
        if i < n - 1:
            await _goto_next(page)
            try:
                await page.wait_for_function(
                    "(k)=>{const e=document.querySelector('.bilet__qs-num');"
                    "return e && parseInt(e.textContent)!==k;}",
                    arg=cur_num, timeout=6000)
            except Exception:
                pass
    return out


def _split_narration(text: str) -> tuple[str, str]:
    """Делит озвучку на «вопрос» и «остальное» по первому знаку «?» (или, если
    его нет, по первой точке). Зелёный зажигаем на стыке этих частей."""
    text = text.strip()
    m = re.search(r"[?]+", text)
    if not m:
        m = re.search(r"\.\s", text)
    if not m:
        return text, ""
    cut = m.end()
    return text[:cut].strip(), text[cut:].strip()


def _answers_cache_path(bilet: int) -> Path:
    return Path(__file__).parent / f"bilet{bilet}_answers.json"


def _load_answers(bilet: int, n: int) -> list[dict] | None:
    """Читает сохранённые ответы; None, если файла нет или он не на N вопросов."""
    p = _answers_cache_path(bilet)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, list) and len(data) >= n and all(d.get("answer") for d in data[:n]):
            return data
    except Exception:
        pass
    return None


async def _discover_pass(url: str, questions, executable_path: str | None,
                         width: int, height: int) -> list[dict]:
    """Отдельный проход без записи: собирает правильные ответы по зелёному."""
    from playwright.async_api import async_playwright
    exe = executable_path or auto_quiz._find_chromium()
    launch_kw = {"args": ["--no-sandbox"]}
    if exe:
        launch_kw["executable_path"] = exe
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(**launch_kw)
        ctx = await browser.new_context(viewport={"width": width, "height": height})
        page = await ctx.new_page()
        await _prepare_page(page, url)
        answers = await discover_answers(page, questions)
        await ctx.close()
        await browser.close()
        return answers


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

        print("── Разведка: прохожу 20 вопросов и по зелёному узнаю верные ответы ──")
        await page.screenshot(path=str(Path(shot_dir) / "01_вопрос.png"))
        answers = await discover_answers(page, questions, shot_dir=shot_dir)

        n = len(answers)
        got = sum(1 for a in answers if a["answer"])
        print(f"\nИтог: определено {got}/{n} правильных ответов (по зелёному на сайте).")
        # Сохраняем ответы в кэш — запись возьмёт их и не будет искать заново.
        cache = _answers_cache_path(int(bilet_hint))
        cache.write_text(json.dumps(answers, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"💾 Ответы сохранены: {cache.name}")

        print(f"\n🖼 Скриншоты в папке: {shot_dir}")
        print("   01_вопрос.png — вопрос 1, 02_зелёный.png — правильный вариант зелёным.")
        print("   Если ответы верные и на 02 горит зелёный — запускай БЕЗ --inspect: будет видео.")
        await ctx.close()
        await browser.close()


async def record(url: str, questions, schedule: list[dict], answers: list[dict],
                 out_dir: str, executable_path: str | None, width: int, height: int,
                 pad: float) -> str:
    """Проходит билет под расписание и пишет видео. Правильный вариант берём из
    заранее найденных ответов (`answers`, по зелёному) и жмём его по совпадению
    текста — надёжно и без красных «ошибок». Возвращает путь к .webm."""
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
            # Правильный вариант — из заранее найденных ответов (по зелёному).
            correct_text = answers[i]["answer"] if i < len(answers) else ""
            idx = _best_answer_index(correct_text, info["opts"]) if correct_text else -1
            if idx < 0:  # запас: если текст не совпал — по комментарию сайта
                idx = _correct_from_hint(info["hint"], info["opts"], q.narration())
            # Читаем вопрос — держим до момента показа ответа.
            await asyncio.sleep(seg["reveal_at"])
            # Зажигаем зелёный: жмём найденный правильный вариант.
            if idx >= 0:
                await _click_answer(page, idx)
            else:
                print(f"   ⚠️ Вопрос {expected}: не удалось определить ответ — без зелёного.")
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
                width: int, height: int, limit: int,
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

    # Правильные ответы: берём из кэша (после --inspect) или собираем разведкой.
    answers = _load_answers(bilet, len(questions))
    if answers is None:
        print("🔎 Правильные ответы ещё не собраны — прохожу билет разведкой (разово)…")
        answers = await _discover_pass(url, questions, chromium_path, width, height)
        _answers_cache_path(bilet).write_text(
            json.dumps(answers, ensure_ascii=False, indent=1), encoding="utf-8")
    else:
        print(f"💾 Правильные ответы взяты из кэша ({_answers_cache_path(bilet).name}).")

    # Предпросмотр: только первые N вопросов (быстрая проверка тайминга).
    if limit and limit > 0:
        questions = questions[:limit]
        answers = answers[:limit]
        print(f"👀 Предпросмотр: только первые {len(questions)} вопрос(ов).")

    synth = make_cached_synth(base_synth, engine, voice, rate, pitch, len(questions) * 2)

    print("⏳ Озвучиваю вопросы голосом Дмитрия…")
    tmp = Path(tempfile.mkdtemp(prefix="sitequiz_"))
    clips: list[tuple[str, float]] = []
    schedule: list[dict] = []
    for q in questions:
        # Делим озвучку на «вопрос» и «пояснение» — зелёный зажигаем ровно на
        # стыке (когда голос дочитал вопрос и переходит к ответу).
        q_part, rest_part = _split_narration(q.narration())
        a1 = await synth(q_part)
        p1 = tmp / f"q{q.number:02d}a.mp3"; p1.write_bytes(a1)
        d1 = media_duration(str(p1))
        clips.append((str(p1), d1))
        d2 = 0.0
        if rest_part:
            a2 = await synth(rest_part)
            p2 = tmp / f"q{q.number:02d}b.mp3"; p2.write_bytes(a2)
            d2 = media_duration(str(p2))
            clips.append((str(p2), d2))
        # Если знака «?» нет — откатываемся на долю reveal_frac.
        reveal_at = d1 if rest_part else round(d1 * reveal_frac, 3)
        schedule.append({"dur": round(d1 + d2, 3), "reveal_at": round(reveal_at, 3),
                         "two": bool(rest_part)})

    total_dur = sum(s["dur"] + pad for s in schedule)
    print(f"🎞 Общая длительность: {int(total_dur // 60)}:{int(total_dur % 60):02d}")

    print("🎥 Записываю прохождение билета на сайте…")
    vid_dir = str(tmp / "vid"); Path(vid_dir).mkdir(exist_ok=True)
    webm = await record(url, questions, schedule, answers, vid_dir, chromium_path,
                        width, height, pad)

    print("🔊 Склеиваю озвучку под тайминг…")
    # Пауза (pad) после последнего куска каждого вопроса.
    gaps = []
    for s in schedule:
        if s["two"]:
            gaps.append(0.0)  # между «вопросом» и «пояснением» без паузы
        gaps.append(pad)      # пауза после конца вопроса
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
    ap.add_argument("script", nargs="?", default=None,
                    help=".txt с 20 вопросами (не нужен для --save-images)")
    ap.add_argument("output", nargs="?", default=None, help="итоговый .mp4")
    ap.add_argument("--save-images", dest="save_images", default=None,
                    help="только скачать картинки билета с сайта в указанную папку (без видео)")
    ap.add_argument("--engine", choices=["edge", "silero"], default="edge")
    ap.add_argument("--voice", default="ru-RU-DmitryNeural")
    ap.add_argument("--rate", default="+0%", help="скорость речи, напр. +8%% (средний темп)")
    ap.add_argument("--pitch", default="+0Hz")
    ap.add_argument("--pad", type=float, default=1.4,
                    help="сколько секунд держать зелёный ответ после озвучки")
    ap.add_argument("--reveal", type=float, default=0.55,
                    help="запасной вариант: доля озвучки для зелёного, если в вопросе нет «?»")
    ap.add_argument("--limit", type=int, default=0,
                    help="записать только первые N вопросов (быстрый предпросмотр тайминга)")
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

    if args.save_images:
        print(f"🖼 Скачиваю картинки билета {args.bilet} с сайта в: {args.save_images}")
        asyncio.run(save_images(BILET_URL.format(n=args.bilet), 20, args.save_images,
                                args.chromium_path, args.width, args.height))
        return

    if not args.script:
        ap.error("нужен файл с вопросами (или используй --save-images для скачивания картинок)")
    text = read_text_any(args.script)
    out = args.output or f"bilet{args.bilet}_auto.mp4"

    asyncio.run(build(
        args.bilet, text, out, voice=args.voice, rate=args.rate, pitch=args.pitch,
        engine=args.engine, pad=args.pad, reveal_frac=args.reveal,
        reveal_mode=args.reveal_mode, width=args.width, height=args.height,
        limit=args.limit, chromium_path=args.chromium_path,
        do_inspect=args.inspect, shot_dir=args.shots,
    ))


if __name__ == "__main__":
    main()
