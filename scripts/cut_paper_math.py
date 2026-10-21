#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""cut_paper_math.py — Edexcel IAL 数学卷切割（只切题干，剔除作答区）

⚠️ 与 cut_paper.py（理化）的根本差别：
    理化卷的题干与作答区是**两本册子**（QP + answer booklet），切割只需在 QP
    里找起止锚点；数学卷的作答区**印在题面里**——题干 → 一整片下划线答题行
    → 下一小问题干 → 再一片 → … → Total 行。所以数学的"一题"不是一段连续
    矩形，而是**若干段内容块**；一题的 regions 因此天然多条，渲染层按序纵向
    拼接，同页两段之间那条缝会画浅灰实线（render_questions.py 的 seam 逻辑）。

两代版式（实测 265 卷 / 13 单元）：
    新（2023-jan 起）  题尾 "(Total for Question N is X marks)"；答题行是文本框线
                       （整行下划线）；小问标签常与作答行粘连成 "(a)______"；
                       绘图题的作答页是整页只有网格 + "Diagram N" / "Copy of
                       Diagram N" / "Only use this copy…" 说明。
    旧（2020-jan–2022-oct） 右上角 "Leave blank" 方框；题尾 "(Total N marks)"（无题号）；
                       每页顶 "Question N continued"；多数一题独占一段页区间。
    共同点：页脚 y≥790、左右竖排 "DO NOT WRITE IN THIS AREA"、整页装饰边框
    （>400×600）、出血图（x 越界）、右下角页码角标都不算内容。

"作答区"的判定（王培琪 2026-10-19 定：只切问题，不切作答区）：
    · 整行下划线 / "(a)____" 这种标签粘连行 → 剔除，并把所在处当作段落分隔；
    · 整页无正文（正文量<60 字符）且成片作答行（≥8 行）或有"可重画"样板 →
      判为作答页，该页文本与图形整页剔除，只保留 Total 行；
    · 因此绘图题的重画网格（Diagram 1/2、Copy of Diagram）一并不进图。

01A 合并卷（2025-oct 起 Pearson 把 QP + answer book 打成一个 PDF）：
    页脚条码族（PxxxxxA）出现第二族 → 该页起是答题册，QP 只到前一页为止。
    QP 部分本身是无答题线的紧凑排版，切割退化为"连续区间"，通常一题一段。

用法:
    python scripts/cut_paper_math.py <session_dir> [--dry-run]
    python scripts/cut_paper_math.py --audit <dir_root>     # 只体检，不落盘

