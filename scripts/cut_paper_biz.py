#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""cut_paper_biz.py — Edexcel IAL 商务卷（WBS11–14）按题切割

为什么单独一份（与经济/会计的差别）：
    商务卷 **一页之内题面与小问交错**，点线作答区夹在中间：
        p2 = (a) 题面 → 点线×6 → "In 2023…" → (b) 题面 → 点线×11
    经济卷是「题面 + 点线 + Total」一页一题，从第一条点线往下全裁即可；
    商务若照搬会把 (c)(d)(e) 连同后续页一起丢掉。
    → 本题面 = **逐页逐带提取**，跳过所有点线块与纯点线页，同页多段按序接回
      （render_questions 的 seam 虚线正是为此存在）。

结构与锚点（86 卷实测，2026-10-22）：
    · QP 与 Source Booklet **合订在同一 PDF**；普通卷 p1 封面→p2.. 题目→p18+ 资料，
      **2022-Jan 全季与 -01a 中国区卷是资料册在前**；
    · 每卷 3 题：WBS11/12 = Q1 30(a-e) / Q2 30(a-e) / Q3 20(整题)；
      WBS13/14 = Q1 40(a-e) / Q2 20(整题) / Q3 20(整题)；
    · 每题以 `(Total for Question N = X marks)` 收尾；
    · 资料归属写在卷面 `Read Extracts A and B in the Source Booklet before answering
      Question 1`。两种写法：列举（A, B and C）与**范围（A–E，en-dash）**——
      只认列举会漏掉 2020-Oct WBS13 / 2021-Oct U3 这类卷的 B–E。
    · 实际归属用**区间法**：「本题首字母 → 下一题首字母」之间的所有 Extract 都归本题。
      这比逐卷解析句型稳（有些卷只写 "Read Extract A"、span 到 E 却藏在页尾）。

切割口径（2026-10-22 王培琪定）：
    · 题图 = **该题资料在前、题面在后**（material_order="before"）；
    · 点线作答区**全部裁掉**；同页多段以浅灰虚线表示接缝；
    · SECTION 字头丢弃，`Read Extracts …` 指引句保留，
      `Write your answers in the spaces provided.` **保留**（培琪 09:56 定，不涂白）；
    · 分值行 (2)(4)(6) 保留原位；
    · 有小问就标（Q1、WBS11/12 的 Q2），整题题不产 subquestions；
    · 表格框线必须包进图内（只按文字裁会削掉底框，2025-June Q1 实测）。

用法:
    python scripts/cut_paper_biz.py <session_dir>            # 切割 + 写 cut.json/qNN.md
    python scripts/cut_paper_biz.py <session_dir> --analyze  # 只打印锚点表
    python scripts/cut_paper_biz.py <session_dir> --questions 1,3

