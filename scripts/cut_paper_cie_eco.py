#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""cut_paper_cie_eco.py — CIE (9708) 经济卷按题自动切割

⚠️ 专用脚本（考试局 × 资格 × 学科各一份，2026-10-21 定规）：
    CIE / A Level / Economics(9708)，只服务这一个学科。
    与 cut_paper_cie.py（物理 9702）/ cut_paper_cie_chem.py（化学 9701）不共享
    代码、不互相 import；各自改动互不影响。产出与 cut_paper.py 同构
    （cut.json + qNN.md，符合 schema/cut.schema.json）。

CIE 9708 的版式（2026-10-21 实测 97 份 / 15 个场次 / 776 页）：
  · P1（qp11–14）= 30 题 MCQ，30 分，1 小时；
    P2（qp21–23）= Q1 资料题 20 分 + Section B/C 各 2 篇选 1；
        老格式（2020–2022）4 题（Q1 + Section B 三选一），新格式（2023+）5 题；
    P3（qp31–33）= 30 题 MCQ，30 分，1 小时 15 分；
    P4（qp41–43）= Q1 资料题 + 4 篇选 1（新格式 5 题）；老格式 7 题（Q1 + 6 篇）。
  · **无作答区**：答案写在另发的 answer booklet 上。全卷没有一个作答点线
    （55 份卷里的 dashed 图形逐条验过，全是图表辅助线/投影线）。故不写
    answer_regions，也不切点线。
  · **无 [Total for Question N] 锚点**（50 份结构卷一份都没有）：这是与理化/
    会计最大的差异。切点只能靠「下一题题号 / Section 头 / EITHER·OR / 页脚」。
  · 题号恒为「粗体 + 左栏 x0≈49.6 + 行首数字」；两位数题号可能内联在题干行
    （"10 The diagram shows…"）。小问标签 "(a)" 在 x0≈72.3、与题干同行。
    Section 头居中（x0≈272），"Answer …" 紧跟其下，"EITHER"/"OR" 在 x0≈49.6
    但为字母，不会和题号混淆。页首页码在居中 x0≈294.6（y≈36），靠 y 过滤。
  · Q1 的资料（图 + 文章 + 表格）在正文页上半，小问在下一页顶部；少数卷资料
    延伸到小问页顶部 → material 段与小问段按第一行 "(a)" 横切。
    资料在前、题目在后，故 cut.json 写 material_order: "before"。
  · 说明性文字一律不入题图：Section A/B/C、Answer all parts of Question 1、
    Answer one question、EITHER、OR。老卷 Q3/Q4 前的引导句（"There is
    increasing concern about…"）属于题干，保留（它在题号行之后，天然被包含）。
  · 小问只记锚点（subquestions），**不切子图**——CIE 在云端子图黑名单里，
    前端按整题图 + 锚点定位。罗马层 (i)(ii) 记成嵌套 label（"d i"），与
    CIE 化学 280 卷的既有格式一致。
  · 选项热区（MCQ）：A–D 粗体单字母，竖排或表格行 → 按行横切；2×2 网格 →
    按四象限切。取「题区内最后一次出现」的 A/B/C/D，避开图内 A/B/C/D 标注。

用法:
    python scripts/cut_paper_cie_eco.py <session_dir>            # 切割 + 写 cut.json/qNN.md
    python scripts/cut_paper_cie_eco.py <session_dir> --analyze  # 只打印锚点表，不落盘
    python scripts/cut_paper_cie_eco.py <session_dir> --png DIR  # 另存每题 PNG 供视觉抽检

<session_dir> 指向 data/cie/alevel/economics/<year>-<session>-qpNN/，
目录内须已放入 source.pdf 与 paper.md。
paper_code 约定：qp11–14 → Paper 1 (MCQ)；qp21–23 → Paper 2；
                qp31–33 → Paper 3 (MCQ)；qp41–43 → Paper 4。

坐标系：PDF 点 pt，页面左上角原点，y 向下；bbox=[x0,y0,x1,y1]。
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

# ── 版式常量（CIE 9708 实测） ────────────────────────────────────────────────
# 两套版式（2025 起 CIE 改用 Letter 612×792，整版右移、字小一号）：
#   595×842 = 2020–2024；612×792 = 2025–2026
PROFILES = {
    595: {"header_y": 56.0, "region_top": 50.0, "footer_y": 792.0, "footer_hard": 812.0,
          "x_left": 36.0, "x_right": 568.0},
    612: {"header_y": 50.0, "region_top": 50.0, "footer_y": 752.0, "footer_hard": 762.0,
          "x_left": 60.0, "x_right": 556.0},
}
GEOM = dict(PROFILES[595])