坐标约定与 cut.json 结构完全沿用 cut_paper.py（PDF pt，左上原点，y 向下）。
"""

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

import pymupdf

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cut_paper import (  # noqa: E402  共用底层，避免多份副本漂移
    r2, yaml_scalar, build_page_lines, page_content_rects,
)

FOOTER_Y = 790.0          # 页脚条带起点（页码/条码/Turn over/角标）
HEADER_Y = 6.0            # 正文上沿（正常卷题号在 y≈58；539×785 小页卷从 y≈18 起，
                          # 且页码/条码跑到页顶，靠 BARCODE/PAGENO 规则剔除，不靠高度切）
LEFT_MARGIN_MAX = 75.0    # 题号/小问标签行的 x0 上限（新卷 42.5，旧卷 54.5）
L_FLOOR = 40.0            # 版心左沿（装饰框内沿 x≈35，题号 x≥42.5）
R_CEIL = 556.0            # 版心右沿（装饰框内沿 x≈560，内容 x≤552.5）
PAD_T = 3.0
PAD_B = 3.0
MIN_SEG_H = 4.0           # 段高下限
MERGE_GAP = 14.0          # 相邻段间距≤此值则合并（只剔了一两行作答线）

RULE_RE = re.compile(r"^[\s_]{8,}$")                        # 纯下划线作答行
LABELED_RULE_RE = re.compile(r"^\s*\([a-z]{1,4}\)(?:\([ivx]{1,4}\))?[\s_]*_{8,}\s*$")
CONT_RE = re.compile(r"^Question\s+\d+\s+continued$", re.I)
QNUM_RE = re.compile(r"^(\d{1,2})(?:\.{0,2}|:)(?:\s|$)")  # "3." / "3.." / "1␉" / "1:"
TOTAL_N_RE = re.compile(
    r"\(\s*Total\s+for\s+Question\s+(\d+)\s+(?:is|=)\s+(\d+)\s+marks?\s*\)")
TOTAL_BARE_RE = re.compile(r"^\(\s*Total\s+(\d+)\s+marks?\s*\)$")   # 旧版式
BARCODE_RE = re.compile(r"(P\d{5}[A-Z])\d*")
BLANK_RE = re.compile(r"^BLANK\s+PAGE$", re.I)
LEAVE_RE = re.compile(r"^(Leave|blank)$", re.I)
MARKS_ONLY_RE = re.compile(r"^\(\s*\d{1,2}\s*\)$")          # 右对齐分值行
SUB_RE = re.compile(r"^(\([a-z]\)|\([ivx]{1,4}\))")
CAPTION_RE = re.compile(
    r"^(Diagram\s+\d+|Figure\s+\d+|Copy\s+of\s+Diagram\s+\d+|"
    r"Only\s+use\s+this\s+copy.*|Turn\s+over\s+for\s+a\s+spare\s+grid.*)$", re.I)
REDRAW_RE = re.compile(r"(Copy\s+of\s+Diagram|Only\s+use\s+this\s+copy)", re.I)
NUMERIC_RE = re.compile(r"^[\d\s.,π–\-+O]+$")
ROMAN = {"i", "ii", "iii", "iv", "v", "vi", "vii", "viii", "ix", "x", "xi", "xii"}

UNITS = {"WMA11", "WMA12", "WMA13", "WMA14", "WST01", "WST02", "WST03",
         "WME01", "WME02", "WME03", "WFM01", "WFM02", "WFM03"}   # D1/WMA01/02 不在范围
BOARDS = {"edexcel", "aqa", "cie"}
QUALS = {"a-level", "alevel", "igcse", "gcse"}
SESSIONS = {"jan", "mar", "may-jun", "june", "oct", "oct-nov"}


# ── 页面行分类 ──────────────────────────────────────────────────────────────

def classify_page(lines):
    """把一页的文本行分成 text / rule 两类（其余丢弃）。"""
    rows = []
    for ln in lines:
        raw = ln["raw"]
        b = ln["bbox"]
        txt = raw.strip()
        if not txt:
            continue
        if b[3] <= HEADER_Y or b[1] >= FOOTER_Y:
            continue
        if re.fullmatch(r"P\d{5}[A-Z]", txt) or re.fullmatch(r"\*?P\d{5}[A-Z]\d*\*?", txt):
            continue                       # 页脚/页顶条码号
        if re.fullmatch(r"\d{1,3}", txt) and b[0] > 400:
            continue                       # 页码（右下/右上角；左下的在 y>=790 已剔）
        if "DO NOT WRITE IN THIS AREA" in txt:
            continue
        if RULE_RE.match(raw) or LABELED_RULE_RE.match(raw):
            rows.append({"kind": "rule", "y0": b[1], "y1": b[3]})
            continue
        if CONT_RE.match(txt) or BLANK_RE.match(txt):
            continue
        if b[0] > 500 and LEAVE_RE.match(txt) and b[1] < 70:
            continue
        if b[0] > 500 and b[1] > 700 and re.fullmatch(r"Q\d{1,2}", txt):
            continue                       # 旧卷右下角 "Q11" 角标框
        rows.append({"kind": "text", "y0": b[1], "y1": b[3],
                     "x0": b[0], "x1": b[2], "raw": txt, "line": ln,
                     "bold": ln["bold"]})
    return rows


def is_junk_drawing(x0, y0, x1, y1):
    """出血图 / 右下角页码角标 / 页底装饰横线 / 退化点。"""
    if x1 < 36 or x0 > 559:
        return True
    if (y0 > 700 and x0 > 500) or (y0 > 745 and (x1 - x0) > 400):
        return True
    if (x1 - x0) < 1.5 and (y1 - y0) < 1.5:
        return True
    return False


def drawing_rows(doc, pi, cache):
    out = []
    for y1, y0, x0, x1, _hl in page_content_rects(doc, pi, cache):
        if y1 <= HEADER_Y or y0 >= FOOTER_Y:
            continue
        if is_junk_drawing(x0, y0, x1, y1):
            continue
        out.append({"kind": "draw", "y0": y0, "y1": min(y1, FOOTER_Y),
                    "x0": x0, "x1": x1})
    return out


def page_is_answer_sheet(rows):
    """整页判为作答页？三条同时成立：

    · 页上没有题号行（'N.' 顶格）——有题号的一定是题面页；
    · 页上正文量（字符数，剔除 Total 行/样板说明/纯刻度数字）不足 60 字符。
      数学卷题面常被拆成 "y = 4x" / "(a)" / "∫" 这种短碎片，不能用单行长度判；
    · 有成片作答行（≥8 行），或有明确的"可重画"样板（Copy of Diagram N /
      Only use this copy…）。

    第三条只认 ≥8 行是防误杀"一页只有一张图"的题面页（2026-jan WMA11 Q3 的
    Figure 就独占一页，若按"有网格即作答页"会把题干图删掉）。
    """
    for r in rows:
        if r["kind"] == "text" and r.get("x0", 999) <= LEFT_MARGIN_MAX \
                and QNUM_RE.match(r["raw"]):
            return False
    chars, rules, redraw = 0, 0, False
    for r in rows:
        if r["kind"] == "rule":
            rules += 1
            continue
        if r["kind"] != "text":
            continue
        raw = r["raw"]
        if TOTAL_N_RE.search(raw) or TOTAL_BARE_RE.match(raw):
            continue
        if CAPTION_RE.match(raw):
            redraw = redraw or bool(REDRAW_RE.search(raw))
            continue
        if NUMERIC_RE.match(raw):
            continue
        chars += len(raw.strip())
    return (chars < 60) and (rules >= 8 or redraw)


def answer_page_cut(rows, draws):
    """作答页的保留上界：只保留第一张 Diagram（含其网格），Copy of 之后全丢。

    王培琪 2026-10-19 定：**保留第一个 Diagram，只删 Copy of**。
    典型版式（2026-jan WMA11 Q9 p27）：

        (b)(i)                                  ← 小问标签，保留
        [网格 A]  Diagram 1                     ← 学生要画的图，保留
        [网格 B]  Copy of Diagram 1             ← 备用重画，删
                  Only use this copy if…        ← 删

    「Diagram N」图注下面是 Diagram N 本体，「Copy of Diagram N」上面那张才是
    Copy——两者是**同一页上的两个图形块**，所以不能整页丢，得按 y 截断：
    截断点 = 第一条「Diagram N」图注之后的第一个图形块的顶边。

    返回 None 表示"整页全丢"（纯作答线页，没有可保留的图）。
    """
    cap_y = None
    copy_y = None
    for r in rows:
        if r["kind"] != "text":
            continue
        if cap_y is None and re.match(r"^Diagram\s+\d+$", r["raw"], re.I):
            cap_y = r["y0"]
        if copy_y is None and REDRAW_RE.search(r["raw"]):
            copy_y = r["y0"]
    if copy_y is None:
        return None                      # 没有 Copy → 整页是纯作答线，全丢
    if cap_y is None:
        return copy_y                    # 只有 Copy → 截到 Copy 处
    # 图注印在图的**下方**（Edexcel 惯例），所以截断点在图注下面一点：
    # 若紧跟着就有大图形（Copy 那张），切在它顶边；否则按图注高度兜底。
    near = [d for d in draws
            if cap_y < d["y0"] <= cap_y + 60 and d["y1"] - d["y0"] > 80]
    if near:
        return min(d["y0"] for d in near) - 12   # 上留一点，别把 y 轴箭头带进来
    return cap_y + 18


def build_page_model(doc, pages_lines, pages):
    """每页行模型。作答页只保留第一张 Diagram 之前的内容 + Total 行。"""
    cache = {}
    model, answer_pages = {}, set()
    for pi in pages:
        rows = classify_page(pages_lines[pi])
        draws = drawing_rows(doc, pi, cache)
        if page_is_answer_sheet(rows + draws):
            answer_pages.add(pi)
            cut_y = answer_page_cut(rows, draws)
            keep = []
            for r in rows:
                if r["kind"] == "text" and (TOTAL_N_RE.search(r["raw"])
                                            or TOTAL_BARE_RE.match(r["raw"])):
                    keep.append(r)
                elif cut_y is not None and r["y0"] < cut_y:
                    keep.append(r)
            if cut_y is not None:
                # 截断处插一条"分隔"标记：否则被删掉的 Copy 图区会被当成空白，
                # 把上面的 Diagram 和页底 Total 行粘成一段（整页又回来了）
                keep.append({"kind": "rule", "y0": cut_y, "y1": cut_y})
            rows = keep
            draws = [d for d in draws
                     if cut_y is not None and d["y1"] <= cut_y] if cut_y else []
        model[pi] = sorted(rows + draws,
                           key=lambda r: (round(r["y0"], 1), r.get("x0", 0)))
    return model, answer_pages


def qp_page_limit(doc):
    """01A 合并卷：按页脚条码族切出 QP 部分（返回答题册起始页，0 = 不截断）。"""
    first = []
    for pi, page in enumerate(doc):
        found = set()
        for b in page.get_text("dict")["blocks"]:
            if b.get("type") != 0:
                continue
            for ln in b["lines"]:
                t = "".join(s["text"] for s in ln["spans"])
                found.update(BARCODE_RE.findall(t))
        for f in found:
            if all(f != g for _, g in first):
                first.append((pi, f))
    first.sort()
    if len(first) > 1 and first[1][0] >= 2:
        return first[1][0]
    return 0


def find_qstarts(model, pages):
    cand = []
    for pi in pages:
        for r in model[pi]:
            if r["kind"] != "text" or r.get("x0", 999) > LEFT_MARGIN_MAX:
                continue
            m = QNUM_RE.match(r["raw"])
            if m:
                cand.append((pi, r["y0"], int(m.group(1)), r.get("bold", False), r))
    cand.sort(key=lambda x: (x[0], x[1]))
    starts, expected, last = {}, 1, (-1, -1.0)
    while True:
        pick = next((c for c in cand
                     if c[2] == expected and (c[0], c[1]) > last and c[3]), None)
        if pick is None:
            pick = next((c for c in cand
                         if c[2] == expected and (c[0], c[1]) > last), None)
        if pick is None:
            break
        starts[expected] = {"page": pick[0], "y0": pick[1], "row": pick[4]}
        last = (pick[0], pick[1])
        expected += 1
    return starts, expected - 1


def find_totals(model, pages):
    """Total 行 → [(page, y0, y1, n|None, marks)]，文档序。"""
    out = []
    for pi in pages:
        for r in model[pi]:
            if r["kind"] != "text":
                continue
            m = TOTAL_N_RE.search(r["raw"])
            if m:
                out.append((pi, r["y0"], r["y1"], int(m.group(1)), int(m.group(2))))
                continue
            m = TOTAL_BARE_RE.match(r["raw"])
            if m:
                out.append((pi, r["y0"], r["y1"], None, int(m.group(1))))
    return out


def segments_of(rows):
    segs, cur = [], None
    for r in rows:
        if r["kind"] == "rule":
            if cur is not None:
                segs.append(cur)
                cur = None
            continue
        if cur is None or r["page"] != cur["page"]:
            if cur is not None:
                segs.append(cur)
            cur = {"page": r["page"], "y0": r["y0"], "y1": r["y1"]}
        else:
            cur["y0"] = min(cur["y0"], r["y0"])
            cur["y1"] = max(cur["y1"], r["y1"])
    if cur is not None:
        segs.append(cur)
    merged = []
    for s in segs:
        if merged and merged[-1]["page"] == s["page"] \
                and s["y0"] - merged[-1]["y1"] <= MERGE_GAP:
            merged[-1]["y1"] = max(merged[-1]["y1"], s["y1"])
        else:
            merged.append(dict(s))
    return [s for s in merged if s["y1"] - s["y0"] >= MIN_SEG_H]


# ── 主分析（cut 与 audit 共用） ─────────────────────────────────────────────

PAPER_MARKS_RE = re.compile(
    r"total\s+mark\s+for\s+this\s+paper\s+is\s+(\d+)", re.I)


def cover_marks(doc):
    """从封面找"试卷总分"（旧卷写 'The total mark for this paper is 75'）。"""
    for pi in range(min(3, doc.page_count)):
        m = PAPER_MARKS_RE.search(doc[pi].get_text())
        if m:
            return int(m.group(1))
    return None


def analyze(doc, pages_lines, paper_marks=None):
    """返回 (questions, meta)。questions 结构即 cut.json 的 questions。"""
    n_pages = doc.page_count
    if paper_marks is None:
        paper_marks = cover_marks(doc)
    hi = qp_page_limit(doc) or n_pages
    pages = list(range(1, hi))          # 封面永远不是题面页，排除
    model, ans_pages = build_page_model(doc, pages_lines, pages)
    starts, nq = find_qstarts(model, pages)
    totals = find_totals(model, pages)
    meta = {"pages": n_pages, "qp_pages": hi, "answer_pages": len(ans_pages),
            "nq": nq, "L": None, "R": None, "marks": 0}
    if nq == 0:
        return [], meta

    # 版心左右只看**正文行**：老卷在 x≈524 有一根通高的"留白/分值"竖栏分隔线，
    # 2026 卷的装饰框在 x≈560，两者都是 drawing，混进来会把版心撑到边上。
    xs0, xs1 = [], []
    for pi in pages:
        for r in model[pi]:
            if r["kind"] != "text":
                continue
            xs0.append(r["x0"])
            xs1.append(r["x1"])
    xs0.sort()
    xs1.sort()
    # 左沿跟着正文走：正常卷题号在 42.5→L≈40；539×785 小页卷正文从 14.2 起，
    # 若死守 40 会把题号和 "(a)" 标签切掉一半。
    L = max(6.0, r2(xs0[int(len(xs0) * 0.002)] - 2))
    R = min(R_CEIL, r2(xs1[int(len(xs1) * 0.998)] + 4))
    meta["L"], meta["R"] = L, R

    bot_y = {}
    for pi in pages:
        for y1, y0, x0, x1, _hl in page_content_rects(doc, pi, {}):
            if y0 > 745 and (x1 - x0) > 400:
                bot_y[pi] = min(bot_y.get(pi, 800.0), y0)
        # 页脚条码/页码行（小页卷在 y≈768 而不是 790 以下）也算下界，
        # 否则题尾 Total 行的裁剪会把页脚残字带进来。
        for ln in pages_lines[pi]:
            t = ln["raw"].strip()
            if ln["bbox"][1] > 700 and (re.fullmatch(r"P\d{5}[A-Z]", t)
                                        or re.fullmatch(r"\d{1,3}", t)):
                bot_y[pi] = min(bot_y.get(pi, 800.0), ln["bbox"][1])
    last_row = {}
    for pi in pages:
        cs = [r for r in model[pi] if r["kind"] != "rule"]
        if cs:
            last_row[pi] = max(r["y1"] for r in cs)

    questions, marks_sum = [], 0
    for n in range(1, nq + 1):
        st = starts.get(n)
        q = {"number": str(n), "status": "auto", "confidence": 0.0,
             "regions": [], "anchors": {}, "review_notes": [],
             "subquestions": [], "question_type": "structured"}
        notes = q["review_notes"]
        if st is None:
            q["status"] = "review"
            notes.append("缺题号起点锚点")
            questions.append(q)
            continue
        q["anchors"]["start"] = {"page": st["page"],
                                 "bbox": [r2(v) for v in st["row"]["line"]["bbox"]]}
        sp, sy = st["page"], st["y0"] - 2
        nxt = starts.get(n + 1)
        next_pos = (nxt["page"], nxt["y0"]) if nxt else (10 ** 6, 0)

        tot = next((t for t in totals if t[3] == n and (t[0], t[1]) > (sp, sy)), None)
        if tot is None:
            tot = next((t for t in totals if t[3] is None
                        and (sp, sy) < (t[0], t[1]) < next_pos), None)
        if tot is not None:
            ep, ey = tot[0], tot[2] + 2
            q["total_marks"] = tot[4]
            marks_sum += tot[4]
            q["anchors"]["end"] = {"page": ep, "bbox": [L, r2(tot[1]), R, r2(tot[2])]}
        else:
            if nxt is not None:
                if nxt["page"] == sp:
                    ep, ey = sp, nxt["y0"] - 2
                else:
                    ep = nxt["page"] - 1
                    ey = last_row.get(ep, FOOTER_Y - 2)
            else:
                ep = max(p for p in pages if p in last_row)
                ey = last_row[ep]
            notes.append("无 Total 行，终点按下一题起点/末内容行推定")

        rows = []
        for pi in range(sp, ep + 1):
            for r in model[pi]:
                if pi == sp and r["y0"] < sy:
                    continue
                if pi == ep and r["y0"] > ey:
                    continue
                rows.append({**r, "page": pi})
        rows.sort(key=lambda r: (r["page"], round(r["y0"], 1)))

        if "total_marks" not in q:
            mk = [r for r in rows if r["kind"] == "text" and r["x0"] > 470
                  and MARKS_ONLY_RE.match(r["raw"])]
            if mk:
                q["total_marks"] = sum(int(re.sub(r"\D", "", r["raw"])) for r in mk)
                marks_sum += q["total_marks"]

        segs = segments_of(rows)
        q["regions"] = [{"page": s["page"],
                         "bbox": [L, r2(max(s["y0"] - PAD_T, HEADER_Y)), R,
                                  r2(min(s["y1"] + PAD_B, FOOTER_Y - 1,
                                         bot_y.get(s["page"], 800.0) - 1.0))]}
                        for s in segs]
        if not q["regions"]:
            q["status"] = "review"
            notes.append("本题未取到任何内容段（整题都在作答页？）")

        subs, parent = [], None
        for r in rows:
            if r["kind"] != "text" or r.get("x0", 999) > 110:
                continue
            m = SUB_RE.match(r["raw"])
            if not m:
                continue
            tok = m.group(1).strip("()")
            lab = f"{parent} {tok}" if tok in ROMAN and parent else tok
            if tok not in ROMAN:
                parent = tok
            subs.append({"label": lab, "page": r["page"],
                         "bbox": [r2(v) for v in r["line"]["bbox"]]})
        q["subquestions"] = subs

        conf = 0.6 if tot is not None else 0.4
        conf += 0.2 if q["regions"] else 0.0
        conf += 0.2 if subs else 0.1
        q["confidence"] = round(min(conf, 1.0), 2)
        if q["confidence"] < 1.0:
            q["status"] = "review"
        questions.append(q)

    if paper_marks and marks_sum:
        if marks_sum == paper_marks:
            # 分值对得上 → 题界几乎不可能同时错：撤销"无 Total 行"的降级
            for q in questions:
                if q["regions"]:
                    q["status"] = "auto"
                    q["confidence"] = 1.0
                    q["review_notes"] = [n for n in q["review_notes"]
                                         if not n.startswith("无 Total")]
        else:
            for q in questions:
                q["review_notes"].append(
                    f"全卷分值合计 {marks_sum} ≠ 试卷总分 {paper_marks}")
    meta["marks"] = marks_sum
    return questions, meta


# ── 落盘 ────────────────────────────────────────────────────────────────────

def is_done(sdir):
    """已完成判定：cut.json + 每题 qNN.md + assets/qNN.webp 齐全。

    ⚠️ 王培琪 2026-10-19 定：**已验收的卷子永不重切重渲染**。
    要强制重跑必须显式 --force。
    """
    # 以 `.done` 为准（batch_math 在校验通过后才写），不是"文件看起来齐了"
    return (sdir / ".done").exists() and (sdir / "cut.json").exists()


def write_outputs(sdir, cut, questions, pages_lines, board, qualification,
                  subject, year, session, paper_code, paper_unit):
    (sdir / "cut.json").write_text(
        json.dumps(cut, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    for q in questions:
        n = int(q["number"])
        fm = [("id", f"{board}-{qualification}-{subject}-{year}-{session}-"
                     f"{paper_code}-q{n:02d}"),
              ("board", board), ("qualification", qualification),
              ("subject", subject), ("paper_code", paper_code.upper())]
        if paper_unit:
            fm.append(("paper_unit", paper_unit))
        fm += [("year", year), ("session", session), ("question_number", str(n))]
        if "total_marks" in q:
            fm.append(("total_marks", q["total_marks"]))
        fm += [("question_type", q["question_type"]),
               ("image", f"assets/q{n:02d}.webp"),
               ("layout", f"assets/q{n:02d}.layout.json"),
               ("source", f"Pearson Edexcel IAL {paper_unit or paper_code.upper()}, {year}"),
               ("cut_status", q["status"]), ("language", "en")]
        body = []
        for rg in q["regions"]:
            bb = rg["bbox"]
            for ln in pages_lines[rg["page"]]:
                lb = ln["bbox"]
                if not (bb[1] <= (lb[1] + lb[3]) / 2 <= bb[3]):
                    continue
                t = ln["raw"].strip()
                if not t or RULE_RE.match(ln["raw"]) or LABELED_RULE_RE.match(ln["raw"]):
                    continue
                if "DO NOT WRITE" in t or CONT_RE.match(t) or BLANK_RE.match(t):
                    continue
                body.append(t)
        lines = ["---"] + [f"{k}: {yaml_scalar(v)}" for k, v in fm] + ["---", ""]
        (sdir / f"q{n:02d}.md").write_text(
            "\n".join(lines + body) + "\n", encoding="utf-8")


def run_one(sdir, args):
    sdir = Path(sdir).resolve()
    if not sdir.is_dir():
        sys.exit(f"目录不存在: {sdir}")
    board = sdir.parents[2].name
    qualification = sdir.parents[1].name
    subject = sdir.parents[0].name
    try:
        year_s, session, paper_code = sdir.name.split("-", 2)
        year = int(year_s)
        assert board in BOARDS and qualification in QUALS and session in SESSIONS
    except Exception:
        sys.exit(f"session 目录路径不符合约定: {sdir}")

    src = sdir / "source.pdf"
    if not src.exists():
        sys.exit(f"缺少 source.pdf: {src}")
    if is_done(sdir) and not getattr(args, "force", False):
        print(f"SKIP {sdir.name}: 已完成（--force 才重切）")
        return
    sha = hashlib.sha256(src.read_bytes()).hexdigest()
    doc = pymupdf.open(src)

    paper_unit = paper_ref = paper_marks = None
    pm = sdir / "paper.md"
    if pm.exists():
        t = pm.read_text(encoding="utf-8")
        m = re.search(r"^paper_unit:\s*(\S+)", t, re.M)
        paper_unit = m.group(1) if m else None
        m = re.search(r"^paper_ref:\s*(\S+)", t, re.M)
        paper_ref = m.group(1) if m else None
        m = re.search(r"^total_marks:\s*(\d+)", t, re.M)
        paper_marks = int(m.group(1)) if m else None

    pages_lines = [build_page_lines(p) for p in doc]
    questions, meta = analyze(doc, pages_lines, paper_marks)

    cut = {
        "schema_version": 1,
        "source": {"file": "source.pdf", "sha256": sha, "pages": meta["pages"]},
        "page_size": {"w": r2(doc[0].rect.width), "h": r2(doc[0].rect.height)},
        "render": {"dpi": 200, "format": "webp", "quality": 85},
        "questions": questions,
    }
    if paper_ref:
        cut["source"]["paper_ref"] = paper_ref
    if meta["qp_pages"] < meta["pages"]:
        cut["answer_booklet"] = {"pages": [meta["qp_pages"], meta["pages"] - 1]}

    nseg = sum(len(q["regions"]) for q in questions)
    print(f"OK  {sdir.name}: {meta['nq']} 题 | 段 {nseg} | "
          f"作答页 {meta['answer_pages']} | L={meta['L']} R={meta['R']} | "
          f"分值 {meta['marks']}" + (f"/{paper_marks}" if paper_marks else ""))
    rev = [q["number"] for q in questions if q["status"] != "auto"]
    if rev:
        print(f"    review: Q{', Q'.join(rev)}")
    if args.dry_run:
        return
    write_outputs(sdir, cut, questions, pages_lines, board, qualification,
                  subject, year, session, paper_code, paper_unit)


def audit(root, limit=20, offset=0):
    """体检：对目录树里的 *que*.pdf 分批跑分析，只打印统计。

    ⚠️ 王培琪 2026-10-19 定：**一次最多 20 卷**，不许一把梭 265 卷——
    出问题能立刻发现，进度也看得见。用 --offset 翻页跑完全部。
    """
    root = Path(root).resolve()
    files = sorted(p for p in root.rglob("*que*.pdf")
                   if p.name[:5].upper() in UNITS)
    total = len(files)
    batch = files[offset:offset + limit]
    print(f"[批次] 第 {offset // max(limit, 1) + 1} 批："
          f"{offset + 1}–{offset + len(batch)} / 共 {total} 卷\n")
    ok = bad = 0
    tot_q = tot_seg = tot_rev = 0
    for p in batch:
        try:
            doc = pymupdf.open(p)
            pages_lines = [build_page_lines(pg) for pg in doc]
            qs, meta = analyze(doc, pages_lines)
        except Exception as e:  # noqa: BLE001
            print(f"FAIL {p.relative_to(root)}: {e}")
            bad += 1
            continue
        if meta["nq"] == 0:
            print(f"NQ0  {p.relative_to(root)}")
            bad += 1
            continue
        nseg = sum(len(q["regions"]) for q in qs)
        nrev = sum(1 for q in qs if q["status"] != "auto")
        zero = [q["number"] for q in qs if not q["regions"]]
        tot_q += meta["nq"]
        tot_seg += nseg
        tot_rev += nrev
        ok += 1
        flag = f" 空段Q{','.join(zero)}" if zero else ""
        print(f"{p.relative_to(root)} | 题{meta['nq']:2d} 段{nseg:2d} "
              f"页{meta['pages']:2d} 作答页{meta['answer_pages']:2d} "
              f"分{meta['marks']:3d} rev{nrev:2d}{flag}")
    print(f"\n汇总: {ok} 卷通过 / {bad} 卷失败 | 共 {tot_q} 题 {tot_seg} 段 | "
          f"review 题 {tot_rev} ({tot_rev / max(tot_q, 1):.1%})")


def main():
    ap = argparse.ArgumentParser(description="Edexcel IAL 数学卷按题切割（只切题干）")
    ap.add_argument("session_dir", nargs="?")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--audit", metavar="ROOT", help="只体检一棵目录树，不落盘")
    ap.add_argument("--force", action="store_true", help="已完成也重切（慎用）")
    ap.add_argument("--limit", type=int, default=20,
                    help="单批最多几卷（默认 20，上限 20）")
    ap.add_argument("--offset", type=int, default=0, help="从第几卷开始")
    args = ap.parse_args()
    if args.limit > 20:
        ap.error("单批最多 20 卷（王培琪 2026-10-19 定）")
    if args.audit:
        audit(args.audit, args.limit, args.offset)
        return
    if not args.session_dir:
        ap.error("需要 session_dir 或 --audit")
    run_one(args.session_dir, args)


if __name__ == "__main__":
    main()
