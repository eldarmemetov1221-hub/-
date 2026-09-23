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

def _close(p: str) -> str:
    """Закрывает фразу точкой, если нет знака в конце — чтобы edge-tts сделал паузу."""
    p = p.strip()
    return p if p[-1:] in ".!?…:;" else p + "."


@dataclass
class Question:
    number: int
    text: str
    options: list[str] = field(default_factory=list)
    correct: int = 0            # индекс правильного варианта (0-based); -1 если неизвестно
    explanation: str = ""
    image: str = ""             # имя файла картинки (необязательно)

    def announce(self) -> str:
        """«Вопрос первый.» словом — чтобы голос объявлял номер вопроса."""
        word = _ORDINAL_WORDS.get(self.number)
        return f"Вопрос {word}." if word else f"Вопрос {self.number}."

    def spoken_question(self) -> str:
        """«Вопрос первый. <вопрос>.» — без зачитывания вариантов (они на экране)."""
        return f"{self.announce()}  {_close(self.text.strip())}"

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
        return "  ".join(_close(p) for p in parts if p.strip())

    def narration_intro(self, announce: bool = True) -> str:
        """Первая часть озвучки: «Вопрос первый» + вопрос + варианты (ДО зелёного)."""
        parts = []
        if announce:
            parts.append(self.announce())
        parts.append(self.text.strip())
        for i, opt in enumerate(self.options, 1):
            parts.append(f"{i}. {opt.strip()}")
        return "  ".join(_close(p) for p in parts if p.strip())

    def narration_answer(self) -> str:
        """Вторая часть: «правильный ответ» + пояснение (её читаем, когда уже
        загорелся зелёный)."""
        parts = []
        if 0 <= self.correct < len(self.options):
            parts.append(f"Правильный ответ: {self.options[self.correct].strip()}.")
        if self.explanation:
            parts.append(self.explanation.strip())
        return "  ".join(_close(p) for p in parts if p.strip())

    def correct_text(self) -> str:
        return self.options[self.correct].strip() if 0 <= self.correct < len(self.options) else ""


# Порядковые числительные словами: «Вопрос седьмой» = 7.
_RU_ORDINALS = {
    "первый": 1, "второй": 2, "третий": 3, "четвертый": 4, "четвёртый": 4,
    "пятый": 5, "шестой": 6, "седьмой": 7, "восьмой": 8, "девятый": 9,
    "десятый": 10, "одиннадцатый": 11, "двенадцатый": 12, "тринадцатый": 13,
    "четырнадцатый": 14, "пятнадцатый": 15, "шестнадцатый": 16,
    "семнадцатый": 17, "восемнадцатый": 18, "девятнадцатый": 19, "двадцатый": 20,
}
# Обратное: номер -> слово (для объявления «Вопрос первый»).
_ORDINAL_WORDS = {
    1: "первый", 2: "второй", 3: "третий", 4: "четвёртый", 5: "пятый",
    6: "шестой", 7: "седьмой", 8: "восьмой", 9: "девятый", 10: "десятый",
    11: "одиннадцатый", 12: "двенадцатый", 13: "тринадцатый", 14: "четырнадцатый",
    15: "пятнадцатый", 16: "шестнадцатый", 17: "семнадцатый", 18: "восемнадцатый",
    19: "девятнадцатый", 20: "двадцатый",
}

# Строка с картинкой: «Картинка: 1.jpg» / «Изображение: ...» / «Фото: ...».
_IMAGE = re.compile(r"^\s*(?:картинк\w*|изображени\w*|фото|рисун\w*|img|image)\s*[:\-—]\s*(.+\S)\s*$",
                    re.IGNORECASE)

