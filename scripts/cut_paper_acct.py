#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""cut_paper_acct.py — Edexcel IAL 会计卷（WAC11/WAC12）按题切割

为什么单独一份（不复用 cut_paper.py 的主流程）：
    会计卷的「三部分」结构与理化卷不同。理化卷是「QP + 答题册另附」两块，
    会计卷是：
      · 普通国际卷（2 块）：QP（题面 **与点线作答区逐小问交错**）+ Source Booklet（资料）；
      · 01A 中国区卷（3 块）：紧凑 QP（只有题面）+ Source Booklet（资料）+ Answer Booklet（答题区）。
    三部分要按「资料 → 题目 → 答题区」拼成一图（2026-10-19 王培琪定），
    所以 cut.json 新增 `material_regions`，`regions` 仍是题目，`answer_block` 仍是答题区。

切点靠什么（比理化卷更硬的锚）：
    每题必以 `(Total for Question N = X marks)` 收尾；同一题在卷内出现 **2 次**
    （普通卷）或 **3 次**（01A）。第 k 次出现 = 第 k 块里的终点。三处分值必须
    相同，天然可交叉校验（59 卷实测全对）。

点线压缩（WebP 单边上限 16383px 的硬约束）：
    作答点线是文本层里的 `.` 串，严格等距 28.2pt、每页 20–26 行。同一 band 内
    只保留前 `--keep-lines` 行，其余整段删除——删除区间正好落在两条点线之间，
    上下拼接后视觉无缝（渲染层会在接缝画一条浅灰虚线提示）。
    普通卷 Q1 ≈ 8 页 → 约 4 页；01A Q1 ≈ 16 页 → 约 7 页。

用法:
    python scripts/cut_paper_acct.py <session_dir>            # 切割 + 写 cut.json/qNN.md
    python scripts/cut_paper_acct.py <session_dir> --analyze  # 只打印锚点表，不落盘
    python scripts/cut_paper_acct.py <session_dir> --keep-lines 10

产出:
    cut.json / qNN.md / qNN.md 的 front-matter（image、layout 指向 assets/）

坐标系与 schema 同理化卷：PDF pt、左上原点、y 向下。
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
    BARE_SUBHEAD_RE,
    FOOTER_Y,
    LEFT_MARGIN_MAX,
    QNUM_ONLY_RE,
    QSTART_RE,
    ROMAN_TOKENS,
    SECTION_RE,
    TOTAL_RE,
    assign_words_to_lines,
    build_page_lines,
    build_regions,
    parse_bare_subhead,
    parse_subhead,
    page_content_rects,
    r2,
    sub_label,
    trim_regions_trailing_blank,
    write_question_md,
    merge_with_existing,
)

# ── 版心（会计卷实测：页框 35–560.3，内容 x 42.2–552.8） ────────────────────
L_DEF, T_DEF = 39.0, 40.0
R_DEF, B_DEF = 556.0, 788.0

# 点线压缩
KEEP_LINES = 12          # 固定压缩行数（--keep-lines 指定时才用）
MIN_ROWS_TO_COMPRESS = 15  # band 行数不超过此值不压缩（不值得）
BAND_TOP_Y = 90.0        # 新页首行 y0 < 此值且上一页尾部有点线 → 视为跨页续接
# 自适应压缩：目标「整题 pt 高度」上限。200 DPI 下 5400pt ≈ 15000px，
# 给 WebP 16383px 上限留出提示带/分隔线的余量。
TARGET_PT = 5400.0
CANDIDATE_KEEPS = (60, 48, 40, 34, 28, 24, 20, 17, 14, 12, 10, 8, 6)

BLOCK_KINDS = ["qp", "material", "answer"]
SECTION_HINT = "SECTION"


def is_dotted(ln):
    """作答点线行：整行只有 '.' 与空白，且够长（表格里的 '.' 不会这么长）。"""
    t = ln["raw"].strip()
    return len(t) >= 20 and set(t) <= set(". ")


def collect_totals(pages_lines):
    """收集 Total 锚点。

    ⚠️ 不按 FOOTER_Y 过滤：2019-june WAC12 的 Q4 Total 印在 y≈800（页脚带里），
    滤掉就少一道题、整卷只剩 5 题（2026-10-19 实测）。
    """
    occ = []
    for pi, lines in enumerate(pages_lines):
        for ln in lines:
            m = TOTAL_RE.search(ln["raw"])
            if m:
                occ.append({"page": pi, "n": int(m.group(1)), "marks": int(m.group(2)),
                            "bbox": [r2(v) for v in ln["bbox"]]})
    return occ


