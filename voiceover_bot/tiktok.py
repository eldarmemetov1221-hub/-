"""
Вертикальные тикток-видео из вопроса ПДД — на том же движке, что билеты
(озвучка Яндекс/edge, рендер страницы Playwright, склейка ffmpeg).

Порядок в ролике:
  1) голос читает ВОПРОС;
  2) пауза (по умолчанию 5 сек — зритель думает);
  3) ПОЯСНЕНИЕ со стрелками, которые появляются ровно в момент фразы;
  4) в конце голос говорит ОТВЕТ, правильный вариант подсвечивается.

Запуск (голос Яндекса, как в билетах; ключ/folder уже прописаны через setx):
    python tiktok.py bilet15.txt out.mp4 --speak tiktok15.txt --images kartinki15 --num 15 --engine yandex --voice filipp

Файл билета (bilet15.txt) даёт КАРТИНКУ, вопрос, варианты и номер правильного
(строка «Ответ: N»). --num 15 — какой вопрос из файла взять (по умолчанию 1-й).

Файл озвучки (--speak, напр. tiktok15.txt) — ОДИН блок, твоими словами:
    Вопрос пятнадцатый. Кому Вы должны уступить дорогу при повороте налево?
    Пояснение: Перед нами {стрелка:0.88,0.27}знак Главная дорога{/} ...
    Ответ: Трамваю Бэ и легковому автомобилю.
"""

import argparse
import asyncio
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
    parse_questions,
    synth_yandex,
    synth_edge,
)
from make_video import read_text_any


# --------------------------------------------------------------------------- #
#  Разбор текста озвучки (вопрос / пояснение со стрелками / ответ)
# --------------------------------------------------------------------------- #

def parse_narration(text: str):
    """Возвращает (вопрос, пояснение_без_тегов, стрелки, ответ).
    стрелки — список (char_start, char_end, x, y): позиции фразы в пояснении,
    по ним потом рассчитываем момент показа стрелки внутри цельной озвучки."""
    block = text.strip()
    m_exp = re.search(r"Пояснени[ея]\s*:\s*", block)
    m_ans = re.search(r"Ответ\s*:\s*", block)
    if not m_exp or not m_ans:
        raise SystemExit("В тексте озвучки нужны метки «Пояснение:» и «Ответ:».")
    question = block[:m_exp.start()].strip()
    expl = block[m_exp.end():m_ans.start()].strip()
    answer = block[m_ans.end():].strip()

    plain = ""
    spans = []  # (char_start, char_end, x, y)
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
    # НЕ схлопываем пробелы — иначе съедут позиции стрелок (char_start/char_end).
    return question, plain, spans, answer


# --------------------------------------------------------------------------- #
#  HTML-страница (вертикаль 1080x1920)
# --------------------------------------------------------------------------- #

