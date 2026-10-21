#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""cut_paper_cie_math.py — CIE 9709 数学卷按题切割（保留答题区，剔除纯答题纸页）

只服务 CIE A Level 数学（9709）。产出与其它 cut_paper_* 同构：
cut.json + qNN.md，可直接进 render_questions.py / validate_cut.py / 打标台。

版式（实测 189 份可读卷，全部 612×792 Letter）：
    · 题号：页面顶部 x0≈68–76、y0≈58–70，独立一行、加粗，纯 1–2 位数字；
    · 题干缩进到 x≈94.7，小问标签 (a)(b)(c) 在 x≈94.7 或 116.8；
    · 答题区就是点线行（"......" 满行），y 间距 25.9pt —— **要保留**；
    · 分值 [n] 右对齐 x≈526；
    · 页眉：页码 y≈37.5 居中，条码 *xxxx* 在 y≈211（封面）；
    · 页脚：© UCLES / 卷号 / [Turn over 在 y≥746；
    · 一道题常独占一页，长题续页（续页顶部没有题号，直接接点线或 "(b)"）。

剔除的页（王培琪 2026-10-20 定）：
    · 封面（含 "You must answer on the question paper"）；
    · "BLANK PAGE" 页；
    · "Additional Page" / "Additional page" 整页答题纸（整页都是横线的那种，不要）；
    · 页脚/页眉装饰。

坐标约定同全仓库：PDF 点 pt，左上原点，y 向下，bbox=[x0,y0,x1,y1]。

用法:
    python scripts/cut_paper_cie_math.py <session_dir> [--dry-run] [--force]
"""

import argparse
import hashlib
import json
import re
import sys
from collections import Counter
from pathlib import Path

import pymupdf

HEADER_Y = 50.0
FOOTER_Y = 744.0
PAD = 4.0
L_MARGIN = 44.0
R_MARGIN = 44.0

# 2024 年起的版式会把题号并进题干同一行（"10 The equation of a circle is ("），
# 老卷是题号独占一行，两种都要认，所以用「行首 1–2 位数字 + 空白/行尾」。
QNUM_RE = re.compile(r"^(\d{1,2})(?:\s|$)")
SUB_RE = re.compile(r"^\(([a-z])\)")
MARK_RE = re.compile(r"\[(\d{1,2})\]")
BLANK_RE = re.compile(r"^BLANK\s+PAGE$", re.I)
ADDITIONAL_RE = re.compile(r"^Additional\s+Page$", re.I)
COVER_RE = re.compile(r"You must answer on the question paper", re.I)
TOTAL_RE = re.compile(r"The total mark for this paper is\s*(\d+)", re.I)

BOARDS = {"edexcel", "aqa", "cie"}
QUALS = {"alevel", "a-level", "igcse", "gcse"}
SESSIONS = {"jan", "mar", "may-jun", "june", "oct", "oct-nov"}


def r2(v):
    return round(float(v), 2)


def yaml_scalar(v):
    s = str(v)
    if s == "" or re.search(r"[:#\[\]{}&*!|>'\"%@`]", s) or s.strip() != s:
        return '"' + s.replace('"', '\\"') + '"'
    return s


# ── 页面内容抽取 ────────────────────────────────────────────────────────────

SERIAL_RE = re.compile(r"^\*\s*\d{6,}\s*\*$")


def looks_garbled(txt):
    """AllAndNone 缺 ToUnicode 排出来的乱码：要么含控制字符（条形码那种），
    要么几乎全是 Latin-1/扩展区的可打印字符（安全码 `ĬÙĊ®Ġ´îÇ…` 那种，
    一个控制字符都没有，只按 ord<32 判会漏）。"""
    if any(ord(c) < 32 for c in txt):
        return True
    if len(txt) >= 6:
        hi = sum(1 for c in txt if ord(c) > 127)
        return hi / len(txt) >= 0.6
    return False


def is_furniture_row(txt, bbox):
    """页边家具：2024 起每页顶部有「条形码」——它其实是一行用
    AllAndNone 乱码字体排的文字（含控制字符），外加 `*0019656641003*`
    流水号。不剔掉就会出现在每张题图顶端（还被裁成半截）。

    ⚠️ 2024-oct 起部分卷的**数学公式也用 AllAndNone**（正文可读、公式乱码），
    所以「含控制字符」单独一条不够——必须同时要求**在题干列左侧**
    （条形码 x0≈60.6，题干列 x0≈93.7）。否则会把 (b) 小问连同 [3] 一起切掉
    （2024-oct-qp33 Q5 分值 3≠6 就是这么来的）。
    """
    if bbox[2] <= L_MARGIN + 2:                     # 左边距「DO NOT WRITE…」
        return True
    if bbox[1] < 80 and bbox[0] < 90 and (SERIAL_RE.match(txt) or looks_garbled(txt)):
        return True
    return False


def page_rows(doc, pi, footer_y):
    """一页的有效文本行（含加粗标记），剔除页眉页码/页脚。

    返回 (rows, pn_bbox)：pn_bbox 是被剔掉的页码包围盒（没有则 None）。
    2024 年起的版式页码下移到 y≈44–56，正好压在 HEADER_Y=50 上，所以
    上沿还得靠这个 bbox 让位，不能只靠固定页眉线。
    """
    page = doc[pi]
    w = page.rect.width
    rows = []
    pn_bbox = None
    furn_bottom = None
    furn_ceil = None
    for b in page.get_text("dict")["blocks"]:
        if b["type"] != 0:
            continue
        for ln in b["lines"]:
            txt = "".join(s["text"] for s in ln["spans"]).strip()
            if not txt:
                continue
            bold = any(("bold" in s["font"].lower()) or (s["flags"] & 16) for s in ln["spans"])
            y0, x0 = ln["bbox"][1], ln["bbox"][0]
            if y0 >= footer_y:
                continue
            if y0 < HEADER_Y:
                # 页码（顶部居中 1–3 位数字）——记下 bbox 供上沿让位
                if re.fullmatch(r"\d{1,3}", txt) and abs((x0 + ln["bbox"][2]) / 2 - w / 2) < 40:
                    pn_bbox = [r2(v) for v in ln["bbox"]]
                continue
            bb = [r2(v) for v in ln["bbox"]]
            # 页底安全码：2024 起的卷在页脚上方几 pt 还有一行 AllAndNone 乱码
            # （如 2024-oct-qp33 的 y=746.1，页脚 751.3），它在 footer_top 之上，
            # 不排掉就会在题图底部留一条残影。只认「紧贴页脚 12pt 内」，
            # 免得把正文里的乱码公式行当家具切掉。
            if y0 >= footer_y - 12.0 and looks_garbled(txt):
                furn_ceil = min(furn_ceil or 1e9, bb[1])
                continue
            if is_furniture_row(txt, bb):
                # 记下家具下沿：区域上沿必须让到它下面，否则条形码会留在图里
                furn_bottom = max(furn_bottom or 0.0, bb[3])
                continue
            rows.append({"raw": txt, "bbox": bb,
                         "y0": ln["bbox"][1], "x0": x0, "bold": bold})
    return rows, pn_bbox, furn_bottom, furn_ceil


def page_draw_rects(doc, pi, footer_y):
    page = doc[pi]
    out = []
    for d in page.get_drawings():
        r = d["rect"]
        if r.height >= 0.5 and r.width >= 0.5 and r.y1 <= footer_y and r.y0 >= HEADER_Y:
            out.append([r.x0, r.y0, r.x1, r.y1])
    return out


def content_bottom(rows, draws, limit):
    ys = [r["bbox"][3] for r in rows if r["bbox"][3] <= limit]
    ys += [d[3] for d in draws if d[3] <= limit]
    return max(ys) if ys else None


# ── 题号识别 ────────────────────────────────────────────────────────────────

def detect_qcol(pages_rows):
    """自动测出题号所在列（x0）。

    绝大多数卷是 Letter 612×792，题号固定在 x0≈72.6，所以历史上是写死常数。
    但 2021-mar-qp12 是 **A4 595×842**，题号在 x0≈49.3、小问在 72.8、题干 96.4，
    写死 64–84 就直接一个题号都认不出来（NO_QUESTIONS）。

    只取「纯数字众数」会被卷内表格骗到：2025-june-qp53 的 Statistics 表格有一整列
    10/20/…/90 排在 x≈127，出现次数压过真正的题号列（x≈49.6，仅十余个），
    结果整卷认不出题号。改为**优先选题号能连成 1..N 的那一列**，连续越多越好，
    平局再看出现次数。
    """
    xs = Counter()
    for rs in pages_rows:
        for r in rs:
            t = r["raw"].strip()
            if len(t) <= 2 and t.isdigit():
                xs[round(r["x0"])] += 1
    if not xs:
        return 72.6

    best_x, best_score = None, -1
    for x, cnt in xs.most_common(8):
        if cnt < 2:
            continue
        starts = find_question_starts(pages_rows, len(pages_rows), float(x))
        # 评分：能连到的最大题号；同分取出现次数多的
        score = starts[-1][0] if starts else 0
        if (score, cnt) > (best_score, -1):
            best_x, best_score = float(x), score
    if best_x is not None and best_score >= 3:
        return best_x
    return float(xs.most_common(1)[0][0])


def find_question_starts(pages_rows, n_pages, qcol=72.6):
    """按 1..N 递增序列找题号，返回 [(num, page, y0, bbox, gaps)]。

    题号列由 detect_qcol() 实测（Letter 卷 72.6、A4 卷 49.3），容差 ±10。
    **不在页顶**：上一题短的时候下一题会紧接着排在同一页中段（如 2021-mar-qp62
    的 Q4 在 p6 y=479.6），所以不能按 y 卡。
    带宽必须收紧：题干里分式的分子会落在题干列左侧（如 2023-june-qp11 的
    "16/3 π" 的 "16" 在 x0=95.3），带宽放宽会把它当题号。
    """
    cands = []
    for pi in range(n_pages):
        for r in pages_rows[pi]:
            if abs(r["x0"] - qcol) > 10.0:
                continue
            m = QNUM_RE.match(r["raw"])
            if not m:
                continue
            cands.append((int(m.group(1)), pi, r["y0"], r["bbox"]))
    cands.sort(key=lambda c: (c[1], c[2]))

    starts, expect = [], 1
    for num, pi, y0, bbox in cands:
        if num < expect:
            continue
        gaps = list(range(expect, num))
        # 缺号只容忍 1–2 个（源卷偶尔丢题号）；更大的跳跃几乎都是误判，
        # 宁可漏认也不要把后面几题并进当前题。
        if len(gaps) > 2:
            continue
        starts.append((num, pi, y0, bbox, gaps))
        expect = num + 1
    return starts


# ── 主体 ────────────────────────────────────────────────────────────────────

def cut_paper(sdir: Path, dry_run=False, force=False):
    board = sdir.parents[2].name
    qualification = sdir.parents[1].name
    subject = sdir.parents[0].name
    year_s, session, paper_code = sdir.name.split("-", 2)
    year = int(year_s)
    if board != "cie" or subject not in ("mathematics", "further-mathematics"):
        sys.exit(f"本脚本只服务 CIE 数学（9709 / 9231）：{sdir}")

    src = sdir / "source.pdf"
    if not src.exists():
        sys.exit(f"缺少 source.pdf: {src}")
    sha = hashlib.sha256(src.read_bytes()).hexdigest()

    doc = pymupdf.open(src)
    n_pages = doc.page_count
    pw, ph = doc[0].rect.width, doc[0].rect.height
    # 左侧整页黑条（2024 起版式，x≈26–46）：直接照 44 切会露出 1–2pt 黑边。
    # 封面页没有这条，所以扫前几页内容页。
    L = L_MARGIN
    for _pi in range(1, min(6, n_pages)):
        for _d in doc[_pi].get_drawings():
            _r = _d["rect"]
            if _r.height > ph * 0.6 and _r.x1 < 100:
                L = max(L, _r.x1 + 1.5)
        if L > L_MARGIN:
            break

    # 页脚上沿：**逐页**取 © UCLES / [Turn over 的最小 y。
    # 不能用全局最小值——"[Turn over" 只在部分页出现，位置还比 "© UCLES" 高
    # 十几 pt（2024-oct-qp33：有的页 751、有的页 731）。全局取最小会把正文
    # 点线区一起截掉（Q5 末条点线 724–734.8 被切在 731）。
    fy_page = {}
    for pi in range(n_pages):
        ys = []
        for b in doc[pi].get_text("dict")["blocks"]:
            if b["type"] != 0:
                continue
            for ln in b["lines"]:
                t = "".join(s["text"] for s in ln["spans"]).strip()
                if ln["bbox"][1] < ph * 0.80:
                    continue
                if "UCLES" in t or t.startswith("[Turn over") or t.startswith("Turn over"):
                    ys.append(round(ln["bbox"][1]))
        fy_page[pi] = ys
    _fy_all = [y for ys in fy_page.values() for y in ys]
    _fy_global = float(min(_fy_all)) if _fy_all else FOOTER_Y

    def page_footer_top(pi):
        ys = fy_page.get(pi) or []
        return float(min(ys)) if ys else _fy_global

    pages_rows, pages_draws, pages_pn, pages_furn, pages_ceil = [], [], [], [], []
    for pi in range(n_pages):
        rs, pn, fb, fc = page_rows(doc, pi, page_footer_top(pi) - 2.5)
        pages_rows.append(rs)
        pages_pn.append(pn)
        pages_furn.append(fb)
        _ft = page_footer_top(pi)
        for _d in doc[pi].get_drawings():
            _r = _d["rect"]
            if _r.y0 >= _ft - 12.0 and _r.y1 > _ft - 2.5 and _r.height <= ph * 0.6:
                fc = min(fc or 1e9, _r.y0)
        pages_ceil.append(fc)
        pages_draws.append(page_draw_rects(doc, pi, page_footer_top(pi) - 2.5))

    # 每页墨迹区间（文字行 + 真图形；左侧整页黑条不算）
    pages_ink = []
    for pi in range(n_pages):
        ink = [(r["bbox"][1], r["bbox"][3]) for r in pages_rows[pi]]
        for _d in doc[pi].get_drawings():
            _r = _d["rect"]
            if _r.height > ph * 0.6:
                continue
            ink.append((_r.y0, _r.y1))
        pages_ink.append(ink)

    def last_ink_before(pi, y):
        """本页在 y 之上结束的最后一条墨迹的下沿。"""
        ys = [y1 for (_a, y1) in pages_ink[pi] if y1 <= y - 2.0]
        return max(ys) if ys else None

    # 特殊页
    def page_kind(pi):
        txts = [r["raw"] for r in pages_rows[pi]]
        joined = " ".join(txts)
        if COVER_RE.search(joined):
            return "cover"
        if any(BLANK_RE.match(t) for t in txts):
            return "blank"
        if any(ADDITIONAL_RE.match(t) for t in txts):
            return "additional"
        return "content"

    kinds = [page_kind(pi) for pi in range(n_pages)]

    def safe_header(pg):
        """页眉安全线：让到页码/条形码下方（2024 版式两者都压在上边距里）。"""
        v = HEADER_Y
        pn, fb = pages_pn[pg], pages_furn[pg]
        if pn:
            v = max(v, pn[3] + 2.0)
        if fb:
            v = max(v, fb + 1.0)
        return v
    qcol = detect_qcol(pages_rows)
    # 小问标签列：Letter 卷 72.6→94.7、A4 卷 49.3→72.8，都在题号列右侧 22pt 左右
    scol = qcol + 22.0
    starts = find_question_starts(pages_rows, n_pages, qcol)
    if not starts:
        sys.exit(f"NO_QUESTIONS {sdir.name}: 未识别到题号")
    if kinds[0] == "content" and starts[0][1] == 0:
        kinds[0] = "cover"

    # 题区
    questions = []
    pending_top = {}       # 页号 → 与上一题的分界线（同页多题时复用，保证首尾相接）
    for i, (num, pi, y0, bbox, gaps) in enumerate(starts):
        if i + 1 < len(starts):
            npi, ny0 = starts[i + 1][1], starts[i + 1][2]
        else:
            npi, ny0 = None, None
        regions = []
        pg = pi
        # 起始页上沿的四种情形：
        #  ① 页首起题（绝大多数）：取本页最靠上的墨迹（文字行 + 图形）。
        #     图形里常有 y0=6 / y0=40 的裁剪包围盒（如 2023-june-qp13 Q10 的
        #     120x177@y40），会白送 10pt 页眉，但绝不会切到内容，所以照收。
        #  ② 同页上一题刚结束：直接用上一题让出来的分界线，首尾相接不重叠。
        #  ③ 首行是积分/分式时，数学符号会高出题号 20pt 以上
        #     （2023-june-qp21 Q6 的 ∫ 上限在 y=59.3，题号在 y=73），
        #     所以不能像物理那样写死「题号 y−4」，必须按墨迹取。
        #  ④ 兜底：取「在本题号之上结束的最后一条墨迹」的下沿 +3。
        top = pending_top.pop(pi, None)
        if top is None:
            prev_same_page = i > 0 and starts[i - 1][1] == pi
            if prev_same_page:
                _cb = last_ink_before(pi, y0)
                top = max(HEADER_Y, (_cb + 3.0) if _cb else (y0 - PAD))
            else:
                ink_y0 = [a for (a, _b) in pages_ink[pi]]
                top = max(HEADER_Y, (min(ink_y0) if ink_y0 else y0) - PAD)
        # 页眉/页码/条形码下方
        top = max(top, safe_header(pg))
        while True:
            if npi is not None and pg == npi:
                # 下一页就是下一题：分界线放在「上题内容底」与「下题首行顶」之间。
                #   上题内容底：last_ink_before(下题首行顶)
                #   下题首行顶：下题首行常带上下标（2026-june-qp12 Q4 的 (3−x/2)^4
                #   的上标在 y=559.1，题号在 565.1，高出 6pt），直接按「题号−2」
                #   会把上标削掉，所以先在题号附近的窗口里找首行墨迹的最高点。
                if kinds[pg] == "content":
                    _win = [a for (a, _b) in pages_ink[pg] if ny0 - 16.0 <= a < ny0 + 2.0]
                    _q_top = min(_win) if _win else (ny0 - 2.0)
                    _q_prev_bot = last_ink_before(pg, _q_top)
                    if _q_prev_bot is not None and _q_prev_bot < _q_top - 1.0:
                        bottom = (_q_prev_bot + _q_top) / 2.0
                    else:
                        bottom = max(_q_top - 1.0, ny0 - 16.0)
                    bottom = min(bottom, ny0 - 1.0)
                    regions.append({"page": pg, "bbox": [r2(L), r2(top),
                                                         r2(pw - R_MARGIN), r2(bottom)]})
                    pending_top[pg] = bottom
                break
            if kinds[pg] == "content":
                # 整页：下沿一律到页脚文字上沿，**必须保留底部留白**。
                # 留白本身就是作答区（2023-oct-qp33 Q2 就是整页白纸让学生
                # 自己画 Argand 图），按 content_bottom 收会把答题区切掉。
                # footer_top 是页脚文字上沿，末条点线（y≈750.5）在它上方，
                # 所以也不会被切半。
                _bot = page_footer_top(pg)
                if pages_ceil[pg]:
                    _bot = min(_bot, pages_ceil[pg] - 1.0)
                regions.append({"page": pg, "bbox": [r2(L), r2(top),
                                                     r2(pw - R_MARGIN), r2(_bot)]})
            if npi is None:
                # 最后一题：只在下一页仍是内容页时续页
                if pg + 1 < n_pages and kinds[pg + 1] == "content":
                    pg += 1
                    top = safe_header(pg)
                    continue
                break
            if pg + 1 < npi:
                pg += 1
                top = safe_header(pg)
                continue
            break
        # 去掉空 region
        regions = [r for r in regions if r["bbox"][3] - r["bbox"][1] > 8]
        if not regions:
            continue
        # 分值
        text = []
        for rg in regions:
            for r in pages_rows[rg["page"]]:
                if rg["bbox"][1] - 1 <= r["y0"] and r["bbox"][3] <= rg["bbox"][3] + 1:
                    text.append(r["raw"])
        marks = sum(int(m) for m in MARK_RE.findall(" ".join(text)))
        # 小问标签（顶层 a/b/c）
        subs = []
        for rg in regions:
            for r in pages_rows[rg["page"]]:
                m = SUB_RE.match(r["raw"])
                if m and abs(r["x0"] - scol) <= 8.0 and rg["bbox"][1] <= r["y0"] <= rg["bbox"][3]:
                    subs.append({"label": m.group(1), "page": rg["page"], "bbox": r["bbox"]})
        questions.append({
            "number": str(num),
            "status": "auto",
            "confidence": 0.95 if not gaps else 0.7,
            "regions": regions,
            "anchors": {"start": {"page": pi, "bbox": [r2(v) for v in bbox]}},
            "review_notes": ([f"题号 {','.join(str(g) for g in gaps)} 未识别，已跳过"]
                             if gaps else []),
            "subquestions": subs,
            "question_type": "structured",
            "total_marks": marks,
            "_text": text,
        })

    cut = {
        "schema_version": 1,
        "source": {"file": "source.pdf", "sha256": sha, "pages": n_pages,
                   "paper_ref": f"{paper_code}"},
        "page_size": {"w": r2(pw), "h": r2(ph)},
        "render": {"dpi": 200, "format": "webp", "quality": 85},
        "questions": questions,
    }

    print(f"  {sdir.name}: {n_pages} 页 → {len(questions)} 题 "
          f"(特殊页: " + ", ".join(f"{k}×{v}" for k, v in Counter(kinds).items() if k != "content") + ")")
    nums = [q["number"] for q in questions]
    missing = [q["number"] for q in questions if q["review_notes"]]
    if missing:
        print(f"    [warn] 有缺号，已跳过: {missing}（题号: {nums}）")

    if dry_run:
        for q in questions:
            print("    Q%-2s 分=%-3s regions=%d 小问=%s" % (
                q["number"], q["total_marks"], len(q["regions"]),
                ",".join(s["label"] for s in q["subquestions"]) or "-"))
        doc.close()
        return

    (sdir / "cut.json").write_text(json.dumps(
        {k: v for k, v in cut.items() if k != "questions"} |
        {"questions": [{k: v for k, v in q.items() if k != "_text"} for q in questions]},
        ensure_ascii=False, indent=1), encoding="utf-8")

    for q in questions:
        write_question_md(sdir, q, board, qualification, subject, year, session, paper_code)
    doc.close()


def write_question_md(sdir, q, board, qualification, subject, year, session, paper_code):
    n = int(q["number"])
    qid = f"{board}-{qualification}-{subject}-{year}-{session}-{paper_code}-q{n:02d}"
    fm = [
        ("id", qid),
        ("board", board),
        ("qualification", qualification),
        ("subject", subject),
        ("paper_code", paper_code.upper()),
        ("year", year),
        ("session", session),
        ("question_number", str(n)),
        ("total_marks", q["total_marks"]),
        ("question_type", q["question_type"]),
        ("image", f"assets/q{n:02d}.webp"),
        ("layout", f"assets/q{n:02d}.layout.json"),
        ("source", f"CIE 9709/{paper_code.upper()}, {year} {session}"),
        ("cut_status", q["status"]),
        ("language", "en"),
    ]
    lines = ["---"] + [f"{k}: {yaml_scalar(v)}" for k, v in fm] + ["---", ""]
    lines += q["_text"]
    (sdir / f"q{n:02d}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main():
    ap = argparse.ArgumentParser(description="CIE 9709 数学卷按题切割")
    ap.add_argument("session_dir")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    sdir = Path(args.session_dir).resolve()
    if not sdir.is_dir():
        sys.exit(f"目录不存在: {sdir}")
    cut_paper(sdir, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