SRC_DIV_RE = re.compile(r"^\s*Source Booklet\s*$", re.M | re.I)
SRC_RET_RE = re.compile(r"Do not return this source booklet", re.I)
ANS_COVER_RE = re.compile(r"Please check the examination details", re.I)


def detect_blocks(pages_text, occ, n_pages):
    """分块：靠「资料册分隔页 / 答题册封面」定位，不靠 Total 的个数。

    三种卷式（59 卷实测）：
      A. 题卷在前 + 资料册在后（绝大多数，2 块）
      B. 资料册在前 + 题卷在后（2020-oct / 2021-june 共 4 卷，COVID 期排版）
      C. 01A 中国区：题卷 + 资料册 + 答题册（3 块）
    只靠 Total 数会踩两个坑：① 20 年那 4 卷 Total 是「资料在前」；② 2026-jan
    WAC12/01A 的资料册**完全不印 Total**，按次数分块会把资料册和答题册并成一块
    （实测把 27 页资料吞进一题，DPI 掉到 57）。
    """
    notes = []
    # Total 按「题号回落」分组：每块内题号 1..N 递增，跨块处 n 会变小
    seq = sorted(occ, key=lambda o: (o["page"], o["bbox"][1]))
    groups, cur, last_n = [], [], 0
    for o in seq:
        if cur and o["n"] <= last_n:
            groups.append(cur)
            cur = []
        cur.append(o)
        last_n = o["n"]
    if cur:
        groups.append(cur)

    mat_div = ans_div = None
    for i, t in enumerate(pages_text):
        if mat_div is None and (SRC_DIV_RE.search(t) or SRC_RET_RE.search(t)):
            mat_div = i
        elif mat_div is not None and ans_div is None and i > mat_div and ANS_COVER_RE.search(t):
            ans_div = i

    blocks = []
    if mat_div is None:
        # 无分隔页：退回「按 Total 分组」逐块切（老卷兜底）
        notes.append("未找到资料册分隔页，按 Total 分组切块")
        lo = 0
        for gi in range(len(groups)):
            hi = max(o["page"] for o in groups[gi])
            if gi == 0:
                kind = "qp"
            else:
                kind = "qp" if gi == 1 else f"extra{gi}"
            blocks.append({"kind": kind, "lo": lo, "hi": hi})
            lo = hi + 1
        if lo < n_pages:
            blocks[-1]["hi"] = n_pages - 1
    elif mat_div == 0:
        # 资料册在前：题卷起点 = 第一组 Total 的末页 + 1
        qp_start = (max(o["page"] for o in groups[0]) + 1) if groups else n_pages
        blocks.append({"kind": "material", "lo": 0, "hi": qp_start - 1})
        blocks.append({"kind": "qp", "lo": qp_start, "hi": n_pages - 1})
        notes.append("资料册在前、题卷在后（倒装排版）")
    else:
        blocks.append({"kind": "qp", "lo": 0, "hi": mat_div - 1})
        if ans_div is not None:
            blocks.append({"kind": "material", "lo": mat_div, "hi": ans_div - 1})
            blocks.append({"kind": "answer", "lo": ans_div, "hi": n_pages - 1})
        else:
            blocks.append({"kind": "material", "lo": mat_div, "hi": n_pages - 1})

    for b in blocks:
        b["totals"] = {}
        for o in occ:
            if b["lo"] <= o["page"] <= b["hi"] and o["n"] not in b["totals"]:
                b["totals"][o["n"]] = o
        if not b["totals"]:
            notes.append(f"{b['kind']} 块无 Total（靠下一题起点定界）")
    return blocks, notes