坐标系：PDF pt，左上原点，y 向下；bbox=[x0,y0,x1,y1]；px = pt × DPI ÷ 72。
"""

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

import pymupdf

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from cut_paper import (  # noqa: E402  复用同一套坐标约定与 md 写法
    FOOTER_Y,
    HEADER_Y,
    LEFT_MARGIN_MAX,
    SECTION_RE,
    TRIM_MIN_GAP,
    TRIM_PAD,
    assign_words_to_lines,
    build_page_lines,
    merge_with_existing,
    parse_bare_subhead,
    parse_subhead,
    r2,
    sub_label,
    write_question_md,
)

# ── 版心（595×842 A4，86 卷实测一致）──────────────────────────────────────
L, T, R, B = 39.0, 40.0, 556.0, 788.0
# 右侧竖排 "DO NOT WRITE IN THIS AREA" 在 x≈570.8–585.6、左侧在 x≈9.6–24.5，
# 都被 L/R 挡在外面；点线右端 550.3，R=556 留 6pt 余量。

TOTAL_RE = re.compile(r"\(?\s*Total\s+for\s+Question\s*(\d+)\s*=\s*(\d+)\s*marks?\s*\)", re.I)
STUDY_RE = re.compile(
    r"Extracts?\s+([A-Z](?:\s*(?:[–—\-–,]|and|to)\s*[A-Z])*)\s+"
    r"in\s+the\s+Source\s+Booklet\s+before\s+answering\s+Question\s*(\d+)", re.I | re.S)
EXT_LABEL_RE = re.compile(r"^Extract\s+([A-Z])\s*$")
SRC_COVER_RE = re.compile(r"^\s*Source Booklet\s*$", re.I)
BLANK_PAGE_RE = re.compile(r"^\s*BLANK PAGE\s*$", re.I)
# 纯版式指引：SECTION 字头丢弃；"Write your answers…" 保留（不参与切段，仅记录）
SECTION_ONLY_RE = re.compile(r"^SECTION\s+[A-C][\s\.]*$", re.I)

# 表格框线判据
THIN_LINE_PT = 3.0


def is_dotted(ln):
    """作答点线行：整行只有 '.' 与空白且够长（表格里的 '.' 不会这么长）。"""
    t = ln["raw"].strip()
    return len(t) >= 20 and set(t) <= set(". ")


def content_lines(pi, pages_lines):
    """去掉页眉页脚、SECTION 字头、BLANK PAGE 与致谢页之后的正文行。"""
    out = []
    for ln in pages_lines[pi]:
        b = ln["bbox"]
        if b[1] < HEADER_Y - 2 or b[3] > B + 8:
            continue
        if b[1] >= FOOTER_Y:
            continue
        t = ln["raw"].strip()
        if SECTION_ONLY_RE.match(t) or BLANK_PAGE_RE.match(t):
            continue
        out.append(ln)
    return out


def page_boxes(pi, doc, cache):
    """该页版心内的「非文字内容」矩形：细线（表格框/分隔线）与图块。

    用于把段的上下界扩到紧贴的框线上——只按最后一行文字收会削掉表格底框
    （2025-June Q1 Extract B 实测：文字底 579.4、底框线 586.0）。
    排除整页装饰边框（宽>400 且高>600）与版心外的竖排文字条。
    """
    if pi in cache:
        return cache[pi]
    rows = []
    try:
        for dr in doc[pi].get_drawings():
            r = dr["rect"]
            if r.width > 400 and r.height > 600:
                continue                     # 整页边框
            if r.x1 < L or r.x0 > R or r.y1 < T or r.y0 > B:
                continue
            rows.append((r.y0, r.y1))
    except Exception:
        pass
    try:
        for item in doc[pi].get_images(full=True):
            r = doc[pi].get_image_bbox(item)
            if r.x1 < L or r.x0 > R or r.y1 < T or r.y0 > B:
                continue
            rows.append((r.y0, r.y1))
    except Exception:
        pass
    cache[pi] = rows
    return rows


def tight_bounds(pi, y_from, y_to, pages_lines, doc, cache, words, pad=5.0):
    """[y_from, y_to] 内内容的上下界：文字行 + 词框（含旋转）+ 框线/图。

    ⚠️ 必须并 `get_text("words")`：图表 x 轴的年份标签是**旋转文字**，
    `build_page_lines` 直接跳过，只按行收紧会把资料页底边切在 "Oct 2022" 中间
    （2025-June wbs12 Q3 实测 8 处，audit 报 edge=254.5）。
    """
    top, bot = None, None
    for ln in content_lines(pi, pages_lines):
        b = ln["bbox"]
        # 归属用「是否整行落在段外」判：只看下沿会让紧贴边界下面的那行
        # （下一段的 Extract 标签）被算进本段，渲染后标签重复一次
        # （2025-June wbs12 Q3 的 Extract F/H 实测）。
        if b[3] <= y_from or b[1] >= y_to - 1:
            continue
        top = b[1] if top is None else min(top, b[1])
        bot = b[3] if bot is None else max(bot, b[3])
    for w in words[pi]:
        x0, y0, x1, y1 = w[0], w[1], w[2], w[3]
        if y1 <= y_from or y0 >= y_to - 1:
            continue
        if x1 < L or x0 > R:
            continue                      # 版心外的竖排 "DO NOT WRITE…"
        if y0 < HEADER_Y - 2 or y1 > FOOTER_Y:
            continue                      # 页眉页脚
        top = y0 if top is None else min(top, y0)
        bot = y1 if bot is None else max(bot, y1)
    if top is None:
        return None
    top = max(T, top - pad)
    bot = min(B, bot + pad)
    for by0, by1 in page_boxes(pi, doc, cache):
        if top - 16 <= by0 <= top + 2:
            top = min(top, by0)
        if bot - 2 <= by1 <= bot + 16:
            bot = max(bot, by1)
    return max(T, r2(top)), min(B, r2(bot))


def bands(pi, y_lo, y_hi, pages_lines, doc, cache, words, pad=5.0):
    """页内「非点线带」：把 [y_lo, y_hi] 按连续点线块切开，返回 [(y0,y1)]。

    空带（全是点线）不返回——纯点线页因此自然被丢弃。
    """
    lines = content_lines(pi, pages_lines)
    dots = sorted((ln["bbox"][1], ln["bbox"][3]) for ln in lines
                  if is_dotted(ln) and ln["bbox"][1] >= y_lo - 2 and ln["bbox"][3] <= y_hi + 2)
    merged = []
    for a, b in dots:
        if merged and a <= merged[-1][1] + 2:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))
    segs, cur = [], y_lo
    for a, b in merged:
        if a - cur > 6:
            segs.append((cur, a))
        cur = max(cur, b)
    if y_hi - cur > 6:
        segs.append((cur, y_hi))

    out = []
    for a, b in segs:
        has_text = any(not is_dotted(ln) and ln["bbox"][1] >= a - 1 and ln["bbox"][3] <= b + 1
                       for ln in lines)
        if not has_text:
            continue
        tb = tight_bounds(pi, a, b, pages_lines, doc, cache, words, pad)
        if tb and tb[1] - tb[0] > 6:
            out.append(tb)
    return out


def split_segments(pages_text):
    """→ (qp_lo, qp_hi, mat_lo, mat_hi)。三种版式：

    ① 普通卷（78 卷）：p1 QP 封面 → 题目 → 资料册封面 → 资料
    ② 2022-Jan（4 卷）：**资料册在最前**（p1 就是 Source Booklet 封面）→ 题目
    ③ -01a 中国区（8 卷）：**三段**——题目（p1 封面 + p2–p4，无点线）
       → 资料册（p5 封面 + p6–p9）→ **作答册**（p13 起，页顶印
       "Write the answer to Question 1(a) in the space provided."，
       且封面带 "Please check the examination details"）。
       ⚠️ 不分出第三段的话，`Please check…` 会被当成 QP 封面，
       于是 QP 段落在作答册上、三题全部找不到题号行（8 卷实测 0 题）。
    """
    qp_cover = next((i for i, t in enumerate(pages_text)
                     if "Please check the examination details" in t), None)
    src_cover = None
    for i, t in enumerate(pages_text):
        if i == qp_cover:
            continue
        if any(SRC_COVER_RE.match(ln) for ln in t.splitlines()):
            src_cover = i
            break
    if src_cover is None:
        cand = [i for i, t in enumerate(pages_text) if "for use with Section" in t]
        if not cand:
            sys.exit("未找到 Source Booklet")
        src_cover = cand[0] - 1
    n = len(pages_text) - 1

    if src_cover == 0:
        # ② 资料册在最前：题目紧跟其后（QP 封面 = "Please check…" 那页）
        if qp_cover is None:
            sys.exit("资料册在前但找不到 QP 封面")
        return qp_cover, n, 0, qp_cover - 1
    if qp_cover is not None and qp_cover > src_cover:
        # ③ -01a 三段式：题目在资料册之前，资料册之后那页是作答册
        return 0, src_cover - 1, src_cover + 1, qp_cover - 1
    # ① 普通卷
    if qp_cover is None:
        sys.exit("未找到 QP 封面（Please check the examination details）")
    return qp_cover, src_cover - 1, src_cover + 1, n


def expand_letters(s):
    """'A, B and C' / 'A–E' / 'A to D' → ['A','B','C'] / ['A'..'E'] / ['A'..'D']。

    三种写法 86 卷里都出现了：
      · 列举 `A, B and C`（最常见）
      · 范围 en-dash `A–E`（2020-Oct WBS13、2021-Oct U3）
      · 范围 to `A to D`（2022-June WBS11）
    """
    s = s.replace("–", "-").replace("—", "-")
    letters = re.findall(r"[A-Z]", s)
    m = re.search(r"([A-Z])\s*(?:-|to)\s*([A-Z])", s, re.I)
    if m and len(letters) == 2:
        return [chr(c) for c in range(ord(m.group(1)), ord(m.group(2)) + 1)]
    return letters


def parse_study(pages_text, qp_lo, qp_hi):
    """→ {题号: [Extract 字母]}（卷面 Read Extracts 句，可能不完整）。"""
    out = {}
    for pi in range(qp_lo, qp_hi + 1):
        flat = re.sub(r"\s+", " ", pages_text[pi])
        for m in STUDY_RE.finditer(flat):
            n = int(m.group(2))
            if n in out:
                continue
            out[n] = expand_letters(m.group(1))
    return out


def extract_marks(mat_lo, mat_hi, pages_lines):
    """→ [(page, y0, y1, letter)]，资料册里所有 Extract 标签，文档序。"""
    out = []
    for pi in range(mat_lo, mat_hi + 1):
        for ln in pages_lines[pi]:
            b = ln["bbox"]
            if b[1] < HEADER_Y - 2 or b[3] > B + 8:
                continue
            m = EXT_LABEL_RE.match(ln["raw"].strip())
            if m:
                out.append((pi, b[1], b[3], m.group(1)))
    out.sort(key=lambda x: (x[0], x[1]))
    return out


def align_extracts(study, marks):
    """区间法确定每题的资料范围。

    首字母来自卷面 study 句；题 n 覆盖 [首字母, 下一题首字母) 之间的所有 Extract。
    这样即使 study 句只写了 "Read Extract A"（实际 A–E），也能补齐。
    返回 {题号: [字母]} 与告警列表。
    """
    order = sorted(study)
    first = {n: study[n][0] for n in order if study[n]}
    all_letters = sorted({m[3] for m in marks})
    if not first:
        return {}, ["没有任何 Read Extracts 句"]
    notes = []
    out = {}
    for i, n in enumerate(order):
        a = first[n]
        b = first[order[i + 1]] if i + 1 < len(order) else None
        if b is None:
            sel = [x for x in all_letters if x >= a]
        else:
            sel = [x for x in all_letters if a <= x < b]
        out[n] = sel
        if not sel:
            notes.append(f"Q{n}: 字母区间 {a}..{b or '∞'} 内没有 Extract 标签")
        if study[n] and set(study[n]) != set(sel):
            notes.append(f"Q{n}: 卷面写 {''.join(study[n])}，区间法得 {''.join(sel)}")
    return out, notes


def material_segments(q, letters, marks, mat_lo, mat_hi, pages_lines, doc, cache, words):
    """该题的资料段（多段，文档序）。一页里若混着别题的资料，按 Extract 标签切开。"""
    if not letters:
        return []
    per_page = {}
    for pi, y0, y1, letter in marks:
        per_page.setdefault(pi, []).append((y0, y1, letter))
    # 致谢块（"Acknowledgements"/"Acknowledgments" 起的版权声明）不算资料内容，但**不能整页排除**：
    # 2020-Jan WBS12 的 Extract D 与致谢印在同一页，整页丢会连 D 一起丢（Q3 只剩题面 172px）。
    # ⚠️ 两种拼写都有：2025-Oct WBS13A 用的是美式 "Acknowledgments"（少一个 e），
    #    只认英式会让整页致谢被当资料拼进题图（培琪 2026-10-22 截图指出）。
    ack_y = {}
    for pi in range(mat_lo, mat_hi + 1):
        for ln in pages_lines[pi]:
            t = ln["raw"].strip().lower()
            if t.startswith(("acknowledgement", "acknowledgment")):
                ack_y[pi] = ln["bbox"][1]
                break
    regions = []
    carry = None                      # 上一页最后一个 Extract 的归属，用于无标签页
    for pi in range(mat_lo, mat_hi + 1):
        lines = content_lines(pi, pages_lines)
        if not lines:
            continue
        limit = (ack_y[pi] - 4) if pi in ack_y else B
        labels = per_page.get(pi)
        if labels:
            for i, (y0, y1, letter) in enumerate(labels):
                own = letter in letters
                carry = own
                if not own:
                    continue
                top = y0 - 4
                nxt = labels[i + 1][0] - 2 if i + 1 < len(labels) else limit
                nxt = min(nxt, limit)
                if nxt - top > 8:
                    regions.append([pi, top, nxt])
            continue
        if carry:
            # 无 Extract 标签的页：继承上一页的归属（资料整页顺排）
            if limit - T > 8:
                regions.append([pi, T, limit])

    # 逐段收紧到内容（文字 + 旋转词框 + 框线 + 图）
    out = []
    for pi, top, bot in regions:
        if not any(not is_dotted(ln) and ln["bbox"][3] > top and ln["bbox"][1] < bot
                   for ln in content_lines(pi, pages_lines)):
            continue
        tb = tight_bounds(pi, top, bot, pages_lines, doc, cache, words, pad=6.0)
        if not tb or tb[1] - tb[0] < 8:
            continue
        out.append({"page": pi, "bbox": [r2(L), tb[0], r2(R), tb[1]]})
    return out


def find_qstart(n, pages_lines, lo, hi):
    """题号行：粗体、顶格（x0≈42.5）。表格数字既不顶格也不粗体。"""
    for pi in range(lo, hi + 1):
        for ln in content_lines(pi, pages_lines):
            b = ln["bbox"]
            if b[0] > LEFT_MARGIN_MAX:
                continue
            if not ln["bold"]:
                continue
            m = re.match(r"^(\d{1,2})[\t\u0020\u00a0\u2009]", ln["raw"].strip())
            if m and int(m.group(1)) == n:
                return {**ln, "page": pi}
    return None


def find_total(n, pages_lines, lo, hi):
    for pi in range(lo, hi + 1):
        for ln in pages_lines[pi]:
            m = TOTAL_RE.search(ln["raw"])
            if m and int(m.group(1)) == n:
                return {"page": pi, "bbox": ln["bbox"], "marks": int(m.group(2))}
    return None


def section_entries(pages_lines, lo, hi):
    """[(page, y0, 'A', bbox)]，直接用未过滤的行（content_lines 把 SECTION 字头滤掉了）。"""
    out = []
    for pi in range(lo, hi + 1):
        for ln in pages_lines[pi]:
            b = ln["bbox"]
            if b[1] < HEADER_Y - 2 or b[3] > B + 8:
                continue
            m = SECTION_RE.match(ln["raw"].strip())
            if m:
                out.append((pi, b[1], m.group(1).upper(), [r2(v) for v in b]))
    out.sort(key=lambda x: (x[0], x[1]))
    return out


def study_line_y(pi, n, pages_lines):
    """该页 `Read Extracts … Question n` 指引行的 y0（Section 字头丢弃后用它当段首）。"""
    flat_y = None
    for ln in content_lines(pi, pages_lines):
        if re.search(r"Read\s+Extracts?", ln["raw"], re.I):
            flat_y = ln["bbox"][1]
            break
    return flat_y


def collect_subs(regions, pages_lines):
    """题面段内的小问标签行 → [{label,page,bbox}]。"""
    subs = []
    parent = None
    for rg in regions:
        pi = rg["page"]
        y0, y1 = rg["bbox"][1], rg["bbox"][3]
        for ln in content_lines(pi, pages_lines):
            b = ln["bbox"]
            if b[1] < y0 - 1 or b[3] > y1 + 1:
                continue
            if b[0] > LEFT_MARGIN_MAX:
                continue
            parsed = parse_subhead(ln["raw"])
            tokens = None
            if parsed and parsed[2]:
                tokens = parsed[2]
            else:
                bare = parse_bare_subhead(ln["raw"])
                if bare and bare[1]:
                    tokens = bare[1]
            if tokens is None:
                continue
            if tokens[0].isalpha() and len(tokens[0]) == 1:
                parent = tokens[0]
            ent = {"label": sub_label(tokens), "page": pi,
                   "bbox": [r2(v) for v in b]}
            if any(s["label"] == ent["label"] for s in subs):
                continue
            subs.append(ent)
    return subs


def main():
    ap = argparse.ArgumentParser(description="Edexcel IAL 商务卷按题切割")
    ap.add_argument("session_dir")
    ap.add_argument("--analyze", action="store_true", help="只打印锚点表，不落盘")
    ap.add_argument("--questions", default=None, help="只处理这些题号（逗号分隔）")
    args = ap.parse_args()

    sdir = Path(args.session_dir).resolve()
    src = sdir / "source.pdf"
    if not src.is_file():
        sys.exit(f"缺少 source.pdf: {sdir}")

    doc = pymupdf.open(src)
    pages_text = [pg.get_text() for pg in doc]
    pages_lines = [build_page_lines(pg) for pg in doc]
    all_words = [doc[i].get_text("words") for i in range(doc.page_count)]
    pages_pairs = [assign_words_to_lines(pages_lines[i], all_words[i])
                   for i in range(doc.page_count)]
    n_pages = doc.page_count
    cache = {}

    qp_lo, qp_hi, mat_lo, mat_hi = split_segments(pages_text)
    marks = extract_marks(mat_lo, mat_hi, pages_lines)
    study = parse_study(pages_text, qp_lo, qp_hi)
    extract_for, notes = align_extracts(study, marks)

    if not extract_for:
        sys.exit("资料归属解析失败")

    # 卷号 / 年份
    meta = {}
    md_path = sdir / "paper.md"
    if md_path.exists():
        m = re.match(r"^---\n(.*?)\n---", md_path.read_text(encoding="utf-8"), re.S)
        if m:
            for line in m.group(1).split("\n"):
                km = re.match(r"^([A-Za-z_][A-Za-z0-9_]*):\s*(.*)$", line)
                if km:
                    meta[km.group(1)] = km.group(2).strip().strip('"')
    paper_code = (meta.get("paper_code") or sdir.name.split("-")[-1]).lower()
    year = int(meta.get("year") or re.search(r"(\d{4})", sdir.name).group(1))
    session = meta.get("session") or sdir.name.split("-")[1]

    secs = section_entries(pages_lines, qp_lo, qp_hi)

    # 每题所属 Section + 各 Section 的首题（首题要连带 `Read Extracts …` 指引行）
    qstart = {}
    for n in sorted(extract_for):
        st0 = find_qstart(n, pages_lines, qp_lo, qp_hi)
        if st0 is None:
            continue
        qstart[n] = st0
    qsec = {}
    for n, st0 in qstart.items():
        s = None
        for sp, sy, sname, _sb in secs:
            if (sp, sy) < (st0["page"], st0["bbox"][1]):
                s = sname
        qsec[n] = s
    sec_first = {}
    for n in sorted(qsec):
        sec_first.setdefault(qsec[n], n)

    print(f"=== {sdir.name}  {n_pages} 页 ===")
    print(f"    QP p{qp_lo + 1}-{qp_hi + 1} | 资料 p{mat_lo + 1}-{mat_hi + 1} | "
          f"Extract {''.join(sorted({m[3] for m in marks}))}")
    print(f"    归属 " + "  ".join(f"Q{k}:{''.join(v)}" for k, v in sorted(extract_for.items())))
    for x in notes:
        print(f"    [note] {x}")

    only = {int(x) for x in args.questions.split(",")} if args.questions else None
    questions = []
    for n in sorted(extract_for):
        if only and n not in only:
            continue
        st = find_qstart(n, pages_lines, qp_lo, qp_hi)
        en = find_total(n, pages_lines, qp_lo, qp_hi)
        if st is None or en is None:
            print(f"  [skip] Q{n}: 起点={st is not None} 终点={en is not None}")
            continue

        # 本节 Section 字母
        sec = qsec.get(n)

        # 段首：Section 首题时上移到 `Read Extracts …` 指引行（SECTION 字头丢弃）
        start_p, start_y = st["page"], r2(st["bbox"][1] - 1)
        head = None
        if sec is not None and sec_first.get(sec) == n:
            ent = next(((a, b, d) for a, b, c, d in secs if c == sec), None)
            if ent is not None:
                sp, sy, sbb = ent
                y = study_line_y(sp, n, pages_lines)
                if y is not None:
                    start_p, start_y = sp, r2(y - 4)
                    head = {"page": sp, "bbox": sbb}
                else:
                    start_p, start_y = sp, r2(sy - 1)

        # 终点：**不含** Total 行（那是卷务信息，分值已标在各小问后）；
        # p7/p13/p16 那种「纯点线 + Total」的页因此整页消失，图更紧凑。
        end_p, end_y = en["page"], r2(en["bbox"][1] - 2)

        # 题面：逐页逐带
        regions = []
        for pi in range(start_p, end_p + 1):
            y_lo = start_y if pi == start_p else T
            y_hi = end_y if pi == end_p else B
            for y0, y1 in bands(pi, y_lo, y_hi, pages_lines, doc, cache, all_words):
                regions.append({"page": pi, "bbox": [r2(L), y0, r2(R), y1]})
        if not regions:
            print(f"  [skip] Q{n}: 题面段为空")
            continue

        q = {
            "number": str(n),
            "status": "auto",
            "confidence": 0.95,
            "question_type": "structured",
            "total_marks": en["marks"],
            "section": sec,
            "anchors": {
                "start": {"page": st["page"], "bbox": [r2(v) for v in st["bbox"]]},
                "end": {"page": en["page"], "bbox": [0.0, 0.0, 0.0, r2(en["bbox"][3])]},
                "cut": "点线全裁（逐页逐带）",
            },
            "regions": regions,
        }
        if head:
            q["anchors"]["section_head_dropped"] = head

        mregs = material_segments(n, extract_for[n], marks, mat_lo, mat_hi,
                                  pages_lines, doc, cache, all_words)
        if mregs:
            q["material_regions"] = mregs

        subs = collect_subs(regions, pages_lines)
        if subs:
            q["subquestions"] = subs

        questions.append(q)

        if args.analyze:
            qp_h = sum(r["bbox"][3] - r["bbox"][1] for r in regions)
            mat_h = sum(r["bbox"][3] - r["bbox"][1] for r in (mregs or []))
            tot = qp_h + mat_h
            pp = sorted({r["page"] + 1 for r in regions})
            mp = sorted({r["page"] + 1 for r in (mregs or [])})
            print(f"  Q{n} {en['marks']:>3}分 sec={sec or '-'} "
                  f"题面 p{'/'.join(map(str, pp))} x{len(regions)}段 {qp_h:5.0f}pt | "
                  f"资料 p{'/'.join(map(str, mp))} x{len(mregs or [])}段 {mat_h:5.0f}pt | "
                  f"共 {tot:5.0f}pt = {tot * 200 / 72:6.0f}px | sub={len(subs)}")

    if args.analyze:
        print(f"\n共 {len(questions)} 题（analyze，未落盘）")
        return

    sha = hashlib.sha256(src.read_bytes()).hexdigest()
    cut_path = sdir / "cut.json"
    existing = None
    if cut_path.exists():
        try:
            existing = json.loads(cut_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            existing = None
        if existing and existing.get("source", {}).get("sha256") != sha:
            print("  [warn] source.pdf 与既有 cut.json 的 sha256 不符，丢弃旧坐标")
            existing = None
    merged, report = merge_with_existing(questions, existing)
    for q in merged:
        for k in [k for k in q if k.startswith("_")]:
            q.pop(k, None)

    r0 = doc[0].rect
    out = {
        "schema_version": 1,
        "source": {"file": "source.pdf", "sha256": sha, "pages": n_pages,
                   "paper_ref": meta.get("paper_ref") or ""},
        "page_size": {"w": r2(r0.width), "h": r2(r0.height)},
        "render": {"dpi": 200, "format": "webp", "quality": 85},
        "blocks": ["qp", "material"],
        "material_order": "before",
        "questions": merged,
    }
    cut_path.write_text(json.dumps(out, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    for q in merged:
        write_question_md(sdir, int(q["number"]), q, "edexcel", "a-level", "business",
                          year, session, paper_code, pages_lines, pages_pairs)
    print(f"\ncut.json 已写入（{len(merged)} 题）")
    for line in report:
        print("  ·", line)
    doc.close()


if __name__ == "__main__":
    main()
