"""
Авто-запись озвученного билета ПДД — БЕЗ ручной записи экрана.

Идея: не надо больше 15 минут вручную читать вопросы, водить мышкой и жать
ответы, переписывая дубли. Ты даёшь готовый текст билета (20 вопросов: сам
вопрос, варианты, правильный ответ и пояснение — по заголовкам), а скрипт:

  1) разбирает текст на 20 вопросов;
  2) озвучивает каждый голосом Дмитрия (edge-tts, ru-RU-DmitryNeural),
     естественный «средний» темп, паузы по знакам препинания — как ты просил;
  3) сам «проходит» билет в браузере (Playwright): показывает вопрос, ведёт
     курсор к правильному варианту, зажигает его ЗЕЛЁНЫМ, идёт таймер и
     прогресс «Вопрос N/20» — всё как в настоящей записи экрана;
  4) держит каждый вопрос ровно столько, сколько длится его озвучка (+пауза),
     поэтому голос НИКОГДА не наезжает на следующий вопрос — синхрон идеальный
     по построению, без ручных таймкодов;
  5) пишет видео и накладывает озвучку -> готовый .mp4.

Запуск (на твоём компе, с включённым VPN для edge-tts):

    pip install playwright && python -m playwright install chromium
    python auto_quiz.py bilet1.txt bilet1.mp4

Формат текста (по заголовкам — гибко, см. parse_questions):

    Вопрос 1
    Разрешён ли обгон в этом месте?
    1. Да
    2. Нет
    Ответ: 2
    Пояснение: Обгон запрещён на пешеходном переходе.

    Вопрос 2
    ...

Заголовок вопроса — «Вопрос 1», «1.», «1)», «Билет 5 вопрос 1», «###» и т.п.
Варианты — строки, начинающиеся с «1.», «2)», «- », «а)». «Ответ:» — номер
или текст правильного варианта. «Пояснение:»/«Объяснение:» — пояснение.
Если вариантов нет — просто покажем вопрос и пояснение, а озвучим весь блок.
"""

import argparse
import asyncio
import json
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import imageio_ffmpeg

# Переиспользуем озвучку и кэш из make_video, чтобы голос/логика были едиными.
from make_video import (
    make_cached_synth,
    read_text_any,
    synth_edge,
)

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()


# --------------------------------------------------------------------------- #
#  Разбор текста билета на вопросы
# --------------------------------------------------------------------------- #

@dataclass
class Question:
    number: int
    text: str
    options: list[str] = field(default_factory=list)
    correct: int = 0            # индекс правильного варианта (0-based); -1 если неизвестно
    explanation: str = ""

    def narration(self) -> str:
        """Что произносит Дмитрий: вопрос -> (варианты, если есть) -> правильный
        ответ -> пояснение. Пунктуация оставлена — edge-tts сам делает паузы."""
        parts = [self.text.strip()]
        if self.options:
            # Читаем варианты по одному, чтобы шли естественные паузы.
            for i, opt in enumerate(self.options, 1):
                parts.append(f"{i}. {opt.strip()}")
        if 0 <= self.correct < len(self.options):
            parts.append(f"Правильный ответ: {self.options[self.correct].strip()}.")
        if self.explanation:
            parts.append(self.explanation.strip())
        # Точки между блоками = аккуратные паузы «среднего» темпа.
        def close(p: str) -> str:
            p = p.strip()
            return p if p[-1:] in ".!?…:;" else p + "."
        return "  ".join(close(p) for p in parts if p.strip())


_HEADING = re.compile(
    r"""^\s*(?:
        \#{1,6}\s*                      # markdown ###
      | (?:билет\s*\d+\s*)?             # 'Билет 5 '
        вопрос\s*№?\s*(\d{1,2})\b       # 'Вопрос 5' / 'Вопрос №5'
      | (\d{1,2})\s*[.)]\s*$            # '5.' или '5)' на отдельной строке
    )""",
    re.IGNORECASE | re.VERBOSE,
)

# Заголовок «Вопрос N ...» может стоять на той же строке, что и текст вопроса.
_HEADING_INLINE = re.compile(
    r"^\s*(?:билет\s*\d+\s*)?вопрос\s*№?\s*(\d{1,2})\b[\s.):-]*",
    re.IGNORECASE,
)