def find_qstart(n, lo, hi, pages_lines):
    """块内找第 n 题的题号行。返回 (line, how) 或 (None, reason)。

    会计卷题号行一律粗体（封面页的数字、页脚页码都不是），故以粗体为准；
    标题行内 'N' 与 '(a)' 之间可以是制表符，也可以是纯空格。
    """
    cands = []
    for pi in range(lo, min(hi, len(pages_lines) - 1) + 1):
        for ln in pages_lines[pi]:
            b = ln["bbox"]
            if b[1] >= FOOTER_Y or b[0] > LEFT_MARGIN_MAX:
                continue
            if not ln["bold"]:
                continue
            t = ln["raw"].strip()
            m = QSTART_RE.match(t)
            num = None
            if m:
                num = int(m.group(1))
            else:
                m2 = QNUM_ONLY_RE.match(t)
                if m2:
                    num = int(m2.group(1))
            if num == n:
                cands.append((pi, b[1], ln))
    if not cands:
        # 放宽：不要求粗体，但必须是该页首行（y0 < 150），避免抓到表格里的数字
        for pi in range(lo, min(hi, len(pages_lines) - 1) + 1):
            for ln in pages_lines[pi]:
                b = ln["bbox"]
                if b[1] >= FOOTER_Y or b[0] > LEFT_MARGIN_MAX or b[1] > 150:
                    continue
                t = ln["raw"].strip()
                m = QSTART_RE.match(t) or QNUM_ONLY_RE.match(t)
                if m and int(m.group(1)) == n:
                    cands.append((pi, b[1], ln))
        if cands:
            cands.sort(key=lambda c: (c[0], c[1]))
            return {**cands[0][2], "page": cands[0][0]}, "非粗体兜底"
        return None, "未找到题号行"
    cands.sort(key=lambda c: (c[0], c[1]))
    return {**cands[0][2], "page": cands[0][0]}, "bold"


def find_question_starts(blocks, n_list, pages_lines):
    """每块、每题一个起点行。返回 {(块序号, n): line} 与 notes。"""
    notes = []
    starts = {}
    for bi, b in enumerate(blocks):
        for n in n_list:
            ln, how = find_qstart(n, b["lo"], b["hi"], pages_lines)
            if ln is None:
                # 兜底：该页文本层把题号弄丢了（2023-jan/june WAC12 的 Q4 题号
                # 只剩一个 '\t' span，文本层里没有 "4"）→ 取上一题 Total 之后
                # 的第一条内容行当起点。
                pt = b["totals"].get(n - 1)
                if pt is not None:
                    for pi in range(pt["page"], b["hi"] + 1):
                        hit = None
                        for l2 in pages_lines[pi]:
                            lb = l2["bbox"]
                            if lb[1] >= FOOTER_Y or lb[0] > LEFT_MARGIN_MAX:
                                continue
                            if pi == pt["page"] and lb[1] <= pt["bbox"][3]:
                                continue
                            hit = l2
                            break
                        if hit is not None:
                            ln = {**hit, "page": pi}
                            how = "上一题 Total 之后补位"
                            break
                elif n == n_list[0]:
                    # 块内第一题：跳过分隔封面，取下一首条内容行
                    for pi in range(b["lo"] + 1, b["hi"] + 1):
                        hit = None
                        for l2 in pages_lines[pi]:
                            lb = l2["bbox"]
                            if lb[1] >= FOOTER_Y or lb[0] > LEFT_MARGIN_MAX:
                                continue
                            hit = l2
                            break
                        if hit is not None:
                            ln = {**hit, "page": pi}
                            how = "块首补位"
                            break
            if ln is None:
                notes.append(f"{b['kind']}块 Q{n}: 未找到题号行")
                continue
            if how != "bold":
                notes.append(f"{b['kind']}块 Q{n}: {how}")
            starts[(bi, n)] = ln
        seq = [starts[(bi, n)] for n in n_list if (bi, n) in starts]
        for a, bb in zip(seq, seq[1:]):
            if (bb["page"], bb["bbox"][1]) <= (a["page"], a["bbox"][1]):
                notes.append(f"{b['kind']}块: 题号起点不递增（Q 顺序异常）")
    return starts, notes


def dotted_rows_by_page(pages_lines, pages):
    out = {}
    for pi in pages:
        rows = [{**ln, "page": pi} for ln in pages_lines[pi] if is_dotted(ln)]
        rows.sort(key=lambda l: l["bbox"][1])
        if rows:
            out[pi] = rows
    return out


