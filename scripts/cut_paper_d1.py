#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""cut_paper_d1.py — Edexcel IAL Decision Mathematics D1（WDM11）切割

⚠️ 与 cut_paper_math.py（WMA/WFM/WST/WME，已做 265 卷）的根本差别：
    那套卷的作答线**印在题面里**，规则是「只切题干、剔掉作答区」。
    D1 反过来：一份 PDF = **QP 题面册 + D1 Answer Book 作答册** 两册，
    作答区根本不在题面里。所以这里按会计/理化那套「题面 + 作答册拼接」做：
        regions       = 题面（QP 段，整段保留）
        answer_block  = 作答册（AB 段，该题区间，压缩后）
    渲染层（render_questions.py）按 material → regions → answer_block 纵向拼接，
    交界画灰线 + 顶部提示带，**无需改动**。

卷面结构（17 卷实测）：
    · 15 卷 QP 在前（QP 封面 p0，AB 封面 p8/p12）；**2 卷 AB 在前**（2022-jan、
      2023-jan，AB 封面 p0、QP 封面 p20）——按两个封面页的先后自动判定；
    · AB 段每题中位 3 页、65 行横线（全 17 卷合计 8128 行）；
    · 页面 595×842（15 卷）、539×785（2026-jan-01A）。

作答册的三类内容（王培琪 2026-10-19 定：**保留答题区，横线最多留一部分**）：
    ① 表格（距离矩阵 / 优先表 / 迭代表 / 单纯形表）→ 整份保留；
    ② 图（预印网络图、LP 坐标网格、顶点位置图）→ 整份保留；
    ③ 整行横线（作答空间）→ 按分值给配额，只留前面一部分；
    ④ 纯空白作图页（只有边框，让学生画 activity network）→ 保留半页高度。

🔴 **作答册每页都重印同一份表格/图**（D1 的排版习惯），不处理会让一题里出现
   3 份同样的表。这里按「页内非横线文本指纹」去重：重复页只保留横线。

用法:
    python scripts/cut_paper_d1.py <session_dir>              # 切割 + 写 cut.json/qNN.md
    python scripts/cut_paper_d1.py <session_dir> --analyze    # 只打印锚点表
    python scripts/cut_paper_d1.py <session_dir> --force      # 已 .done 也重切

坐标系与 cut.json schema 完全沿用 cut_paper.py（PDF pt、左上原点、y 向下）。
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

from cut_paper import (  # noqa: E402
    build_page_lines, build_regions, merge_with_existing, page_content_rects,
    r2, yaml_scalar,
)

# ── 版心（按 595×842 实测，其他页面尺寸按比例缩放） ────────────────────────
BASE_W, BASE_H = 595.28, 841.89
L_DEF, T_DEF, R_DEF, B_DEF = 39.0, 40.0, 556.0, 788.0

RULE_RE = re.compile(r"^[\s_]{8,}$")                    # 整行下划线
FILL_RE = re.compile(r"\S.*_{6,}\s*$")                  # 文字后接下划线（填空线）
TOT_N_RE = re.compile(r"Total\s+for\s+Question\s+(\d+)\s+(?:is|=)\s+(\d+)", re.I)
TOT_BARE_RE = re.compile(r"\(?\s*Total\s+(\d+)\s+marks?\s*\)?", re.I)
QHEAD_RE = re.compile(r"^(\d{1,2})[.:](?:\s|$)")
QONLY_RE = re.compile(r"^(\d{1,2})[.:]$")
CONT_RE = re.compile(r"^Question\s+(\d+)\s+continued$", re.I)
QREF_RE = re.compile(r"^Q(\d{1,2})$")
ANSWER_HDR_RE = re.compile(r"^Write\s+the\s+answer\s+to\s+Question\s+\d+", re.I)
QNAME_RE = re.compile(r"^Question\s+(\d+)$", re.I)
BLANK_RE = re.compile(r"^BLANK\s+PAGE$", re.I)
BARCODE_RE = re.compile(r"^\*?P\d{5}[A-Z]\d*\*?$")
SUB_RE = re.compile(r"^\(\s*([a-z])\s*\)")

LINEGAP = 26.0          # band 内相邻横线最大间距（pt）
LINE_H_DEF = 26.6       # 单行横线占高（实测 26.4–26.8）
KEEP_SCALE = 2.0        # 配额 = 2 × 分值
KEEP_MIN, KEEP_MAX = 8, 30
BLANK_PAGE_RATIO = 0.5  # 纯空白作图页保留比例（王培琪 2026-10-19 定：留半页）
TARGET_PT = 5400.0      # 整题高度上限（200DPI 下 15000px，给 WebP 16383 留余量）


