#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""cut_paper_cie_acct.py — CIE (9706) 会计卷按题自动切割

⚠️ 专用脚本（考试局 × 资格 × 学科各一份，2026-10-21 定规）：
    CIE / A Level / Accounting(9706)，只服务这一个学科。与 cut_paper_cie.py（物理 9702）/
    cut_paper_cie_eco.py（经济 9708）/ cut_paper_cie_chem.py（化学 9701）不共享代码、
    不互相 import；各自改动互不影响。产出与 cut_paper.py 同构（cut.json + qNN.md，
    符合 schema/cut.schema.json）。

CIE 9706 的版式（2026-10-22 实测 P1 43 份）：
  · P1（qp11–13）= 30 题 MCQ，30 分，1 小时，12 页；无作答区；选项 A–D 竖排或 2×2 网格。
  · P2/P3/P4 版式**尚未适配**，本脚本遇到会直接退出（别拿 P1 的刀去切结构化卷）。
  · 版式与 CIE 9708 完全同构（同一套 CIE 模板），故几何常量与题号/选项识别逻辑
    沿用 9708 的实测值：
      - 595×842 pt = 2020–2024；612×792 pt = 2025–2026（整版右移、字小一号）
      - 题号恒为「粗体 + 左栏行首数字」，x0≈49.6（595）/ 72.4（612）
      - 页脚 `06_9706_11_2024_1.8a | © UCLES 2024`，y≈789（595）/ 752（612）
      - 2025 起部分 span 是「整段 +0x1D 位移」，_decode_line 逐 span 还原
  · 选项热区：竖排按行横切、2×2 网格按四象限切；取题区内最后一次出现的 A/B/C/D。
  · 小问只记锚点不切子图（CIE 在云端子图黑名单里）。

用法:
    python scripts/cut_paper_cie_acct.py <session_dir>            # 切割 + 写 cut.json/qNN.md
    python scripts/cut_paper_cie_acct.py <session_dir> --analyze  # 只打印锚点表，不落盘
    python scripts/cut_paper_cie_acct.py <session_dir> --png DIR  # 另存每题 PNG 供视觉抽检

<session_dir> 指向 data/cie/alevel/accounting/<year>-<session>-qpNN/，
目录内须已放入 source.pdf 与 paper.md。
paper_code 约定：qp11–13 → Paper 1 (MCQ)。

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
SUB_RE = re.compile(r"^\(([a-h])\)")   # 9706 P2 会做到 (f)(g)(h)；(i) 留给罗马层
ROMAN_RE = re.compile(r"^\((i{1,3}|iv|v|vi{1,3})\)")
QSTART_RE = re.compile(r"^(\d{1,2})(?![0-9])")
OPT_RE = re.compile(r"^([A-D])$")
MARK_RE = re.compile(r"\[(\d{1,3})\]")
FOOT_BLOCK_RE = re.compile(r"^(Permission to reproduce|BLANK PAGE|Cambridge Assessment)", re.I)
INS_Q_RE = re.compile(r"^Question\s+(\d+)$")
INS_SRCQ_RE = re.compile(r"Source\b.*\bfor Question\s*(\d+)", re.I)
INS_DUP_RE = re.compile(r"^Answer the following questions in the question paper", re.I)
BARCODE_RE = re.compile(r"^\*\s*\d[\d ]*\*$")
FOOT_MARK_RE = re.compile(r"^(©|\[Turn over|Permission to reproduce|BLANK PAGE|\d{4}/\d{1,2}/|\d{2}_\d{4}_\d{2}_\d{4})", re.I)

PAPER_INFO = {
    "1": {"title": "Paper 1 Multiple Choice", "marks": 30, "mcq": True},
    "2": {"title": "Paper 2 Fundamentals of Accounting", "marks": 90, "mcq": False},
    "3": {"title": "Paper 3 Structured Questions", "marks": 150, "mcq": False},
    "4": {"title": "Paper 4 Cost and Management Accounting", "marks": 50, "mcq": False},
}
SUPPORTED = {"1", "2", "3", "4"}
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