_OPTION = re.compile(r"^\s*(?:(\d{1,2})|[а-яa-z])\s*[.)]\s+(.*\S)\s*$", re.IGNORECASE)
_DASH_OPTION = re.compile(r"^\s*[-–—•]\s+(.*\S)\s*$")
_ANSWER = re.compile(r"^\s*(?:правильный\s+)?ответ\s*[:\-—]\s*(.+\S)\s*$", re.IGNORECASE)
_EXPLAIN = re.compile(r"^\s*(?:пояснение|объяснение|коммент(?:арий)?)\s*[:\-—]\s*(.+\S)\s*$",
                      re.IGNORECASE)


def _split_blocks(text: str) -> list[list[str]]:
    """Режет текст на блоки по заголовкам вопросов. Если заголовков нет —
    режет по пустым строкам (один абзац = один вопрос)."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    blocks: list[list[str]] = []
    cur: list[str] = []
    seen_heading = False

    for line in lines:
        if _HEADING.match(line) or _HEADING_INLINE.match(line):
            seen_heading = True
            if cur:
                blocks.append(cur)
            cur = [line]
        else:
            cur.append(line)
    if cur:
        blocks.append(cur)

    if seen_heading:
        # Первый блок до самого первого заголовка (шапка/пусто) — выкидываем.
        if blocks and not (_HEADING.match(blocks[0][0]) or _HEADING_INLINE.match(blocks[0][0])):
            blocks = blocks[1:]
        return [b for b in blocks if any(s.strip() for s in b)]

    # Заголовков нет — делим по пустым строкам.
    para_blocks: list[list[str]] = []
    cur = []
    for line in lines:
        if line.strip():
            cur.append(line)
        elif cur:
            para_blocks.append(cur)
            cur = []
    if cur:
        para_blocks.append(cur)
    return para_blocks


def _match_answer(ans: str, options: list[str]) -> int:
    """Определяет индекс правильного варианта: по номеру ('2'), по букве ('б')
    или по совпадению текста."""
    ans = ans.strip()
    m = re.match(r"^\s*(\d{1,2})", ans)
    if m and options:
        idx = int(m.group(1)) - 1
        if 0 <= idx < len(options):
            return idx
    # Буква: а/б/в/г или a/b/c/d
    letters = "абвгдеabcdef"
    low = ans.lower()
    if low and low[0] in letters:
        idx = letters.index(low[0]) % 6
        if 0 <= idx < len(options):
            return idx
    # По тексту.
    for i, opt in enumerate(options):
        if opt.strip().lower() == low or low in opt.strip().lower():
            return i
    return -1


def parse_questions(text: str) -> list[Question]:
    """Текст билета -> список вопросов (вопрос, варианты, правильный, пояснение)."""
    out: list[Question] = []
    for bi, block in enumerate(_split_blocks(text), 1):
        number = bi
        q_lines: list[str] = []
        options: list[str] = []
        answer_raw = ""
        explanation = ""
        first = block[0]

        # Номер вопроса из заголовка, если есть.
        mnum = (_HEADING.match(first) or _HEADING_INLINE.match(first))
        if mnum:
            for g in mnum.groups():
                if g and g.isdigit():
                    number = int(g)
                    break
            # Если «Вопрос N» стоит на одной строке с текстом — оставляем хвост.
            inline = _HEADING_INLINE.match(first)
            rest = ""
            if inline:
                rest = first[inline.end():].strip()
            body = ([rest] if rest else []) + block[1:]
        else:
            body = block

        for line in body:
            if not line.strip():
                continue
            ma = _ANSWER.match(line)
            me = _EXPLAIN.match(line)
            mo = _OPTION.match(line)
            md = _DASH_OPTION.match(line)
            if ma:
                answer_raw = ma.group(1)
            elif me:
                explanation = (explanation + " " + me.group(1)).strip()
            elif mo:
                options.append(mo.group(2))
            elif md:
                options.append(md.group(1))
            elif options or answer_raw or explanation:
                # Строки после вариантов/ответа считаем продолжением пояснения.
                explanation = (explanation + " " + line.strip()).strip()
            else:
                q_lines.append(line.strip())

        q_text = " ".join(q_lines).strip() or f"Вопрос {number}"
        correct = _match_answer(answer_raw, options) if answer_raw else -1
        out.append(Question(number=number, text=q_text, options=options,
                            correct=correct, explanation=explanation))
    return out


# --------------------------------------------------------------------------- #
#  HTML-страница «прохождения» билета
# --------------------------------------------------------------------------- #

def build_page(questions: list[Question], schedule: list[dict], title: str) -> str:
    """Самодостаточная HTML-страница: показывает вопросы по расписанию, ведёт
    курсор к правильному варианту, зажигает зелёным, крутит таймер/прогресс."""
    data = {
        "title": title,
        "total": len(questions),
        "questions": [
            {
                "number": q.number,
                "text": q.text,
                "options": q.options,
                "correct": q.correct,
                "explanation": q.explanation,
            }
            for q in questions
        ],
        "schedule": schedule,   # [{dur, revealAt}] на каждый вопрос
    }
    # В <script type="application/json"> содержимое — сырой текст (HTML-сущности
    # не декодируются), поэтому кавычки НЕ экранируем. Достаточно закрыть только
    # «</» (чтобы не оборвать тег <script>) и спец-переводы строк.
    payload = (json.dumps(data, ensure_ascii=False)
               .replace("</", "<\\/")
               .replace(" ", "\\u2028")
               .replace(" ", "\\u2029"))
    return _PAGE_TEMPLATE.replace("__DATA__", payload)


_PAGE_TEMPLATE = r"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Билет ПДД</title>
<style>
  * { box-sizing: border-box; }
  html, body { margin: 0; height: 100%; }
  body {
    font-family: "Segoe UI", Roboto, Arial, sans-serif;
    background: #eef2f7; color: #1b2733;
    display: flex; align-items: center; justify-content: center;
    overflow: hidden;
  }
  .card {
    width: 1100px; max-width: 96vw; background: #fff; border-radius: 18px;
    box-shadow: 0 12px 40px rgba(20,40,80,.14); padding: 34px 40px 40px;
    position: relative;
  }
  .top { display: flex; align-items: center; justify-content: space-between; }
  .badge {
    font-size: 22px; font-weight: 700; color: #2b6cff;
    background: #eaf1ff; padding: 8px 16px; border-radius: 10px;
  }
  .timer { font-size: 20px; font-weight: 600; color: #55657a; }
  .progress { height: 8px; background: #e6ecf5; border-radius: 6px; margin: 18px 0 26px; }
  .progress > i { display: block; height: 100%; width: 0; background: #2b6cff; border-radius: 6px; }
  .question { font-size: 30px; line-height: 1.35; font-weight: 600; min-height: 84px; }
  .options { margin-top: 26px; display: grid; gap: 14px; }
  .opt {
    font-size: 24px; padding: 16px 20px; border: 2px solid #dce3ee; border-radius: 12px;
    background: #f7f9fc; transition: background .25s, border-color .25s, transform .1s;
  }
  .opt .n { display: inline-block; min-width: 34px; font-weight: 700; color: #7a8aa0; }
  .opt.correct { background: #e4f8e9; border-color: #34c759; color: #12692e; font-weight: 700; }
  .opt.correct .n { color: #2ea24a; }
  .opt.press { transform: scale(.99); }
  .explain {
    margin-top: 22px; font-size: 21px; color: #3a4a5e; line-height: 1.4;
    background: #f4f7fb; border-left: 4px solid #34c759; border-radius: 8px;
    padding: 14px 18px; opacity: 0; transition: opacity .4s;
  }
  .explain.show { opacity: 1; }
  #cursor {
    position: fixed; width: 26px; height: 26px; left: 0; top: 0; z-index: 99;
    pointer-events: none; transition: left .5s ease, top .5s ease;
    filter: drop-shadow(0 2px 3px rgba(0,0,0,.35));
  }
</style>
</head>
<body>
  <div class="card">
    <div class="top">
      <div class="badge" id="badge">Вопрос 1</div>
      <div class="timer" id="timer">00:00</div>
    </div>
    <div class="progress"><i id="bar"></i></div>
    <div class="question" id="question"></div>
    <div class="options" id="options"></div>
    <div class="explain" id="explain"></div>
  </div>
  <svg id="cursor" viewBox="0 0 24 24"><path fill="#fff" stroke="#222" stroke-width="1.2"
     d="M4 2 L4 20 L9 15 L12.5 22 L15 21 L11.5 14 L18 14 Z"/></svg>

<script id="payload" type="application/json">__DATA__</script>
<script>
(function () {
  const DATA = JSON.parse(document.getElementById("payload").textContent);
  const badge = document.getElementById("badge");
  const timer = document.getElementById("timer");
  const bar = document.getElementById("bar");
  const qEl = document.getElementById("question");
  const optsEl = document.getElementById("options");
  const explEl = document.getElementById("explain");
  const cursor = document.getElementById("cursor");

  const fmt = s => {
    s = Math.max(0, Math.floor(s));
    const m = String(Math.floor(s / 60)).padStart(2, "0");
    const c = String(s % 60).padStart(2, "0");
    return m + ":" + c;
  };

  function render(q) {
    badge.textContent = "Вопрос " + q.number + " / " + DATA.total;
    qEl.textContent = q.text;
    explEl.classList.remove("show");
    explEl.textContent = q.explanation || "";
    optsEl.innerHTML = "";
    (q.options || []).forEach((opt, i) => {
      const d = document.createElement("div");
      d.className = "opt";
      d.dataset.i = i;
      d.innerHTML = '<span class="n">' + (i + 1) + '.</span> ' +
                    opt.replace(/[<>&]/g, c => ({'<':'&lt;','>':'&gt;','&':'&amp;'}[c]));
      optsEl.appendChild(d);
    });
  }

  function moveCursorTo(el) {
    if (!el) return;
    const r = el.getBoundingClientRect();
    cursor.style.left = (r.left + Math.min(r.width - 30, 80)) + "px";
    cursor.style.top = (r.top + r.height / 2 - 6) + "px";
  }

  function reveal(q) {
    const el = optsEl.querySelector('.opt[data-i="' + q.correct + '"]');
    if (el) {
      el.classList.add("press");
      setTimeout(() => { el.classList.remove("press"); el.classList.add("correct"); }, 140);
    }
    if (q.explanation) setTimeout(() => explEl.classList.add("show"), 260);
  }

  // Публичные функции — их дёргает Playwright по расписанию.
  let clockBase = 0, clockDur = 0, clockStart = 0, raf = 0;
  function tick() {
    const t = (performance.now() - clockStart) / 1000;
    const cur = Math.min(clockDur, t);
    bar.style.width = (100 * (clockBase + cur) / DATA._totalDur) + "%";
    timer.textContent = fmt(DATA._elapsedBefore + cur);
    raf = requestAnimationFrame(tick);
  }

  DATA._totalDur = DATA.schedule.reduce((a, s) => a + s.dur, 0) || 1;

  window.__quiz = {
    show(i) {
      const q = DATA.questions[i];
      render(q);
      DATA._elapsedBefore = DATA.schedule.slice(0, i).reduce((a, s) => a + s.dur, 0);
      clockBase = DATA._elapsedBefore;
      clockDur = DATA.schedule[i].dur;
      clockStart = performance.now();
      cancelAnimationFrame(raf);
      // Курсор к правильному варианту (плавно «наводимся» заранее).
      const el = optsEl.querySelector('.opt[data-i="' + q.correct + '"]');
      setTimeout(() => moveCursorTo(el), 350);
      tick();
    },
    reveal(i) { reveal(DATA.questions[i]); },
    total: DATA.questions.length,
  };
})();
</script>
</body>
</html>
"""


