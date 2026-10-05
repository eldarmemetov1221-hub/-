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
    synth_yandex_v3,
    synth_edge,
)
from make_video import read_text_any


# --------------------------------------------------------------------------- #
#  Разбор текста озвучки
# --------------------------------------------------------------------------- #

def make_ticks(seconds: int, out: str) -> None:
    """Дорожка «тик-тик» на N секунд: короткий щелчок в начале каждой секунды."""
    import subprocess as sp
    with tempfile.TemporaryDirectory() as td:
        tick = str(Path(td) / "t.wav")
        sil = str(Path(td) / "s.wav")
        sp.run([FFMPEG, "-hide_banner", "-y", "-f", "lavfi", "-t", "0.045",
                "-i", "sine=frequency=1200:sample_rate=48000", "-ac", "2",
                "-af", "volume=0.5", tick], capture_output=True, check=True)
        sp.run([FFMPEG, "-hide_banner", "-y", "-f", "lavfi", "-t", "0.955",
                "-i", "anullsrc=r=48000:cl=stereo", "-ac", "2", sil],
               capture_output=True, check=True)
        lst = Path(td) / "l.txt"
        lst.write_text(f"file '{tick}'\nfile '{sil}'\n" * int(seconds), encoding="utf-8")
        sp.run([FFMPEG, "-hide_banner", "-y", "-f", "concat", "-safe", "0",
                "-i", str(lst), "-ar", "48000", "-ac", "2", out],
               capture_output=True, check=True)


def detect_boxes(image_path):
    """Сам находит боксы вариантов на карточке (светлые рамки на всю ширину).
    Возвращает список (y_верх, y_низ) в долях картинки, сверху вниз."""
    from PIL import Image
    im = Image.open(image_path).convert("RGB"); W, H = im.size; px = im.load()
    x0, x1 = int(W * 0.06), int(W * 0.94); n = max(1, (x1 - x0) // 3)
    borders = []
    for y in range(int(H * 0.45), H):
        c = 0
        for x in range(x0, x1, 3):
            r, g, b = px[x, y]; lum = (r + g + b) // 3
            if 195 < lum < 249:
                c += 1
        if c / n > 0.5:          # длинная горизонтальная линия-рамка
            if borders and y - borders[-1] <= 6:
                borders[-1] = y
            else:
                borders.append(y)
    boxes = []; i = 0
    while i + 1 < len(borders):
        t, b = borders[i], borders[i + 1]; h = (b - t) / H
        if 0.05 <= h <= 0.14:
            boxes.append((round(t / H, 3), round(b / H, 3))); i += 2
        else:
            i += 1
    return boxes


_EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF"
    "\U00002B00-\U00002BFF\U00002190-\U000021FF️⃣]+")


def _speakable(text: str) -> str:
    """Убирает эмодзи и прочие непроизносимые значки — чтобы Яндекс не менял
    интонацию/тембр, встречая их. На экране текст остаётся с эмодзи."""
    return re.sub(r"\s{2,}", " ", _EMOJI_RE.sub("", text)).strip()