def _is_junk_line(tx):
    """条形码字形行 / 控制字符行：不是正文，任何边界计算都要跳过。

    CIE 答案页的页眉页脚都有一条条形码，抽出来是一串高位拉丁字母
    （`ĬÑĉ¯Ġ³íÇùÙđÝĢÜó¸Ď×`）或控制字符，FOOT_MARK_RE 抓不到。
    """
    if not tx:
        return True
    if BARCODE_RE.match(tx):
        return True
    hi = sum(1 for c in tx if ord(c) > 127)
    ctrl = sum(1 for c in tx if ord(c) < 32 and c not in "\t\n\r")
    n = len(tx)
    return (hi / n > 0.4) or (ctrl / n > 0.2)


def footer_rule_y(doc, pages, pi):
    """版权块（Permission to reproduce…）上方那条通栏细线的 y；没有返回 None。

    CIE 有的页在版权块上方压一条 0 高度的通栏线，`_last_content_y` 会把它当内容，
    于是区域一路收到线下、尾部多出一大块空白（2023-june-qp32 的 Source C 页实测）。
    """
    if not any(FOOT_BLOCK_RE.match(r["text"]) for r in pages[pi]):
        return None
    page = doc[pi]
    W, H = page.rect.width, page.rect.height
    ys = []
    for d in page.get_drawings():
        rc = d["rect"]
        if rc.height <= 2 and rc.width >= W * 0.6 and rc.y0 > H * 0.6:
            ys.append(rc.y0)
    return min(ys) if ys else None


def cap_for(doc, pages, pi):
    """该页允许的收尾上限：页脚文字起点与页脚细线取小。"""
    cap = content_bottom(pages[pi])
    fr = footer_rule_y(doc, pages, pi)
    if fr is not None:
        cap = min(cap, fr)
    return cap


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
            if r["y0"] < GEOM["header_y"]:
                continue
            # 题号行 = 粗体；但 2024-oct 起的卷偶有字重丢失（bold=False），
            # 所以「整行就是个 1–2 位数」也认——页码在版心居中，列位置天然排除。
            if not r["bold"] and not (r["text"].strip().isdigit()
                                      and len(r["text"].strip()) <= 2):
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
            if len(rows) == 1:
                # 选项并排一行（A 1,2 and 4  B 2,3 and 4 …）：分界取「下一个标签左边-留白」，
                # 取两点中点会把 A/B/C 的选项文字切一半（2026-june-qp11 Q29/Q30 实测）
                xcut = [x0] + [round(cols[i + 1][0] - PAD - 2, 2)
                               for i in range(len(cols) - 1)] + [x1]
            else:
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
def _last_content_y(doc, pages, pi, y_from, y_cap, soft=False):
    """该页 y_from..y_cap 内最后一条内容的下沿（文本 + 图形；越界/超宽图形忽略）。"""
    y = y_from
    page = doc[pi]
    W, H = page.rect.width, page.rect.height
    for r in pages[pi]:
        if not (y_from - 1 <= r["y0"] < y_cap + (12 if soft else 0)):
            continue
        if FOOT_MARK_RE.match(r["text"]):
            continue
        # 只在「页脚带」（底部 58pt）里把条形码字形行当家具；页面中段的
        # 高位字符可能是 2025 卷的 +29 位移正文，不能一刀切（2025-oct-qp11 实测误伤 12 题）
        if r["y0"] > H - 58 and _is_junk_line(r["text"]):
            continue
        y = max(y, r["y1"])
    fr_y = footer_rule_y(doc, pages, pi)
    for d in page.get_drawings():
        rc = d["rect"]
        if rc.width <= 0 or rc.width > W * 0.95:
            continue
        if rc.x0 < -5 or rc.x1 > W + 5 or rc.y0 < 0 or rc.y1 > H:
            continue
        # 页脚家具（角落黑方块、底部条形码、套准线）在页底 55pt 内，不算内容——
        # 否则末段会收到 809 把方块切一半（2024-june-qp23 Q3 实测）。
        if rc.y0 > H - 55:
            continue
        # 版权块上方那条通栏细线本身也不算内容（否则区域被它拽到页脚，尾部一大块空白）
        if fr_y is not None and rc.y0 >= fr_y - 1:
            continue
        if y_from - 1 <= rc.y0 and rc.y1 <= y_cap + 1:
            y = max(y, rc.y1)
    # soft：页底场景，行首在 cap 之下就整行收进来（[7] 这类贴页脚的标记），
    # 但结果仍夹在 cap 上——cap 是页脚文字的起点，再多就切到页脚了
    return min(y_cap, round(y + 6, 2))


