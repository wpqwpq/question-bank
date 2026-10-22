#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""cut_paper_cie_busi.py — CIE 9609 商务卷按题自动切割

⚠️ 专用脚本（考试局 × 资格 × 学科各一份，2026-10-21 定规）：CIE / A Level / Business(9609)。
与 cut_paper_cie_acct.py（会计）/ cut_paper_cie_eco.py（经济）不共享代码、不互相 import。

CIE 9609 四种卷型（2026-10-22 实测 135 份 QP）：
  · P1 Business Concepts 1：4 页，Section A（全做）+ Section B（EITHER/OR 二选一），
    **无点线作答区**、无资料，题面挤在 p2。
  · P2 Business Concepts 2：4–8 页，**2 题，每题自带 case**（题号与 case 标题同行、印在页顶
    x≈49.6），无点线，答在另发答题册。
  · P3 Business Decision-Making（旧名 Case Study）：12 页（2023+，全做）/ 20 页（2020–2022，
    Section B 选一题）；题面极短、**满页点线作答区**（每行 154 个 '.'，行距 26pt）；
    案例在**独立 INSERT 材料册**（4 页 / 旧版 8 页），全卷共用一份。
  · P4 Business Strategy：4 页，2 题，case + Appendix 1–4 都在 QP 内（p2–p3），无点线。

切割口径（王培琪 2026-10-22 定）：
  1. 题图 = **资料在前、题面在后**（cut.json 写 material_order="before"）；
  2. **点线作答区全部裁掉**——页内按「连续点线块」切成若干带，只留带内的正文；
     同页多段之间的空隙由 render_questions 画浅灰虚线接缝；
  3. Section 字头 / `Answer all questions` 之类指引句丢弃；EITHER / OR 视为断点；
  4. 表格框线、图表、页码线号随带外扩，不能削掉底框；
  5. 分值 [n] 原位保留；小问锚点只记录不产子图（CIE 在云端子图黑名单里）；
  6. 资料归属：P3 = INSERT 全册（封面 / BLANK PAGE / 版权页除外）拼在**每题**之前；
     P2 = 该题自己的 case 段；P4 = 共用的 case + Appendix（拼在每题之前）；P1 无资料。

用法:
    python scripts/cut_paper_cie_busi.py <session_dir>            # 切割 + 写 cut.json/qNN.md
    python scripts/cut_paper_cie_busi.py <session_dir> --analyze  # 只打印锚点表，不落盘
    python scripts/cut_paper_cie_busi.py <session_dir> --png DIR  # 另存每题 PNG 供视觉抽检