def geom(doc, ab_pages=None):
    """版心：以**作答横线的实际跨度**为准（左右沿），上下用 A4 实测值再与页框求交。

    · 595 卷老版式（2020–2022）右侧有 "Leave blank" 竖栏，栏线在 x≈532，
      横线到 x≈529——死守 R=556 会把那条竖线和竖排字带进图右上角。
    · 2026-jan-01A 的 PDF 页面框是 539×785，但**内容坐标其实是 A4 排版**
      （下划线到 y≈766、Total 行 y≈770、页码 798 都在页面高 785 之外）。
      按页面尺寸等比缩放会把 Total 行和末几行横线整片切掉，所以上下沿不缩放。
    """
    w, h = doc[0].rect.width, doc[0].rect.height
    xs0, xs1, xt1 = [], [], []
    for pi in range(min(doc.page_count, 32)):
        for ln in build_page_lines(doc[pi]):
            t = ln["raw"].strip()
            if RULE_RE.match(ln["raw"]):
                xs0.append(ln["bbox"][0])
                xs1.append(ln["bbox"][2])
            elif t and t not in ("Leave", "blank") and "DO NOT WRITE" not in t:
                xt1.append(ln["bbox"][2])
    xt1.sort()
    right = max(xs1) if xs1 else 0.0
    if xt1:
        right = max(right, xt1[min(int(len(xt1) * 0.995), len(xt1) - 1)])
    L = r2(max(6.0, min(L_DEF, (min(xs0) - 3) if xs0 else L_DEF)))
    R = r2(min(R_DEF, right + 4) if right else R_DEF)
    # 作答册的右沿：老版式每页右侧有 "Leave blank" 竖栏框（x≈531、高近整页），
    # 栏线紧贴作答横线右端（529），死守 556 会把那条竖线和竖排字带进图。
    R_ab = R
    scan = (ab_pages or list(range(doc.page_count)))[:8]
    for pi in scan:
        for dr in doc[pi].get_drawings():
            rr = dr["rect"]
            if rr.width < 60 and rr.height > 300 and rr.x0 > 470:
                R_ab = r2(min(R_ab, rr.x0 - 2))
                break
        else:
            continue
        break
    return {"L": L, "T": T_DEF, "R": R, "R_ab": R_ab, "B": r2(min(B_DEF, h - 2)),
            "W": w, "H": h}


# ── 页面行模型 ──────────────────────────────────────────────────────────────

def page_rotate(page):
    """整页横排印刷（内容旋转 90°）→ 返回需要旋转的角度（逆时针为正），否则 0。

    D1 作答册里画网络图/网格的页是**横向排版**的：正文、Key 表、题号、"Total"
    行全是竖排文字。不转正的话 build_page_lines 会把它们全过滤掉，这一页就只剩
    页脚几个横排行——被当成空白页、还按纵向版心把右边裁掉一大块。
    """
    cnt = {}
    for b in page.get_text("dict")["blocks"]:
        if b.get("type") != 0:
            continue
        for ln in b["lines"]:
            t = "".join(s["text"] for s in ln["spans"]).strip()
            if len(t) < 2:
                continue
            d = tuple(round(v, 1) for v in ln["dir"])
            cnt[d] = cnt.get(d, 0) + 1
    horiz = cnt.pop((1.0, 0.0), 0)
    if not cnt:
        return 0
    d, n = max(cnt.items(), key=lambda kv: kv[1])
    if n < 5 or n <= horiz:
        return 0
    # dir 是文字基线方向（PDF 坐标，y 向下）。d=(0,-1)=文字朝上 → 图像要顺时针转
    # 90°（PIL 的 rotate 逆时针为正，故取 -90）才能读正；d=(0,1) 反之。
    if d == (0.0, -1.0):
        return -90
    if d == (0.0, 1.0):
        return 90
    return 0


def rows_of(doc, pi, G, cache):
    """该页的有效文本行（剔页眉页脚/条码/页码/竖排说明/题号角标）。"""
    key = ("rows", pi)
    if key in cache:
        return cache[key]
    out = []
    for ln in build_page_lines(doc[pi]):
        b = ln["bbox"]
        t = ln["raw"].strip()
        if not t:
            continue
        if b[3] <= G["T"] - 6 or b[1] >= G["B"]:
            continue
        if BARCODE_RE.match(t) or re.fullmatch(r"\d{1,3}", t) and b[0] > G["R"] - 60:
            continue
        if "DO NOT WRITE" in t or t in ("Leave", "blank") or t.startswith("Turn over"):
            continue
        if BLANK_RE.match(t):
            continue
        out.append(ln)
    cache[key] = out
    return out


def is_noise_text(t):
    """md 正文/指纹都该剔除的行。"""
    return (not t or BARCODE_RE.match(t) or "DO NOT WRITE" in t
            or t in ("Leave", "blank") or t.startswith("Turn over")
            or BLANK_RE.match(t) or CONT_RE.match(t) or QREF_RE.match(t))