def setup_geom(doc):
    """按首页宽度选版式参数；陌生尺寸按比例推。页码位置再按卷实测抬高一次
    （同尺寸里 P2/P4 的页码比 P1 低，2025-mar-qp22 的页码压到 y=45–59，
    固定 50 会把「3」的下半截切进图里）。"""
    w, h = doc[0].rect.width, doc[0].rect.height
    for key in (595, 612):
        if abs(w - key) < 3:
            GEOM.update(PROFILES[key])
            pn = []
            for pi in range(1, min(doc.page_count, 4)):
                for b in doc[pi].get_text("dict")["blocks"]:
                    if b.get("type") != 0:
                        continue
                    for ln in b["lines"]:
                        t = "".join(sp["text"] for sp in ln["spans"]).strip()
                        if (t.isdigit() and len(t) <= 2 and ln["bbox"][1] < 80
                                and abs(ln["bbox"][0] - w / 2) < 60):
                            pn.append(ln["bbox"][3])
            if pn and max(pn) + 4 > GEOM["header_y"]:
                GEOM["header_y"] = round(max(pn) + 4, 1)
                GEOM["region_top"] = GEOM["header_y"]
            return
    GEOM.update({"header_y": round(h * 0.066, 1), "region_top": round(h * 0.062, 1),
                 "footer_y": round(h * 0.94, 1), "footer_hard": round(h - 30, 1),
                 "x_left": round(w * 0.06, 1), "x_right": round(w * 0.94, 1)})
PAD = 3.0           # 起点相对题号行的上留白
END_PAD = 7.0       # 终点相对下一锚点行的下留白（留点气，别贴着上一题的字）
QCOL_TOL = 4.0      # 题号列允许偏移

SEC_RE = re.compile(r"^Section\s+([ABC])$")
INSTR_RE = re.compile(r"^Answer\s+(?:all|one|this|three|two)\b", re.I)
EITHER_RE = re.compile(r"^(either|or)$", re.I)
SUB_RE = re.compile(r"^\(([a-e])\)")
ROMAN_RE = re.compile(r"^\((i{1,3}|iv|v|vi{1,3})\)")
QSTART_RE = re.compile(r"^(\d{1,2})(?![0-9])")
OPT_RE = re.compile(r"^([A-D])$")
MARK_RE = re.compile(r"\[(\d{1,3})\]")
FOOT_BLOCK_RE = re.compile(r"^(Permission to reproduce|BLANK PAGE|Cambridge Assessment)", re.I)
FOOT_MARK_RE = re.compile(r"^(©|\[Turn over|Permission to reproduce|BLANK PAGE|\d{4}/\d{1,2}/)", re.I)

PAPER_INFO = {
    "1": {"title": "Paper 1 AS Level Multiple Choice", "marks": 30, "mcq": True},
    "2": {"title": "Paper 2 AS Level Data Response and Essays", "marks": 60, "mcq": False},
    "3": {"title": "Paper 3 A Level Multiple Choice", "marks": 30, "mcq": True},
    "4": {"title": "Paper 4 A Level Data Response and Essays", "marks": 60, "mcq": False},
}
ROMAN_SET = {"i", "ii", "iii", "iv", "v", "vi", "vii", "viii", "ix"}


# ── 行抽取 ──────────────────────────────────────────────────────────────────
def _decode_line(texts):
    """一行内的 span 列表 → 文本。处理 CIE 2025 起的「整段 −0x1D 位移」。

    不能整行统一解码：同一行里常有正常 span（如题号 "13"）和位移 span 混排，
    整行 +0x1D 会把 "13" 变成 "NP"。所以逐 span 判：
      · 没有控制字符 → 原样；
      · 单控制字符且紧挨纯数字（题号被拆成 "1" + "\x19"）→ 解；
      · 解码后「像英文」（字母+空格占比 ≥75%）→ 解；
      · 其余（2020 卷里 \x16/\x1a 这类单字符符号）→ 原样留着。
    """
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
    """[{y0,x0,x1,y1,text,bold,size}]，按阅读顺序排序。"""
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
                "y0": round(ln["bbox"][1], 2),
                "x0": round(ln["bbox"][0], 2),
                "x1": round(ln["bbox"][2], 2),
                "y1": round(ln["bbox"][3], 2),
                "text": text,
                "bold": "Bold" in first["font"] or "bold" in first["font"].lower(),
                "size": round(first["size"], 1),
            })
    out.sort(key=lambda r: (r["y0"], r["x0"]))
    return out