def _split_runs(rows):
    """同页内的点线行按行距切成若干段（中间夹题面行则断开）。"""
    if not rows:
        return []
    gaps = [b["bbox"][1] - a["bbox"][1] for a, b in zip(rows, rows[1:])]
    gap_med = sorted(gaps)[len(gaps) // 2] if gaps else 28.2
    lim = max(gap_med * 1.6, 40.0)
    runs, run = [], [rows[0]]
    for a, b in zip(rows, rows[1:]):
        if b["bbox"][1] - a["bbox"][1] > lim:
            runs.append(run)
            run = [b]
        else:
            run.append(b)
    runs.append(run)
    return runs


def build_bands(rows_by_page, pages):
    """把点线行按「跨页续接」归成 band。返回 [[row, …], …]（文档序）。

    ⚠️ 同页内的点线**必须按行距连续**才算同一 band：中间夹着 "(ii) …" 之类的
    题面行时，行距会翻倍（28.2 → 56.4pt），此时必须断开——否则压缩会把
    题面行一起删掉（2026-10-19 在 2017-jan Q1 上发现的隐患）。
    """
    bands = []
    last_page = None
    for pi in pages:
        runs = _split_runs(rows_by_page.get(pi) or [])
        for idx, run in enumerate(runs):
            if (idx == 0 and bands and last_page == pi - 1
                    and run[0]["bbox"][1] < BAND_TOP_Y):
                bands[-1].extend(run)   # 续接上一页末尾的 band
            else:
                bands.append(run)
        last_page = pi
    return bands


def plan_compression(regions, pages_lines, keep):
    """算出要点线压缩的删除区间。返回 (cuts_by_page, stats)。

    删除区间只允许落在**纯点线**区域：落区间内一旦扫到非点线文本行
    （题面、分值、表格），就把区间上界收到该行之上——宁可少压，
    绝不吞内容（最后一道保险，2026-10-19）。
    """
    pages = []
    for rg in regions:
        if rg["page"] not in pages:
            pages.append(rg["page"])
    rows_by_page = dotted_rows_by_page(pages_lines, pages)
    bands = build_bands(rows_by_page, pages)

    def safe(page, y0, y1):
        for ln in pages_lines[page]:
            if is_dotted(ln):
                continue
            b = ln["bbox"]
            if b[1] >= FOOTER_Y or b[3] <= y0 or b[1] >= y1:
                continue
            y1 = min(y1, b[1] - 2)
        return (y0, y1) if y1 - y0 >= 20 else None

    cuts = {}
    n_cut = 0
    for band in bands:
        m = len(band)
        if m <= max(keep, MIN_ROWS_TO_COMPRESS) or m <= keep:
            continue
        head = band[keep - 1]
        tail = band[-1]
        cut_start = (head["bbox"][3] + band[keep]["bbox"][1]) / 2.0
        cut_end = tail["bbox"][3] + 2.0
        p0, p1 = head["page"], tail["page"]
        applied = False
        if p0 == p1:
            seg = safe(p0, r2(cut_start), r2(cut_end))
            if seg:
                cuts.setdefault(p0, []).append(seg)
                applied = True
        else:
            seg = safe(p0, r2(cut_start), B_DEF)
            if seg:
                cuts.setdefault(p0, []).append(seg)
                applied = True
            for p in pages:
                if p0 < p < p1:
                    seg = safe(p, T_DEF, B_DEF)
                    if seg:
                        cuts.setdefault(p, []).append(seg)
                        applied = True
            seg = safe(p1, T_DEF, r2(cut_end))
            if seg:
                cuts.setdefault(p1, []).append(seg)
                applied = True
        if applied:
            n_cut += 1
    return cuts, {"bands": len(bands), "cut_bands": n_cut}


def subtract(regions, cuts):
    """按删除区间切分 region。返回新的有序 region 列表（同页可出现多段）。"""
    out = []
    for rg in regions:
        p, b = rg["page"], rg["bbox"]
        pieces = [(b[1], b[3])]
        for c0, c1 in sorted(cuts.get(p, [])):
            nxt = []
            for a0, a1 in pieces:
                if c1 <= a0 + 0.5 or c0 >= a1 - 0.5:
                    nxt.append((a0, a1))
                    continue
                if c0 > a0 + 0.5:
                    nxt.append((a0, c0))
                if c1 < a1 - 0.5:
                    nxt.append((c1, a1))
            pieces = nxt
        for a0, a1 in pieces:
            if a1 - a0 < 25:      # 碎片（页尾 10pt 空白条）不值得单独成段
                continue
            out.append({"page": p, "bbox": [b[0], r2(a0), b[2], r2(a1)]})
    return out


def pt_height(regs):
    return sum(r["bbox"][3] - r["bbox"][1] for r in regs)


def compress_with(regions, pages_lines, keep):
    """按 keep 行压缩一份拷贝，返回 (新 regions, 统计)。不修改入参。"""
    if not regions or keep <= 0:
        return [dict(r) for r in regions], {"bands": 0, "cut": 0}
    cuts, st = plan_compression(regions, pages_lines, keep)
    out = subtract(regions, cuts) if cuts else [dict(r) for r in regions]
    return out, {"bands": st["bands"], "cut": st["cut_bands"]}


def collect_subquestions(regions, pages_lines, n=None):
    """QP/答题册内的标签行 → [{label,page,bbox}]。同 cut_paper 的父标签接续规则。"""
    subs = []
    parent = None
    for rg in regions:
        bb = rg["bbox"]
        for ln in pages_lines[rg["page"]]:
            lb = ln["bbox"]
            if lb[1] >= FOOTER_Y or lb[0] > LEFT_MARGIN_MAX:
                continue
            if lb[1] < bb[1] - 1 or lb[1] > bb[3] + 1:
                continue
            tokens = starred = None
            parsed = parse_subhead(ln["raw"])
            if parsed and (n is None or parsed[0] == n) and parsed[2]:
                tokens, starred = parsed[2], parsed[1]
            else:
                bare = parse_bare_subhead(ln["raw"])
                if bare and bare[1]:
                    tokens, starred = bare[1], bare[0]
            if tokens is None:
                continue
            if tokens[0] in ROMAN_TOKENS:
                if parent is not None:
                    tokens = (parent,) + tokens
            else:
                parent = tokens[0]
            ent = {"label": sub_label(tokens), "page": rg["page"],
                   "bbox": [r2(v) for v in lb]}
            if starred:
                ent["starred"] = True
            subs.append(ent)
    return subs


def section_before(start_line, qp_regions, pages_lines):
    """题号行之前最近的 SECTION 头（同一块内）。"""
    sp, sy = start_line["page"], start_line["bbox"][1]
    best = None
    for rg in qp_regions:
        if rg["page"] > sp:
            continue
        for ln in pages_lines[rg["page"]]:
            m = SECTION_RE.match(ln["raw"].strip())
            if not m:
                continue
            lb = ln["bbox"]
            if rg["page"] == sp and lb[1] > sy:
                continue
            key = (rg["page"], lb[1])
            if best is None or key > best[0]:
                best = (key, m.group(1).upper())
    return best[1] if best else None


def trim_total_line(regs, pages_lines, n):
    """把块尾的 "(Total for Question N = X marks)" 行裁掉（原地修改）。

    会计卷每一块（题目 / 资料 / 答题册）各自以同一行 Total 收尾，三段拼成
    一图后会重复出现 2–3 次（2026-10-19 看图发现）。只保留**最后一块**的
    那一行，前面的块一律裁掉，让 Total 出现在整图末尾。
    """
    if not regs:
        return False
    last = regs[-1]
    bb = last["bbox"]
    hit = None
    for ln in pages_lines[last["page"]]:
        b = ln["bbox"]
        if b[1] < bb[1] - 1 or b[3] > bb[3] + 1:
            continue
        m = TOTAL_RE.search(ln["raw"])
        if m and int(m.group(1)) == n:
            hit = b
    if hit is None:
        return False
    new_y1 = r2(hit[1] - 3)
    if new_y1 - bb[1] < 25:
        regs.pop()
    else:
        last["bbox"] = [bb[0], bb[1], bb[2], new_y1]
    return True


def top_positions(subqs):
    """答题册标签 → 顶层组的 (label, page, y) 列表。"""
    tops = []
    for s in subqs:
        top = s["label"].split()[0]
        if top in ROMAN_TOKENS:
            continue
        if not tops or tops[-1]["label"] != top:
            tops.append({"label": top, "page": s["page"], "y": s["bbox"][1]})
    return tops


def answer_regions_from_tops(tops, pieces, end_line):
    """顶层组区间 ∩ 压缩后的答题册 pieces → [{'label','regions'}]。

    必须在**压缩之后**做：作答区坐标要落在最终的 block 段内，
    否则渲染层按 bbox 在 answer_block 里找不到它（2026-10-19 踩过）。
    """
    out = []
    for i, t in enumerate(tops):
        if i + 1 < len(tops):
            ep, ey = tops[i + 1]["page"], r2(tops[i + 1]["y"] - 2)
        else:
            ep, ey = end_line["page"], r2(end_line["bbox"][3] + 2)
        y0 = r2(t["y"] - 2)
        regs = []
        for rg in pieces:
            p, b = rg["page"], rg["bbox"]
            if p < t["page"] or p > ep:
                continue
            a0, a1 = b[1], b[3]
            if p == t["page"]:
                a0 = max(a0, y0)
            if p == ep:
                a1 = min(a1, ey)
            if a1 - a0 < 3:
                continue
            regs.append({"page": p, "bbox": [b[0], r2(a0), b[2], r2(a1)]})
        if regs:
            out.append({"label": t["label"], "regions": regs})
    return out


def page_size(doc):
    r = doc[0].rect
    return {"w": r2(r.width), "h": r2(r.height)}


def read_paper_meta(sdir):
    md = sdir / "paper.md"
    meta = {}
    if md.exists():
        m = re.match(r"^---\n(.*?)\n---", md.read_text(encoding="utf-8"), re.S)
        if m:
            for line in m.group(1).split("\n"):
                km = re.match(r"^([A-Za-z_][A-Za-z0-9_]*):\s*(.*)$", line)
                if km:
                    meta[km.group(1)] = km.group(2).strip().strip('"')
    return meta


def main():
    ap = argparse.ArgumentParser(description="Edexcel IAL 会计卷按题切割")
    ap.add_argument("session_dir")
    ap.add_argument("--analyze", action="store_true", help="只打印锚点表，不落盘")
    ap.add_argument("--keep-lines", type=int, default=None,
                    help="固定保留的点线行数；缺省=自适应（取能塞进 TARGET_PT 的最大值）")
    ap.add_argument("--questions", default=None, help="只处理这些题号（逗号分隔，调试用）")
    args = ap.parse_args()

    sdir = Path(args.session_dir).resolve()
    src = sdir / "source.pdf"
    if not src.is_file():
        sys.exit(f"缺少 source.pdf: {sdir}")

    doc = pymupdf.open(src)
    pages_lines = [build_page_lines(pg) for pg in doc]
    pages_pairs = [assign_words_to_lines(pages_lines[i], doc[i].get_text("words"))
                   for i in range(doc.page_count)]
    content_cache = {}

    meta = read_paper_meta(sdir)
    # 卷号必须小写：qid 是 `...-<paper_code>-qNN`，而 schema 的 id 模式只允许小写。
    # A 卷兜底：paper.md 里常写成 `paper_code: WAC11`（漏了 A），而目录名是
    # `2026-june-wac11a` —— 不补的话 A 卷与普通卷生成同一个 qid，索引主键互撞
    # （2026-10-19 实测 36 个重复 id / 72 个文件）。目录名以 a 结尾即视为 A 卷。
    paper_code = (meta.get("paper_code") or sdir.name.split("-")[-1])
    if sdir.name.endswith("a") and not paper_code.lower().endswith("a"):
        paper_code += "a"
    paper_code = paper_code.lower()
    year = int(meta.get("year") or re.search(r"(\d{4})", sdir.name).group(1))
    session = meta.get("session") or sdir.name.split("-")[1]

    totals = collect_totals(pages_lines)
    if not totals:
        sys.exit("未识别出任何 Total 锚点")
    pages_text = [pg.get_text() for pg in doc]
    blocks, notes = detect_blocks(pages_text, totals, doc.page_count)
    n_list = sorted({o["n"] for o in totals})
    by_n = {}
    for o in totals:
        by_n.setdefault(o["n"], []).append(o)

    starts, n2 = find_question_starts(blocks, n_list, pages_lines)
    notes += n2
    qp_bi = next((i for i, b in enumerate(blocks) if b["kind"] == "qp"), 0)

    print(f"=== {sdir.name}  {doc.page_count} 页  分块 {len(blocks)} 块  题数 {len(n_list)} ===")
    print("    块: " + " / ".join(f"{b['kind']} p{b['lo'] + 1}-{b['hi'] + 1}" for b in blocks))

    # ── 分值一致性（同一题在各块的分值必须相同）──
    for n, occ in sorted(by_n.items()):
        marks = [o["marks"] for o in occ]
        if len(set(marks)) > 1:
            notes.append(f"Q{n}: 各块分值不一致 {marks}")

    questions = []
    last_section = None
    only = {int(x) for x in args.questions.split(",")} if args.questions else None
    for n in sorted(by_n):
        if only and n not in only:
            continue
        q = {"number": str(n), "status": "auto", "confidence": 0.95,
             "question_type": "structured"}
        q["total_marks"] = by_n[n][0]["marks"]
        q["anchors"] = {}

        block_regions = {}
        ans_end_anchor = None
        for bi, b in enumerate(blocks):
            kind = b["kind"]
            st = starts.get((bi, n))
            en = b["totals"].get(n)
            if st is None:
                if en is not None:
                    q["status"] = "review"
                    notes.append(f"Q{n} {kind}: 无起点，跳过该块")
                continue
            end_p, end_y = None, None
            if en is not None:
                end_p, end_y = en["page"], r2(en["bbox"][3])
            else:
                # 该块不印 Total（2026-jan WAC12/01A 的资料册）→ 用下一题起点定界
                nxt = None
                for m in n_list:
                    if m > n and (bi, m) in starts:
                        nxt = starts[(bi, m)]
                        break
                if nxt is not None:
                    end_p, end_y = nxt["page"], r2(nxt["bbox"][1] - 2)
                else:
                    end_p, end_y = b["hi"], B_DEF
            if (st["page"], st["bbox"][1]) >= (end_p, end_y):
                q["status"] = "review"
                notes.append(f"Q{n} {kind}: 起点不早于终点，跳过该块")
                continue
            regs = build_regions(st["page"], r2(st["bbox"][1] - 1),
                                 end_p, end_y, L_DEF, T_DEF, R_DEF, B_DEF)
            block_regions[kind] = regs
            q["anchors"][f"{kind}_start"] = {"page": st["page"],
                                             "bbox": [r2(v) for v in st["bbox"]]}
            q["anchors"][f"{kind}_end"] = {"page": end_p,
                                           "bbox": [0.0, 0.0, 0.0, end_y]}
            if kind == "answer":
                ans_end_anchor = q["anchors"][f"{kind}_end"]

        qp = block_regions.get("qp") or []
        mat = block_regions.get("material") or []
        ans = block_regions.get("answer") or []

        # 点线压缩（题目块 / 答题册块）
        # 自适应：不写死「保留 12 行」——12 行对 17 分的报表题明显不够看。
        # 从大到小试候选值，取「仍能塞进 WebP 单边上限」的最大保留行数，
        # 尽量给足作答空间，实在装不下才继续砍。
        keep = args.keep_lines
        qp_raw, ans_raw = qp, ans
        mat_pt = pt_height(mat)
        candidates = ([keep] if keep and keep > 0 else []) if keep is not None \
            else list(CANDIDATE_KEEPS)
        chosen = None
        stat = {}
        for k in candidates:
            qp_try, st1 = compress_with(qp_raw, pages_lines, k)
            ans_try, st2 = compress_with(ans_raw, pages_lines, k)
            if mat_pt + pt_height(qp_try) + pt_height(ans_try) <= TARGET_PT:
                chosen, qp, ans = k, qp_try, ans_try
                stat = {"qp": st1, "answer": st2}
                break
        if chosen is None and candidates:
            chosen = candidates[-1]
            qp, st1 = compress_with(qp_raw, pages_lines, chosen)
            ans, st2 = compress_with(ans_raw, pages_lines, chosen)
            stat = {"qp": st1, "answer": st2}
            notes.append(f"Q{n}: 压到 {chosen} 行仍超目标高度 {TARGET_PT}pt")
        if chosen:
            if qp_raw:
                trim_regions_trailing_blank(doc, qp, pages_lines, content_cache)
            if ans_raw:
                trim_regions_trailing_blank(doc, ans, pages_lines, content_cache)
            for kind, s in stat.items():
                if s.get("cut"):
                    q["anchors"][f"{kind}_compressed"] = {
                        "bands": s["bands"], "cut": s["cut"], "keep": chosen}

        # 资料块不压缩，只裁页尾排版空白
        if mat:
            trim_regions_trailing_blank(doc, mat, pages_lines, content_cache)

        # 三段各带一行 "(Total for Question N = X marks)"，只保留最后一块的那行
        if mat:
            trim_total_line(mat, pages_lines, n)
        if ans and qp:
            trim_total_line(qp, pages_lines, n)

        q["regions"] = qp
        if mat:
            q["material_regions"] = mat
        if ans:
            q["answer_block"] = ans

        subs = collect_subquestions(qp, pages_lines)
        if subs:
            q["subquestions"] = subs

        if ans:
            # 标签位置在**压缩前**的原始答题册上取（压缩只删点线，不改标签 y）
            tops = top_positions(collect_subquestions(ans_raw, pages_lines))
            ar = answer_regions_from_tops(tops, ans, ans_end_anchor)
            # 作答区标签对齐 QP：QP 写 "(a)(i)" 连排时没有裸 "a"，只有 "a i"。
            # 裸字母找不到就换成「首个以其为前缀的 QP 标签」——render_subsplit
            # 按 label 首 token 归组，仍会归到 a，不影响切分。
            qp_labels = [s["label"] for s in (q.get("subquestions") or [])]
            fixed = []
            for a in ar:
                if a["label"] in qp_labels:
                    fixed.append(a)
                    continue
                cand = next((l for l in qp_labels if l.split()[0] == a["label"]), None)
                if cand:
                    fixed.append({**a, "label": cand})
                else:
                    notes.append(f"Q{n}: 作答区标签 {a['label']} 在 QP 中不存在，已丢弃")
            ar = fixed
            if ar:
                q["answer_regions"] = ar

        if "qp_start" in q["anchors"]:
            st_line = starts.get((qp_bi, n))
            sec = section_before(st_line, qp, pages_lines) if st_line else None
            if sec:
                last_section = sec
            elif last_section:
                sec = last_section
            if sec:
                q["section"] = sec

        questions.append(q)

        if args.analyze:
            spans = []
            for kind in ("material", "qp", "answer"):
                regs = block_regions.get(kind)
                if not regs:
                    continue
                spans.append(f"{kind} p{regs[0]['page'] + 1}-{regs[-1]['page'] + 1}"
                             f"({len(regs)}r)")
            extra = ""
            if "qp_compressed" in q["anchors"]:
                c = q["anchors"]["qp_compressed"]
                extra += f" qp压缩{c['bands']}带/{c['cut']}处"
            if "answer_compressed" in q["anchors"]:
                c = q["anchors"]["answer_compressed"]
                extra += f" ans压缩{c['bands']}带/{c['cut']}处"
            print(f"  Q{n} {q['total_marks']:>3}分 sec={q.get('section', '-')} "
                  f"{' | '.join(spans)}{extra}"
                  f" sub={len(q.get('subquestions', []))} ansr={len(q.get('answer_regions', []))}")

    if args.analyze:
        if notes:
            print("\n[提示]")
            for x in notes:
                print("  -", x)
        print(f"\n共 {len(questions)} 题（analyze 模式，未落盘）")
        return

    # ── 落盘 cut.json ──
    sha = hashlib.sha256(src.read_bytes()).hexdigest()
    cut_path = sdir / "cut.json"
    existing = None
    if cut_path.exists():
        try:
            existing = json.loads(cut_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            existing = None

    payload = list(questions)
    if existing and existing.get("source", {}).get("sha256") != sha:
        print("  [warn] source.pdf 与既有 cut.json 的 sha256 不符，丢弃旧坐标重新生成")
        existing = None
    merged, report = merge_with_existing(payload, existing)
    # 内部字段（_options 等）不落盘
    for q in merged:
        for k in [k for k in q if k.startswith("_")]:
            q.pop(k, None)

    out = {
        "schema_version": 1,
        "source": {"file": "source.pdf", "sha256": sha, "pages": doc.page_count,
                   "paper_ref": meta.get("paper_ref") or ""},
        "page_size": page_size(doc),
        "render": {"dpi": 200, "format": "webp", "quality": 85},
        "blocks": [b["kind"] for b in blocks if b["kind"] in ("qp", "material", "answer")],
        "questions": merged,
    }
    cut_path.write_text(json.dumps(out, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")

    # ── 写 qNN.md ──
    for q in merged:
        write_question_md(sdir, int(q["number"]), {**q, "status": q.get("status", "auto")},
                          "edexcel", "a-level", "accounting", year, session,
                          paper_code, pages_lines, pages_pairs)

    print(f"\ncut.json 已写入（{len(merged)} 题）")
    for line in report:
        print("  ·", line)
    if notes:
        print("[提示]")
        for x in notes:
            print("  -", x)


if __name__ == "__main__":
    main()