def is_rule(ln):
    return bool(RULE_RE.match(ln["raw"]))


def marks_of(ln):
    """该行给出的小题分值（Total 行）→ int 或 None。"""
    m = TOT_N_RE.search(ln["raw"])
    if m:
        return int(m.group(2))
    m = TOT_BARE_RE.match(ln["raw"].strip())
    if m:
        return int(m.group(1))
    return None


def total_marks_of(ln):
    m = TOT_N_RE.search(ln["raw"])
    if m:
        return int(m.group(2))
    m = TOT_BARE_RE.match(ln["raw"].strip())
    if m:
        return int(m.group(1))
    return None


def content_bottom(doc, pi, G, cache, y_hi):
    """页内正文/图形的最低边（用作段落终点，避免把页脚带进来）。"""
    bot = y_hi
    for y1, y0, x0, x1, _hl in page_content_rects(doc, pi, cache):
        if y0 >= G["B"] or y1 <= G["T"]:
            continue
        if (x1 - x0) > 420 and y0 > G["B"] - 40:      # 页底通栏装饰线
            continue
        bot = max(bot, min(y1, y_hi))
    return min(bot, G["B"])


# ── 分册 ────────────────────────────────────────────────────────────────────

def find_parts(doc):
    """返回 (qp_pages, ab_pages, ab_before)。

    QP 封面：含 "Instructions"（国际卷）或 "Question Paper"（01A）；
    作答册封面：含 "Candidate surname" + "Total Marks"。
    """
    n = doc.page_count
    qp_c = ab_c = None
    for pi in range(n):
        t = doc[pi].get_text()
        if "Candidate surname" in t and "Total Marks" in t:
            if ab_c is None:
                ab_c = pi
        elif ("Instructions" in t or "Question Paper" in t) and "Candidate surname" not in t:
            if qp_c is None:
                qp_c = pi
    if ab_c is None:
        return None
    ab_before = qp_c is not None and ab_c < qp_c
    if ab_before:
        ab_pages = list(range(ab_c + 1, qp_c))
        qp_pages = [pi for pi in range(qp_c + 1, n)
                    if not BLANK_RE.search(doc[pi].get_text().strip()[:40])]
    else:
        qp_pages = list(range((qp_c or 0) + 1, ab_c))
        ab_pages = list(range(ab_c + 1, n))
    return qp_pages, ab_pages, ab_before


# ── QP 侧：题号 / 分值 / 小问 ───────────────────────────────────────────────

def qp_questions(doc, qp_pages, G, cache):
    """QP 段每题：[{n, page, y0, marks, subs}]（subs 在区间确定后单独收集）。

    题号与 Total 行**按文档序单遍处理**：Total 归属"当前最后一题"，与它是否
    跨页无关——2021-jan 卷 Q5/Q6 的 Total 就落在下一页，按页归属会丢掉分值。
    """
    starts = []
    for pi in qp_pages:
        for ln in rows_of(doc, pi, G, cache):
            b = ln["bbox"]
            t = ln["raw"].strip()
            m = QHEAD_RE.match(t) or QONLY_RE.match(t)
            if m and b[0] <= 95:
                n = int(m.group(1))
                if starts and starts[-1]["n"] == n and starts[-1]["page"] == pi:
                    continue
                if starts and starts[-1]["n"] == n:
                    continue
                starts.append({"n": n, "page": pi, "y0": b[1], "marks": None, "subs": []})
                continue
            if starts:
                mk = marks_of(ln)
                if mk and starts[-1]["marks"] is None:
                    starts[-1]["marks"] = mk
    return starts


def collect_subs(regs, pages_lines):
    """从本题 QP 段里收小问标签 (a)/(b)/…（只在本题区间内）。"""
    subs = []
    for rg in regs:
        bb = rg["bbox"]
        for ln in pages_lines[rg["page"]]:
            lb = ln["bbox"]
            if not (bb[1] - 1 <= (lb[1] + lb[3]) / 2 <= bb[3] + 1):
                continue
            m = SUB_RE.match(ln["raw"].strip())
            if m and lb[0] < 120:
                subs.append({"label": m.group(1), "page": rg["page"],
                             "bbox": [r2(v) for v in lb]})
    return subs


def drop_trailing_rule(doc, pi, y_end, G, cache):
    """题末 Total 行下面紧贴着一条通栏装饰线（x 42–553、厚 0.5pt）——不是题面内容，剔掉。

    它落在 Total 行 bbox 内部（文字 + 下划线同一个 line 对象），只按 y1 裁会连它一起带进图。
    """
    for y1_, y0_, x0_, x1_, _hl in page_content_rects(doc, pi, cache):
        if (x1_ - x0_) > 300 and (y1_ - y0_) < 3 and y_end - 22 <= y0_ <= y_end:
            # build_regions 会在终点 +2pt，这里多减一点才能把线挡在框外
            return min(y_end, y0_ - 3.5)
    return y_end