def page_spans(page):
    """页内所有粗体 span（单字母选项可能在同一行里被合并成 "B C"，必须按 span 取）。"""
    out = []
    for b in page.get_text("dict")["blocks"]:
        if b.get("type") != 0:
            continue
        for ln in b["lines"]:
            for sp in ln["spans"]:
                t = sp["text"].strip()
                if not t:
                    continue
                if "Bold" not in sp["font"]:
                    continue
                out.append((round(sp["bbox"][1], 2), round(sp["bbox"][0], 2),
                            round(sp["bbox"][3], 2), round(sp["bbox"][2], 2), t))
    out.sort()
    return out


def content_bottom(lines):
    """本页内容底界：页脚行起点为止（不预设 792——有的卷最后一行 Source 排到 797，
    硬切 792 会把那行切掉半截；而页脚起点本身天然是安全边界）。"""
    ys = [r["y0"] for r in lines if FOOT_MARK_RE.match(r["text"])]
    cap = min(ys) if ys else GEOM["footer_hard"]
    return max(GEOM["header_y"], min(cap, GEOM["footer_hard"]))


# ── 锚点识别 ────────────────────────────────────────────────────────────────
def detect_qcol(pages):
    """题号列：粗体、行首数字、y 在正文区 的行 x0 众数。"""
    from collections import Counter
    cnt = Counter()
    for lines in pages:
        for r in lines:
            if r["y0"] < GEOM["header_y"] or not r["bold"]:
                continue
            m = QSTART_RE.match(r["text"])
            if m and 1 <= int(m.group(1)) <= 30 and 40 <= r["x0"] <= 120:
                cnt[round(r["x0"], 1)] += 1
    if not cnt:
        return 49.6
    return cnt.most_common(1)[0][0]


def find_qnums(pages, qcol):
    """题号序列：返回 [(page_idx, y0, y1, n, line)]，1..N 单调递增。"""
    cands = []
    for pi, lines in enumerate(pages):
        if pi == 0:
            continue
        for r in lines:
            if r["y0"] < GEOM["header_y"] or not r["bold"]:
                continue
            if abs(r["x0"] - qcol) > QCOL_TOL:
                continue
            m = QSTART_RE.match(r["text"])
            if not m:
                continue
            n = int(m.group(1))
            if 1 <= n <= 30:
                cands.append((pi, r["y0"], r["y1"], n, r))
    seq = []
    nxt = 1
    for c in cands:
        n = c[3]
        if n == nxt or n == nxt + 1:      # 允许跳 1（防御性）
            seq.append(c)
            nxt = n + 1
    return seq


def boundary_events(pages, doc, qcol):
    """所有「题结束边界」事件：(page, y, kind)，按文档序。"""
    ev = []
    for pi, lines in enumerate(pages):
        if pi == 0:
            continue
        for r in lines:
            t = r["text"]
            if r["y0"] < GEOM["header_y"]:
                continue
            if SEC_RE.match(t):
                ev.append((pi, r["y0"], "section"))
            elif (EITHER_RE.match(t) and r["bold"]
                  and abs(r["x0"] - qcol) <= QCOL_TOL):
                ev.append((pi, r["y0"], "either"))
            elif FOOT_BLOCK_RE.match(t):
                # 版权块上方常压一条贯穿版心的细横线，真正的边界是那条线——
                # 用文字行 y 会让末题的图一路撑到版权块，下面留一大片白。
                y = r["y0"]
                page = doc[pi]
                W = page.rect.width
                for d in page.get_drawings():
                    rc = d["rect"]
                    if rc.height <= 2.0 and rc.width >= W * 0.6 and y - 18 <= rc.y0 <= y:
                        y = min(y, rc.y0)
                ev.append((pi, y, "footer"))
    ev.sort()
    return ev


def find_subs(pages, qcol):
    """小问锚点：(page, y, label, depth) ；depth=0 顶层字母，1 罗马层。"""
    subs = []
    for pi, lines in enumerate(pages):
        if pi == 0:
            continue
        for r in lines:
            if r["y0"] < GEOM["header_y"]:
                continue
            if r["x0"] > 150:
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


