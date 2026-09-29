"""
Вертикальные тикток-видео из ОДНОЙ картинки вопроса + твоего текста.
Движок тот же, что билеты (озвучка Яндекс/edge, рендер Playwright, ffmpeg).

Ты даёшь:
  • картинку вопроса (готовую карточку-скрин: вопрос, варианты, сцена);
  • текст озвучки (--speak) со стрелками.

Порядок в ролике:
  1) голос читает ВОПРОС;
  2) пауза (по умолчанию 5 сек — зритель думает);
  3) ПОЯСНЕНИЕ со стрелками (появляются ровно в момент фразы);
  4) в конце голос говорит ОТВЕТ.

Запуск (ключ/folder Яндекса уже прописаны через setx):
    python tiktok.py вопрос15.png "%USERPROFILE%\\Desktop\\пдд\\tiktok15.mp4" --speak tiktok15.txt

Формат файла --speak (ОДИН блок, твоими словами):
    Вопрос пятнадцатый. Кому Вы должны уступить дорогу при повороте налево?
    Пояснение: Перед нами {стрелка:0.88,0.27}знак Главная дорога{/}, а под ним
    {стрелка:0.88,0.39}табличка{/}. ... уступаем {стрелка:0.64,0.40}автомобилю{/}.
    Ответ: Трамваю Бэ и легковому автомобилю.

Тег {стрелка:x,y}текст{/}: пока голос читает текст между тегами — на картинке
появляется красная стрелка в точку (x,y) (доли КАРТИНКИ: 0,0 — левый верх,
1,1 — правый низ). Дочитал — убралась. Стрелок сколько угодно.
"""

import argparse
import asyncio
import json
import re
import tempfile
from pathlib import Path

from auto_quiz import (
    FFMPEG,
    make_cached_synth,
    media_duration,
    concat_audio,
    webm_to_mp4_with_audio,
    _find_chromium,
    _image_data_uri,
    synth_yandex,
    synth_edge,
)
from make_video import read_text_any


# --------------------------------------------------------------------------- #
#  Разбор текста озвучки
# --------------------------------------------------------------------------- #

def parse_narration(text: str):
    """(вопрос, пояснение_без_тегов, стрелки, ответ).
    стрелки: список (char_start, char_end, x, y) — позиции фразы в пояснении."""
    block = text.strip()
    m_exp = re.search(r"Пояснени[ея]\s*:\s*", block)
    m_ans = re.search(r"Ответ\s*:\s*", block)
    if not m_exp or not m_ans:
        raise SystemExit("В тексте озвучки нужны метки «Пояснение:» и «Ответ:».")
    question = block[:m_exp.start()].strip()
    expl = block[m_exp.end():m_ans.start()].strip()
    answer = block[m_ans.end():].strip()

    plain = ""
    spans = []
    pos = 0
    for mo in re.finditer(r"\{\s*стрелка\s*:\s*([0-9.]+)\s*,\s*([0-9.]+)\s*\}(.*?)\{\s*/\s*\}",
                          expl, re.S):
        plain += expl[pos:mo.start()]
        phrase = mo.group(3)
        cs = len(plain)
        plain += phrase
        ce = len(plain)
        spans.append((cs, ce, float(mo.group(1)), float(mo.group(2))))
        pos = mo.end()
    plain += expl[pos:]
    return question, plain, spans, answer


# --------------------------------------------------------------------------- #
#  HTML-страница (вертикаль 1080x1920): картинка на весь экран + стрелки
# --------------------------------------------------------------------------- #