def qp_region_of(doc, st, nxt, qp_pages, G, cache):
    """一题的 QP 段 regions（到 Total 行或下一题起点为止）。"""
    sp = st["page"]
    sy = max(st["y0"] - 4, G["T"] - 4)
    # 终点：本题自己的 Total
    ep, ey = None, None
    for pi in range(sp, (nxt["page"] if nxt else qp_pages[-1]) + 1):
        if pi not in qp_pages:
            break
        for ln in rows_of(doc, pi, G, cache):
            if pi == sp and ln["bbox"][3] <= st["y0"]:
                continue
            m = marks_of(ln)
            if not m:
                continue
            if TOT_N_RE.search(ln["raw"]):
                qn = int(TOT_N_RE.search(ln["raw"]).group(1))
                if qn != st["n"]:
                    continue
            ep, ey = pi, ln["bbox"][3] + 5
            break
        if ep is not None:
            break
    if ep is None:
        if nxt:
            if nxt["page"] == sp:
                ep, ey = sp, nxt["y0"] - 4
            else:
                ep = nxt["page"] - 1
                ey = content_bottom(doc, ep, G, cache, G["B"])
        else:
            ep = qp_pages[-1]
            ey = content_bottom(doc, ep, G, cache, G["B"])
    ey = drop_trailing_rule(doc, ep, ey, G, cache)
    regs = build_regions(sp, sy, ep, ey, G["L"], G["T"], G["R"], G["B"])
    # 逐段收紧：页底不超过最后一行内容
    return [r for r in regs if r["bbox"][3] - r["bbox"][1] > 4]


# ── AB 侧：题区间 ───────────────────────────────────────────────────────────

def ab_qstarts(doc, ab_pages, G, cache):
    """作答册题号锚点：{n: (page, y0)}。四级锚，取最早的可用信号。"""
    hits = {}
    for pi in ab_pages:
        for ln in rows_of(doc, pi, G, cache):
            b = ln["bbox"]
            t = ln["raw"].strip()
            m = QONLY_RE.match(t) or QHEAD_RE.match(t)
            if m and b[0] < 95 and len(t) <= 4:
                n = int(m.group(1))
                hits.setdefault(n, (pi, b[1] - 6))
            elif ANSWER_HDR_RE.match(t) or QNAME_RE.match(t):
                n = int(re.search(r"Question\s+(\d+)", t).group(1))
                hits.setdefault(n, (pi, b[1] - 6))
            else:
                m = CONT_RE.match(t)
                if m:
                    n = int(m.group(1))
                    hits.setdefault(n, (pi, b[1] - 6))
                elif QREF_RE.match(t) and b[0] > G["R"] - 80:
                    n = int(QREF_RE.match(t).group(1))
                    hits.setdefault(n, (pi, b[1] - 6))
    return hits


def ab_totals(doc, ab_pages, G, cache):
    """作答册所有 Total 行：[(page, y0, y1, marks)]（文档序）。"""
    out = []
    for pi in ab_pages:
        for ln in rows_of(doc, pi, G, cache):
            m = total_marks_of(ln)
            if m:
                out.append((pi, ln["bbox"][1], ln["bbox"][3], m))
    return out


