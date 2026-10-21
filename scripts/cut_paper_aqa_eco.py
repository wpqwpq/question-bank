#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""cut_paper_aqa_eco.py — 牛津国际 AQA 经济（9625 / EC01–EC04）切割

口径（2026-10-21 培琪定）：
  · 一题一图，主问题为单位（q18 含 18 / 18.1 / 18.2），不按小问拆
  · 不留答题区：裁到内容底，答案横线区整段丢弃
  · 需要资料的题：**整本资料册两页**拼在题目前面（material_order=before）
  · 资料册封面页（p1）与空白版权页（p4）丢弃
  · 选择题的选项答题圈**保留**
  · Section D 的 Q15/Q16 各自独立成题（Either / or 丢弃）
  · 2023-june U2/U3/U4 缺资料册，整卷不切

落盘：data/aqa/a-level/economics/<year>-<session>-ec0N/
      source.pdf = 资料册 p2+p3 + 原卷全部页（合并卷，cut.json 记 booklet_pages）
      cut.json / paper.md / qNN.md / assets/qNN.webp + qNN.layout.json
"""
import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

import pymupdf

ROOT = Path(__file__).resolve().parent.parent
QP_ROOT = ROOT / "downloadPapers" / "AQA_Alevel_Economy"
INS_DIR = QP_ROOT / "_booklets"
DATA = ROOT / "data" / "aqa" / "a-level" / "economics"
RENDER = ROOT / "scripts" / "render_questions.py"

X0, X1 = 41.0, 538.0      # 卷面版心（页框 39.2–539.1）；X1 由 538.6 收到 538.0，
# 538.6 恰好压在右框线左沿，2024 起的卷会留 1px 竖线（2026-10-21 修）
BX0, BX1 = 44.0, 553.0    # 资料册版心（正文右界 551.9）
Y_TOP, Y_BOT = 56.0, 792.0
PAD_TOP, PAD_BOT = 9.0, 5.0

SKIP = {("202306", "2"), ("202306", "3"), ("202306", "4")}   # 缺资料册，整卷不切

DROP_TXT = re.compile(
    r"^(Section [A-D]\b|Look at the Source Booklet|Answer all questions in the spaces|"
    r"Answer EITHER\b|Shade the circle\b|CORRECT METHOD\b|WRONG METHODS\b|"
    r"Only one answer per question|For each question completely fill|"
    r"Do not write|Turn over|Extra space|END OF EXTRACTS|END OF QUESTIONS|"
    r"There are no questions|There is no source material|Copyright|For confidentiality|"
    r"DO NOT WRITE ON THIS PAGE|Question \d+ continues|Additional page|"
    r"Either$|or$|IB/G/|\*[0-9A-Z]+\*|\d{1,2}$)", re.I)

STOP_TXT = re.compile(
    r"^(Answer EITHER\b|Shade the circle\b|CORRECT METHOD\b|WRONG METHODS\b)", re.I)

UNIT_NAME = {
    "1": "The Operation of Markets, Market Failure and the Role of Government",
    "2": "The National Economy in a Global Environment",
    "3": "The Economics of Business Behaviour and the Distribution of Income",
    "4": "Economic Development and the Global Economy",
}


# ───────────────────────── 版面解析 ─────────────────────────
def _norm(t):
    return re.sub(r"\s+", " ", t).strip()


def page_elements(pg):
    """(rect, kind, text)；kind ∈ text / draw / rule"""
    els = []
    for b in pg.get_text("dict")["blocks"]:
        if b.get("type") != 0:
            continue
        for line in b["lines"]:
            txt = "".join(s["text"] for s in line["spans"]).strip()
            if not txt:
                continue
            r = pymupdf.Rect(line["bbox"])
            if r.y1 > Y_BOT or r.y0 < Y_TOP or r.x0 > X1 - 2:
                continue
            if DROP_TXT.match(_norm(txt)):
                continue
            els.append((r, "text", txt))
    for dr in pg.get_drawings():
        r = pymupdf.Rect(dr["rect"])
        if r.y1 > Y_BOT or r.y0 < Y_TOP or r.x0 > X1 - 2:
            continue
        if r.width < 1 and r.height < 1:
            continue
        kind = "rule" if (r.height < 2.2 and r.width > 300) else "draw"
        els.append((r, kind, ""))
    for info in pg.get_image_info():
        r = pymupdf.Rect(info["bbox"])
        if r.y0 < Y_TOP or r.y1 > Y_BOT or r.x0 > X1 - 2:
            continue
        els.append((r, "img", ""))
    els.sort(key=lambda e: (e[0].y0, e[0].x0))
    return els


def rule_runs(els):
    """答案横线成串：≥3 条、间距 ≤45pt，且相邻两条之间**没有文字**（表格行线会被排除）"""
    rules = [r for r, k, _ in els if k == "rule"]
    others = [(r.y0, r.y1) for r, k, _ in els if k in ("text", "img")]
    runs, cur = [], []
    for r in rules:
        if cur:
            gap_top, gap_bot = cur[-1].y1, r.y0
            has_text = any(t0 < gap_bot - 1 and t1 > gap_top + 1 for t0, t1 in others)
            if r.y0 - cur[-1].y0 > 45 or has_text:
                runs.append(cur)
                cur = []
        cur.append(r)
    if cur:
        runs.append(cur)
    return [x for x in runs if len(x) >= 3]


def add_question_spots(doc):
    """(page, y, qnum, sub, bbox) —— 题号锚点"""
    spots = []
    for i, pg in enumerate(doc):
        if i == 0:
            continue
        buckets = {}
        for w in pg.get_text("words"):
            if re.fullmatch(r"\d", w[4]) and w[0] < 100 and Y_TOP < w[1] < 760:
                buckets.setdefault(round(w[1]), []).append(w)
        for _y, ws in sorted(buckets.items()):
            ws.sort(key=lambda w: w[0])
            dg = [w[4] for w in ws]
            if len(dg) < 2:
                continue
            y0 = min(w[1] for w in ws)
            y1 = max(w[3] for w in ws)
            bx = pymupdf.Rect(min(w[0] for w in ws), y0, max(w[2] for w in ws), y1)
            spots.append((i, y0, int(dg[0] + dg[1]), "".join(dg[2:]) or None, tuple(bx)))
    return sorted(spots)


def paper_end(doc):
    for i, pg in enumerate(doc):
        for b in pg.get_text("dict")["blocks"]:
            if b.get("type") != 0:
                continue
            for line in b["lines"]:
                t = "".join(s["text"] for s in line["spans"])
                if "END OF QUESTIONS" in _norm(t):
                    return (i, line["bbox"][1] - 4)
    return (len(doc) - 1, Y_BOT)


def all_text_lines(pg):
    out = []
    for b in pg.get_text("dict")["blocks"]:
        if b.get("type") != 0:
            continue
        for line in b["lines"]:
            if "".join(sp["text"] for sp in line["spans"]).strip():
                r = pymupdf.Rect(line["bbox"])
                if Y_TOP <= r.y0 and r.y1 <= Y_BOT:
                    out.append(r)
    return out


def chunks_of_page(pg, y_from, y_to, page_i):
    """一页内切成若干内容块，跳过答题区/大空白"""
    els = page_elements(pg)
    lines = all_text_lines(pg)
    rule_ys = sorted(r.y0 for run in rule_runs(els) for r in run)
    seq = [(r, k, t) for r, k, t in els if y_from - 4 <= r.y0 < y_to]
    out, cur, prev_b = [], [], y_from
    for r, k, t in seq:
        if k == "rule":
            continue
        if cur and any(abs(r.y0 - ry) < 6 for ry in rule_ys):
            out.append(cur)
            cur = []
            prev_b = r.y1
            continue
        if cur and (r.y0 - prev_b > 110):
            out.append(cur)
            cur = []
        cur.append(r)
        prev_b = max(prev_b, r.y1)
    if cur:
        out.append(cur)
    slices = []
    for g in out:
        y0 = max(y_from, min(r.y0 for r in g) - PAD_TOP)
        y1 = min(y_to, max(r.y1 for r in g) + PAD_BOT)
        # 边界防切字：跨边界的文字行整行排除
        for lr in lines:
            if lr.y0 < y0 < lr.y1:
                y0 = lr.y1 + 1
            if lr.y0 < y1 < lr.y1:
                y1 = lr.y0 - 1
        rect = pymupdf.Rect(X0, y0, X1, y1)
        if rect.height > 8 and (rect.height > 30 or pg.get_text(clip=rect).strip()):
            slices.append((page_i, rect))
    return slices


def first_stop_y(pg, y_from, y_to):
    """该页 (y_from, y_to) 区间内第一个硬停标记的 y（无则 None）"""
    best = None
    for b in pg.get_text("dict")["blocks"]:
        if b.get("type") != 0:
            continue
        for line in b["lines"]:
            t = _norm("".join(s["text"] for s in line["spans"]))
            if STOP_TXT.match(t) and y_from <= line["bbox"][1] < y_to:
                y = line["bbox"][1]
                best = y if best is None else min(best, y)
    return best


def question_regions(doc, spots, qnum):
    main = [s for s in spots if s[2] == qnum][:1]
    if not main:
        return []
    p0, y0 = main[0][0], main[0][1]
    tail = [s for s in spots if (s[0], s[1]) > (p0, y0) and s[2] != qnum]
    end = paper_end(doc)
    nxt = (tail[0][0], tail[0][1]) if tail else (end[0] + 1, Y_BOT)
    if end > (p0, y0):
        p1, y1 = min(nxt, end)
    else:
        p1, y1 = nxt
    slices = []
    for p in range(p0, min(p1, len(doc) - 1) + 1):
        yf = y0 - 10 if p == p0 else Y_TOP
        yt = y1 - 6 if p == p1 else Y_BOT
        stop = first_stop_y(doc[p], yf, yt)
        if stop is not None:
            yt = min(yt, stop - 4)
        if yt > yf:
            slices += chunks_of_page(doc[p], yf, yt, p)
    return slices


def end_of_extracts_y(pg):
    for b in pg.get_text("dict")["blocks"]:
        if b.get("type") != 0:
            continue
        for line in b["lines"]:
            if "END OF EXTRACTS" in _norm("".join(sp["text"] for sp in line["spans"])):
                return line["bbox"][1]
    return None


def content_bbox(pg, x0, x1):
    """资料册整页内容外框（去页眉页脚与空白；尾部 END OF EXTRACTS 一并裁掉）"""
    ys = []
    for b in pg.get_text("blocks"):
        if 46 <= b[1] and b[3] <= 790 and b[2] < 560:
            ys.append((b[1], b[3]))
    for dr in pg.get_drawings():
        r = pymupdf.Rect(dr["rect"])
        if r.y0 >= 46 and r.y1 <= 790 and r.x0 < 560 and (r.width > 5 or r.height > 5):
            ys.append((r.y0, r.y1))
    if not ys:
        return pymupdf.Rect(x0, Y_TOP, x1, 700)
    y1 = max(b for _, b in ys)
    eoe = end_of_extracts_y(pg)
    if eoe:
        y1 = min(y1, eoe - 16)
    return pymupdf.Rect(x0, min(a for a, _ in ys) - PAD_TOP,
                        x1, min(Y_BOT, y1 + PAD_BOT))


def sections(doc):
    """[(page,y,letter)]"""
    out = []
    for i, pg in enumerate(doc):
        for b in pg.get_text("dict")["blocks"]:
            if b.get("type") != 0:
                continue
            for line in b["lines"]:
                t = _norm("".join(s["text"] for s in line["spans"]))
                m = re.match(r"^Section ([A-D])\b", t)
                if m:
                    out.append((i, line["bbox"][1], m.group(1)))
    return sorted(out)


def sum_marks(text):
    ms = [int(x) for x in re.findall(r"\[(\d+) marks?\]", text)]
    return sum(ms), ms


# ───────────────────────── 建卷 ─────────────────────────
def build(source_qp: Path, source_ins: Path, out_dir: Path, overwrite=False):
    ym = re.search(r"qp-(\d{6})-economics-u(\d)", source_qp.name)
    yyyymm, unit = ym.group(1), ym.group(2)
    year, mm = int(yyyymm[:4]), yyyymm[4:]
    session = "jan" if mm == "01" else "june"
    code = f"EC0{unit}"
    sid = f"{year}-{session}-ec0{unit}"
    out_dir = out_dir / sid
    if (out_dir / "cut.json").exists() and not overwrite:
        print(f"  SKIP {sid}（已有 cut.json）")
        return None

    qp = pymupdf.open(source_qp)
    ins = pymupdf.open(source_ins)
    out_dir.mkdir(parents=True, exist_ok=True)

    # 1) 合并 source.pdf：资料册 p2, p3 + 原卷全部页
    merged = pymupdf.open()
    merged.insert_pdf(ins, from_page=1, to_page=2)
    merged.insert_pdf(qp)
    tmp = out_dir / "source.pdf"
    merged.save(tmp, garbage=3, deflate=True)
    merged.close()
    doc = pymupdf.open(tmp)

    mat_bbox = [content_bbox(ins[1], BX0, BX1), content_bbox(ins[2], BX0, BX1)]
    OFF = 2                                  # 卷面页在合并后的页偏移

    # 2) 题目（从原卷解析，落盘时页码 +OFF）
    spots = add_question_spots(qp)
    _seen, mains = set(), []
    for _s in spots:
        if _s[2] not in _seen:
            _seen.add(_s[2])
            mains.append(_s)
    if not mains:
        raise SystemExit(f"{sid}: 未识别到题号")
    nums = [s[2] for s in mains]
    if nums != list(range(1, len(nums) + 1)):
        print(f"  ! {sid} 题号不连续: {nums}")

    secs = sections(qp)
    qs = []
    for i, (p, y, qn, _sub, _bb) in enumerate(mains):
        regs = question_regions(qp, spots, qn)
        if not regs:
            print(f"  ! {sid} Q{qn} 空切块，跳过")
            continue
        txt = "\n".join(qp[pi].get_text(clip=r) for pi, r in regs)
        marks, _ = sum_marks(txt)
        cur_sec = "A"
        for sp, _sy, letter in secs:
            if (sp, _sy) <= (p, y):
                cur_sec = letter
        qtype = "mcq" if cur_sec == "A" else "structured"
        subs = [
            {"label": s[3], "page": s[0] + OFF, "bbox": [round(v, 1) for v in s[4]]}
            for s in spots if s[2] == qn and s[3]
        ]
        qs.append({
            "number": str(qn),
            "status": "auto",
            "confidence": 0.95,
            "total_marks": marks,
            "question_type": qtype,
            "section": cur_sec,
            "material_regions": (
                [{"page": 0, "bbox": [round(v, 1) for v in mat_bbox[0]]},
                 {"page": 1, "bbox": [round(v, 1) for v in mat_bbox[1]]}]
                if re.search(r"Extract|Source Booklet", txt, re.I) else []
            ),
            "regions": [{"page": pi + OFF, "bbox": [round(v, 1) for v in r]} for pi, r in regs],
            "anchors": {
                "start": {"page": p + OFF,
                          "bbox": [round(v, 1) for v in (main_bbox(spots, qn) or (X0, y, X1, y + 12))]},
                "end": {"page": regs[-1][0] + OFF,
                        "bbox": [round(v, 1) for v in regs[-1][1]]},
            },
            "subquestions": subs,
            "_text": txt,
        })

    sha = hashlib.sha256(tmp.read_bytes()).hexdigest()
    cut = {
        "schema_version": 1,
        "source": {
            "file": "source.pdf",
            "sha256": sha,
            "pages": doc.page_count,
            "paper_ref": f"{year}-{session}-ec0{unit}",
        },
        "sources": [
            {"role": "booklet", "file": source_ins.name,
             "sha256": hashlib.sha256(source_ins.read_bytes()).hexdigest(), "pages": 4,
             "used_pages": [1, 2]},
            {"role": "qp", "file": source_qp.name,
             "sha256": hashlib.sha256(source_qp.read_bytes()).hexdigest(), "pages": qp.page_count},
        ],
        "booklet_pages": OFF,
        "page_size": {"w": round(doc[0].rect.width, 2), "h": round(doc[0].rect.height, 2)},
        "render": {"dpi": 200, "format": "webp", "quality": 85},
        "material_order": "before",
        "questions": qs,
    }
    texts = {q["number"]: q.pop("_text") for q in cut["questions"]}
    (out_dir / "cut.json").write_text(
        json.dumps(cut, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")

    # 3) paper.md
    total = sum(q["total_marks"] for q in qs)
    exam_date, exam_time = parse_cover(qp)
    pm = [
        "---",
        f"id: aqa-a-level-economics-{sid}",
        "board: aqa",
        "qualification: a-level",
        "subject: economics",
        f"paper_code: {code}",
        f"paper_unit: U{unit}",
        f"year: {year}",
        f"session: {session}",
        'spec: "9625"',
        f"level: {'AS' if unit in ('1', '2') else 'A2'}",
        f'title: "Unit {unit} {UNIT_NAME[unit]}"',
    ]
    if exam_date:
        pm.append(f'exam_date: "{exam_date}"')
    if exam_time:
        pm.append(f'exam_time: "{exam_time}"')
    printed = parse_printed_total(qp)
    if printed:
        pm.append(f"printed_total_marks: {printed}")
    # A2 的 Section D 是「Q15 或 Q16 二选一」，两题都存 → 合计 ≠ 卷面分。
    # 沿用 CIE 9708 P2/P4（choose-1 卷）的既有约定：不写 total_marks，让 validate 跳过合计校验。
    if unit in ("1", "2"):
        pm.append(f"total_marks: {total}")
    pm += [
        f"source_pdf: {source_qp.name}",
        f"booklet_pdf: {source_ins.name}",
        f"booklet_pages: {OFF}",
        "cut: cut.json",
        "language: en",
        "---",
        "",
        f"Oxford AQA International A-Level · Economics · {code} · Unit {unit} {UNIT_NAME[unit]}",
        f"源卷 `{source_qp.name}`（{qp.page_count} 页）+ 资料册 `{source_ins.name}`（只用第 2、3 页）。",
        f"合并为 `source.pdf`（资料册 {OFF} 页在前），故 cut.json 里卷面题区页码整体 +{OFF}。",
        (f"卷面印刷 {printed} 分；Section D 为「Q15 或 Q16 二选一」，两题均入库，"
         f"故不入库合计值（各题分值之和 {total}），校验跳过总分对齐。") if unit in ("3", "4") else "",
        "",
    ]
    (out_dir / "paper.md").write_text("\n".join(pm), encoding="utf-8")

    # 4) qNN.md
    for q in qs:
        qid = f"aqa-a-level-economics-{sid}-q{int(q['number']):02d}"
        fm = [
            "---",
            f"id: {qid}",
            "board: aqa",
            "qualification: a-level",
            "subject: economics",
            f"paper_code: {code}",
            f"paper_unit: U{unit}",
            f"year: {year}",
            f"session: {session}",
            f"question_number: {q['number']}",
            f"total_marks: {q['total_marks']}",
            f"question_type: {q['question_type']}",
            f"section: {q['section']}",
            f"image: assets/q{int(q['number']):02d}.webp",
            f"layout: assets/q{int(q['number']):02d}.layout.json",
            f'source: "Oxford AQA International A-Level Economics {code}, {year} {session}"',
            "cut_status: auto",
            "language: en",
            "---",
            "",
            texts[q["number"]].strip(),
            "",
        ]
        (out_dir / f"q{int(q['number']):02d}.md").write_text("\n".join(fm), encoding="utf-8")

    print(f"  OK  {sid}: {len(qs)} 题 / {total} 分 / {doc.page_count} 页(+{OFF} 资料)")
    return out_dir


def main_bbox(spots, qn):
    for s in spots:
        if s[2] == qn and not s[3]:
            return s[4]
    return None


def parse_printed_total(qp):
    t = _norm(qp[0].get_text())
    m = re.search(r"maximum mark for this paper is (\d+)", t)
    return int(m.group(1)) if m else None


def parse_cover(qp):
    t = _norm(qp[0].get_text())
    date = None
    m = re.search(r"(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\s+(\d{1,2} \w+ \d{4})", t)
    if m:
        try:
            from datetime import datetime
            date = datetime.strptime(m.group(2), "%d %B %Y").strftime("%Y-%m-%d")
        except ValueError:
            date = None
    tm = re.search(r"Time allowed:\s*([0-9]+ hour[s]?(?: [0-9]+ minutes?)?)", t)
    return date, (tm.group(1) if tm else None)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="*", help="只跑这些卷，如 202401-u1")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--overwrite", action="store_true")
    ap.add_argument("--no-render", action="store_true")
    args = ap.parse_args()

    qps = sorted(QP_ROOT.glob("**/qp-*-economics-u*.pdf"))
    todo = []
    for qp in qps:
        m = re.search(r"qp-(\d{6})-economics-u(\d)", qp.name)
        key = f"{m.group(1)}-u{m.group(2)}"
        if (m.group(1), m.group(2)) in SKIP:
            continue
        if args.only and key not in args.only:
            continue
        ins = INS_DIR / f"in-{m.group(1)}-economics-u{m.group(2)}.pdf"
        if not ins.exists():
            print(f"  ! 缺资料册，跳过 {key}")
            continue
        todo.append((qp, ins, key))
    if args.limit:
        todo = todo[:args.limit]

    print(f"待切 {len(todo)} 卷")
    made = []
    for i, (qp, ins, key) in enumerate(todo, 1):
        print(f"[{i}/{len(todo)}] {key}")
        d = build(qp, ins, DATA, args.overwrite)
        if d:
            made.append(d)

    if made and not args.no_render:
        for d in made:
            r = subprocess.run([sys.executable, str(RENDER), str(d)],
                               capture_output=True, text=True)
            print(f"  render {d.name}: {r.stdout.strip() or r.stderr.strip()[:200]}")


if __name__ == "__main__":
    main()
