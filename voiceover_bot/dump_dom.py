"""Диагностика: показать, что сайт pdd-exam отдаёт как «варианты ответа» у
конкретного вопроса билета. Нужен, чтобы понять, откуда берутся лишние
варианты (было 5 вместо 3), и точечно их отсечь.

Запуск (в папке voiceover_bot, с интернетом/VPN как обычно):
    python dump_dom.py 4 14

Выведет по каждому вопросу до нужного: сколько списков ответов на странице,
и по каждому пункту — текст, классы, видимость (display/visibility/opacity),
размеры и имя родительского узла. Скопируй весь вывод и пришли мне.
"""

import asyncio
import json
import sys

import auto_quiz
from playwright.async_api import async_playwright

BILET_URL = "https://pdd-exam.ru/bilet/{n}/"

INIT_JS = "window.yaContextCb={push:function(){}};"

CLEAN_CSS = """
  .main-upline,.main-header,.main-footer,[id^="yandex_rtb"],[id^="adfox"],
  ins,.adsbygoogle{display:none!important;}
"""

PROBE = r"""() => {
  const dump = el => {
    const bt = el.querySelector('.bilet__answer-btn') || el;
    const s = getComputedStyle(el);
    const r = el.getBoundingClientRect();
    return {
      txt: bt.textContent.trim().slice(0, 60),
      item_cls: el.className,
      btn_cls: (el.querySelector('.bilet__answer-btn')||{}).className || '(нет .bilet__answer-btn)',
      display: s.display, visibility: s.visibility, opacity: s.opacity,
      w: Math.round(r.width), h: Math.round(r.height),
      parent: el.parentElement ? el.parentElement.className : '(root)',
    };
  };
  const lists = [...document.querySelectorAll('.bilet__answer-list')];
  const numEl = document.querySelector('.bilet__qs-num');
  const q = document.querySelector('.bilet__question');
  return {
    num: numEl ? numEl.textContent.trim() : '?',
    question: q ? q.textContent.trim().slice(0, 70) : '',
    n_lists: lists.length,
    lists: lists.map((L, li) => ({
      list_index: li,
      list_cls: L.className,
      list_parent: L.parentElement ? L.parentElement.className : '(root)',
      items: [...L.querySelectorAll('.bilet__answer-item')].map(dump),
      // на случай, если пункты не .bilet__answer-item:
      raw_btns: [...L.querySelectorAll('.bilet__answer-btn')].map(b => b.textContent.trim().slice(0,60)),
    })),
  };
}"""


async def main(bilet: int, qnum: int) -> None:
    exe = auto_quiz._find_chromium()
    kw = {"args": ["--no-sandbox"]}
    if exe:
        kw["executable_path"] = exe
    async with async_playwright() as pw:
        b = await pw.chromium.launch(**kw)
        ctx = await b.new_context(viewport={"width": 1280, "height": 720})
        page = await ctx.new_page()
        await page.add_init_script(INIT_JS)
        await page.goto(BILET_URL.format(n=bilet), wait_until="domcontentloaded", timeout=60000)
        try:
            await page.click("[data-cookie-consent-accept]", timeout=2500)
        except Exception:
            pass
        await page.add_style_tag(content=CLEAN_CSS)
        await page.wait_for_selector(".bilet__answer-btn", timeout=30000)

        async def cur_num():
            return await page.evaluate(
                "()=>{const e=document.querySelector('.bilet__qs-num');return e?parseInt(e.textContent):null;}")

        for _ in range(40):
            c = await cur_num()
            if c == qnum:
                break
            # Ответить (жмём первый вариант из зоны вопроса) — иначе сайт не
            # пускает на следующий вопрос.
            await page.evaluate("""()=>{
              const L=document.querySelector('.bilet__qs-zone .bilet__answer-list')
                      ||document.querySelector('.bilet__answer-list');
              const b=L&&L.querySelector('.bilet__answer-item .bilet__answer-btn');
              if(b)b.click();
            }""")
            await asyncio.sleep(0.4)
            await page.evaluate("()=>{const b=document.querySelector('.bilet__next-btn'); if(b)b.click();}")
            try:
                await page.wait_for_function(
                    "(k)=>{const e=document.querySelector('.bilet__qs-num');"
                    "return e && parseInt(e.textContent)!==k;}", arg=c, timeout=6000)
            except Exception:
                pass
            await asyncio.sleep(0.3)
        await asyncio.sleep(1.0)
        print(f"(дошёл до вопроса {await cur_num()})")

        info = await page.evaluate(PROBE)
        print("=" * 70)
        print(json.dumps(info, ensure_ascii=False, indent=1))
        print("=" * 70)
        await b.close()


if __name__ == "__main__":
    bilet = int(sys.argv[1]) if len(sys.argv) > 1 else 4
    qnum = int(sys.argv[2]) if len(sys.argv) > 2 else 14
    asyncio.run(main(bilet, qnum))