def find_options(doc, pi_from, y_from, pi_to, y_to):
    """MCQ 选项 A–D 的标签位置（span 级）。取题区内「最后一次出现」的 A/B/C/D。

    必须按 span 而不是按行取：CIE 常把挨得近的两个标注印成一行 "B C"。
    """
    rows = []
    for pi in range(pi_from, pi_to + 1):
        for (y0, x0, y1, x1, t) in page_spans(doc[pi]):
            if pi == pi_from and y0 < y_from:
                continue
            if pi == pi_to and y0 >= y_to:
                continue
            if y0 < GEOM["header_y"]:
                continue
            if OPT_RE.match(t):
                rows.append((pi, y0, y1, x0, x1, t))
                continue
            # 两个标注挤在一个 span 里："B C"（2021-oct-qp33 Q1 这种）
            mm = re.fullmatch(r"([A-D])(?:\s+([A-D]))+", t)
            if mm:
                letters = [c for c in t if c in "ABCD"]
                idxs = [i for i, c in enumerate(t) if c in "ABCD"]
                w = (x1 - x0) / max(1, len(t))
                for lab, i in zip(letters, idxs):
                    rows.append((pi, y0, y1, round(x0 + w * i, 2),
                                 round(x0 + w * (i + 1), 2), lab))
    rows.sort()
    picked = {}
    for row in rows:
        picked[row[5]] = row            # 后出现的覆盖前面的
    if len(picked) != 4:
        return None
    return [picked[k] for k in "ABCD"]


def _cluster(vals, tol=3.0):
    """把坐标按容差聚成若干簇，返回 [[v,...],...]（升序）。"""
    vs = sorted(vals)
    out = [[vs[0]]]
    for v in vs[1:]:
        if v - out[-1][-1] <= tol:
            out[-1].append(v)
        else:
            out.append([v])
    return out


def build_hotzones(seq, regions):
    """A–D 标签 → 4 个热区。两套打法：

    ① 网格（多数题）：4 个标签按 x/y 聚成 行×列，恰好 4 格且每格一个标签
       → 按格切。覆盖 4 行 1 列（竖排 / 表格行）、1 行 4 列（并排）、
       任意 2×2（A/C 左列或 A/B 左列）。
    ② 散点（选项就是图上的 A/B/C/D 标注，如「哪条曲线 / 哪个点」）：
       网格拼不出来时，按标签自身 bbox 外扩成可点小框。
    """
    pD = seq[3][0]
    reg = None
    for r in regions or []:
        if r["page"] == pD:
            reg = r
    x0 = reg["bbox"][0] if reg else GEOM["x_left"]
    x1 = reg["bbox"][2] if reg else GEOM["x_right"]
    bot = reg["bbox"][3] if reg else seq[3][1] + 30

    def idx(val, groups):
        best, bd = 0, 1e9
        for i, g in enumerate(groups):
            d = min(abs(val - v) for v in g)
            if d < bd:
                bd, best = d, i
        return best

    ys = [r[1] for r in seq]
    xs = [r[3] for r in seq]
    rows, cols = _cluster(ys), _cluster(xs)
    if len(rows) * len(cols) == 4:
        cells = {}
        for lab, r in zip("ABCD", seq):
            key = (idx(r[1], rows), idx(r[3], cols))
            if key in cells:
                break
            cells[key] = lab
        if len(cells) == 4:
            ytop = [g[0] - PAD for g in rows]
            ybot = [g[0] - PAD for g in rows[1:]] + [bot]
            xcut = [x0] + [round((cols[i][-1] + cols[i + 1][0]) / 2, 2)
                           for i in range(len(cols) - 1)] + [x1]
            return [{"label": lab,
                     "bbox": [xcut[ci], round(ytop[ri], 2), xcut[ci + 1], round(ybot[ri], 2)]}
                    for (ri, ci), lab in sorted(cells.items(), key=lambda kv: kv[1])]

    # 散点：按标签自身框外扩
    return [{"label": lab,
             "bbox": [round(r[3] - 16, 2), round(r[1] - 8, 2),
                      round(r[4] + 16, 2), round(r[2] + 8, 2)]}
            for lab, r in zip("ABCD", seq)]