def ab_ranges(doc, ab_pages, G, cache, qp_qs):
    """每题在作答册里的区间 {n: {"start":(p,y), "end":(p,y), "marks":m}}。

    终点靠 Total 行的**分值序列对齐**（最硬：QP 与 AB 的分值序列必须逐项相等）；
    起点靠题号锚点，但**锚点可能滞后**——D1 里常有一页整页作答图（网络图/网格）
    压在题号之前，那页没有横排题号（题号印成竖排或根本在下一页），只按锚点会
    把这张图漏掉。所以起点取「上一题结束位置」与「题号锚点」中**更早**的那个，
    但若锚点比上一题结束晚了一整页以上，说明中间是图页，仍从上一题结束处起。
    """
    totals = ab_totals(doc, ab_pages, G, cache)
    hits = ab_qstarts(doc, ab_pages, G, cache)

    # ① 按分值顺序贪心配 Total（允许个别题缺 Total 行）
    ends, pos = {}, 0
    for q in qp_qs:
        for j in range(pos, len(totals)):
            if totals[j][3] == q["marks"]:
                ends[q["n"]] = totals[j]
                pos = j + 1
                break
    aligned = (len(ends) == len(qp_qs) and pos == len(totals))

    # ② 缺 Total 的题：终点取下一题锚点的上方（D1 有些题的 Total 行整行印成了图形）
    for i, q in enumerate(qp_qs):
        if q["n"] in ends:
            continue
        nx = qp_qs[i + 1] if i + 1 < len(qp_qs) else None
        ns = hits.get(nx["n"]) if nx else None
        if ns:
            # 缺 Total：切到下一题锚点上方。锚点若在**下一页**，直接切到该页顶，
            # 否则同页会残留下一题的 "Question N" 标题（2026-june Q4 → Q5 就栽在这）。
            if ns[1] <= G["T"] + 12:
                ends[q["n"]] = (ns[0], G["T"], G["T"], q["marks"])
            else:
                ends[q["n"]] = (ns[0], ns[1] - 24, ns[1] - 12, q["marks"])
        else:
            ends[q["n"]] = (ab_pages[-1], G["B"] - 16, G["B"] - 2, q["marks"])

    rot_pages = {pi for pi in ab_pages if page_rotate(doc[pi])}
    out = {}
    prev_end = (ab_pages[0], G["T"])
    for i, q in enumerate(qp_qs):
        n = q["n"]
        anchor = hits.get(n)
        if anchor is None or (anchor[0], anchor[1]) <= prev_end:
            start = prev_end
        elif anchor[0] > prev_end[0] + 1:
            start = prev_end          # 锚点滞后一页以上：中间是图页，从上一题结束处起
        elif anchor[0] == prev_end[0] and anchor[1] - prev_end[1] > 220:
            start = prev_end          # 同页但隔了一大块空白/图形
        else:
            start = anchor
        # 起点落在上一页页底（Total 之后已无内容）→ 推到下一页顶
        if start[0] + 1 <= ab_pages[-1] and \
                content_bottom(doc, start[0], G, cache, G["B"]) <= start[1] + 2:
            start = (start[0] + 1, G["T"])

        if start[0] in rot_pages:          # 旋转页整页归本题，起点贴页顶
            start = (start[0], G["T"])
        t = ends[n]
        if t[0] in rot_pages:              # 旋转页整页保留，终点贴页底
            t = (t[0], G["T"], G["B"], t[3])
        if (start[0], start[1]) >= (t[0], t[2]):
            # 起点不早于终点（锚点错位）：退回上一题结束处重算
            start = prev_end
            if start[1] >= G["B"] - 40:
                start = (start[0] + 1, G["T"])
        out[n] = {"start": start, "end": (t[0], t[2] + 6), "marks": q["marks"],
                  "aligned": aligned}
        prev_end = (t[0], t[2] + 1)
    return out, aligned


# ── AB 侧：压缩 ─────────────────────────────────────────────────────────────

def page_bands(rows, y0, y1):
    """页内纯横线块：[{top, bottom, n, pitch}]（只算落在 y0..y1 内的）。"""
    lines = [ln["bbox"] for ln in rows
             if is_rule(ln) and ln["bbox"][1] >= y0 - 1 and ln["bbox"][3] <= y1 + 1]
    bands = []
    for b in lines:
        if bands and b[1] - bands[-1]["bottom"] <= LINEGAP:
            bands[-1]["bottom"] = b[3]
            bands[-1]["n"] += 1
            bands[-1]["last_y0"] = b[1]
        else:
            bands.append({"top": b[1], "bottom": b[3], "n": 1, "last_y0": b[1],
                          "first_y0": b[1]})
    for bd in bands:
        bd["pitch"] = ((bd["last_y0"] - bd["first_y0"]) / (bd["n"] - 1)
                       if bd["n"] > 1 else LINE_H_DEF)
        bd["pitch"] = min(max(bd["pitch"], 18.0), 40.0)
    return bands


def page_keys(rows):
    """页内非横线文本集合（识别"重印素材页"）。"""
    keys = set()
    for ln in rows:
        t = ln["raw"].strip()
        if is_rule(ln) or is_noise_text(t) or QONLY_RE.match(t):
            continue
        keys.add(t)
    return keys


def is_reprint(keys, seen):
    """这一页的素材是否与前面某页高度重合（D1 作答册每页都重印同一张表/图）。"""
    if len(keys) < 3:
        return False
    for s in seen:
        if not s:
            continue
        inter = len(keys & s)
        if inter >= 3 and inter / max(len(keys), len(s)) >= 0.8:
            return True
    return False


def has_drawings(doc, pi, G, cache, min_w=300, min_h=200):
    """页内是否有大图形（表格 / 网格 / 预印图）。"""
    for y1, y0, x0, x1, _hl in page_content_rects(doc, pi, cache):
        if (x1 - x0) >= min_w and (y1 - y0) >= min_h:
            return True
    return False