_PAGE = r"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<style>
  * { margin:0; padding:0; box-sizing:border-box; }
  html,body { width:1080px; height:1920px; }
  body { background:#0e0f12; overflow:hidden;
         display:flex; align-items:center; justify-content:center; }
  .bg { position:absolute; inset:-8%; background-size:cover; background-position:center;
        filter: blur(30px) brightness(.5); }
  .wrap { position:relative; display:inline-block; line-height:0; }
  .wrap img { display:block; width:1040px; height:auto; max-height:1860px;
              border-radius:26px; box-shadow:0 24px 80px rgba(0,0,0,.5); }
  .arrows { position:absolute; inset:0; pointer-events:none; }
  .arrow { position:absolute; width:150px; height:150px; opacity:0;
           transform: translate(-50%,-50%) scale(.55);
           transition: opacity .18s ease-out, transform .18s ease-out; }
  .arrow.show { opacity:1; transform: translate(-50%,-50%) scale(1); }
  .arrow svg { width:100%; height:100%; filter: drop-shadow(0 3px 7px rgba(0,0,0,.55)); }
  .think { position:absolute; top:24px; left:50%; transform:translateX(-50%);
           background:#e2574c; color:#fff; font-family:-apple-system,Arial,sans-serif;
           font-size:46px; font-weight:800; padding:14px 40px; border-radius:999px;
           box-shadow:0 8px 24px rgba(0,0,0,.4); opacity:0; transition:opacity .2s; z-index:5; }
  .think.show { opacity:1; }
</style></head>
<body>
  <div class="bg" id="bg"></div>
  <div class="wrap">
    <img id="img" alt="">
    <div class="arrows" id="arrows"></div>
    <div class="think" id="think">Думай…</div>
  </div>
<script id="payload" type="application/json">__DATA__</script>
<script>
  const D = JSON.parse(document.getElementById("payload").textContent);
  const img = document.getElementById("img");
  img.src = D.image;
  document.getElementById("bg").style.backgroundImage = "url(" + D.image + ")";
  const arrowsEl = document.getElementById("arrows");
  const ARROW_SVG = '<svg viewBox="0 0 100 100"><g stroke="#ff2323" stroke-width="11" fill="none" stroke-linecap="round" stroke-linejoin="round"><line x1="16" y1="16" x2="72" y2="72"/><path d="M72 44 L72 72 L44 72"/></g></svg>';
  function showArrow(x,y){
    const a = document.createElement("div"); a.className="arrow";
    a.style.left=(x*100)+"%"; a.style.top=(y*100)+"%"; a.innerHTML=ARROW_SVG;
    arrowsEl.appendChild(a); requestAnimationFrame(()=>a.classList.add("show"));
  }
  function clearArrows(){ arrowsEl.querySelectorAll(".arrow").forEach(a=>{
    a.classList.remove("show"); setTimeout(()=>a.remove(),200); }); }
  function think(on){ document.getElementById("think").classList.toggle("show", !!on); }
  window.__tt = { ready:true, showArrow, clearArrows, think };