def clamp_hotzones(hz, regions):
    """把热区收口到题目区域内，保证 x1-x0、y1-y0 都 > 0（否则 layout 归一化出负宽）。"""
    out = []
    for h in hz or []:
        x0, y0, x1, y1 = h["bbox"]
        reg = None
        for r in regions or []:
            b = r["bbox"]
            if b[1] - 1 <= y0 <= b[3] + 1:
                reg = b
                break
        if reg:
            x0, x1 = max(x0, reg[0] + 1), min(x1, reg[2] - 1)
            y0, y1 = max(y0, reg[1] + 1), min(y1, reg[3] - 1)
        if x1 - x0 < 6:
            x1 = x0 + 6
        if y1 - y0 < 6:
            y1 = y0 + 6
        out.append({"label": h["label"],
                    "bbox": [round(x0, 2), round(y0, 2), round(x1, 2), round(y1, 2)]})
    return out


# ── 区域构造 ────────────────────────────────────────────────────────────────
def _last_content_y(doc, pages, pi, y_from, y_cap):
    """该页 y_from..y_cap 内最后一条内容的下沿（文本 + 图形；越界/超宽图形忽略）。"""
    y = y_from
    for r in pages[pi]:
        if y_from - 1 <= r["y0"] and r["y1"] <= y_cap + 1 and not FOOT_MARK_RE.match(r["text"]):
            y = max(y, r["y1"])
    page = doc[pi]
    W, H = page.rect.width, page.rect.height
    for d in page.get_drawings():
        rc = d["rect"]
        if rc.width <= 0 or rc.width > W * 0.95:
            continue
        if rc.x0 < -5 or rc.x1 > W + 5 or rc.y0 < 0 or rc.y1 > H:
            continue
        if y_from - 1 <= rc.y0 and rc.y1 <= y_cap + 1:
            y = max(y, rc.y1)
    return min(y_cap, round(y + 6, 2))


def page_top(doc, pages, pi):
    """该页允许起切的最上沿：躲开页码。

    页码有时是文字（居中、y<60），有时是矢量路径（2025-mar-qp22 的 "3" 就是
    10x14 的路径块，压在 y=45–59），所以两样都查。
    """
    w = doc[pi].rect.width
    top = GEOM["region_top"]
    for r in pages[pi]:
        if (r["text"].isdigit() and len(r["text"]) <= 2
                and r["y0"] < 80 and abs(r["x0"] - w / 2) < 60):
            top = max(top, r["y1"] + 3)
    for d in doc[pi].get_drawings():
        rc = d["rect"]
        if (rc.y0 < 80 and rc.y1 < 90 and 5 <= rc.width <= 40 and 5 <= rc.height <= 26
                and abs((rc.x0 + rc.x1) / 2 - w / 2) < 40):
            top = max(top, rc.y1 + 3)
    return round(top, 1)


def regions_between(pages, doc, a, b):
    """a=(page,y) 起，b=(page,y) 止（不含 b 行），跨页自动补中间整页。"""
    pa, ya = a
    pb, yb = b
    if pa == pb:
        if yb - ya < 4:
            return []
        # 同页也要收到「最后一行内容」为止：末题没有下一题/分节可当边界时，
        # 否则会一路拉到页底，图下方留一大片白（2023-oct-qp11 Q30 曾留 511pt）。
        bot = _last_content_y(doc, pages, pa, ya, yb)
        if bot - ya < 4:
            return []
        return [{"page": pa, "bbox": [GEOM["x_left"], round(ya, 2), GEOM["x_right"], bot]}]
    # 末页落在页首 8pt 内（版权页/下一页顶）→ 视为「上页底结束」，不产碎片
    if yb - GEOM["header_y"] < 8:
        bot = _last_content_y(doc, pages, pa, ya, content_bottom(pages[pa]))
        return [{"page": pa, "bbox": [GEOM["x_left"], round(ya, 2), GEOM["x_right"], bot]}]
    bot = _last_content_y(doc, pages, pa, ya, content_bottom(pages[pa]))
    out = [{"page": pa, "bbox": [GEOM["x_left"], round(ya, 2), GEOM["x_right"], bot]}]
    for p in range(pa + 1, pb):
        body = [r for r in pages[p] if r["y0"] >= GEOM["header_y"] and not FOOT_MARK_RE.match(r["text"])]
        if not body:                      # 空白页 / 纯版权页跳过
            continue
        ptop = page_top(doc, pages, p)
        out.append({"page": p, "bbox": [GEOM["x_left"], ptop, GEOM["x_right"],
                                        _last_content_y(doc, pages, p, ptop,
                                                        content_bottom(pages[p]))]})
    out.append({"page": pb, "bbox": [GEOM["x_left"], page_top(doc, pages, pb), GEOM["x_right"],
                                     round(yb, 2)]})
    return out