# --------------------------------------------------------------------------- #
#  Аудио: длительности и склейка
# --------------------------------------------------------------------------- #

def media_duration(path: str) -> float:
    out = subprocess.run(
        [FFMPEG, "-hide_banner", "-i", path],
        capture_output=True, text=True,
    ).stderr
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)", out)
    if not m:
        return 0.0
    h, mm, ss = m.groups()
    return int(h) * 3600 + int(mm) * 60 + float(ss)


def concat_audio(clips: list[tuple[str, float]], gaps: list[float], out: str) -> None:
    """Склеивает озвучки вопросов с тишиной между ними так, чтобы каждая начиналась
    ровно в начале своего вопроса. `gaps[i]` — тишина ПОСЛЕ i-й озвучки до конца
    сцены (пауза)."""
    with tempfile.TemporaryDirectory() as tmp:
        parts = []
        for i, ((clip, _), gap) in enumerate(zip(clips, gaps)):
            parts.append(clip)
            if gap > 0.01:
                sil = str(Path(tmp) / f"sil_{i}.m4a")
                subprocess.run(
                    [FFMPEG, "-hide_banner", "-y", "-f", "lavfi", "-t", f"{gap:.3f}",
                     "-i", "anullsrc=r=48000:cl=stereo", "-c:a", "aac", sil],
                    capture_output=True, check=True,
                )
                parts.append(sil)
        listfile = Path(tmp) / "list.txt"
        listfile.write_text("".join(f"file '{p}'\n" for p in parts), encoding="utf-8")
        subprocess.run(
            [FFMPEG, "-hide_banner", "-y", "-f", "concat", "-safe", "0",
             "-i", str(listfile), "-c:a", "aac", "-b:a", "192k", out],
            capture_output=True, check=True,
        )