</script>
</body></html>"""


def build_page(image_uri):
    payload = json.dumps({"image": image_uri}, ensure_ascii=False).replace("</", "<\\/")
    return _PAGE.replace("__DATA__", payload)


# --------------------------------------------------------------------------- #
#  Запись видео по таймлайну событий
# --------------------------------------------------------------------------- #

async def record(page_html, events, total_dur, out_dir, executable_path=None):
    from playwright.async_api import async_playwright
    with tempfile.NamedTemporaryFile("w", suffix=".html", delete=False, encoding="utf-8") as f:
        f.write(page_html)
        page_uri = Path(f.name).as_uri()
    exe = executable_path or _find_chromium()
    launch_kw = {"args": ["--no-sandbox", "--force-color-profile=srgb"]}
    if exe:
        launch_kw["executable_path"] = exe
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(**launch_kw)
        context = await browser.new_context(
            viewport={"width": 1080, "height": 1920},
            record_video_dir=out_dir,
            record_video_size={"width": 1080, "height": 1920},
            device_scale_factor=1,
        )
        page = await context.new_page()
        await page.goto(page_uri)
        await page.wait_for_function("window.__tt && window.__tt.ready")
        clock = 0.0
        for t, js in events:
            dt = t - clock
            if dt > 0:
                await asyncio.sleep(dt)
                clock = t
            await page.evaluate(js)
        rest = total_dur - clock
        if rest > 0:
            await asyncio.sleep(rest)
        await asyncio.sleep(0.3)
        video = page.video
        await context.close()
        await browser.close()
        return await video.path()


# --------------------------------------------------------------------------- #
#  Оркестратор
# --------------------------------------------------------------------------- #

async def build(image_path, out, speak_txt, voice, engine, rate, pitch,
                think_pause, chromium_path):
    image_uri = _image_data_uri(Path(image_path))
    question_txt, expl_txt, spans, answer_txt = parse_narration(read_text_any(speak_txt))

    if engine == "yandex":
        import os
        ya_key = os.environ.get("YANDEX_API_KEY", "").strip()
        ya_folder = os.environ.get("YANDEX_FOLDER_ID", "").strip()
        if not ya_key or not ya_folder:
            raise SystemExit("Для yandex задай YANDEX_API_KEY и YANDEX_FOLDER_ID.")
        ya_voice = voice if voice and not re.search(r"Neural|ru-RU-", voice) else "filipp"

        async def base_synth(t):
            return await asyncio.to_thread(synth_yandex, t, ya_voice, ya_key, ya_folder)
    else:
        async def base_synth(t):
            return await synth_edge(t, voice, rate, pitch)

    synth = make_cached_synth(base_synth, engine, voice, rate, pitch, 3)
    tmp = Path(tempfile.mkdtemp(prefix="tiktok_"))
    print("⏳ Озвучиваю…")

    async def voice_to(text, name):
        data = await synth(text)
        p = tmp / name
        p.write_bytes(data)
        return str(p), media_duration(str(p))

    clips, gaps, events = [], [], []
    qp, qd = await voice_to(question_txt, "q.mp3")
    clips.append((qp, qd)); gaps.append(think_pause)
    ep, ed = await voice_to(expl_txt, "expl.mp3")
    clips.append((ep, ed)); gaps.append(0.0)
    ap, ad = await voice_to("Ответ. " + answer_txt, "a.mp3")
    clips.append((ap, ad)); gaps.append(0.0)

    events.append((0.0, "window.__tt.clearArrows()"))
    events.append((round(qd, 3), "window.__tt.think(true)"))
    t_expl = qd + think_pause
    events.append((round(t_expl, 3), "window.__tt.think(false)"))
    L = max(1, len(expl_txt))
    for cs, ce, x, y in spans:
        events.append((round(t_expl + (cs / L) * ed, 3), f"window.__tt.showArrow({x},{y})"))
        events.append((round(t_expl + (ce / L) * ed, 3), "window.__tt.clearArrows()"))
    events.append((round(qd + think_pause + ed, 3), "window.__tt.clearArrows()"))
    total = qd + think_pause + ed + ad
    print(f"🎞 Длительность: {int(total // 60)}:{int(total % 60):02d}")

    page = build_page(image_uri)
    webm_dir = str(tmp / "vid"); Path(webm_dir).mkdir(exist_ok=True)
    print("🎥 Записываю вертикальное видео…")
    events.sort(key=lambda e: e[0])
    webm = await record(page, events, total, webm_dir, executable_path=chromium_path)

    print("🔊 Склеиваю озвучку…")
    audio = str(tmp / "track.m4a")
    concat_audio(clips, gaps, audio)
    print("🎬 Собираю финальное видео…")
    webm_to_mp4_with_audio(webm, audio, out, total)
    print(f"✅ Готово: {out}")


def main():
    ap = argparse.ArgumentParser(description="Вертикальное тикток-видео из картинки вопроса + текста")
    ap.add_argument("image", help="картинка вопроса (png/jpg/webp) — карточка со скрина")
    ap.add_argument("output", help="итоговый .mp4 (вертикаль 1080x1920)")
    ap.add_argument("--speak", required=True, help=".txt твоей озвучки (со стрелками)")
    ap.add_argument("--engine", choices=["edge", "silero", "yandex"], default="yandex")
    ap.add_argument("--voice", default="filipp")
    ap.add_argument("--rate", default="+0%")
    ap.add_argument("--pitch", default="+0Hz")
    ap.add_argument("--think", type=float, default=5.0, help="пауза «зритель думает», сек")
    ap.add_argument("--chromium-path", default=None)
    args = ap.parse_args()
    asyncio.run(build(
        args.image, args.output, args.speak, args.voice, args.engine,
        args.rate, args.pitch, args.think, args.chromium_path,
    ))


if __name__ == "__main__":
    main()