def text_between(pages, a, b, drop_instruction=True):
    """取区间内的正文文本（供 qNN.md 的 body），剔除说明性文字与页眉页脚。"""
    pa, ya = a
    pb, yb = b
    out, sec = [], None
    for pi in range(pa, pb + 1):
        cap = content_bottom(pages[pi]) if pi != pb else yb
        for r in pages[pi]:
            t = r["text"]
            if pi == pa and r["y0"] < ya:
                continue
            if r["y0"] >= cap:
                continue
            if r["y0"] < GEOM["header_y"] or FOOT_MARK_RE.match(t):
                continue
            if SEC_RE.match(t) or EITHER_RE.match(t):
                continue
            if drop_instruction and INSTR_RE.match(t):
                continue
            if re.fullmatch(r"\d{1,3}", t) and 280 < r["x0"] < 320:   # 居中页码
                continue
            out.append(t)
    return "\n".join(out)


# ── 主流程 ──────────────────────────────────────────────────────────────────
def analyse(doc):
    setup_geom(doc)
    pages = [page_lines(pg) for pg in doc]
    qcol = detect_qcol(pages)
    qnums = find_qnums(pages, qcol)
    bounds = boundary_events(pages, doc, qcol)
    subs = find_subs(pages, qcol)
    return pages, qcol, qnums, bounds, subs