# «Вопрос N» / «Вопрос №N» / «Вопрос седьмой» — число цифрой или словом; заголовок
# может стоять на той же строке, что и текст вопроса.
_HEADING_INLINE = re.compile(
    r"^\s*(?:билет\s*\d+\s*)?вопрос\s*(№?\s*\d{1,2}|[а-яё]+)\b[\s.):\-—]*",
    re.IGNORECASE,
)
# «5.» или «5)» на отдельной строке.
_HEADING_NUMLINE = re.compile(r"^\s*(?:\#{1,6}\s*)?(\d{1,2})\s*[.)]\s*$")


def _resolve_number(token: str) -> int | None:
    """Число из заголовка: цифрой ('7', '№7') или словом ('седьмой')."""
    token = token.strip().lstrip("№").strip().lower().replace("ё", "ё")
    m = re.match(r"\d{1,2}", token)
    if m:
        return int(m.group(0))
    return _RU_ORDINALS.get(token)


def _heading_match(line: str):
    """Если строка — заголовок вопроса, возвращает (number, rest_text), иначе None."""
    m = _HEADING_NUMLINE.match(line)
    if m:
        return int(m.group(1)), ""
    m = _HEADING_INLINE.match(line)
    if m:
        num = _resolve_number(m.group(1))
        if num is not None and 1 <= num <= 60:
            return num, line[m.end():].strip()
    return None


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
        if _heading_match(line):
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
        if blocks and not _heading_match(blocks[0][0]):
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
        image = ""
        first = block[0]

        # Номер вопроса из заголовка, если есть (цифрой или словом), и хвост строки.
        hm = _heading_match(first)
        if hm:
            number, rest = hm
            body = ([rest] if rest else []) + block[1:]
        else:
            body = block

        for line in body:
            if not line.strip():
                continue
            mi = _IMAGE.match(line)
            ma = _ANSWER.match(line)
            me = _EXPLAIN.match(line)
            mo = _OPTION.match(line)
            md = _DASH_OPTION.match(line)
            if mi:
                image = mi.group(1).strip().strip('"\'')
            elif ma:
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
                            correct=correct, explanation=explanation, image=image))
    return out


# --------------------------------------------------------------------------- #
#  HTML-страница «прохождения» билета
# --------------------------------------------------------------------------- #