def ab_block(doc, q, rng, G, cache, keep_scale, kmin, kmax):
    """该题作答册的裁剪段（含去重 / 配额 / 空白作图页）。"""
    sp, sy = rng["start"]
    ep, ey = rng["end"]
    marks = rng["marks"] or 6
    budget = int(max(kmin, min(kmax, round(marks * keep_scale))))
    pages = [pi for pi in range(sp, ep + 1)]
    bands_by_page = {}
    for pi in pages:
        y0 = sy if pi == sp else G["T"]
        y1 = ey if pi == ep else G["B"]
        bands_by_page[pi] = page_bands(rows_of(doc, pi, G, cache), y0, y1)
    n_band_pages = sum(1 for pi in pages if bands_by_page[pi])
    per_page = max(4, budget // n_band_pages) if n_band_pages else budget

    seen = []
    left = budget
    segs = []
    for pi in pages:
        y0 = sy if pi == sp else G["T"]
        y1 = ey if pi == ep else G["B"]
        if y1 - y0 < 4:
            continue
        rot = page_rotate(doc[pi])
        if rot:
            # 横排页整页保留，渲染时转正。右沿收窄到 "Leave blank" 竖栏框左侧，
            # 否则那条竖线和竖排小字会跟着进图（2020-jan Q6 右侧那条）。
            right = G["R"]
            for dr in doc[pi].get_drawings():
                rr = dr["rect"]
                if rr.width < 60 and rr.height > 300 and rr.x0 > 470:
                    right = min(right, rr.x0 - 2)
            segs.append({"page": pi, "bbox": [r2(G["L"]), r2(y0), r2(right), r2(y1)],
                         "kind": "rotated", "rotate": rot})
            continue
        rows = rows_of(doc, pi, G, cache)
        keys = page_keys(rows)
        nkeys = len(keys)
        dup = is_reprint(keys, seen)
        seen.append(keys)
        bands = bands_by_page[pi]

        # 纯空白作图页：无横线、无成片文字，只有大图形 → 保留半页
        if not bands and nkeys <= 4 and has_drawings(doc, pi, G, cache):
            segs.append({"page": pi, "bbox": [r2(G["L"]), r2(y0), r2(G["R_ab"]),
                                              r2(y0 + (G["B"] - G["T"]) * BLANK_PAGE_RATIO)],
                         "kind": "blank_canvas"})
            continue

        # 续页顶部的 "Question N continued" 标题剔掉，直接从下面的横线开始
        hdr_bottom = y0
        for ln in rows:
            if CONT_RE.match(ln["raw"].strip()) and ln["bbox"][1] >= y0 - 2:
                hdr_bottom = max(hdr_bottom, ln["bbox"][3] + 3)
        cur = max(y0, hdr_bottom)
        keep_here = 0
        for bd in bands:
            top = max(bd["top"], y0)
            # 素材段：从 cur 到横线块上方（重复素材页整段丢弃）
            if not dup and top - 5 > cur:
                segs.append({"page": pi, "bbox": [r2(G["L"]), r2(cur), r2(G["R_ab"]),
                                                  r2(min(top - 5, y1))], "kind": "content"})
            take = 0
            if left > 0:
                take = min(left, per_page, bd["n"])
            if take:
                bottom = min(top + (take - 1) * bd["pitch"] + bd["pitch"], bd["bottom"], y1)
                segs.append({"page": pi, "bbox": [r2(G["L"]), r2(top - 3), r2(G["R_ab"]),
                                                  r2(bottom + 2)], "kind": "rules"})
                left -= take
                keep_here += take
            cur = max(cur, min(bd["bottom"], y1))
        if y1 - cur > 10:
            # 页尾（Total 行 / 图）照留；7pt 那种是页脚残片，丢掉。
            # 超过半页的纯空白尾巴（留给学生的作图区）压到半页，别整页留白。
            bottom = min(y1, cur + (G["B"] - G["T"]) * BLANK_PAGE_RATIO)
            segs.append({"page": pi, "bbox": [r2(G["L"]), r2(cur), r2(G["R_ab"]), r2(bottom)],
                         "kind": "tail"})
        q["_keep"] = q.get("_keep", 0) + keep_here
    return segs


# ── 落盘 ────────────────────────────────────────────────────────────────────

def md_body(regions, pages_lines):
    """题面正文（作答册内容不进正文）。"""
    out = []
    for rg in regions:
        bb = rg["bbox"]
        for ln in pages_lines[rg["page"]]:
            lb = ln["bbox"]
            if not (bb[1] <= (lb[1] + lb[3]) / 2 <= bb[3]):
                continue
            t = ln["raw"].strip()
            if is_noise_text(t) or RULE_RE.match(ln["raw"]):
                continue
            out.append(t)
    return out


def write_qmd(sdir, n, q, body, board, qual, subject, year, session,
              paper_code, paper_unit):
    uid = f"{board}-{qual}-{subject}-{year}-{session}-{paper_code}-q{n:02d}"
    fm = [("id", uid), ("board", board), ("qualification", qual),
          ("subject", subject), ("paper_code", paper_code.upper()),
          ("paper_unit", paper_unit), ("year", year), ("session", session),
          ("question_number", str(n))]
    if "total_marks" in q:
        fm.append(("total_marks", q["total_marks"]))
    fm += [("question_type", q.get("question_type", "structured")),
           ("image", f"assets/q{n:02d}.webp"),
           ("layout", f"assets/q{n:02d}.layout.json"),
           ("source", f"Pearson Edexcel IAL {paper_unit}, {year}"),
           ("cut_status", q["status"]), ("language", "en")]
    lines = ["---"] + [f"{k}: {yaml_scalar(v)}" for k, v in fm if v is not None] + ["---", ""]
    body_lines = body or [f"{n}."]
    (sdir / f"q{n:02d}.md").write_text(
        "\n".join(lines + body_lines) + "\n", encoding="utf-8")


def read_paper_meta(sdir):
    meta = {}
    pm = sdir / "paper.md"
    if pm.exists():
        t = pm.read_text(encoding="utf-8")
        for k in ("paper_unit", "paper_ref", "total_marks"):
            m = re.search(rf"^{k}:\s*(\S+)", t, re.M)
            if m:
                meta[k] = m.group(1)
    return meta


def main():
    ap = argparse.ArgumentParser(description="D1 卷切割（题面 + 作答册）")
    ap.add_argument("session_dir")
    ap.add_argument("--analyze", action="store_true", help="只打印锚点表，不落盘")
    ap.add_argument("--force", action="store_true", help="已 .done 也重切")
    ap.add_argument("--keep-scale", type=float, default=KEEP_SCALE,
                    help="横线配额 = 分值 × 此系数（默认 2）")
    ap.add_argument("--keep-min", type=int, default=KEEP_MIN)
    ap.add_argument("--keep-max", type=int, default=KEEP_MAX)
    args = ap.parse_args()

    sdir = Path(args.session_dir).resolve()
    if not sdir.is_dir():
        sys.exit(f"目录不存在: {sdir}")
    if (sdir / ".done").exists() and not args.force:
        print(f"SKIP {sdir.name}: 已验收（--force 才重切）")
        return
    src = sdir / "source.pdf"
    if not src.exists():
        sys.exit(f"缺少 source.pdf: {src}")
    board = sdir.parents[2].name
    qual = sdir.parents[1].name
    subject = sdir.parents[0].name
    try:
        year_s, session, paper_code = sdir.name.split("-", 2)
        year = int(year_s)
    except Exception:
        sys.exit(f"session 目录名不合约定: {sdir.name}")

    doc = pymupdf.open(src)
    parts = find_parts(doc)
    if not parts:
        sys.exit("找不到作答册封面（Candidate surname + Total Marks）")
    qp_pages, ab_pages, ab_before = parts
    G = geom(doc, ab_pages)
    cache = {}
    pages_lines = [build_page_lines(p) for p in doc]

    qp_qs = qp_questions(doc, qp_pages, G, cache)
    if not qp_qs:
        sys.exit("QP 段未识别到任何题号")
    ranges, aligned = ab_ranges(doc, ab_pages, G, cache, qp_qs)

    meta = read_paper_meta(sdir)
    paper_unit = meta.get("paper_unit") or f"{paper_code.upper()}/01"
    paper_marks = int(meta["total_marks"]) if meta.get("total_marks") else None

    questions, notes = [], []
    for i, st in enumerate(qp_qs):
        nxt = qp_qs[i + 1] if i + 1 < len(qp_qs) else None
        q = {"number": str(st["n"]), "status": "auto", "confidence": 0.0,
             "question_type": "structured", "total_marks": st["marks"],
             "anchors": {}, "regions": [], "answer_block": [], "subquestions": []}
        n = st["n"]
        regs = qp_region_of(doc, st, nxt, qp_pages, G, cache)
        q["regions"] = regs
        q["subquestions"] = collect_subs(regs, pages_lines)
        q["anchors"]["start"] = {"page": st["page"],
                                 "bbox": [r2(v) for v in
                                          next(ln["bbox"] for ln in rows_of(doc, st["page"], G, cache)
                                               if abs(ln["bbox"][1] - st["y0"]) < 0.01)]}
        if regs:
            last = regs[-1]
            q["anchors"]["end"] = {"page": last["page"],
                                   "bbox": [0.0, 0.0, 0.0, last["bbox"][3]]}
        rng = ranges.get(n)
        if rng:
            q["anchors"]["answer_start"] = {"page": rng["start"][0],
                                            "bbox": [0.0, r2(rng["start"][1]), 0.0, 0.0]}
            q["anchors"]["answer_end"] = {"page": rng["end"][0],
                                          "bbox": [0.0, 0.0, 0.0, r2(rng["end"][1])]}
            q["answer_block"] = ab_block(doc, q, rng, G, cache,
                                         args.keep_scale, args.keep_min, args.keep_max)
            q["anchors"]["answer_compressed"] = {
                "kept_lines": q.pop("_keep", 0),
                "budget": int(max(args.keep_min, min(args.keep_max,
                                                    round((rng["marks"] or 6) * args.keep_scale)))),
                "aligned_by_marks": bool(rng["aligned"]),
            }
        else:
            q["status"] = "review"
            notes.append(f"Q{n}: 作答册里找不到该题区间")
        if not q["answer_block"]:
            q["status"] = "review" if q["status"] == "auto" else q["status"]
            notes.append(f"Q{n}: 作答区为空")
        # 置信度 / 状态
        conf = 0.5
        conf += 0.25 if regs else 0
        conf += 0.2 if q["answer_block"] else 0
        conf += 0.05 if q["subquestions"] else 0
        q["confidence"] = round(min(conf, 1.0), 2)
        if q["confidence"] < 0.95:
            q["status"] = "review"
        if st["marks"] is None:
            q["status"] = "review"
            notes.append(f"Q{n}: 题面缺 Total 行，分值未知")
        q["total_marks"] = st["marks"] or 0
        questions.append(q)

    total = sum(q["total_marks"] for q in questions)
    if paper_marks and total != paper_marks:
        for q in questions:
            q["status"] = "review"
        notes.append(f"全卷分值合计 {total} ≠ 试卷总分 {paper_marks}")
    if not aligned:
        notes.append("作答册 Total 序列与 QP 分值序列不一致，已退回题号锚点")
    # 作答册页覆盖连续性：有页没被任何题认领 = 漏切
    covered = {r["page"] for q in questions for r in q["answer_block"]}
    missing = [p for p in ab_pages if p not in covered]
    if missing:
        notes.append(f"作答册有 {len(missing)} 页没被任何题覆盖：p"
                     + ", p".join(str(p) for p in missing))

    if args.analyze:
        print(f"{sdir.name}  {doc.page_count}页 {G['W']:.0f}x{G['H']:.0f}  "
              f"{'AB在前' if ab_before else 'QP在前'}  QP p{qp_pages[0]}..{qp_pages[-1]} "
              f"AB p{ab_pages[0]}..{ab_pages[-1]}")
        for q in questions:
            ab = q["answer_block"]
            h = sum(r["bbox"][3] - r["bbox"][1] for r in q["regions"] + ab)
            ap = f"p{ab[0]['page']}..{ab[-1]['page']}" if ab else "-"
            print(f"  Q{q['number']:>2} {q['total_marks']:>3}分 "
                  f"QP p{q['regions'][0]['page'] if q['regions'] else '-'} "
                  f"AB {ap:<9} 段{len(ab):>2} "
                  f"线{q['anchors'].get('answer_compressed', {}).get('kept_lines', 0):>3} "
                  f"总高{h:6.0f}pt ({h * 200 / 72:5.0f}px) sub={len(q['subquestions'])} "
                  f"{q['status']}")
        if notes:
            print("[提示]")
            for x in notes:
                print("  -", x)
        return

    # ── 落盘 ──
    sha = hashlib.sha256(src.read_bytes()).hexdigest()
    existing = None
    cut_path = sdir / "cut.json"
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

    out = {
        "schema_version": 1,
        "source": {"file": "source.pdf", "sha256": sha, "pages": doc.page_count,
                   "paper_ref": meta.get("paper_ref") or ""},
        "page_size": {"w": r2(G["W"]), "h": r2(G["H"])},
        "render": {"dpi": 200, "format": "webp", "quality": 85},
        "blocks": ["qp", "answer"],
        "questions": merged,
    }
    cut_path.write_text(json.dumps(out, ensure_ascii=False, indent=1) + "\n",
                        encoding="utf-8")

    for q in merged:
        n = int(q["number"])
        write_qmd(sdir, n, q, md_body(q["regions"], pages_lines), board, qual,
                  subject, year, session, paper_code, paper_unit)

    print(f"OK  {sdir.name}: {len(merged)} 题 | 分值 {total}"
          + (f"/{paper_marks}" if paper_marks else "")
          + f" | 作答册 {'序列对齐' if aligned else '锚点对齐'}")
    rev = [q["number"] for q in merged if q["status"] != "auto"]
    if rev:
        print(f"    review: Q{', Q'.join(rev)}")
    for line in report:
        print("  ·", line)
    for x in notes:
        print("  -", x)


if __name__ == "__main__":
    main()