def build(doc, paper_no, pages, qcol, qnums, bounds, subs):
    mcq = PAPER_INFO[paper_no]["mcq"]
    questions = []
    # 分节：扫描 Section 头，给每个题号配当前 section
    sec_of = []
    cur = ""
    for q in qnums:
        for (p, y, kind) in bounds:
            if kind == "section" and (p, y) < (q[0], q[1]):
                # 用该 Section 头的字母
                for r in pages[p]:
                    if SEC_RE.match(r["text"]) and abs(r["y0"] - y) < 1:
                        cur = SEC_RE.match(r["text"]).group(1)
        sec_of.append(cur)

    for idx, (pi, y0, y1, n, line) in enumerate(qnums):
        start = (pi, max(GEOM["header_y"], y0 - PAD))
        # 结束边界：下一个「题号/Section/EITHER·OR/页脚」事件
        cand = [(qnums[idx + 1][0], qnums[idx + 1][1] - END_PAD, "next-q")] if idx + 1 < len(qnums) else []
        for (p, y, kind) in bounds:
            if (p, y) > (pi, y0):
                cand.append((p, y - END_PAD, kind))
        if not cand:
            end = (pi, content_bottom(pages[pi]))
        else:
            cand.sort()
            p, y, kind = cand[0]
            end = (p, max(GEOM["header_y"] + 2, y))

        q = {
            "number": str(n),
            "status": "auto",
            "confidence": 0.95,
            "question_type": "mcq" if mcq else "structured",
            "section": sec_of[idx] or "",
        }
        # 分值
        seg = text_between(pages, start, end, drop_instruction=False)
        marks = [int(m.group(1)) for m in MARK_RE.finditer(seg)]
        if mcq:
            q["total_marks"] = 1
        elif marks:
            q["total_marks"] = sum(marks)

        # 小问锚点（仅结构卷）
        qsubs = [(p, y, lab, dep, r) for (p, y, lab, dep, r) in subs
                 if (pi, y0) <= (p, y) < end and r["x0"] < 150]
        if qsubs:
            q["subquestions"] = [{"label": lab if dep == 0 else lab,
                                  "page": p,
                                  "bbox": [r["x0"], r["y0"], r["x1"], r["y1"]]}
                                 for (p, y, lab, dep, r) in qsubs]
        # 罗马层补父标签（"i" → "d i"）
        if q.get("subquestions"):
            fixed, parent = [], None
            for s in q["subquestions"]:
                lab = s["label"]
                if lab in ROMAN_SET:
                    s["label"] = f"{parent} {lab}" if parent else lab
                else:
                    parent = lab
                fixed.append(s)
            q["subquestions"] = fixed

        if mcq:
            q["regions"] = regions_between(pages, doc, start, end)
            seq = find_options(doc, start[0], start[1], end[0], end[1])
            hz = build_hotzones(seq, q["regions"]) if seq else None
            if hz:
                hz = clamp_hotzones(hz, q["regions"])
            if hz:
                q["option_hotzones"] = hz
            else:
                q["status"] = "review"
                q["confidence"] = 0.4
        else:
            # 结构卷：只有第 1 题是「资料 + 小问」结构 → 资料切成 material_regions
            first_sub = qsubs[0] if qsubs else None
            if idx == 0 and first_sub and (first_sub[0], first_sub[1]) > start:
                m_end = (first_sub[0], max(GEOM["header_y"] + 2, first_sub[1] - PAD))
                mat = regions_between(pages, doc, start, m_end)
                if mat:
                    q["material_regions"] = mat
                q["regions"] = regions_between(pages, doc, m_end, end)
            else:
                q["regions"] = regions_between(pages, doc, start, end)

        if not q["regions"] and not q.get("material_regions"):
            q["status"] = "review"
            q["confidence"] = 0.2
        q["anchors"] = {
            "start": {"page": start[0], "bbox": [line["x0"], line["y0"], line["x1"], line["y1"]]},
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
    lines.append("source: " + yaml_scalar(f"CIE 9708/{meta['paper_code'].upper()}, "
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
    if not paper_no or paper_no.group(1) not in PAPER_INFO:
        sys.exit(f"paper_code 异常: {code}")
    paper_no = paper_no.group(1)

    doc = pymupdf.open(src)
    pages, qcol, qnums, bounds, subs = analyse(doc)
    qs = build(doc, paper_no, pages, qcol, qnums, bounds, subs)

    print(f"[{sdir.name}] {doc.page_count} 页 · 题号列 x0={qcol} · 题数 {len(qs)} · "
          f"卷型 P{paper_no}{'(MCQ)' if PAPER_INFO[paper_no]['mcq'] else ''}")
    if args.analyze:
        for q in qs:
            nr = len(q.get("regions") or [])
            nm = len(q.get("material_regions") or [])
            ns = len(q.get("subquestions") or [])
            nh = len(q.get("option_hotzones") or [])
            print(f"  Q{q['number']:>2} sec={q['section'] or '-':1} {q['status']:>6} "
                  f"marks={q.get('total_marks','?'):>3} regions={nr} mat={nm} subs={ns:>2} hz={nh}")
        return
    if args.png:
        out = Path(args.png)
        out.mkdir(parents=True, exist_ok=True)
        for q in qs:
            rg = (q.get("material_regions") or []) + (q.get("regions") or [])
            pix = None
            parts = []
            for r in rg:
                pg = doc[r["page"]]
                clip = pymupdf.Rect(*r["bbox"])
                parts.append(pg.get_pixmap(dpi=110, clip=clip))
            import PIL.Image as Image
            ims = [Image.frombytes("RGB", (p.width, p.height), p.samples) for p in parts]
            W = max(i.width for i in ims)
            H = sum(i.height for i in ims) + 12 * (len(ims) - 1)
            canvas = Image.new("RGB", (W, H), "white")
            y = 0
            for i in ims:
                canvas.paste(i, (0, y))
                y += i.height + 12
            canvas.save(out / f"q{q['number']:0>2}.png")
        print(f"  PNG → {out}")
        return

    # ── 写盘 ──
    sha = hashlib.sha256(src.read_bytes()).hexdigest()
    cut_path = sdir / "cut.json"
    if cut_path.exists() and not args.force:
        old = json.loads(cut_path.read_text(encoding="utf-8"))
        if old.get("source", {}).get("sha256") != sha:
            sys.exit("source.pdf 的 sha256 与 cut.json 记录不符——源头已变，请人工处理。")
    (sdir / "assets").mkdir(exist_ok=True)

    year = int(year_m.group(1)) if year_m else 0
    session = sess_m.group(1).strip() if sess_m else ""
    base_id = f"cie-alevel-economics-{year}-{session}-{code}"
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
            "qualification": "alevel", "subject": "economics", "paper_code": code.upper(),
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
        "material_order": "before",
        "questions": out_qs,
    }
    cut_path.write_text(json.dumps(cut, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"  已写 cut.json（{len(out_qs)} 题）+ {len(out_qs)} 个 qNN.md")


if __name__ == "__main__":
    main()