def furniture_bottom(pages, pi):
    """页眉家具（条形码数字串、条形码细条、左侧竖排 DO NOT WRITE）的下沿。
    没有则返回 0，调用方据此判断「该页要不要抬高起点」。"""
    bottom = 0.0
    for r in pages[pi]:
        if r["y1"] - r["y0"] >= 30 or r["y0"] >= 120:
            continue
        ctrl = sum(1 for c in r["text"] if ord(c) < 32 and c not in "\t\n\r")
        junk = ctrl / max(1, len(r["text"]))
        if "DO NOT WRITE IN THIS MARGIN" in r["text"]:
            continue          # 竖排页边文字，y 跨度大，不参与
        if junk > 0.2 or BARCODE_RE.match(r["text"]):
            bottom = max(bottom, r["y1"] + 3)
    return round(bottom, 1)


def header_skip(pages, pi):
    """有页眉家具（条形码/角标）的页：返回「第一行真正正文的上沿 - 3」。

    只按家具下沿抬高不够——条形码残渣、右上/左上角的黑方块还在 60–75 之间，
    会留在图顶上（2024-june-qp23 实测）。没有家具的页返回 0，调用方据此保持原样。
    """
    fb = furniture_bottom(pages, pi)
    if fb <= 0:
        return 0.0
    best = None
    for r in pages[pi]:
        if r["y0"] <= fb or r["y0"] > fb + 140:
            continue
        tx = r["text"]
        if FOOT_MARK_RE.match(tx) or BARCODE_RE.match(tx):
            continue
        if _is_junk_line(tx):
            continue
        if tx.isdigit() and len(tx) <= 2:
            continue
        if len(tx.strip()) < 2:
            continue
        best = r["y0"] if best is None else min(best, r["y0"])
    if best is None:
        return fb
    return round(max(fb, best - 3), 1)


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
    # 条形码等页眉家具（不排掉会印在题图正上方，2024-june-qp23 实测）
    top = max(top, header_skip(pages, pi))
    return round(top, 1)


def expand_top(pages, pi, ya, cap=22.0):
    """题号行上方压下来的内容（分式分子、上标）要一起收进来。

    题号行是「基线」，分式的分子可能比题号行还高——只按题号行上留 PAD 会把
    分子削顶（2021-june-qp12 Q19 的 $20200/$5100 实测切掉顶部 3pt）。
    只收「纵向与题号行有重叠」的正文行（左边界在题号列右侧），不回溯到上一题。
    """
    top = ya
    for r in pages[pi]:
        if r["x0"] <= GEOM["x_left"] + 20:
            continue
        if r["y1"] > ya + 0.5 and r["y0"] < ya:
            top = min(top, r["y0"])
    if top < ya:
        top = max(top - 1.0, ya - cap)
    return round(top, 2)