# --------------------------------------------------------------------------- #
#  Запись видео через Playwright
# --------------------------------------------------------------------------- #

def _find_chromium() -> str | None:
    """Ищет уже установленный chromium (напр. в облачном окружении), чтобы не
    упереться в несовпадение версий. На обычном компе вернёт None — тогда
    Playwright берёт свой (после `python -m playwright install chromium`)."""
    import os
    env = os.environ.get("AUTOQUIZ_CHROMIUM") or os.environ.get("CHROMIUM_PATH")
    if env and Path(env).exists():
        return env
    base = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if base:
        link = Path(base) / "chromium"
        if link.exists():
            return str(link)
    return None


async def record_video(page_html: str, schedule: list[dict], out_webm_dir: str,
                       width: int, height: int, executable_path: str | None = None) -> str:
    """Открывает страницу в Chromium, проигрывает билет по расписанию и пишет
    видео. Возвращает путь к .webm."""
    from playwright.async_api import async_playwright

    with tempfile.NamedTemporaryFile("w", suffix=".html", delete=False, encoding="utf-8") as f:
        f.write(page_html)
        page_path = Path(f.name).as_uri()

    exe = executable_path or _find_chromium()
    launch_kw = {"args": ["--no-sandbox", "--force-color-profile=srgb"]}
    if exe:
        launch_kw["executable_path"] = exe

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(**launch_kw)
        context = await browser.new_context(
            viewport={"width": width, "height": height},
            record_video_dir=out_webm_dir,
            record_video_size={"width": width, "height": height},
            device_scale_factor=1,
        )
        page = await context.new_page()
        await page.goto(page_path)
        await page.wait_for_function("window.__quiz && window.__quiz.total > 0")

        for i, seg in enumerate(schedule):
            await page.evaluate("i => window.__quiz.show(i)", i)
            reveal_at = max(0.0, float(seg.get("revealAt", seg["dur"] * 0.45)))
            await asyncio.sleep(reveal_at)
            await page.evaluate("i => window.__quiz.reveal(i)", i)
            await asyncio.sleep(max(0.0, seg["dur"] - reveal_at))

        # Дать последнему кадру «дожить» и корректно закрыть — тогда видео финализируется.
        await asyncio.sleep(0.3)
        video = page.video
        await context.close()
        await browser.close()
        return await video.path()