def build_page(questions: list[Question], schedule: list[dict], title: str,
               images: dict[int, str] | None = None, show_expl: bool = False) -> str:
    """Самодостаточная HTML-страница: показывает вопросы по расписанию, зажигает
    зелёный правильный ответ, крутит таймер. `images` — {индекс: data-URI}.
    Текст пояснения на экране по умолчанию не показываем (его читает голос)."""
    images = images or {}
    data = {
        "title": title,
        "total": len(questions),
        "questions": [
            {
                "number": q.number,
                "text": q.text,
                "options": q.options,
                "correct": q.correct,
                "explanation": q.explanation if show_expl else "",
                "image": images.get(i, ""),
            }
            for i, q in enumerate(questions)
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
    display: flex; align-items: flex-start; justify-content: center;
    overflow: hidden;
  }
  .card {
    width: 1100px; max-width: 96vw; background: #fff; border-radius: 18px;
    box-shadow: 0 12px 40px rgba(20,40,80,.14); padding: 16px 36px 18px;
    margin: 10px 0; position: relative;
  }
  .top { display: flex; align-items: center; justify-content: space-between; }
  .badge {
    font-size: 22px; font-weight: 700; color: #2b6cff;
    background: #eaf1ff; padding: 8px 16px; border-radius: 10px;
  }
  .timer { font-size: 22px; font-weight: 700; color: #55657a; }
  .imgzone { text-align: center; margin: 10px 0 10px; }
  .imgzone img { max-width: 100%; max-height: min(210px, 30vh); border-radius: 12px;
                 border: 1px solid #e3e9f2; display: none; }
  .imgzone img.show { display: inline-block; }
  .question { font-size: 25px; line-height: 1.28; font-weight: 600; }
  .options { margin-top: 12px; display: grid; gap: 8px; }
  .opt {
    font-size: 21px; padding: 11px 18px; border: 2px solid #dce3ee; border-radius: 12px;
    background: #f7f9fc; box-shadow: 0 0 0 rgba(52,199,89,0);
    transition: background .3s ease, border-color .3s ease, color .3s ease, box-shadow .3s ease;
  }
  .opt .n { display: inline-block; min-width: 30px; font-weight: 700; color: #7a8aa0;
            transition: color .3s ease; }
  .opt.correct { box-shadow: 0 0 0 3px rgba(52,199,89,.25); }
  .opt.correct { background: #e4f8e9; border-color: #34c759; color: #12692e; font-weight: 700; }
  .opt.correct .n { color: #2ea24a; }
  .explain {
    margin-top: 16px; font-size: 19px; color: #3a4a5e; line-height: 1.4;
    background: #f4f7fb; border-left: 4px solid #34c759; border-radius: 8px;
    padding: 12px 16px; opacity: 0; transition: opacity .4s;
  }
  .explain.show { opacity: 1; }
</style>
</head>
<body>
  <div class="card">
    <div class="top">
      <div class="badge" id="badge">Вопрос 1</div>
      <div class="timer" id="timer">00:00</div>
    </div>
    <div class="imgzone"><img id="qimg" alt=""></div>
    <div class="question" id="question"></div>
    <div class="options" id="options"></div>
    <div class="explain" id="explain"></div>
  </div>

<script id="payload" type="application/json">__DATA__</script>
<script>
(function () {
  const DATA = JSON.parse(document.getElementById("payload").textContent);
  const badge = document.getElementById("badge");
  const timer = document.getElementById("timer");
  const qEl = document.getElementById("question");
  const optsEl = document.getElementById("options");
  const explEl = document.getElementById("explain");
  const imgEl = document.getElementById("qimg");

  const fmt = s => {
    s = Math.max(0, Math.floor(s));
    const m = String(Math.floor(s / 60)).padStart(2, "0");
    const c = String(s % 60).padStart(2, "0");
    return m + ":" + c;
  };

  function render(q) {
    badge.textContent = "Вопрос " + q.number + " / " + DATA.total;
    if (q.image) { imgEl.src = q.image; imgEl.classList.add("show"); }
    else { imgEl.classList.remove("show"); imgEl.removeAttribute("src"); }
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

  function reveal(q) {
    // Плавно зажигаем зелёный (переход .7s задан в CSS).
    const el = optsEl.querySelector('.opt[data-i="' + q.correct + '"]');
    if (el) el.classList.add("correct");
    if (q.explanation) setTimeout(() => explEl.classList.add("show"), 200);
  }

  // Публичные функции — их дёргает Playwright по расписанию.
  let clockBase = 0, clockDur = 0, clockStart = 0, raf = 0;
  function tick() {
    const t = (performance.now() - clockStart) / 1000;
    const cur = Math.min(clockDur, t);
    timer.textContent = fmt(DATA._elapsedBefore + cur);
    raf = requestAnimationFrame(tick);
  }

  DATA._totalDur = DATA.schedule.reduce((a, s) => a + s.dur, 0) || 1;

  const card = document.querySelector(".card");
  window.__quiz = {
    show(i) {
      const q = DATA.questions[i];
      render(q);
      // Плавное появление нового вопроса: мягкое затухание + лёгкий подъём.
      card.style.transition = "none";
      card.style.opacity = "0";
      card.style.transform = "translateY(16px)";
      requestAnimationFrame(() => {
        card.style.transition = "opacity .8s cubic-bezier(.22,.61,.36,1), " +
                                "transform .8s cubic-bezier(.22,.61,.36,1)";
        card.style.opacity = "1";
        card.style.transform = "translateY(0)";
      });
      DATA._elapsedBefore = DATA.schedule.slice(0, i).reduce((a, s) => a + s.dur, 0);
      clockBase = DATA._elapsedBefore;
      clockDur = DATA.schedule[i].dur;
      clockStart = performance.now();
      cancelAnimationFrame(raf);
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
    """Склеивает озвучки вопросов с тишиной между ними. Чтобы разные форматы
    (mp3/aac/wav) склеивались надёжно, КАЖДЫЙ кусок сначала приводим к единому
    WAV (48кГц, стерео), потом склеиваем встык и кодируем в aac."""
    with tempfile.TemporaryDirectory() as tmp:
        parts = []
        idx = 0
        for (clip, _), gap in zip(clips, gaps):
            w = str(Path(tmp) / f"p{idx:04d}.wav"); idx += 1
            subprocess.run(
                [FFMPEG, "-hide_banner", "-y", "-i", clip,
                 "-ar", "48000", "-ac", "2", w],
                capture_output=True, check=True,
            )
            parts.append(w)
            if gap > 0.01:
                s = str(Path(tmp) / f"p{idx:04d}.wav"); idx += 1
                subprocess.run(
                    [FFMPEG, "-hide_banner", "-y", "-f", "lavfi", "-t", f"{gap:.3f}",
                     "-i", "anullsrc=r=48000:cl=stereo", "-ar", "48000", "-ac", "2", s],
                    capture_output=True, check=True,
                )
                parts.append(s)
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

def _image_data_uri(path: Path) -> str:
    """Файл картинки -> data:URI (встраиваем прямо в страницу, без путей)."""
    import base64
    import mimetypes
    mime = mimetypes.guess_type(str(path))[0] or "image/jpeg"
    b64 = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{b64}"


def _resolve_images(questions: list[Question], images_dir: Path | None,
                    base_dir: Path) -> dict[int, str]:
    """Находит файлы картинок вопросов и превращает их в data:URI. Ищет по имени
    из «Картинка:», иначе пробует <номер>.jpg/.png рядом с текстом или в --images."""
    dirs = [d for d in (images_dir, base_dir) if d]
    out: dict[int, str] = {}
    for i, q in enumerate(questions):
        candidates = []
        if q.image:
            candidates.append(q.image)
        for ext in (".jpg", ".jpeg", ".png", ".webp"):
            candidates.append(f"{q.number}{ext}")
        found = None
        for name in candidates:
            for d in dirs:
                p = (d / name)
                if p.exists():
                    found = p
                    break
            if found:
                break
        if found:
            try:
                out[i] = _image_data_uri(found)
            except Exception as e:  # noqa: BLE001
                print(f"   ⚠️ Картинка для вопроса {q.number} не прочиталась: {e}")
        elif q.image:
            print(f"   ⚠️ Картинка «{q.image}» для вопроса {q.number} не найдена.")
    return out


def _split_at_question(prose: str) -> tuple[str, str]:
    """Делит прозу на «вопрос» и «остальное» по первому «?» (иначе по первой
    точке). Зелёный зажигаем на стыке."""
    prose = prose.strip()
    m = re.search(r"[?]+", prose) or re.search(r"\.\s", prose)
    if not m:
        return prose, ""
    return prose[:m.end()].strip(), prose[m.end():].strip()


async def build(text: str, out: str, *, voice: str, rate: str, pitch: str,
                engine: str, pad: float, reveal_frac: float,
                width: int, height: int, title: str,
                images_dir: Path | None = None, base_dir: Path | None = None,
                speak_map: dict[int, str] | None = None, green_frac: float = 0.6,
                before: float = 1.5, start_gap: float = 1.0, show_expl: bool = False,
                chromium_path: str | None = None) -> dict:
    speak_map = speak_map or {}
    green_frac = min(1.0, max(0.0, green_frac))
    questions = parse_questions(text)
    if not questions:
        raise SystemExit("❌ Не удалось разобрать ни одного вопроса из текста.")
    print(f"📋 Разобрано вопросов: {len(questions)}")
    for q in questions:
        opt = f", вариантов: {len(q.options)}" if q.options else ""
        ans = f", ответ №{q.correct + 1}" if 0 <= q.correct < len(q.options) else ""
        img = " 🖼" if q.image else ""
        print(f"   {q.number:2}. {q.text[:56]}{'…' if len(q.text) > 56 else ''}{opt}{ans}{img}")

    images = _resolve_images(questions, images_dir, base_dir or Path("."))
    if images:
        print(f"🖼 Картинок подключено: {len(images)}")

    # Озвучка (с кэшем) — голос Дмитрия.
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
    tmp = Path(tempfile.mkdtemp(prefix="autoquiz_"))
    # Тихий кусок — пауза в НАЧАЛЕ вопроса (открылся -> пауза -> потом читаем).
    silence = tmp / "silence.m4a"
    subprocess.run(
        [FFMPEG, "-hide_banner", "-y", "-f", "lavfi", "-t", f"{max(0.05, start_gap):.3f}",
         "-i", "anullsrc=r=48000:cl=stereo", "-c:a", "aac", str(silence)],
        capture_output=True, check=True)
    clips: list[tuple[str, float]] = []
    gaps: list[float] = []
    schedule: list[dict] = []
    for q in questions:
        # Если задан «мой текст» (--speak): ВОПРОС читаем авто (без вариантов),
        # а ПОЯСНЕНИЕ — твоими словами. Иначе — полностью авто-озвучка.
        prose = speak_map.get(q.number)
        if prose:
            intro_text = q.spoken_question()          # вопрос — авто
            _, my_expl = _split_at_question(prose)    # твоё пояснение (после «?»)
            answer_text = my_expl or prose
        else:
            intro_text, answer_text = q.narration_intro(), q.narration_answer()
        tail = round(before + pad, 3)   # тишина в конце вопроса (before + после зелёного)
        mid = 0.0
        # 0) пауза в начале: вопрос открылся -> тишина start_gap -> потом читаем.
        clips.append((str(silence), start_gap)); gaps.append(0.0)
        # 1) вопрос (у --speak — твоими словами).
        a1 = await synth(intro_text)
        p1 = tmp / f"q{q.number:02d}a.mp3"; p1.write_bytes(a1)
        d1 = media_duration(str(p1))
        clips.append((str(p1), d1))
        # 2) пояснение/ответ.
        ans = answer_text
        d2 = 0.0
        if ans:
            mid = 0.45                 # короткая пауза-вдох между вопросом и пояснением
            gaps.append(mid)
            a2 = await synth(ans)
            p2 = tmp / f"q{q.number:02d}b.mp3"; p2.write_bytes(a2)
            d2 = media_duration(str(p2))
            clips.append((str(p2), d2))
        gaps.append(tail)
        # Порядок: открылся -> пауза -> читает -> пауза -> ЗЕЛЁНЫЙ -> пауза -> дальше.
        reveal_at = start_gap + d1 + mid + d2 + before
        schedule.append({"dur": round(start_gap + d1 + mid + d2 + before + pad, 3),
                         "revealAt": round(reveal_at, 3)})

    total_dur = sum(s["dur"] for s in schedule)
    print(f"🎞 Общая длительность: {int(total_dur // 60)}:{int(total_dur % 60):02d} "
          f"({len(questions)} вопросов)")

    # Видео.
    print("🎥 Записываю прохождение билета в браузере…")
    page_html = build_page(questions, schedule, title, images, show_expl=show_expl)
    webm_dir = str(tmp / "vid")
    Path(webm_dir).mkdir(exist_ok=True)
    webm = await record_video(page_html, schedule, webm_dir, width, height,
                              executable_path=chromium_path)

    # Аудио-дорожка: озвучка + паузы.
    print("🔊 Склеиваю озвучку под тайминг…")
    audio_track = str(tmp / "track.m4a")
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
    ap.add_argument("--rate", default="-15%",
                    help="скорость речи (по умолчанию -15%% — медленно и внятно)")
    ap.add_argument("--pitch", default="+0Hz")
    ap.add_argument("--pad", type=float, default=2.0,
                    help="пауза ПОСЛЕ зелёного до следующего вопроса, сек")
    ap.add_argument("--before", type=float, default=1.5,
                    help="пауза ПОСЛЕ чтения до зажигания зелёного, сек")
    ap.add_argument("--start", type=float, default=1.0,
                    help="пауза в НАЧАЛЕ вопроса (открылся → пауза → читает), сек")
    ap.add_argument("--show-expl", action="store_true",
                    help="показывать текст пояснения на экране (по умолчанию скрыт)")
    ap.add_argument("--reveal", type=float, default=0.5, help="(не используется)")
    ap.add_argument("--green", type=float, default=0.6,
                    help="когда зажигать зелёный: доля пояснения (0=сразу после вопроса, "
                         "0.6=ближе к концу, 1=в самом конце). По умолчанию 0.6")
    ap.add_argument("--width", type=int, default=1280)
    ap.add_argument("--height", type=int, default=720)
    ap.add_argument("--title", default="Билет ПДД")
    ap.add_argument("--images", default=None,
                    help="папка с картинками вопросов (по умолчанию — рядом с текстом)")
    ap.add_argument("--speak", default=None,
                    help="файл с ТВОИМ текстом для голоса (проза по вопросам); варианты "
                         "тогда вслух не читаются, а берётся твоя формулировка")
    ap.add_argument("--limit", type=int, default=0,
                    help="сделать только первые N вопросов (быстрый предпросмотр)")
    ap.add_argument("--chromium-path", default=None,
                    help="путь к chrome.exe/chromium (обычно не нужен: Playwright берёт свой)")
    ap.add_argument("--dump-page", default=None,
                    help="только сохранить HTML-страницу (для проверки вида), без записи")
    args = ap.parse_args()

    text = read_text_any(args.script)
    out = args.output or str(Path(args.script).with_suffix("").name + "_auto.mp4")
    base_dir = Path(args.script).resolve().parent
    images_dir = Path(args.images).resolve() if args.images else None

    if args.dump_page:
        questions = parse_questions(text)
        if args.limit:
            questions = questions[:args.limit]
        imgs = _resolve_images(questions, images_dir, base_dir)
        sched = [{"dur": 6.0, "revealAt": 3.0} for _ in questions]
        Path(args.dump_page).write_text(build_page(questions, sched, args.title, imgs),
                                        encoding="utf-8")
        print(f"🖼 Страница сохранена: {args.dump_page} (открой в браузере — это макет)")
        return

    if args.limit:
        # Ограничение делаем на уровне текста: берём первые N блоков.
        blocks = parse_questions(text)[:args.limit]
        text = "\n\n".join(
            f"Вопрос {q.number}\n" + (f"Картинка: {q.image}\n" if q.image else "") +
            q.text + "\n" + "\n".join(f"{i+1}. {o}" for i, o in enumerate(q.options)) +
            (f"\nОтвет: {q.correct+1}" if q.correct >= 0 else "") +
            (f"\nПояснение: {q.explanation}" if q.explanation else "")
            for q in blocks)

    # «Мой текст» для голоса: проза по вопросам, ключ — номер вопроса.
    speak_map = {}
    if args.speak:
        for q in parse_questions(read_text_any(args.speak)):
            speak_map[q.number] = q.text.strip()   # твоя проза (вопрос+пояснение)
        print(f"🗣  Пояснение — твоими словами (--speak): {len(speak_map)} вопрос(ов)")

    asyncio.run(build(
        text, out, voice=args.voice, rate=args.rate, pitch=args.pitch,
        engine=args.engine, pad=args.pad, reveal_frac=args.reveal,
        width=args.width, height=args.height, title=args.title,
        images_dir=images_dir, base_dir=base_dir, speak_map=speak_map,
        green_frac=args.green, before=args.before, start_gap=args.start,
        show_expl=args.show_expl, chromium_path=args.chromium_path,
    ))


if __name__ == "__main__":
    main()
