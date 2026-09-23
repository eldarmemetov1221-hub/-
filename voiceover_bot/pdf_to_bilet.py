"""
Конвертер: PDF со страницы «Ответы на билет N» (экзамен-пдд.рф) -> файл для
auto_quiz.py (вопрос + варианты + правильный ответ + пояснение).

    python pdf_to_bilet.py "билет6.pdf" bilet6.txt

Правильный ответ определяется по пояснению (оно цитирует верный вариант). Это
не 100% надёжно на вопросах, где варианты — числа; поэтому в конце печатается
список ответов — сверь его глазами и при необходимости поправь строку
«Ответ: N» в готовом файле. Картинки этот конвертер не вытаскивает.
"""

import re
import sys
from pathlib import Path

import pymupdf


def _norm(s: str) -> str:
    # «ĸ» (U+0138) на страницах экзамен-пдд стоит вместо «к».
    return re.sub(r"\s+", " ", s.replace("ĸ", "к").replace("\xad", "")).strip()


def _stems(s: str) -> set[str]:
    out = set()
    for w in re.findall(r"[а-я]+|\d+", s.lower().replace("ё", "е")):
        if w.isdigit():
            out.add(w)
        elif len(w) > 3:
            out.add(w[:6])
    return out


def _guess_correct(expl: str, options: list[str]) -> int:
    """Правильный вариант = тот, чьи слова сильнее всего встречаются в пояснении."""
    hs = _stems(expl)
    best, bs = 0, 0.0
    for i, o in enumerate(options):
        os_ = _stems(o)
        if not os_:
            continue
        sc = len(os_ & hs) / len(os_)
        if sc > bs:
            best, bs = i, sc
    return best


def parse_pdf(pdf_path: str) -> list[dict]:
    d = pymupdf.open(pdf_path)
    blocks = [_norm(b[4]) for b in d[0].get_text("blocks") if b[4].strip()]
    # Вопросы — от первого вопроса до «Пройти тест по билету».
    i0 = next((i for i, t in enumerate(blocks)
               if re.match(r"^[А-ЯЁ].{10,}\?", t) or t.startswith("Что называется")), 0)
    i1 = next((i for i, t in enumerate(blocks)
               if i > i0 and t.startswith("Пройти тест по билету")), len(blocks))
    region = blocks[i0:i1]

    qs: list[dict] = []
    cur = None
    for t in region:
        if t == "комментировать":
            if cur:
                qs.append(cur); cur = None
            continue
        mo = re.match(r"^(\d+)\.\s*(.*)", t)
        # Вариант — только если номер идёт по порядку (иначе это номер знака 3.2 и т.п.).
        if cur is not None and mo and len(mo.group(1)) == 1 \
                and int(mo.group(1)) == len(cur["options"]) + 1:
            cur["options"].append(mo.group(2))
        elif cur is None:
            cur = {"q": t, "options": [], "expl": ""}
        elif not cur["options"]:
            cur["q"] += " " + t
        else:
            cur["expl"] = (cur["expl"] + " " + t).strip()
    if cur:
        qs.append(cur)
    return qs


def main() -> None:
    if len(sys.argv) < 2:
        print("Использование: python pdf_to_bilet.py билет.pdf [итог.txt]")
        return
    pdf = sys.argv[1]
    out = sys.argv[2] if len(sys.argv) > 2 else str(Path(pdf).with_suffix(".txt"))
    qs = parse_pdf(pdf)
    print(f"Разобрано вопросов: {len(qs)}")

    lines = []
    for n, q in enumerate(qs, 1):
        idx = _guess_correct(q["expl"], q["options"])
        print(f"  Вопрос {n}: ответ №{idx + 1}  {q['options'][idx][:50] if q['options'] else ''}")
        lines.append(f"Вопрос {n}")
        lines.append(q["q"])
        for k, o in enumerate(q["options"], 1):
            lines.append(f"{k}. {o}")
        lines.append(f"Ответ: {idx + 1}")
        if q["expl"]:
            lines.append(f"Пояснение: {q['expl']}")
        lines.append("")
    Path(out).write_text("\n".join(lines), encoding="utf-8")
    print(f"\n💾 Готово: {out}")
    print("⚠️  Сверь ответы выше глазами (особенно где варианты — числа) и поправь "
          "строки «Ответ: N» при необходимости.")


if __name__ == "__main__":
    main()