def webm_to_mp4_with_audio(webm: str, audio: str, out: str,
                           total_dur: float) -> None:
    """Конвертирует запись в mp4 и накладывает озвучку. Видео обрезаем/тянем до
    общей длительности озвучки, чтобы всё сошлось секунда-в-секунду."""
    subprocess.run(
        [FFMPEG, "-hide_banner", "-y",
         "-i", webm, "-i", audio,
         "-map", "0:v:0", "-map", "1:a:0",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
         "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-b:a", "192k",
         "-t", f"{total_dur:.3f}",
         "-movflags", "+faststart", out],
        capture_output=True, check=True,
    )


# --------------------------------------------------------------------------- #
#  Оркестратор
# --------------------------------------------------------------------------- #

async def build(text: str, out: str, *, voice: str, rate: str, pitch: str,
                engine: str, pad: float, reveal_frac: float,
                width: int, height: int, title: str,
                chromium_path: str | None = None) -> dict:
    questions = parse_questions(text)
    if not questions:
        raise SystemExit("❌ Не удалось разобрать ни одного вопроса из текста.")
    print(f"📋 Разобрано вопросов: {len(questions)}")
    for q in questions:
        opt = f", вариантов: {len(q.options)}" if q.options else ""
        ans = f", ответ №{q.correct + 1}" if 0 <= q.correct < len(q.options) else ""
        print(f"   {q.number:2}. {q.text[:60]}{'…' if len(q.text) > 60 else ''}{opt}{ans}")

    # Озвучка (с кэшем) — голос Дмитрия.
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
    tmp = Path(tempfile.mkdtemp(prefix="autoquiz_"))
    clips: list[tuple[str, float]] = []
    schedule: list[dict] = []
    for q in questions:
        audio = await synth(q.narration())
        p = tmp / f"q{q.number:02d}.mp3"
        p.write_bytes(audio)
        dur = media_duration(str(p))
        scene = dur + pad
        clips.append((str(p), dur))
        schedule.append({"dur": round(scene, 3),
                         "revealAt": round(min(dur, dur * reveal_frac + 0.3), 3)})

    total_dur = sum(s["dur"] for s in schedule)
    print(f"🎞 Общая длительность: {int(total_dur // 60)}:{int(total_dur % 60):02d} "
          f"({len(questions)} вопросов)")

    # Видео.
    print("🎥 Записываю прохождение билета в браузере…")
    page_html = build_page(questions, schedule, title)
    webm_dir = str(tmp / "vid")
    Path(webm_dir).mkdir(exist_ok=True)
    webm = await record_video(page_html, schedule, webm_dir, width, height,
                              executable_path=chromium_path)

    # Аудио-дорожка: озвучка + паузы.
    print("🔊 Склеиваю озвучку под тайминг…")
    audio_track = str(tmp / "track.m4a")
    gaps = [pad] * len(clips)
    concat_audio(clips, gaps, audio_track)

    print("🎬 Собираю финальное видео…")
    webm_to_mp4_with_audio(webm, audio_track, out, total_dur)
    print(f"✅ Готово: {out}")
    return {"questions": len(questions), "duration": total_dur, "video": out}


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Авто-запись озвученного билета ПДД (без ручной записи экрана)")
    ap.add_argument("script", help=".txt с 20 вопросами (вопрос, варианты, ответ, пояснение)")
    ap.add_argument("output", nargs="?", default=None, help="итоговый .mp4")
    ap.add_argument("--engine", choices=["edge", "silero"], default="edge")
    ap.add_argument("--voice", default="ru-RU-DmitryNeural",
                    help="голос: edge — ru-RU-DmitryNeural; silero — eugene/aidar")
    ap.add_argument("--rate", default="+0%", help="скорость речи, напр. +8%% (средний темп)")
    ap.add_argument("--pitch", default="+0Hz")
    ap.add_argument("--pad", type=float, default=1.1,
                    help="пауза после озвучки вопроса, сек (чтобы зелёный ответ повисел)")
    ap.add_argument("--reveal", type=float, default=0.5,
                    help="в какой доле вопроса зажечь зелёный ответ (0.5 = на середине)")
    ap.add_argument("--width", type=int, default=1280)
    ap.add_argument("--height", type=int, default=720)
    ap.add_argument("--title", default="Билет ПДД")
    ap.add_argument("--chromium-path", default=None,
                    help="путь к chrome.exe/chromium (обычно не нужен: Playwright берёт свой)")
    ap.add_argument("--dump-page", default=None,
                    help="только сохранить HTML-страницу (для проверки вида), без записи")
    args = ap.parse_args()

    text = read_text_any(args.script)
    out = args.output or str(Path(args.script).with_suffix("").name + "_auto.mp4")

    if args.dump_page:
        questions = parse_questions(text)
        sched = [{"dur": 6.0, "revealAt": 3.0} for _ in questions]
        Path(args.dump_page).write_text(build_page(questions, sched, args.title),
                                        encoding="utf-8")
        print(f"🖼 Страница сохранена: {args.dump_page} (открой в браузере — это макет)")
        return

    asyncio.run(build(
        text, out, voice=args.voice, rate=args.rate, pitch=args.pitch,
        engine=args.engine, pad=args.pad, reveal_frac=args.reveal,
        width=args.width, height=args.height, title=args.title,
        chromium_path=args.chromium_path,
    ))


if __name__ == "__main__":
    main()