<session_dir> 指向 data/cie/alevel/business/<year>-<session>-qpNN/，
目录内须已放入 source.pdf 与 paper.md（用 prep_cie_busi.py 铺）。
坐标系：PDF 点 pt，左上角原点，y 向下；bbox=[x0,y0,x1,y1]。
重跑保护：source.sha256 不符硬停；status=verified/manual 的题不被覆盖。
"""

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

import pymupdf

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# ── 版式常量 ────────────────────────────────────────────────────────────────
# 595×842 = 2020–2024；612×792 = 2025 起（CIE 换 Letter，整版内缩、字小一号）
PROFILES = {
    595: {"region_top": 56.0, "footer_y": 792.0, "x_left": 44.0, "x_right": 553.0},
    612: {"region_top": 60.0, "footer_y": 745.0, "x_left": 40.0, "x_right": 547.0},
}
GEOM = dict(PROFILES[595])
_CHARS = {}        # page_idx -> [(y0,x0,y1,x1,char)]，只有分值条定位用

PAD = 3.0          # 起点相对题号行的上留白
END_PAD = 6.0      # 终点相对下一锚点的下留白
QCOL_TOL = 4.0     # 题号列允许偏移
DOT_MIN = 20       # 点线行至少这么多个 '.'
DOT_GAP = 4.0      # 点线块合并容差
BAND_MIN = 8.0     # 小于这个高度的带不成图

SEC_RE = re.compile(r"^Section\s+([ABC])\b\s*(?:\(.*\))?$")
INSTR_RE = re.compile(r"^(Answer\s+(?:all|one|both|this|three|two)\b|You\s+(?:are advised|must)\b)", re.I)
EITHER_RE = re.compile(r"^(either|or)$", re.I)
SUB_RE = re.compile(r"^\(([a-h])\)")
ROMAN_RE = re.compile(r"^\((i{1,3}|iv|v|vi{1,3})\)")
QSTART_RE = re.compile(r"^(\d{1,2})(?![0-9])")
MARK_RE = re.compile(r"\[(\d{1,3})\]")
FOOT_BLOCK_RE = re.compile(r"^(Permission to reproduce|BLANK PAGE|Cambridge Assessment)", re.I)
BLANK_RE = re.compile(r"^BLANK PAGE$", re.I)
# 作答区家具（旧版 P3 的 Section B「Write the question number here: ……」）
ANSWER_HINT_RE = re.compile(r"^Write the question number here", re.I)
BARCODE_RE = re.compile(r"^\*?\s*(?:\d\s*){8,}\*?$")
FOOT_MARK_RE = re.compile(r"^(©|\[Turn over|Permission to reproduce|BLANK PAGE|Cambridge Assessment"
                          r"|\d{4}/\d{2}/|\d{4}/\d\d/[A-Z])", re.I)
PAPER_TITLE = {
    "1": "Paper 1 Business Concepts 1",
    "2": "Paper 2 Business Concepts 2",
    "3": "Paper 3 Business Decision-Making",
    "4": "Paper 4 Business Strategy",
}
ROMAN_SET = {"i", "ii", "iii", "iv", "v", "vi", "vii", "viii", "ix"}
# 资料从哪来：insert = 独立材料册；inline = QP 自带的 case 段；none = 无资料
MATERIAL_KIND = {"1": "none", "2": "inline-per-q", "3": "insert", "4": "inline-shared"}


# ── 行抽取 ──────────────────────────────────────────────────────────────────
def _decode_line(texts):
    """一行内的 span 列表 → 文本。处理 CIE 2025 起的「整段 +0x1D 位移」。"""
    raw = "".join(texts)
    if not any(ord(c) < 32 and c not in "\t" for c in raw):
        return raw
    out = []
    for i, t in enumerate(texts):
        if not any(ord(c) < 32 and c not in "\t" for c in t):
            out.append(t)
            continue
        dec = "".join(chr(ord(c) + 29) if ord(c) <= 0x5D else c for c in t)
        if len(t) == 1 and ord(t) < 32:
            prev = texts[i - 1].strip() if i else ""
            nxt = texts[i + 1].strip() if i + 1 < len(texts) else ""
            if (prev.isdigit() or nxt.isdigit()) and dec.isdigit():
                out.append(dec)
                continue
        good = sum(1 for c in dec if c.isalpha() or c.isspace())
        out.append(dec if good / max(1, len(dec)) >= 0.75 else t)
    return "".join(out)


def page_lines(page):
    out = []
    for b in page.get_text("dict")["blocks"]:
        if b.get("type") != 0:
            continue
        for ln in b["lines"]:
            spans = ln["spans"]
            text = _decode_line([sp["text"] for sp in spans]).strip()
            if not text:
                continue
            first = spans[0]
            out.append({
                "y0": round(ln["bbox"][1], 2), "x0": round(ln["bbox"][0], 2),
                "x1": round(ln["bbox"][2], 2), "y1": round(ln["bbox"][3], 2),
                "text": text,
                "bold": "Bold" in first["font"] or "bold" in first["font"].lower(),
            })
    out.sort(key=lambda r: (r["y0"], r["x0"]))
    return out


def _is_junk_line(tx):
    """条形码字形行 / 控制字符行：不是正文。"""
    if not tx:
        return True
    if BARCODE_RE.match(tx.strip()):
        return True
    hi = sum(1 for c in tx if ord(c) > 127)
    ctrl = sum(1 for c in tx if ord(c) < 32 and c not in "\t\n\r")
    n = len(tx)
    return (hi / n > 0.5) or (ctrl / n > 0.2)


def is_dotted(r):
    """作答点线行：几乎整行都是 '.'（长行 154 个，缩进小问也有 8 个以上的短行）。

    ⚠️ 不能只认「长度 ≥ 20」——小问缩进后的点线只有 12 个左右，漏掉会把答题区
    当成正文带进来（2020-june-qp31 Q2 实测）。
    """
    t = r["text"].strip()
    if len(t) < 8:
        return False
    return t.count(".") / len(t) >= 0.85


# ── 版心测量 ────────────────────────────────────────────────────────────────
def setup_geom(doc, pages, qp_pages=None):
    """按页面尺寸选版式，再用实测值修正页码带 / 页脚 / 左右边界。

    qp_pages：QP 本体页数（其后是并入的 INSERT）。INSERT 的页脚比 QP 高一截
    （2020-june-qp31 的 insert 页脚 y=776.9 vs QP 796.7），混着测会把正文底界
    拉到 775，末题最后一行被砍。
    """
    qp = pages if qp_pages is None else pages[:qp_pages]
    w, h = doc[0].rect.width, doc[0].rect.height
    key = min(PROFILES, key=lambda k: abs(w - k))
    GEOM.update(PROFILES[key])
    # 页码（居中 1–2 位数）下沿。⚠️ 2025 版（612×792）题号就在 y≈59，
    # 而页码到 y≈47；若照抄 profile 里的 60，题号会被 `y0 < region_top` 整批滤掉，
    # 结果 0 题（2025-june-qp21 / qp22 / qp23 实测）。→ 按实测页码推。
    tops = []
    for pi in range(1, min(len(qp), 6)):
        for r in qp[pi]:
            if (r["text"].isdigit() and len(r["text"].strip()) <= 2
                    and r["y0"] < 90 and abs(r["x0"] - w / 2) < 80):
                tops.append(r["y1"] + 3)
    GEOM["region_top"] = round(max(40.0, max(tops) if tops else GEOM["region_top"] * 0.6), 1)
    # 页脚（© UCLES / 卷号）。⚠️ 不能宽写成 ^Camb——末页版权段落首行也是
    # "Cambridge Assessment International Education is part of…"，会把页脚上限
    # 拽到 y=769，末题下沿被砍（2020-june-qp11 实测）。
    foots = []
    for lines in qp[1:]:             # 跳过封面：封面的 © UCLES 在 y≈777
        for r in lines:
            if r["y0"] > h * 0.85 and re.match(r"^(©\s*UCLES|\d{4}/\d\d/)", r["text"]):
                foots.append(r["y0"] - 2)
    if foots:
        GEOM["footer_y"] = min(GEOM["footer_y"], round(min(foots), 1))
    # 左右边界：排除左侧竖排的 "DO NOT WRITE IN THIS MARGIN" / 条形码数字串
    # （x≈18–28、竖向拉得很长），否则 x_left 会被拽到 24
    xs0, xs1 = [], []
    for lines in qp[:8]:
        for r in lines:
            if not (GEOM["region_top"] - 2 <= r["y0"] < GEOM["footer_y"]) or is_dotted(r):
                continue
            if r["y1"] - r["y0"] > 25 and r["x1"] - r["x0"] < 40:
                continue
            xs0.append(r["x0"])
            xs1.append(r["x1"])
    if xs0:
        GEOM["x_left"] = round(max(24.0, min(xs0) - 5), 1)
        GEOM["x_right"] = round(min(w - 18, max(xs1) + 5), 1)


def _is_red(col):
    """印前核对用的红色批注（框+箭头）。CIE 偶尔漏删，如 2022-june-qp22 p2。
    这类批注在 prep 阶段已用 redaction 涂掉，这里再挡一道，防止漏网图形把边界拽歪。"""
    if col is None:
        return False
    try:
        return col[0] > 0.55 and col[1] < 0.35 and col[2] < 0.35
    except Exception:
        return False


def body_lines(pages, pi):
    """正文行：去掉页码带、页脚、页脚版权块、条形码垃圾。"""
    out = []
    for r in pages[pi]:
        if r["y0"] < GEOM["region_top"] - 1 or r["y0"] >= GEOM["footer_y"]:
            continue
        if FOOT_MARK_RE.match(r["text"]) or _is_junk_line(r["text"]) or ANSWER_HINT_RE.match(r["text"]):
            continue
        out.append(r)
    return out


def page_cap(pages, pi):
    """本页允许的收尾上限：**本页自己那条页脚**（© UCLES / 卷号）的 y0。

    ⚠️ 不能退回版式常量 792：INSERT 页正文经常排到 796.6（页脚 796.7），
    按 792 收会把最后一行削掉 4–5pt（2020-mar-qp32 的 insert 末行实测，
    audit 报 7 处 edge=792.0）。
    """
    cap = None
    for r in pages[pi]:
        if r["y0"] < GEOM["region_top"]:
            continue
        if re.match(r"^(©\s*UCLES|\d{4}/\d\d/)", r["text"]):
            cap = r["y0"] if cap is None else min(cap, r["y0"])
        elif FOOT_BLOCK_RE.match(r["text"]):
            cap = (r["y0"] - 3) if cap is None else min(cap, r["y0"] - 3)
    return round(cap if cap is not None else GEOM["footer_y"], 1)


# ── 带切分（核心：裁掉点线作答区） ──────────────────────────────────────────
def _extend_by_drawings(doc, pi, top, bot):
    """把带的上下界扩到紧贴的框线 / 图表（只按文字收会削掉表格底框）。"""
    page = doc[pi]
    H = page.rect.height
    for d in page.get_drawings():
        rc = d["rect"]
        if rc.height > H * 0.75 or rc.width > page.rect.width * 0.98:
            continue                     # 整页边框 / 通栏线不参与
        if _is_red(d.get("color")) or _is_red(d.get("fill")):
            continue                     # 红色批注框不参与（否则把材料底边拽进题目）
        if d.get("fill") and min(d["fill"]) > 0.97 and (d.get("color") is None or min(d["color"]) > 0.97):
            continue                     # 纯白不可见块（redaction 残留）也不算内容
        if rc.y1 < top - 14 or rc.y0 > bot + 14:
            continue
        if top - 14 <= rc.y0 <= top + 4:
            top = min(top, rc.y0)
        if bot - 4 <= rc.y1 <= bot + 14:
            bot = max(bot, rc.y1)
    return top, bot


def bands(doc, pages, pi, y_lo, y_hi):
    """页内「非点线带」：把 [y_lo, y_hi] 按连续点线块切开，返回 [(y0,y1)]。

    纯点线带（含整页都是点线的页）不返回 —— 这就是「不要答题区」。
    """
    lines = [r for r in body_lines(pages, pi)
             if r["y0"] >= y_lo - 2 and r["y1"] <= y_hi + 2]
    if not lines:
        return []
    dots = sorted((r["y0"], r["y1"]) for r in lines if is_dotted(r))
    merged = []
    for a, b in dots:
        if merged and a <= merged[-1][1] + DOT_GAP:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))
    segs, cur = [], y_lo
    for a, b in merged:
        if a - cur > 4:
            segs.append((cur, a))
        cur = max(cur, b)
    if y_hi - cur > 4:
        segs.append((cur, y_hi))

    out = []
    for a, b in segs:
        inner = [r for r in lines if not is_dotted(r) and r["y0"] >= a - 1 and r["y1"] <= b + 1]
        if not inner:
            continue
        top = min(r["y0"] for r in inner) - PAD
        bot = max(r["y1"] for r in inner) + PAD
        top, bot = _extend_by_drawings(doc, pi, top, bot)
        top = max(GEOM["region_top"], round(top, 2))
        bot = min(page_cap(pages, pi), round(bot, 2))
        if bot - top >= BAND_MIN:
            out.append((top, bot))
    return out


def mark_strips(pages, pi, y_from, y_to):
    """分值条：CIE 把 [8] 印在**最后一条点线**的尾巴上（点线和一个 "[8]" 在同一个
    span 里），整行按点线裁掉会把分值一起丢掉。

    → 用 rawdict 的逐字符 bbox 定位 "[" 的 x，只留 "[n]" 那一小段（不含任何点线），
      并标 align=right，让渲染层把它贴到题图右沿、跟真卷位置一致。
    """
    out = []
    chars = _CHARS.get(pi, [])
    for r in body_lines(pages, pi):
        if not (is_dotted(r) and MARK_RE.search(r["text"])):
            continue
        if not (y_from - 2 <= r["y0"] <= y_to + 40):
            continue
        x0 = None
        for (cy0, cx0, cy1, cx1, ch) in chars:
            if ch == "[" and abs(cy0 - r["y0"]) < 6:
                x0 = cx0
                break
        out.append({
            "top": round(max(GEOM["region_top"], r["y0"] - 2), 2),
            "bot": round(min(page_cap(pages, pi), r["y1"] + 2), 2),
            "x0": round((x0 - 2) if x0 else (r["x1"] - 42), 2),
            "align": "right",
        })
    return out


def page_regions(doc, pages, pi, y_lo, y_hi):
    """该页 [y_lo, y_hi] 内的区段：正文带（满宽）+ 分值条（右对齐窄条）。"""
    items = [{"top": t, "bot": b} for t, b in bands(doc, pages, pi, y_lo, y_hi)]
    items.sort(key=lambda d: d["top"])
    merged = []
    for it in items:
        if merged and it["top"] <= merged[-1]["bot"] + 1:
            merged[-1]["bot"] = max(merged[-1]["bot"], it["bot"])
        else:
            merged.append(dict(it))
    for m in mark_strips(pages, pi, y_lo, y_hi):
        if any(m["top"] < x["bot"] and x["top"] < m["bot"] for x in merged):
            continue
        merged.append(m)
    merged.sort(key=lambda d: d["top"])
    return merged


def _rg(pi, item):
    bbox = [item.get("x0", GEOM["x_left"]), item["top"], GEOM["x_right"], item["bot"]]
    out = {"page": pi, "bbox": bbox}
    if item.get("align"):
        out["align"] = item["align"]
    return out




def regions_between(doc, pages, a, b):
    """a=(page,y) 起，b=(page,y) 止（不含 b）→ region 列表（跨页按带切）。"""
    pa, ya = a
    pb, yb = b
    out = []
    if pa == pb:
        return [_rg(pa, it) for it in page_regions(doc, pages, pa, ya, min(yb, page_cap(pages, pa)))]
    for it in page_regions(doc, pages, pa, ya, page_cap(pages, pa)):
        out.append(_rg(pa, it))
    for p in range(pa + 1, pb):
        for it in page_regions(doc, pages, p, GEOM["region_top"], page_cap(pages, p)):
            out.append(_rg(p, it))
    for it in page_regions(doc, pages, pb, GEOM["region_top"], min(yb, page_cap(pages, pb))):
        out.append(_rg(pb, it))
    return out


# ── 锚点识别 ────────────────────────────────────────────────────────────────
def detect_qcol(pages):
    from collections import Counter
    cnt = Counter()
    for pi, lines in enumerate(pages):
        if pi == 0:
            continue
        for r in lines:
            if r["y0"] < GEOM["region_top"]:
                continue
            # 粗体；2024-oct 起部分卷整卷丢字重（bold=False），「整行就是个 1–2 位数」也认
            if not r["bold"] and not (r["text"].strip().isdigit() and len(r["text"].strip()) <= 2):
                continue
            m = QSTART_RE.match(r["text"])
            if m and 1 <= int(m.group(1)) <= 20 and 40 <= r["x0"] <= 120:
                cnt[round(r["x0"], 1)] += 1
    return cnt.most_common(1)[0][0] if cnt else 49.6


def find_qnums(pages, qcol):
    """题号序列 [(page, y0, y1, n, line)]，1..N 单调递增。"""
    cands = []
    for pi, lines in enumerate(pages):
        if pi == 0:
            continue
        for r in lines:
            if r["y0"] < GEOM["region_top"]:
                continue
            # ⚠️ 2024-oct 起部分卷题号丢字重（bold=False）→ 「整行纯数字」也认
            if not r["bold"] and not (r["text"].strip().isdigit() and len(r["text"].strip()) <= 2):
                continue
            if abs(r["x0"] - qcol) > QCOL_TOL:
                continue
            m = QSTART_RE.match(r["text"])
            if not m:
                continue
            n = int(m.group(1))
            if 1 <= n <= 20:
                cands.append((pi, r["y0"], r["y1"], n, r))
    seq, nxt = [], 1
    for c in cands:
        if c[3] in (nxt, nxt + 1):
            seq.append(c)
            nxt = c[3] + 1
    return seq


def find_subs(pages, qcol):
    """小问锚点 [(page, y, label, depth, line)]；depth=0 字母，1 罗马层。"""
    subs = []
    for pi, lines in enumerate(pages):
        if pi == 0:
            continue
        for r in lines:
            if r["y0"] < GEOM["region_top"] or r["x0"] > 160:
                continue
            m = SUB_RE.match(r["text"])
            if m:
                subs.append((pi, r["y0"], m.group(1), 0, r))
                continue
            m = ROMAN_RE.match(r["text"])
            if m and m.group(1) in ROMAN_SET:
                subs.append((pi, r["y0"], m.group(1), 1, r))
    subs.sort(key=lambda s: (s[0], s[1]))
    return subs


def boundary_events(pages, qcol):
    """题结束边界：Section 头 / EITHER / OR / 指引句 / 页脚版权块。"""
    ev = []
    for pi, lines in enumerate(pages):
        if pi == 0:
            continue
        for r in lines:
            if r["y0"] < GEOM["region_top"]:
                continue
            t = r["text"]
            if SEC_RE.match(t):
                ev.append((pi, r["y0"], "section"))
            elif EITHER_RE.match(t) and r["bold"] and abs(r["x0"] - qcol) <= QCOL_TOL:
                ev.append((pi, r["y0"], "either"))
            elif INSTR_RE.match(t):
                ev.append((pi, r["y0"], "instr"))
            elif FOOT_BLOCK_RE.match(t):
                ev.append((pi, r["y0"] - 3, "footer"))
    ev.sort()
    return ev


# ── 资料 ────────────────────────────────────────────────────────────────────
def insert_regions(doc, pages, ins_start):
    """P3 的 INSERT 全册 → region 列表（封面 / BLANK PAGE / 版权页除外）。"""
    if ins_start is None:
        return []
    regs = []
    for pi in range(ins_start, doc.page_count):
        raw = [r for r in pages[pi] if r["y0"] >= GEOM["region_top"]]
        head = next((r["text"] for r in raw if not r["text"].isdigit()), "")
        if pi == ins_start and re.search(r"\bINSERT\b|INFORMATION", " ".join(r["text"] for r in raw)):
            continue                      # 材料册封面
        if BLANK_RE.match(head) or FOOT_BLOCK_RE.match(head):
            continue                      # 空白页 / 版权页
        body = body_lines(pages, pi)
        if not body:
            continue
        top = max(GEOM["region_top"], round(body[0]["y0"] - PAD, 2))
        bot = min(page_cap(pages, pi), round(max(r["y1"] for r in body) + PAD, 2))
        top, bot = _extend_by_drawings(doc, pi, top, bot)
        if bot - top >= 40:
            regs.append({"page": pi, "bbox": [GEOM["x_left"], top, GEOM["x_right"], round(bot, 2)]})
    return regs


def inline_material(doc, pages, start, stop):
    """QP 自带的 case 段（P2 每题一份 / P4 全卷共用一份）→ region 列表。"""
    if stop is None or (stop[0], stop[1]) <= (start[0], start[1]):
        return []
    return regions_between(doc, pages, start, stop)


# ── 主流程 ──────────────────────────────────────────────────────────────────
def page_chars(page):
    """逐字符 bbox（只收非空白字符）。分值条要精确到 "[" 的位置，行级/span 级都不够。"""
    out = []
    for b in page.get_text("rawdict")["blocks"]:
        if b.get("type") != 0:
            continue
        for ln in b["lines"]:
            for sp in ln["spans"]:
                for ch in sp["chars"]:
                    if not ch["c"].strip():
                        continue
                    r = ch["bbox"]
                    out.append((round(r[1], 2), round(r[0], 2), round(r[3], 2), round(r[2], 2), ch["c"]))
    return out


def analyse(doc, qp_pages=None):
    pages = [page_lines(pg) for pg in doc]
    setup_geom(doc, pages, qp_pages)
    _CHARS.clear()
    for pi, pg in enumerate(doc):
        _CHARS[pi] = page_chars(pg)
    qp = pages if qp_pages is None else pages[:qp_pages]
    qcol = detect_qcol(qp)
    qnums = find_qnums(qp, qcol)
    subs = find_subs(qp, qcol)
    bounds = boundary_events(qp, qcol)
    return pages, qcol, qnums, subs, bounds


def text_between(pages, a, b):
    """区间正文（供 qNN.md 的 body），剔除页眉页脚与点线。"""
    pa, ya = a
    pb, yb = b
    out = []
    for pi in range(pa, pb + 1):
        cap = page_cap(pages, pi) if pi != pb else yb
        for r in body_lines(pages, pi):
            if pi == pa and r["y0"] < ya:
                continue
            if r["y0"] >= cap:
                continue
            if is_dotted(r):
                # 分值印在最后一条点线上：点线丢掉，[n] 留下（否则 total_marks 全 None）
                m = MARK_RE.search(r["text"])
                if m:
                    out.append(m.group(0))
                continue
            if INSTR_RE.match(r["text"]) or SEC_RE.match(r["text"]):
                continue
            out.append(r["text"])
    return "\n".join(out)


def build(doc, paper_no, pages, qcol, qnums, subs, bounds, ins_start):
    kind = MATERIAL_KIND[paper_no]
    questions = []

    # 共享资料（P4）：从 p2 顶到第一题之前（含 "Answer both questions." 之前）
    shared_mat = []
    if kind == "inline-shared" and qnums:
        first = (qnums[0][0], qnums[0][1])
        stops = [first] + [(p, y) for (p, y, k) in bounds if k == "instr" and (p, y) < first]
        stop = min(stops)
        shared_mat = inline_material(doc, pages, (1, GEOM["region_top"]),
                                     (stop[0], stop[1] - END_PAD))
    if kind == "insert":
        shared_mat = insert_regions(doc, pages, ins_start)

    for idx, (pi, y0, y1, n, line) in enumerate(qnums):
        start = (pi, max(GEOM["region_top"], y0 - PAD))
        # 结束边界：下一题 / Section / EITHER·OR / 指引句 / 页脚
        cand = [(qnums[idx + 1][0], qnums[idx + 1][1] - END_PAD, "next-q")] if idx + 1 < len(qnums) else []
        for (p, y, k) in bounds:
            if (p, y) > (pi, y0):
                cand.append((p, y - END_PAD, k))
        if cand:
            cand.sort()
            p, y, _k = cand[0]
            end = (p, max(GEOM["region_top"] + 2, y))
        else:
            end = (pi, page_cap(pages, pi))

        q = {
            "number": str(n),
            "status": "auto",
            "confidence": 0.95,
            "question_type": "structured",
            "section": "",
        }
        for (p, y, k) in bounds:
            if k == "section" and (p, y) < (pi, y0):
                for r in pages[p]:
                    if SEC_RE.match(r["text"]) and abs(r["y0"] - y) < 1:
                        q["section"] = SEC_RE.match(r["text"]).group(1)

        seg = text_between(pages, start, end)
        marks = [int(m.group(1)) for m in MARK_RE.finditer(seg)]
        if marks:
            q["total_marks"] = sum(marks)

        qsubs = [(pp, yy, lab, dep, r) for (pp, yy, lab, dep, r) in subs
                 if (pi, y0) <= (pp, yy) < end]
        if qsubs:
            fixed, parent = [], None
            for (pp, yy, lab, dep, r) in qsubs:
                if lab in ROMAN_SET:
                    fixed.append({"label": f"{parent} {lab}" if parent else lab,
                                  "page": pp, "bbox": [r["x0"], r["y0"], r["x1"], r["y1"]]})
                else:
                    parent = lab
                    fixed.append({"label": lab, "page": pp,
                                  "bbox": [r["x0"], r["y0"], r["x1"], r["y1"]]})
            q["subquestions"] = fixed

        q["regions"] = regions_between(doc, pages, start, end)

        if kind == "inline-per-q":
            # P2：该题自己的 case 段 = 从题号行到第一个小问锚点；题面从第一个小问起，
            # 否则 case 会被「资料」和「题面」各收一遍（2024-june-qp21 Q1 实测重复两遍）
            first_sub = next(((pp, yy) for (pp, yy, lab, dep, r) in subs
                              if (pp, yy) > (pi, y0)), None)
            if first_sub is None:
                mat = []
            else:
                stops = [first_sub] + [(p, y) for (p, y, k) in bounds
                                       if k == "instr" and (pi, y0) < (p, y) < first_sub]
                stop = min(stops)
                mat = inline_material(doc, pages, (pi, y0), (stop[0], stop[1] - END_PAD))
                q["regions"] = regions_between(doc, pages,
                                               (first_sub[0], max(GEOM["region_top"],
                                                                  first_sub[1] - PAD)), end)
            if mat:
                q["material_regions"] = mat
        elif shared_mat and kind in ("insert", "inline-shared"):
            q["material_regions"] = [dict(r) for r in shared_mat]

        if not q["regions"]:
            q["status"] = "review"
            q["confidence"] = 0.2
        q["anchors"] = {
            "start": {"page": pi, "bbox": [line["x0"], line["y0"], line["x1"], line["y1"]]},
            "end": {"page": end[0], "bbox": [0.0, 0.0, 0.0, round(end[1], 2)]},
            "cut": "next-q" if idx + 1 < len(qnums) else "page-bottom",
        }
        q["_body"] = text_between(pages, start, end)
        questions.append(q)
    return questions


def yaml_scalar(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    s = str(v)
    if re.search(r'[:#\[\]{}&*!|>\'"%@`]', s) or s.strip() != s or s == "":
        return '"' + s.replace('"', '\\"') + '"'
    return s


def write_qmd(path, meta, q, body):
    lines = ["---"]
    for k in ("id", "board", "qualification", "subject", "paper_code", "year", "session",
              "question_number", "section", "total_marks", "question_type", "image", "layout"):
        if k in meta:
            lines.append(f"{k}: {yaml_scalar(meta[k])}")
    lines.append("source: " + yaml_scalar(f"CIE 9609/{meta['paper_code'].upper()}, "
                                          f"{meta['year']} {meta['session']}"))
    lines.append(f"cut_status: {q['status']}")
    lines.append("language: en")
    lines.append("---")
    lines.append("")
    lines.append(body)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("session_dir")
    ap.add_argument("--analyze", action="store_true")
    ap.add_argument("--png", default="")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    sdir = Path(args.session_dir)
    src = sdir / "source.pdf"
    if not src.exists():
        sys.exit(f"缺少 source.pdf: {src}")
    pm = sdir / "paper.md"
    if not pm.exists():
        sys.exit(f"缺少 paper.md: {pm}")
    fm = pm.read_text(encoding="utf-8")
    code_m = re.search(r"^paper_code:\s*(\S+)", fm, re.M)
    year_m = re.search(r"^year:\s*(\d{4})", fm, re.M)
    sess_m = re.search(r"^session:\s*(\S+)", fm, re.M)
    code = code_m.group(1).strip().lower() if code_m else ""
    paper_no = re.match(r"qp(\d)", code)
    if not paper_no or paper_no.group(1) not in PAPER_TITLE:
        sys.exit(f"paper_code 异常: {code}")
    paper_no = paper_no.group(1)

    ins_m = re.search(r"^insert_start_page:\s*(\d+)", fm, re.M)
    ins_start = int(ins_m.group(1)) if ins_m else None

    doc = pymupdf.open(src)
    pages, qcol, qnums, subs, bounds = analyse(doc, ins_start)
    qs = build(doc, paper_no, pages, qcol, qnums, subs, bounds, ins_start)
    if not qs:
        sys.exit(f"[{sdir.name}] 一题都没识别出来（题号列 x0={qcol}，region_top={GEOM['region_top']}）"
                 "——版式不符，拒绝写空卷。")

    print(f"[{sdir.name}] {doc.page_count} 页 · 题号列 x0={qcol} · 题数 {len(qs)} · P{paper_no} "
          f"· 资料={MATERIAL_KIND[paper_no]} · 版心 x{GEOM['x_left']}–{GEOM['x_right']} "
          f"y{GEOM['region_top']}–{GEOM['footer_y']}")
    if args.analyze:
        for q in qs:
            print(f"  Q{q['number']:>2} sec={q['section'] or '-':1} {q['status']:>6} "
                  f"marks={q.get('total_marks','?'):>3} regions={len(q['regions'])} "
                  f"mat={len(q.get('material_regions') or [])} subs={len(q.get('subquestions') or []):>2}")
        return
    if args.png:
        out = Path(args.png)
        out.mkdir(parents=True, exist_ok=True)
        import PIL.Image as Image
        for q in qs:
            rg = (q.get("material_regions") or []) + (q.get("regions") or [])
            parts = []
            for r in rg:
                pix = doc[r["page"]].get_pixmap(dpi=110, clip=pymupdf.Rect(*r["bbox"]))
                parts.append(Image.frombytes("RGB", (pix.width, pix.height), pix.samples))
            if not parts:
                continue
            W = max(i.width for i in parts)
            H = sum(i.height for i in parts) + 12 * (len(parts) - 1)
            canvas = Image.new("RGB", (W, H), "white")
            y = 0
            for i in parts:
                canvas.paste(i, (0, y))
                y += i.height + 12
            canvas.save(out / f"q{q['number']:0>2}.png")
        print(f"  PNG → {out}")
        return

    sha = hashlib.sha256(src.read_bytes()).hexdigest()
    cut_path = sdir / "cut.json"
    if cut_path.exists() and not args.force:
        old = json.loads(cut_path.read_text(encoding="utf-8"))
        if old.get("source", {}).get("sha256") != sha:
            sys.exit("source.pdf 的 sha256 与 cut.json 记录不符——源头已变，请人工处理。")
    (sdir / "assets").mkdir(exist_ok=True)
    keep_nums = {int(q["number"]) for q in qs}
    for stale in sdir.glob("q*.md"):
        m2 = re.fullmatch(r"q(\d{2})\.md", stale.name)
        if m2 and int(m2.group(1)) not in keep_nums:
            stale.unlink()
            for ext in ("webp", "layout.json"):
                (sdir / "assets" / f"q{m2.group(1)}.{ext}").unlink(missing_ok=True)

    year = int(year_m.group(1)) if year_m else 0
    session = sess_m.group(1).strip() if sess_m else ""
    base_id = f"cie-alevel-business-{year}-{session}-{code}"
    keep = {}
    if cut_path.exists():
        old = json.loads(cut_path.read_text(encoding="utf-8"))
        keep = {str(q["number"]): q for q in old.get("questions", [])
                if q.get("status") in ("verified", "manual")}

    out_qs = []
    for q in qs:
        body = q.pop("_body", "")
        if q["number"] in keep:
            out_qs.append(keep[q["number"]])
            continue
        out_qs.append(q)
        meta = {
            "id": f"{base_id}-q{int(q['number']):02d}", "board": "cie",
            "qualification": "alevel", "subject": "business", "paper_code": code.upper(),
            "year": year, "session": session, "question_number": int(q["number"]),
            "section": q.get("section") or None,
            "total_marks": q.get("total_marks"), "question_type": q["question_type"],
            "image": f"assets/q{int(q['number']):02d}.webp",
            "layout": f"assets/q{int(q['number']):02d}.layout.json",
        }
        write_qmd(sdir / f"q{int(q['number']):02d}.md",
                  {k: v for k, v in meta.items() if v is not None}, q, body)

    cut = {
        "schema_version": 1,
        "source": {"file": "source.pdf", "sha256": sha, "pages": doc.page_count},
        "page_size": {"w": round(doc[0].rect.width, 2), "h": round(doc[0].rect.height, 2)},
        "render": {"dpi": 200, "format": "webp", "quality": 85},
    }
    if any(q.get("material_regions") for q in out_qs):
        cut["material_order"] = "before"
    cut["questions"] = out_qs
    cut_path.write_text(json.dumps(cut, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"  已写 cut.json（{len(out_qs)} 题）+ {len(out_qs)} 个 qNN.md")


if __name__ == "__main__":
    main()