def regions_between(pages, doc, a, b):
    """a=(page,y) 起，b=(page,y) 止（不含 b 行），跨页自动补中间整页。"""
    pa, ya = a
    pb, yb = b
    ya = expand_top(pages, pa, ya)
    # 顺序要紧：expand_top 会向上扩 22pt，可能把起点又拉回条形码里（2024-june-qp23 实测）
    ya = max(ya, header_skip(pages, pa))
    if pa == pb:
        if yb - ya < 4:
            return []
        # 同页也要收到「最后一行内容」为止：末题没有下一题/分节可当边界时，
        # 否则会一路拉到页底，图下方留一大片白（2023-oct-qp11 Q30 曾留 511pt）。
        bot = _last_content_y(doc, pages, pa, ya, min(yb, cap_for(doc, pages, pa)))
        if bot - ya < 4:
            return []
        return [{"page": pa, "bbox": [GEOM["x_left"], round(ya, 2), GEOM["x_right"], bot]}]
    # 注意：不要在这里加「末页落在页首 → 只切首页」的短路。
    # 结构卷（P2/P3/P4）一道题能横跨 5–6 页，下一题的题号恰好也在页首，
    # 短路会把整道题压成一张首页图（2026-10-22 实测 s24-qp21 的 Q1 只切到第 1 页）。
    bot = _last_content_y(doc, pages, pa, ya, cap_for(doc, pages, pa), soft=True)
    out = [{"page": pa, "bbox": [GEOM["x_left"], round(ya, 2), GEOM["x_right"], bot]}]
    for p in range(pa + 1, pb):
        body = [r for r in pages[p] if r["y0"] >= GEOM["header_y"] and not FOOT_MARK_RE.match(r["text"])]
        if not body:                      # 空白页 / 纯版权页跳过
            continue
        ptop = page_top(doc, pages, p)
        out.append({"page": p, "bbox": [GEOM["x_left"], ptop, GEOM["x_right"],
                                        _last_content_y(doc, pages, p, ptop,
                                                        cap_for(doc, pages, p), soft=True)]})
    # 末页若整页只有 BLANK PAGE / 版权块（末题止于页底时的常见情形），不产碎片——
    # 否则会切出一大块空白 + "BLANK PAGE" 字样（2024-june-qp11 Q30 实测）。
    pb_body = [r for r in pages[pb]
               if r["y0"] >= GEOM["header_y"] and not FOOT_MARK_RE.match(r["text"])
               and r["y0"] < yb]
    if not pb_body:
        return out
    # 末页同样收到「最后一行内容」为止：直接写 yb 会把 [Total: 25] 之后的整段空白带进图里
    ptop = page_top(doc, pages, pb)
    pbot = _last_content_y(doc, pages, pb, ptop, min(yb, cap_for(doc, pages, pb)), soft=True)
    if pbot - ptop >= 12:
        out.append({"page": pb, "bbox": [GEOM["x_left"], ptop, GEOM["x_right"], round(pbot, 2)]})
    # 丢掉不足以成图的薄片（<12pt）：换页时常见「下一题题号紧贴页首」留下一两条
    # 4pt 级别的碎条，既没内容又会和下一题的区域重叠（2024-june-qp23 实测 18pt 重叠）。
    out = [r for r in out if r["bbox"][3] - r["bbox"][1] >= 12]
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
def insert_materials(doc, pages, insert_start, paper_no):
    """INSERT 段 → {题号: [region]}。资料拼在题面之前（material_order=before）。

    插入册每题的版式：`Question N` / `Source X for Question N` 标题起，后面跟数据表；
    2020–2022 版在题末还印一段「Answer the following questions in the question paper.
    Questions are printed here for reference only.」＋(a)(b)(c)＋Additional information——
    这段是与 QP 重复的题干，必须截掉（培琪 2026-10-22 定）。
    """
    if insert_start is None:
        return {}
    n = doc.page_count
    heads = []
    for pi in range(insert_start, n):
        for r in pages[pi]:
            s = r["text"].strip()
            m = INS_Q_RE.match(s) or INS_SRCQ_RE.search(s)
            if m:
                heads.append((pi, r["y0"], int(m.group(1))))
    heads.sort()
    out = {}
    for i, (pi, y, qno) in enumerate(heads):
        end_pi = heads[i + 1][0] if i + 1 < len(heads) else n
        regs = []
        dup = False
        for p in range(pi, end_pi):
            if dup:
                break            # 重复题干一旦开始，本 section 后面所有页都是重复内容
            top = page_top(doc, pages, p)
            if p == pi:
                top = max(top, round(y - 6, 1))
            cap = cap_for(doc, pages, p)
            dup_y = None
            for r in pages[p]:
                if r["y0"] > top and INS_DUP_RE.match(r["text"].strip()):
                    dup_y = r["y0"] - 5
                    dup = True     # 本页截断，且不再收后面的页
                    break
            bot = _last_content_y(doc, pages, p, top, cap, soft=True)
            if dup_y is not None:
                bot = min(bot, dup_y)
            if bot - top >= 18:
                regs.append({"page": p,
                             "bbox": [GEOM["x_left"], round(top, 2), GEOM["x_right"], round(bot, 2)]})
        if regs:
            out[qno] = regs
    return out