_PAGE = r"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<style>
  * { margin:0; padding:0; box-sizing:border-box; }
  html,body { width:1080px; height:1920px; }
  body {
    font-family: -apple-system, "Segoe UI", Roboto, Arial, sans-serif;
    background:#0f1115; color:#1b2733; overflow:hidden;
    display:flex; align-items:center; justify-content:center;
  }
  .bgwrap { position:absolute; inset:0; overflow:hidden; }
  .bg { position:absolute; inset:-8%; background-size:cover; background-position:center;
        filter: blur(26px) brightness(.55); }
  .card {
    position:relative; width:980px; background:#fff; border-radius:40px;
    box-shadow:0 24px 80px rgba(0,0,0,.45); padding:40px 44px 48px;
  }
  .top { display:flex; align-items:center; justify-content:space-between; margin-bottom:26px; }
  .qnum { font-size:52px; font-weight:800; color:#12181f; }
  .heart { width:56px; height:56px; }
  .scene { position:relative; width:100%; border-radius:26px; overflow:hidden;
           aspect-ratio: 16/10; background:#e9edf1; }
  .scene img { position:absolute; inset:0; width:100%; height:100%; object-fit:cover; }
  .arrows { position:absolute; inset:0; pointer-events:none; }
  .arrow { position:absolute; width:150px; height:150px; opacity:0;
           transform: translate(-50%,-50%) scale(.6);
           transition: opacity .18s ease-out, transform .18s ease-out; }
  .arrow.show { opacity:1; transform: translate(-50%,-50%) scale(1); }
  .arrow svg { width:100%; height:100%; filter: drop-shadow(0 3px 6px rgba(0,0,0,.5)); }
  .q { margin-top:30px; font-size:52px; line-height:1.18; font-weight:800; color:#12181f; }
  .opts { margin-top:30px; display:grid; gap:20px; }
  .opt { position:relative; display:flex; align-items:center; gap:26px;
         border:2px solid #e6ebf2; border-radius:22px; padding:26px 30px;
         font-size:40px; line-height:1.2; color:#1b2733; background:#fff; }
  .opt .n { flex:0 0 auto; width:54px; height:54px; border-radius:50%;
            border:2px solid #cfd8e6; display:flex; align-items:center; justify-content:center;
            font-size:34px; font-weight:800; color:#5b6b82; }
  .opt.correct { background:#2f9e35; border-color:#278a2b; color:#fff; font-weight:800; }
  .opt.correct .n { background:#fff; color:#2f9e35; border-color:#fff; }
  .opt.dim { opacity:.4; }
  .think { position:absolute; top:30px; left:50%; transform:translateX(-50%);
           background:#e2574c; color:#fff; font-size:44px; font-weight:800;
           padding:14px 34px; border-radius:999px; opacity:0; transition:opacity .2s; }
  .think.show { opacity:1; }
</style></head>
<body>
  <div class="bgwrap"><div class="bg" id="bg"></div></div>
  <div class="card">
    <div class="think" id="think">Думай…</div>
    <div class="top"><div class="qnum" id="qnum">Вопрос 1</div>
      <svg class="heart" viewBox="0 0 24 24" fill="#ff4d67"><path d="M12 21s-7.5-4.6-10-9.3C.6 9 1.6 5.7 4.7 5c2-.4 3.6.6 4.5 2 .9-1.4 2.5-2.4 4.5-2 3.1.7 4.1 4 2.7 6.7C19.5 16.4 12 21 12 21z"/></svg>
    </div>
    <div class="scene" id="scene"><img id="img" alt=""><div class="arrows" id="arrows"></div></div>
    <div class="q" id="q"></div>
    <div class="opts" id="opts"></div>
  </div>
<script id="payload" type="application/json">__DATA__</script>
<script>
  const D = JSON.parse(document.getElementById("payload").textContent);
  document.getElementById("qnum").textContent = D.qnum;
  const img = document.getElementById("img");
  if (D.image) { img.src = D.image; document.getElementById("bg").style.backgroundImage = "url(" + D.image + ")"; }
  document.getElementById("q").textContent = D.question;
  const opts = document.getElementById("opts");
  D.options.forEach((o,i) => {
    const d = document.createElement("div"); d.className = "opt"; d.dataset.i = i;
    d.innerHTML = '<span class="n">' + (i+1) + '</span><span>' +
                  o.replace(/[<>&]/g,c=>({'<':'&lt;','>':'&gt;','&':'&amp;'}[c])) + '</span>';
    opts.appendChild(d);
  });
  const arrowsEl = document.getElementById("arrows");
  const ARROW_SVG = '<svg viewBox="0 0 100 100"><g stroke="#ff2d2d" stroke-width="10" fill="none" stroke-linecap="round"><line x1="18" y1="18" x2="74" y2="74"/><path d="M74 46 L74 74 L46 74"/></g></svg>';
  function showArrow(x,y){
    const a = document.createElement("div"); a.className="arrow";
    a.style.left=(x*100)+"%"; a.style.top=(y*100)+"%"; a.innerHTML=ARROW_SVG;
    arrowsEl.appendChild(a); requestAnimationFrame(()=>a.classList.add("show"));
  }
  function clearArrows(){ arrowsEl.querySelectorAll(".arrow").forEach(a=>{a.classList.remove("show"); setTimeout(()=>a.remove(),200);}); }
  function think(on){ document.getElementById("think").classList.toggle("show", !!on); }
  function reveal(){
    document.querySelectorAll(".opt").forEach(el=>{
      if(parseInt(el.dataset.i)!==D.correct) el.classList.add("dim");
    });
    const c = document.querySelector('.opt[data-i="'+D.correct+'"]'); if(c) c.classList.add("correct");
  }
  window.__tt = { ready:true, showArrow, clearArrows, think, reveal };
</script>
</body></html>"""


def build_page(qnum, question, options, correct, image_uri):
    import json as _json
    data = {
        "qnum": qnum, "question": question, "options": options,
        "correct": correct, "image": image_uri or "",
    }
    payload = _json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
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

async def build(bilet_txt, out, speak_txt, images_dir, num, voice, engine,
                rate, pitch, think_pause, chromium_path):
    # 1) вопрос/варианты/ответ/картинка — из файла билета
    questions = parse_questions(read_text_any(bilet_txt))
    q = next((x for x in questions if x.number == num), None) or questions[0]
    image_uri = None
    from auto_quiz import _resolve_images
    imgs = _resolve_images([q], images_dir, Path(bilet_txt).resolve().parent)
    if imgs.get(0):
        image_uri = imgs[0]
    print(f"📋 Вопрос {q.number}: {q.text[:60]}  вариантов {len(q.options)}  "
          f"ответ №{q.correct+1}{'  🖼' if image_uri else '  (без картинки)'}")

    # 2) озвучка твоими словами
    question_txt, expl_txt, spans, answer_txt = parse_narration(read_text_any(speak_txt))

    # синтез с кэшем
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

    clips = []   # (path, dur)
    gaps = []    # тишина ПОСЛЕ клипа
    events = []  # (time, js)

    # (A) вопрос
    qp, qd = await voice_to(question_txt, "q.mp3")
    clips.append((qp, qd)); gaps.append(think_pause)
    # (B) пояснение — ОДНИМ куском (гладкий голос)
    ep, ed = await voice_to(expl_txt, "expl.mp3")
    clips.append((ep, ed)); gaps.append(0.0)
    # (C) ответ
    ap, ad = await voice_to("Ответ. " + answer_txt, "a.mp3")
    clips.append((ap, ad)); gaps.append(0.0)

    # Таймлайн
    events.append((0.0, "window.__tt.clearArrows()"))
    t_pause = qd
    events.append((round(t_pause, 3), "window.__tt.think(true)"))
    t_expl = qd + think_pause
    events.append((round(t_expl, 3), "window.__tt.think(false)"))
    # стрелки: момент = доля позиции фразы в пояснении * длительность пояснения
    L = max(1, len(expl_txt))
    for cs, ce, x, y in spans:
        on = t_expl + (cs / L) * ed
        off = t_expl + (ce / L) * ed
        events.append((round(on, 3), f"window.__tt.showArrow({x},{y})"))
        events.append((round(off, 3), "window.__tt.clearArrows()"))
    t_ans = qd + think_pause + ed
    events.append((round(t_ans, 3), "window.__tt.clearArrows()"))
    events.append((round(t_ans, 3), "window.__tt.reveal()"))
    total = qd + think_pause + ed + ad

    print(f"🎞 Длительность: {int(total//60)}:{int(total%60):02d}")

    # 3) страница + запись
    page = build_page(f"Вопрос {q.number}", q.text, q.options, q.correct, image_uri)
    webm_dir = str(tmp / "vid"); Path(webm_dir).mkdir(exist_ok=True)
    print("🎥 Записываю вертикальное видео…")
    events.sort(key=lambda e: e[0])
    webm = await record(page, events, total, webm_dir, executable_path=chromium_path)

    # 4) звук + склейка
    print("🔊 Склеиваю озвучку…")
    audio = str(tmp / "track.m4a")
    concat_audio(clips, gaps, audio)
    print("🎬 Собираю финальное видео…")
    webm_to_mp4_with_audio(webm, audio, out, total)
    print(f"✅ Готово: {out}")


def main():
    ap = argparse.ArgumentParser(description="Вертикальное тикток-видео из вопроса ПДД")
    ap.add_argument("bilet", help=".txt с вопросом (вопрос, варианты, «Ответ: N», картинка)")
    ap.add_argument("output", help="итоговый .mp4 (вертикаль 1080x1920)")
    ap.add_argument("--speak", required=True, help=".txt твоей озвучки (со стрелками)")
    ap.add_argument("--images", default=None, help="папка с картинками")
    ap.add_argument("--num", type=int, default=1, help="номер вопроса из файла билета")
    ap.add_argument("--engine", choices=["edge", "silero", "yandex"], default="yandex")
    ap.add_argument("--voice", default="filipp")
    ap.add_argument("--rate", default="+0%")
    ap.add_argument("--pitch", default="+0Hz")
    ap.add_argument("--think", type=float, default=5.0, help="пауза «зритель думает», сек")
    ap.add_argument("--chromium-path", default=None)
    args = ap.parse_args()
    asyncio.run(build(
        args.bilet, args.output, args.speak, args.images, args.num,
        args.voice, args.engine, args.rate, args.pitch, args.think, args.chromium_path,
    ))


if __name__ == "__main__":
    main()