def parse_narration(text: str):
    """(вопрос, пояснение_без_тегов, стрелки, ответ, зелёный).
    стрелки: список (char_start, char_end, x, y, dir) — позиции фразы в пояснении.
    зелёный: (x, y, ш, в) — прямоугольник правильного варианта (доли картинки) или None."""
    block = text.strip()

    # Тег зелёной полоски {зелёный:x,y,ш,в} — может стоять где угодно; вырезаем,
    # чтобы голос его не читал. Загорится в момент «Ответ».
    green = None
    mg = re.search(
        r"\{\s*зел[её]н(?:ый|ая)?\s*:\s*([0-9.]+)\s*,\s*([0-9.]+)"
        r"(?:\s*,\s*([0-9.]+)\s*,\s*([0-9.]+))?\s*\}",
        block)
    if mg:
        if mg.group(3) is not None:
            # 4 цифры: x, y, ширина, высота (полный контроль).
            green = (float(mg.group(1)), float(mg.group(2)),
                     float(mg.group(3)), float(mg.group(4)))
        else:
            # 2 цифры: y (верх полоски) и высота — ширина на всю карточку.
            green = (0.0, float(mg.group(1)), 1.0, float(mg.group(2)))
        block = (block[:mg.start()] + block[mg.end():]).strip()

    m_exp = re.search(r"Пояснени[ея]\s*:\s*", block)
    m_ans = re.search(r"Ответ\s*:\s*", block)
    if not m_exp or not m_ans:
        raise SystemExit("В тексте озвучки нужны метки «Пояснение:» и «Ответ:».")
    question = block[:m_exp.start()].strip()
    expl = block[m_exp.end():m_ans.start()].strip()
    answer = block[m_ans.end():].strip()

    # Теги пометок в пояснении (появляются в такт голосу):
    #   {стрелка:x,y[,dir]}текст{/}          — красная стрелка
    #   {обвести:x,y[,r]}текст{/}            — обводка маркером (как от руки)
    #   {подчеркнуть:x,y[,ш]}текст{/}        — подчёркивание маркером
    tag_re = re.compile(
        r"\{\s*(стрелка|обвести|круг|подчеркнуть|линия)\s*:\s*([0-9.]+)\s*,\s*([0-9.]+)"
        r"\s*(?:,\s*([0-9.a-zA-Zа-яА-Я]{1,4}))?\s*\}(.*?)\{\s*/\s*\}", re.S)
    plain = ""
    spans = []    # стрелки: (cs, ce, x, y, dir)
    marks = []    # пометки: (cs, ce, x, y, size, kind)  kind: circle/underline
    pos = 0
    for mo in tag_re.finditer(expl):
        plain += expl[pos:mo.start()]
        kind = mo.group(1).lower()
        x, y = float(mo.group(2)), float(mo.group(3))
        extra = (mo.group(4) or "").strip()
        phrase = mo.group(5)
        cs = len(plain); plain += phrase; ce = len(plain)
        if kind == "стрелка":
            spans.append((cs, ce, x, y, extra.lower()))
        else:
            try:
                size = float(extra) if extra else 0.0
            except ValueError:
                size = 0.0
            mk = "underline" if kind in ("подчеркнуть", "линия") else "circle"
            marks.append((cs, ce, x, y, size, mk))
        pos = mo.end()
    plain += expl[pos:]
    return question, plain, spans, answer, green, marks


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
  .arrow { position:absolute; width:112px; height:112px; --r:0deg; opacity:0;
           margin-left:-12px; margin-top:-12px; transform-origin:12px 12px;
           transform: rotate(var(--r)) scale(.55);
           transition: opacity .16s ease-out, transform .16s ease-out; }
  .arrow.show { opacity:1; transform: rotate(var(--r)) scale(1); }
  .arrow svg { width:100%; height:100%; filter: drop-shadow(0 3px 7px rgba(0,0,0,.55)); }
  /* Пометки маркером (обводка/подчёркивание) — «рисуются» от руки. */
  .marks { position:absolute; inset:0; pointer-events:none; z-index:22; }
  .mark { position:absolute; opacity:0; transition:opacity .15s ease-out; }
  .mark.show { opacity:1; }
  .mark svg { width:100%; height:100%; overflow:visible;
              filter: drop-shadow(0 2px 5px rgba(0,0,0,.4)); }
  .mark .ink { fill:none; stroke:#ff2e2e; stroke-width:10; stroke-linecap:round;
               stroke-linejoin:round;
               stroke-dasharray: var(--len); stroke-dashoffset: var(--len);
               animation: draw .5s ease-out forwards; }
  @keyframes draw { to { stroke-dashoffset: 0; } }
  .green { position:absolute; opacity:0; border-radius:14px;
           background:rgba(52,199,89,.42); border:6px solid #2fbf57;
           box-shadow:0 0 0 3px rgba(255,255,255,.3) inset, 0 8px 26px rgba(0,0,0,.35);
           transition: opacity .16s ease-out, transform .16s ease-out;
           transform: scale(.96); }
  .green.show { opacity:1; transform: scale(1); }
  /* Галочка ✅ рядом с зелёным — выпрыгивает. */
  .check { position:absolute; z-index:25; font-size:120px; opacity:0;
           margin-left:-60px; margin-top:-70px;
           transform: scale(.2) rotate(-25deg);
           filter: drop-shadow(0 6px 16px rgba(0,0,0,.5)); }
  .check.show { animation: pop .5s cubic-bezier(.2,1.5,.4,1) forwards; }
  @keyframes pop { 0%{opacity:0;transform:scale(.2) rotate(-25deg);}
                   60%{opacity:1;transform:scale(1.25) rotate(8deg);}
                   100%{opacity:1;transform:scale(1) rotate(0);} }
  /* Лёгкий зум фона (как у блогеров). */
  .bg { animation: kb 14s ease-in-out infinite alternate; }
  @keyframes kb { from{transform:scale(1);} to{transform:scale(1.12);} }
  /* Карточка влетает с зумом. */
  .wrap { opacity:0; transform: scale(.88) translateY(40px); }
  .wrap.enter { animation: cardin .5s cubic-bezier(.2,.8,.2,1) forwards; }
  @keyframes cardin { to{opacity:1; transform: scale(1) translateY(0);} }
  /* Интро-хук — экран-интрига в начале. */
  /* Хук-экран в теме ПДД: тёмный фон, жёлто-чёрные полосы опасности сверху и
     снизу, большой предупреждающий знак, бейдж и текст-интрига. */
  .hook { position:fixed; inset:0; z-index:60; display:flex;
          align-items:center; justify-content:center;
          background: radial-gradient(130% 90% at 50% 30%, #17263f 0%, #070a12 100%);
          opacity:0; pointer-events:none; overflow:hidden; }
  .hook.show { opacity:1; }
  .hstripe { position:absolute; left:0; right:0; height:46px; opacity:.92;
             background: repeating-linear-gradient(45deg,#ffcf2b 0 26px,#141821 26px 52px); }
  .htop { top:0; } .hbot { bottom:0; }
  .hcenter { display:flex; flex-direction:column; align-items:center;
             gap:38px; padding:0 70px; text-align:center; }
  .hbadge { background:#ff3b30; color:#fff; font:800 42px Arial; padding:12px 34px;
            border-radius:44px; letter-spacing:2px; box-shadow:0 10px 30px rgba(0,0,0,.5);
            opacity:0; transform:translateY(-14px); }
  .hook.show .hbadge { animation: hbin .4s ease-out .05s forwards; }
  @keyframes hbin { to{opacity:1; transform:translateY(0);} }
  .hsign { width:300px; filter: drop-shadow(0 14px 34px rgba(230,30,30,.55));
           opacity:0; transform:scale(.6); }
  .hook.show .hsign { animation: hsin .5s cubic-bezier(.2,1.5,.4,1) .12s forwards,
                                 signpulse 1.2s ease-in-out .7s infinite; }
  @keyframes hsin { to{opacity:1; transform:scale(1);} }
  @keyframes signpulse { 0%,100%{transform:scale(1);} 50%{transform:scale(1.07);} }
  .hsign svg { width:100%; height:auto; display:block; }
  .htext { color:#fff; font:900 80px/1.16 Arial,sans-serif;
           text-shadow:0 6px 30px rgba(0,0,0,.6); opacity:0; transform:scale(.8); }
  .hook.show .htext { animation: htin .45s cubic-bezier(.2,1.4,.4,1) .25s forwards; }
  @keyframes htin { to{opacity:1; transform:scale(1);} }
  /* Плашка-интрига ПОВЕРХ карточки в начале (вместо отдельного экрана). */
  .caption { position:fixed; top:5%; left:50%; z-index:45;
             transform:translateX(-50%) translateY(-24px); opacity:0;
             width:min(920px,92%); text-align:center;
             background:rgba(10,14,22,.85); border:4px solid #ffcf2b;
             border-radius:28px; padding:24px 34px;
             box-shadow:0 16px 44px rgba(0,0,0,.55); }
  .caption.show { animation: capin .45s cubic-bezier(.2,1.4,.4,1) forwards; }
  @keyframes capin { to{opacity:1; transform:translateX(-50%) translateY(0);} }
  .caption .cbadge { display:inline-block; background:#ff3b30; color:#fff;
             font:800 30px Arial; padding:7px 20px; border-radius:30px;
             letter-spacing:1px; margin-bottom:14px; }
  .caption .ctext { color:#fff; font:900 58px/1.15 Arial;
             text-shadow:0 3px 14px rgba(0,0,0,.65); }
  .timer { position:fixed; top:50%; left:50%; z-index:30;
           width:340px; height:340px; border-radius:50%;
           background:rgba(15,17,22,.72); border:12px solid #fff;
           box-shadow:0 24px 70px rgba(0,0,0,.55);
           display:flex; align-items:center; justify-content:center;
           font:900 200px -apple-system,Arial,sans-serif; color:#fff;
           opacity:0; transform:translate(-50%,-50%) scale(.7);
           transition:opacity .2s ease-out; }
  .timer.show { opacity:1; }
  .timer.tick { animation: tk .5s ease-out; }
  @keyframes tk { 0%{transform:translate(-50%,-50%) scale(1.18);}
                  100%{transform:translate(-50%,-50%) scale(1);} }
  .grid { position:absolute; inset:0; pointer-events:none; }
  .gl { position:absolute; background:rgba(255,0,0,.45); }
  .gl.h { left:0; right:0; height:2px; }
  .gl.v { top:0; bottom:0; width:2px; }
  .glab { position:absolute; font:700 22px Arial; color:#fff; background:rgba(200,0,0,.8);
          padding:2px 7px; border-radius:6px; }
</style></head>
<body>
  <div class="bg" id="bg"></div>
  <div class="wrap" id="wrap">
    <img id="img" alt="">
    <div class="green" id="green"></div>
    <div class="check" id="check">✅</div>
    <div class="marks" id="marks"></div>
    <div class="arrows" id="arrows"></div>
    <div class="grid" id="grid"></div>
  </div>
  <div class="timer" id="timer">5</div>
  <div class="hook" id="hook">
    <div class="hstripe htop"></div>
    <div class="hcenter">
      <div class="hbadge">⚠️ ТЕСТ ПДД</div>
      <div class="hsign">
        <svg viewBox="0 0 120 108">
          <polygon points="60,7 115,101 5,101" fill="#fff" stroke="#e11919"
                   stroke-width="9" stroke-linejoin="round"/>
          <text x="60" y="90" text-anchor="middle" font-family="Arial"
                font-size="64" font-weight="900" fill="#141414">?</text>
        </svg>
      </div>
      <div class="htext" id="htext"></div>
    </div>
    <div class="hstripe hbot"></div>
  </div>
  <div class="caption" id="caption">
    <div class="cbadge">⚠️ ТЕСТ ПДД</div>
    <div class="ctext" id="ctext"></div>
  </div>
<script id="payload" type="application/json">__DATA__</script>
<script>
  const D = JSON.parse(document.getElementById("payload").textContent);
  const img = document.getElementById("img");
  img.src = D.image;
  document.getElementById("bg").style.backgroundImage = "url(" + D.image + ")";
  const arrowsEl = document.getElementById("arrows");
  const ARROW_SVG = '<svg viewBox="0 0 112 112"><g stroke="#ff2323" stroke-width="12" fill="none" stroke-linecap="round" stroke-linejoin="round"><line x1="104" y1="104" x2="12" y2="12"/><path d="M12 46 L12 12 L46 12"/></g></svg>';
  function dirAngle(x,dir){
    const m={l:135,r:315,t:225,b:45,tl:0,tr:90,br:180,bl:270};
    if(dir && m[dir]!==undefined) return m[dir];
    return x<0.5 ? 315 : 135;   // авто: объект слева → остриё влево; справа → вправо
  }
  function showArrow(x,y,dir){
    const a = document.createElement("div"); a.className="arrow";
    a.style.left=(x*100)+"%"; a.style.top=(y*100)+"%";
    a.style.setProperty("--r", dirAngle(x,dir)+"deg");
    a.innerHTML=ARROW_SVG;
    arrowsEl.appendChild(a); requestAnimationFrame(()=>a.classList.add("show"));
  }
  function clearArrows(){ arrowsEl.querySelectorAll(".arrow").forEach(a=>{
    a.classList.remove("show"); setTimeout(()=>a.remove(),200); }); }
  function showGreen(x,y,w,h){ const g=document.getElementById("green");
    g.style.left=(x*100)+"%"; g.style.top=(y*100)+"%";
    g.style.width=(w*100)+"%"; g.style.height=(h*100)+"%";
    g.classList.add("show");
    // галочка ✅ у правого края зелёной полоски — выпрыгивает
    const c=document.getElementById("check");
    c.style.left=((x+w)*100)+"%"; c.style.top=((y+h/2)*100)+"%";
    c.classList.remove("show"); void c.offsetWidth; c.classList.add("show"); }
  function hideGreen(){ const g=document.getElementById("green"); if(g) g.classList.remove("show"); }
  // Пометки маркером (как от руки): обводка-эллипс и подчёркивание.
  const marksEl = document.getElementById("marks");
  const ELLIPSE = '<svg viewBox="0 0 200 130"><path class="ink" d="M104 10 C 44 8 14 42 14 66 C 14 102 58 122 104 120 C 156 118 192 96 189 60 C 186 26 150 12 98 12"/></svg>';
  const UNDER   = '<svg viewBox="0 0 220 34"><path class="ink" d="M6 20 C 55 10 120 28 160 16 C 180 11 200 15 214 12"/></svg>';
  function _mark(x, y, wpx, hpx, svg) {
    const iw = img.clientWidth || 1040;
    const d = document.createElement("div"); d.className = "mark";
    d.style.width = wpx + "px"; d.style.height = hpx + "px";
    d.style.left = "calc(" + (x * 100) + "% - " + (wpx / 2) + "px)";
    d.style.top = "calc(" + (y * 100) + "% - " + (hpx / 2) + "px)";
    d.innerHTML = svg;
    marksEl.appendChild(d);
    const p = d.querySelector(".ink");
    try { const len = p.getTotalLength(); p.style.setProperty("--len", len); } catch (e) {}
    requestAnimationFrame(() => d.classList.add("show"));
  }
  function showCircle(x, y, r) {
    const iw = img.clientWidth || 1040;
    const wpx = (r && r > 0 ? r : 0.12) * iw * 2;
    _mark(x, y, wpx, wpx * 0.66, ELLIPSE);
  }
  function showUnderline(x, y, w) {
    const iw = img.clientWidth || 1040;
    const wpx = (w && w > 0 ? w : 0.22) * iw;
    _mark(x, y, wpx, wpx * 0.16, UNDER);
  }
  function clearMarks() {
    marksEl.querySelectorAll(".mark").forEach(m => {
      m.classList.remove("show"); setTimeout(() => m.remove(), 200);
    });
  }
  function enterCard(){ document.getElementById("wrap").classList.add("enter"); }
  function showHook(text){ document.getElementById("htext").textContent=text||"";
    document.getElementById("hook").classList.add("show"); }
  function hideHook(){ document.getElementById("hook").classList.remove("show"); }
  function showCaption(text){ document.getElementById("ctext").textContent=text||"";
    document.getElementById("caption").classList.add("show"); }
  function hideCaption(){ const c=document.getElementById("caption");
    c.classList.remove("show"); c.style.opacity="0"; }
  function countdown(sec){
    const el = document.getElementById("timer");
    let n = Math.round(sec); el.textContent = n;
    el.classList.add("show","tick");
    setTimeout(()=>el.classList.remove("tick"), 500);
    const iv = setInterval(()=>{ n--;
      if(n>=1){ el.textContent = n; el.classList.remove("tick");
                void el.offsetWidth; el.classList.add("tick"); }
      else { clearInterval(iv); el.classList.remove("show","tick"); } }, 1000);
  }
  // Режим калибровки: сетка координат + все стрелки статично.
  if (D.grid) {
    const g = document.getElementById("grid");
    for (let i=1;i<10;i++){
      const f=i/10;
      const h=document.createElement("div"); h.className="gl h"; h.style.top=(f*100)+"%"; g.appendChild(h);
      const v=document.createElement("div"); v.className="gl v"; v.style.left=(f*100)+"%"; g.appendChild(v);
      const lt=document.createElement("div"); lt.className="glab"; lt.textContent=f.toFixed(1);
      lt.style.top="2px"; lt.style.left=(f*100)+"%"; g.appendChild(lt);
      const ll=document.createElement("div"); ll.className="glab"; ll.textContent=f.toFixed(1);
      lt.style.transform="translateX(-50%)"; ll.style.left="2px"; ll.style.top=(f*100)+"%"; g.appendChild(ll);
    }
    // Сетка: карточка видна СРАЗУ, без анимации влёта (иначе кадр смазан).
    const _w = document.getElementById("wrap");
    _w.style.opacity = "1"; _w.style.transform = "none";
    (D.arrows||[]).forEach(p=>showArrow(p[0],p[1],p[2]));
    arrowsEl.querySelectorAll(".arrow").forEach(a=>a.classList.add("show"));
    // Пометки рисуем, ТОЛЬКО когда у картинки появилась реальная ширина
    // (иначе размер пометок нулевой и их не видно). Опрашиваем до ~1.5 сек.
    const drawMarks = () => (D.marks||[]).forEach(m=>{
      if(m[3]==='underline') showUnderline(m[0],m[1],m[2]); else showCircle(m[0],m[1],m[2]); });
    const tryDrawMarks = (n) => {
      if (img.clientWidth) { drawMarks(); return; }
      if (n > 0) requestAnimationFrame(() => tryDrawMarks(n - 1));
    };
    tryDrawMarks(90);
    if (D.green) showGreen(D.green[0],D.green[1],D.green[2],D.green[3]);
  }
  window.__tt = { ready:true, showArrow, clearArrows, countdown, showGreen, hideGreen,
                  enterCard, showHook, hideHook, showCaption, hideCaption,
                  showCircle, showUnderline, clearMarks };
</script>
</body></html>"""


def build_page(image_uri, grid=False, arrows=None, green=None, marks=None):
    data = {"image": image_uri, "grid": grid, "arrows": arrows or [],
            "green": green, "marks": marks or []}
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    return _PAGE.replace("__DATA__", payload)


async def screenshot_page(page_html, out_png, executable_path=None):
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
        page = await browser.new_page(viewport={"width": 1080, "height": 1920})
        await page.goto(page_uri)
        await page.wait_for_function("window.__tt && window.__tt.ready")
        await asyncio.sleep(1.1)
        await page.screenshot(path=out_png)
        await browser.close()


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
                think_pause, chromium_path, grid=False, correct=None, role="good",
                hook="", no_hook=False):
    image_uri = _image_data_uri(Path(image_path))
    raw = read_text_any(speak_txt)
    # Интро-хук: строка «Хук: …» в тексте, либо --hook, либо дефолт.
    hook_text = hook or ""
    mh = re.search(r"(?mi)^\s*Хук\s*:\s*(.+)$", raw)
    if mh:
        if not hook_text:
            hook_text = mh.group(1).strip()
        raw = raw[:mh.start()] + raw[mh.end():]
    if not hook_text:
        hook_text = "А ты знаешь ответ? У тебя пять секунд!"
    if no_hook:
        hook_text = ""
    question_txt, expl_txt, spans, answer_txt, green, marks = parse_narration(raw)

    # Автоматика: если дан номер правильного (--correct N) и нет ручного тега —
    # сами находим боксы вариантов и красим нужный. Без координат и калибровки.
    if green is None and correct:
        boxes = detect_boxes(image_path)
        if not boxes:
            print("⚠️ Не нашёл боксы вариантов на картинке — поставь тег {зелёный:y,высота} вручную.")
        elif 1 <= correct <= len(boxes):
            t, b = boxes[correct - 1]
            green = (0.03, round(t - 0.004, 3), 0.94, round(b - t + 0.008, 3))
            print(f"🟩 Нашёл {len(boxes)} вариант(ов); зелёным будет №{correct} (y {t}-{b}).")
        else:
            print(f"⚠️ На картинке {len(boxes)} вариант(ов), а --correct {correct} — проверь номер.")

    # Режим калибровки: сохранить картинку с сеткой координат, стрелками и зелёной полоской.
    if grid:
        arrows = [(x, y, d) for _, _, x, y, d in spans]
        gmarks = [(x, y, size, mk) for _, _, x, y, size, mk in marks]
        page = build_page(image_uri, grid=True, arrows=arrows,
                          green=list(green) if green else None, marks=gmarks)
        await screenshot_page(page, out, executable_path=chromium_path)
        print(f"🧭 Сетка координат готова: {out}")
        print("   Красные стрелки — где сейчас стоят твои координаты. Подгони цифры "
              "по подписям сетки (0.0–1.0) и пересобери видео.")
        return

    cache_rate = rate
    if engine == "yandex3":
        # Живые голоса v3 (anton, alexander, kirill, marina…) с интонацией-ролью.
        import os
        ya_key = os.environ.get("YANDEX_API_KEY", "").strip()
        if not ya_key:
            raise SystemExit("Для yandex3 задай YANDEX_API_KEY.")
        ya_voice = voice if voice and not re.search(r"Neural|ru-RU-", voice) else "anton"
        cache_rate = role or ""   # роль входит в ключ кэша

        async def base_synth(t):
            return await asyncio.to_thread(synth_yandex_v3, t, ya_voice, ya_key, role)
    elif engine == "yandex":
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

    synth = make_cached_synth(base_synth, engine, voice, cache_rate, pitch, 4)
    tmp = Path(tempfile.mkdtemp(prefix="tiktok_"))
    print("⏳ Озвучиваю…")

    async def voice_to(text, name):
        data = await synth(_speakable(text))   # без эмодзи — чтобы голос не «спотыкался»
        p = tmp / name
        p.write_bytes(data)
        return str(p), media_duration(str(p))

    tp = max(1, int(round(think_pause)))
    clips, gaps, events = [], [], []

    # 0) Карточка видна С НАЧАЛА; поверх в начале — плашка-интрига + голос.
    t0 = 0.0
    events.append((0.0, "window.__tt.enterCard()"))
    if hook_text:
        hp, hd = await voice_to(hook_text, "hook.mp3")
        clips.append((hp, hd)); gaps.append(0.25)   # маленькая пауза после хука
        events.append((0.0, f"window.__tt.showCaption({json.dumps(hook_text)})"))
        events.append((round(hd, 3), "window.__tt.hideCaption()"))
        t0 = hd + 0.25

    qp, qd = await voice_to(question_txt, "q.mp3")
    clips.append((qp, qd)); gaps.append(0.0)
    ticks_path = str(tmp / "ticks.wav")
    make_ticks(tp, ticks_path)
    td_ = media_duration(ticks_path)
    clips.append((ticks_path, td_)); gaps.append(0.0)   # пауза с тиканьем
    ep, ed = await voice_to(expl_txt, "expl.mp3")
    clips.append((ep, ed)); gaps.append(0.0)
    ap, ad = await voice_to("Ответ. " + answer_txt, "a.mp3")
    clips.append((ap, ad)); gaps.append(0.0)

    events.append((round(t0, 3), "window.__tt.clearArrows()"))
    events.append((round(t0 + qd, 3), f"window.__tt.countdown({tp})"))
    t_expl = t0 + qd + td_
    L = max(1, len(expl_txt))
    for cs, ce, x, y, d in spans:
        events.append((round(t_expl + (cs / L) * ed, 3), f"window.__tt.showArrow({x},{y},'{d}')"))
        events.append((round(t_expl + (ce / L) * ed, 3), "window.__tt.clearArrows()"))
    # Пометки маркером (обводка/подчёркивание) — в такт голосу.
    for cs, ce, x, y, size, mk in marks:
        fn = "showUnderline" if mk == "underline" else "showCircle"
        events.append((round(t_expl + (cs / L) * ed, 3), f"window.__tt.{fn}({x},{y},{size})"))
        events.append((round(t_expl + (ce / L) * ed, 3), "window.__tt.clearMarks()"))
    events.append((round(t_expl + ed, 3), "window.__tt.clearArrows(); window.__tt.clearMarks()"))
    total = t0 + qd + td_ + ed + ad
    # Зелёная полоска — ПОСЛЕ того как голос договорил ответ (конец реплики),
    # и держим её ещё пару секунд, чтобы зритель увидел.
    if green:
        green_hold = 1.8
        events.append((round(total, 3),
                       f"window.__tt.showGreen({green[0]},{green[1]},{green[2]},{green[3]})"))
        total += green_hold
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
    ap.add_argument("--engine", choices=["edge", "silero", "yandex", "yandex3"],
                    default="yandex3",
                    help="yandex3 — живые голоса (anton и др.), по умолчанию")
    ap.add_argument("--voice", default="anton", help="голос (для yandex3: anton, alexander, "
                                                      "kirill, marina, jane…)")
    ap.add_argument("--role", default="good", help="интонация для yandex3: good/neutral/"
                                                   "friendly/strict (по голосу)")
    ap.add_argument("--rate", default="+0%")
    ap.add_argument("--pitch", default="+0Hz")
    ap.add_argument("--think", type=float, default=5.0, help="пауза «зритель думает», сек")
    ap.add_argument("--correct", type=int, default=None,
                    help="номер правильного варианта (1,2,3…). Программа сама найдёт бокс "
                         "этого варианта и зажжёт его зелёным в момент «Ответ» — без координат")
    ap.add_argument("--grid", action="store_true",
                    help="калибровка: сохранить PNG с сеткой координат и текущими стрелками "
                         "(output укажи как .png), видео не собирать")
    ap.add_argument("--hook", default="",
                    help="текст интро-интриги в начале (голос + анимация). Можно задать и "
                         "строкой «Хук: …» в файле. По умолчанию — свой текст.")
    ap.add_argument("--no-hook", dest="no_hook", action="store_true",
                    help="без интро-хука (сразу карточка)")
    ap.add_argument("--chromium-path", default=None)
    args = ap.parse_args()
    asyncio.run(build(
        args.image, args.output, args.speak, args.voice, args.engine,
        args.rate, args.pitch, args.think, args.chromium_path, grid=args.grid,
        correct=args.correct, role=args.role, hook=args.hook, no_hook=args.no_hook,
    ))


if __name__ == "__main__":
    main()