def analyse(doc, qp_pages=None):
    """qp_pages: QP 本体页数（其余为 INSERT 插页）；None 表示没有插页。"""
    setup_geom(doc)
    pages = [page_lines(pg) for pg in doc]
    # 题号/边界/小问只在 QP 本体里找；INSERT 插页的页码和 Question N 标题会污染锚点
    qp = pages if qp_pages is None else pages[:qp_pages]
    qcol = detect_qcol(qp)
    qnums = find_qnums(qp, qcol)
    bounds = boundary_events(qp, doc, qcol)
    subs = find_subs(qp, qcol)
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
            # 9706 的 P2–P4：情景/数据表与题目本来就连着，**不拆 material_regions**，
            # 免得渲染层插「上方为资料，下方为题目」的分隔提示（培琪 2026-10-22 定）。
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
    if paper_no not in SUPPORTED:
        sys.exit(f"9706 P{paper_no} 版式尚未适配（本脚本当前只支持 P1 选择题卷）。")

    ins_m = re.search(r"^insert_start_page:\s*(\d+)", fm, re.M)
    ins_start = int(ins_m.group(1)) if ins_m else None

    doc = pymupdf.open(src)
    pages, qcol, qnums, bounds, subs = analyse(doc, ins_start)
    qs = build(doc, paper_no, pages, qcol, qnums, bounds, subs)

    # 插页资料（P3/P4）：按 Question N 归到对应题，拼在题面之前
    mats = insert_materials(doc, pages, ins_start, paper_no)
    for q in qs:
        mr = mats.get(int(q["number"]))
        if mr:
            q["material_regions"] = mr

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
    # 清掉目录里已不该存在的 qNN.md（曾因题数从 7 变 6 留下 q07.md 的假题）
    keep_nums = {int(q["number"]) for q in qs}
    for stale in sdir.glob("q*.md"):
        m2 = re.fullmatch(r"q(\d{2})\.md", stale.name)
        if m2 and int(m2.group(1)) not in keep_nums:
            stale.unlink()
            (sdir / "assets" / ("q%s.webp" % m2.group(1))).unlink(missing_ok=True)
            (sdir / "assets" / ("q%s.layout.json" % m2.group(1))).unlink(missing_ok=True)

    year = int(year_m.group(1)) if year_m else 0
    session = sess_m.group(1).strip() if sess_m else ""
    base_id = f"cie-alevel-accounting-{year}-{session}-{code}"
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
            "qualification": "alevel", "subject": "accounting", "paper_code": code.upper(),
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
    # 只有真有 material_regions 的卷才写 material_order（9706 现在不拆资料，故通常不写）
    if any(q.get("material_regions") for q in out_qs):
        cut["material_order"] = "before"
    cut["questions"] = out_qs
    cut_path.write_text(json.dumps(cut, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"  已写 cut.json（{len(out_qs)} 题）+ {len(out_qs)} 个 qNN.md")


if __name__ == "__main__":
    main()
