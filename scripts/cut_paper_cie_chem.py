#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""cut_paper_cie_chem.py — CIE (9701) 化学卷按题自动切割

⚠️ 专用脚本（考试局 × 资格 × 学科各一份，2026-10-17 定规）：
    CIE / A Level / Chemistry(9701)，只服务这一个学科。
    与 cut_paper_cie.py（CIE 物理 9702）**不共享代码、不互相 import**；
    物理那卷的乱码解码、页尾策略等改动不会流到这里，反之亦然。
    产出与 cut_paper.py 完全同构（cut.json + qNN.md，符合
    schema/cut.schema.json），可直接进 render_questions.py / validate_cut.py / 打标台。

CIE 9701 化学的版式（2026-10-18 实测 2016–2026 共 360 份、入库 280 份）：
    · P1 = 40 题 MCQ（1 分/题）；P2 = 10 题 60 分；P4 = 10 题 100 分；
      P5 = 2 题 30 分（Planning, Analysis and Evaluation）。
      **P3（qp31–38，实验操作卷）不切割、不入库**。
    · 题号行恒为独立行，且**带制表符**："1\\t" / "11\\t"（物理多为 "1 "）；
      题号列 x0 老卷 49.6、2024+ 卷 72.8，按各卷众数自适应（QCOL）。
    · 题号下一行才是首个小问（"(a)\\t \\x07Complete the table…"），
      子问层两级：(a)(b)… → (i)(ii)…，也可能同行 (a)(i)。
    · 分值：行内 [1] [2] 贴在作答点线右端（x1≈545.7），每题末一条右对齐
      [Total: N]（x0≈498–504）。P2/P4/P5 十年全有，合计恰等于卷面总分。
    · 无答题册：直接答在题卷点线上，answer_booklet / answer_regions 不存在。
    · 封面 + 说明页（P1 2 页 / P2、P4 3 页），夹 BLANK PAGE 页，序列约束天然跳过。

    · **化学专属乱码（与物理不同）**：正文 span 里夹 `\\x07`（段首不换行空格）
      与 `\\x08`（封面标题后的制表符），这是**正常卷**的占位符，不能当乱码解码；
      而 2023+ 部分渠道卷整卷 −0x1D 位移（空格→\\x03），需整卷判定后统一还原。
      化学按「整卷 ctrl 统计」判定，而不是物理的逐 span 判据 —— 否则正常卷的
      `\\x07` 会被误当乱码，整行 "The…" 变成 "6kh…"。
    · **不解码就切不动的卷一律不硬切**（用户定规：乱码卷交给视觉）：
      题号序列断裂 / 解码后仍有大量控制字符 / 无文本层（扫描卷）→ 退出码 3，
      打印 `NEEDS_VISION`，不写 cut.json、不写 qNN.md。

坐标约定（全仓库统一）: PDF 点 pt，页面左上角原点，y 向下；bbox=[x0,y0,x1,y1]。

用法:
    python scripts/cut_paper_cie_chem.py <session_dir>

<session_dir> 指向 data/cie/alevel/chemistry/<year>-<session>-qpNN/，
目录内须已放入 source.pdf 与 paper.md（含 total_marks 更佳）。
paper_code 约定：qp11–14 → Paper 1 (MCQ)；qp21–24 → Paper 2；
                qp41–44 → Paper 4；qp51–54 → Paper 5。qp3x 直接拒绝（实验卷）。

重跑保护: 与 cut_paper.py 相同——source.sha256 不符硬停；
status=verified/manual 的题其 regions/anchors 不被自动结果覆盖。
"""

import argparse
import hashlib
import json
import re
import sys
from collections import Counter
from pathlib import Path

import pymupdf

# Windows 控制台默认 GBK：化学正文里的 − (U+2212)、Δ、⇌ 等字符会让 print 抛
# UnicodeEncodeError 而中断整卷（2023–2025 共 20 卷踩到）。强制 UTF-8 输出。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# ── 排版常量（CIE 9701 化学实测） ────────────────────────────────────────────
HEADER_Y = 50.0     # 页眉安全线：页码在 y0≈34.1（高 11.5）→ y1≈45.7，条码 <50
FOOTER_Y = 790.0    # 页脚：© UCLES / 卷号 y0≈799.8，[Turn over y0≈795.8
QCOL_SLACK = 4.0    # 题号行 x0 允许偏离题号列的余量（同卷内恒定，差 >5 就是表格序号列）
SUBQ_SLACK = 70.0   # 小问标签行 x0 允许偏离题号列的余量（嵌套 (i) 缩进 ~46pt）
TOTAL_RE = re.compile(r"\[\s*Total\s*:?\s*(\d+)\s*\]")
MARK_RE = re.compile(r"\[(\d{1,2})\]")
# 化学题号行恒为独立行且**带制表符**："1\t" / "11\t"；个别年有 "(a)" 同行型。
# 第二分支的分隔符保留物理那套 [ \t=~]，化学 2016–2019 未见到 '=' 错位，留着无妨。
QSTART_RE = re.compile(
    r"^(\d{1,2})\s*(?:\(\s*[a-z]{1,4}\s*\)\s*(.*))?$"
    r"|^(\d{1,2})[ \t=~]+(\S.*)$")
SUBHEAD_RE = re.compile(r"^(\*?)((?:\([a-z]{1,4}\)[\t\u0020\u00a0\u2009]*){1,3})[\t\u0020\u00a0\u2009]*(.*)$")
OPTION_LABEL_RE = re.compile(r"^([A-D])$")
SPACE_FOR_WORKING_RE = re.compile(r"space\s+for\s+working", re.I)
ROMAN_SUB = {"ii", "iii", "iv", "vi", "vii", "viii", "ix", "xi", "xii"}
ROMAN_TOKENS = ROMAN_SUB | {"i", "v", "x"}
JUNK_RE = re.compile(r"^[\s,.\-*\u0000-\u001f]*$")

BOARDS = {"edexcel", "aqa", "cie"}
QUALS = {"alevel", "a-level", "igcse", "gcse"}
SESSION_TOKENS = {"jan", "mar", "may-jun", "june", "oct", "oct-nov"}
SCRIPT_OWNED_KEYS = ("total_marks", "question_type", "section", "image", "layout", "cut_status")

# paper_code → (paper 号, 卷面总分, 题数)
PAPER_OF_CODE = {}
for _p, _tot, _nq, _vs in ((1, 40, 40, (1, 2, 3, 4)), (2, 60, 10, (1, 2, 3, 4)),
                           (4, 100, 10, (1, 2, 3, 4)), (5, 30, 2, (1, 2, 3, 4))):
    for _v in _vs:
        PAPER_OF_CODE[f"qp{_p}{_v}"] = (_p, _tot, _nq)

TRIM_MIN_GAP = 20.0
TRIM_PAD = 8.0
TRIM_CAP = 120.0    # 末段散文封顶（≈4 行，化学 11pt 行距 26–32pt）

# 化学正常卷的占位控制符：\x07 = 段首不换行空格，\x08 = 封面标题后的制表符。
# 二者**不是乱码**，绝不能按 −0x1D 还原（否则 "The" → "6kh"）。
PLACEHOLDER_CTRL = {"\x07": " ", "\x08": "", "\x01": "", "\x02": ""}
# 整卷判定为「−0x1D 位移卷」的阈值：空格被压成 \x03，干净卷这个数是 0。
SHIFT_SIG_COUNT = 5

# ── A 型乱码解码（span 分治） ────────────────────────────────────────────────
# 渠道加工卷把整卷字符统一 −0x1D 位移：UCLES→8&/(6、空格→\x03：
#   化学在 2023 暑季起出现（s23/s24/s25/w23/w24/w25 与 m23–m26 部分卷），
#   同一卷要么整卷位移、要么整卷干净，**没有物理那种部分 span 错位**。
#   因此按整卷 ctrl 统计判定，不做逐 span 词频猜测——化学的 \x07/\x08 是
#   正常占位符，逐 span 判据会把 "The…" 还原成 "6kh…"。
COMMON_WORDS = frozenset("""
the of in is to on and that with for as at this which what are from by an be or not it
state shown shows figure diagram calculate determine explain sketch complete suggest
define describe energy force mass speed velocity time current voltage charge field wave
light object table graph point between particles electrons atoms molecules circuit power
work amount change initial final constant uniform horizontal vertical temperature pressure
volume resistance capacitor momentum acceleration distance weight spring wire cell battery
proton neutron nucleus radiation frequency wavelength amplitude period height angle
direction motion moving travels travelling released raised decreases increases maximum
minimum ratio value units correct statement question answer marks total continue blank
page chemistry paper candidate centre name number alpha beta gamma metal glass water block
acid base salt ion bond molecule atom reaction solution mixture compound organic
oxidation reduction electrode cell enthalpy entropy equilibrium rate catalyst isomer
hydrocarbon alcohol halogen alkane alkene polymer ester amine amide amino acid protein
mole mass concentration volume gas liquid solid aqueous state symbol equation formula
structure displayed skeletal mechanism curve graph table calculation percentage yield
""".split())


def decode_shift(text):
    """+0x1D 位移还原。

    控制字符只还原 ord∈[0x03,0x1F] 的（能映射回可打印区；\t 与
    \x01/\x02 是点线装饰的本征字符，映射结果 <0x20，还原无意义）；
    其余字符越界保留原样。
    """
    out = []
    for c in text:
        o = ord(c)
        if o < 0x20:
            if c != "\t" and o >= 0x03:
                o += 0x1D
        else:
            o += 0x1D
        out.append(chr(o) if 0x20 <= o <= 0x7E else c)
    return "".join(out)


def word_hits(text):
    return sum(1 for w in re.findall(r"[A-Za-z]{2,}", text.lower()) if w in COMMON_WORDS)


def scan_shift_flag(doc):
    """整卷判定是否为 −0x1D 位移卷。

    判据：全卷 ctrl 中 \\x03（被压的空格）出现 ≥ SHIFT_SIG_COUNT 次。
    干净卷的 \\x03 计数恒为 0（实测 2016–2022 全部 0），位移卷 200–3000。
    返回 (是否位移, \\x03 计数, 其余异常控制符计数)。

    ⚠️ **只统计含字母的 span**（2026-10-18 踩过）：新渠道的干净 A4 源，页眉有条
    防伪码串 `,\\x01×11\\x03,`，落在 y≈54–59，跟正文起点只差 0.7pt，靠 y 分不掉；
    每页一条、7 页就凑够 8 个 \\x03，于是整卷被误判成位移卷 → 整卷 decode_shift
    把本来正常的正文翻成乱码 → 退出码 3 打 NEEDS_VISION。
    这类装饰串**一个字母都没有**，而真位移卷的正文 span 一定有字母，用它区分最稳。
    """
    n_x03 = 0
    n_other = 0
    for page in doc:
        for block in page.get_text("dict")["blocks"]:
            if block.get("type") != 0:
                continue
            for ln in block["lines"]:
                for s in ln["spans"]:
                    txt = s["text"]
                    if not re.search(r"[A-Za-z]", txt):
                        continue        # 装饰/防伪码串，不参与整卷位移判定
                    for c in txt:
                        o = ord(c)
                        if o < 0x20 and c != "\t":
                            if o == 0x03:
                                n_x03 += 1
                            elif c not in PLACEHOLDER_CTRL:
                                n_other += 1
    return n_x03 >= SHIFT_SIG_COUNT, n_x03, n_other


def clean_text(text, shifted=False):
    """单 span 文本清洗。

    ⚠️ 化学的乱码**不是整卷统一的**：同一份卷里正文 span 可能 −0x1D 位移，
    而题号/表格数字仍是正常编码（2026 qp12、2023 qp12 实测）。整卷一刀切
    会把正常题号 '1'→'N'，题号序列直接崩。
    所以按 **span** 判：含 [\x03–\x1F]（\t / 化学占位符 \x07\x08 除外）→ 该 span 位移还原；
    否则只把 \x07 换空格、\x08 与残余控制符丢掉。
    """
    if not text:
        return ""
    # ⚠️ 纯控制符装饰串直接丢（2026-10-18 踩过）：新渠道干净源的页眉有防伪码
    # `,\x01×11\x03,`，含 \x03 会走下面的 decode_shift，被翻成一串不可打印字符，
    # 于是「解码后仍有异常控制符」判据报 15 行 / 2.3%、整卷退出 3。
    # 这类串一个字母都没有；真正的位移正文一定有字母，用它区分。
    if (not re.search(r"[A-Za-z]", text)
            and any(ord(c) < 0x20 and c != "\t" and c not in ("\x07", "\x08")
                    for c in text)):
        return ""
    if any(0x03 <= ord(c) < 0x20 and c != "\t" and c not in ("\x07", "\x08")
           for c in text):
        return decode_shift(text)
    out = []
    for c in text:
        if c in PLACEHOLDER_CTRL:
            out.append(PLACEHOLDER_CTRL[c])
        elif ord(c) < 0x20 and c != "\t":
            out.append("")          # 残余控制符（点线装饰本征字符）一律丢弃
        else:
            out.append(c)
    return "".join(out)


# ── 页面文本结构提取 ────────────────────────────────────────────────────────

def build_page_lines(page, shifted=False):
    """一页的全部横排文本行（整卷位移标志统一解码后），按 (y0, x0) 排序。

    ⚠️ 化学不能按 strip() 判空：位移卷的题号 span 形如 "\x14"（'1'−0x1D），
    0x1C–0x1F 恰是 Python 空白字符 → strip() 后为空、整行题号被丢。
    改为「非空字符串即收」，行级再判可见性。
    ⚠️ 干净卷的 \x07 是段首不换行空格，必须换成空格再判空，否则
    "(a)\t \x07Complete…" 这行会只剩 "(a)\t " → 小问文本全丢。
    """
    lines = []
    for block in page.get_text("dict")["blocks"]:
        if block.get("type") != 0:
            continue
        for ln in block["lines"]:
            if tuple(ln.get("dir", (1, 0))) != (1.0, 0.0):
                continue
            spans = [s for s in ln["spans"] if s["text"]]
            if not spans:
                continue
            parts = []
            for s in ln["spans"]:
                parts.append(clean_text(s["text"], shifted))
            raw = "".join(parts)
            # 行级可见性：剥掉控制字符后仍须有可见字符（纯点线/装饰行丢弃）
            if not re.sub(r"[\x00-\x1f]", "", raw).strip():
                continue
            lines.append({
                "bbox": list(ln["bbox"]),
                "raw": raw,
                "bold": any("bold" in s["font"].lower() for s in ln["spans"]),
                "size": max(s["size"] for s in ln["spans"]),
                "decoded": shifted,
            })
    lines.sort(key=lambda l: (round(l["bbox"][1], 1), round(l["bbox"][0], 1)))
    return lines


# 页脚行按内容过滤（y 阈值兜底）：各年页脚 y 不一（750.5 / 793），
# 文本特征恒定：版权行、卷号行、[Turn over、整页占位 BLANK PAGE。
FOOTER_LINE_RE = re.compile(
    r"^(?:\xa9?\s*UCLES|\d{4}/\d{2}/|\[?\s*Turn over\s*\]?)", re.I)
BLANK_PAGE_RE = re.compile(r"^BLANK PAGE$", re.I)
# 卷末版权声明（7pt 小字，4 段，每卷末页都有）与周期表附录页。
# 它们不是题目内容：不滤会让最后一题的 region 一路吃到页底，
# 把整段版权声明 + 空白一起切进题图（2016 qp12 Q40 实测）。
COPYRIGHT_RE = re.compile(
    r"^(?:Permission to reproduce items|reasonable effort has been made|"
    r"publisher will be pleased to make amends|be pleased to make amends|"
    r"effort has been made by the publisher|To avoid the issue of disclosure|"
    r"Examinations Copyright Acknowledgements Booklet|"
    r"the live examination series\.|Cambridge International Examinations is part|"
    r"Examinations Syndicate \(UCLES\))", re.I)
APPENDIX_RE = re.compile(r"^The Periodic Table of Elements$", re.I)


# 化学答题点线（"................"）是**文本行**，不是矢量线——物理卷才是矢量。
# 它是作答区边界，必须留在 pages_content 里参与页尾裁剪与 layout 定位；
# 只有写进 qNN.md 正文时才滤掉。
DOTS_RE = re.compile(r"^[.\u00b7\s]{3,}$|^[.][\s.]{10,}$")


def content_lines(lines, footer_y=None):
    """版心内容行：滤掉页眉（页码/条码）、页脚（UCLES/卷号/Turn over/水印/
    BLANK PAGE 占位）、装饰垃圾（', ,' 等）。footer_y 缺省用全局 FOOTER_Y。

    ⚠️ **点线行保留**（打 is_dots 标记）：化学的作答横线就是文本点线，
    用 JUNK_RE 一并滤掉会让页尾裁剪以「上一行文字」为末项、把整块答题
    区切掉。纯装饰行（全是逗号/星号）照旧丢弃。
    """
    fy = FOOTER_Y if footer_y is None else footer_y
    out = []
    for ln in lines:
        b = ln["bbox"]
        if b[1] < HEADER_Y or b[1] > fy:
            continue
        raw = ln["raw"].strip()
        if not raw:
            continue
        if JUNK_RE.fullmatch(raw):
            if DOTS_RE.match(raw):
                ln = dict(ln, is_dots=True)
                out.append(ln)
            continue
        if FOOTER_LINE_RE.match(raw) or BLANK_PAGE_RE.match(raw):
            continue
        if COPYRIGHT_RE.match(raw) or APPENDIX_RE.match(raw):
            continue
        if DOTS_RE.match(raw):
            ln = dict(ln, is_dots=True)
        out.append(ln)
    return out


def assign_words_to_lines(lines, words):
    """get_text('words') 的词按中心点归入横排行；返回 [(word, line_ref), ...]。"""
    out = []
    for w in words:
        cx, cy = (w[0] + w[2]) / 2.0, (w[1] + w[3]) / 2.0
        hit = None
        for ln in lines:
            b = ln["bbox"]
            if b[0] <= cx <= b[2] and b[1] <= cy <= b[3]:
                hit = ln
                break
        out.append((w, hit))
    return out


# ── 通用小工具（与 cut_paper.py 同名同义） ──────────────────────────────────

def r2(v):
    return round(float(v), 2)


def pct(sorted_vals, q):
    if not sorted_vals:
        return 0.0
    return sorted_vals[min(int(len(sorted_vals) * q), len(sorted_vals) - 1)]


def yaml_scalar(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    s = str(v)
    if s == "":
        return '""'
    if re.fullmatch(r"[A-Za-z0-9_/.:+-]+", s):
        return s
    return json.dumps(s, ensure_ascii=False)


def build_regions(start_page, sy0, end_page, end_y1, L, T, R, B, bottoms=None):
    """跨页 region 拼装。bottoms[pi] = 该页有效下界（版权段/附录上方），缺省 B。"""
    def bot(pi):
        return B if bottoms is None else min(B, bottoms.get(pi, B))
    if end_page == start_page:
        return [{"page": start_page,
                 "bbox": [r2(L), r2(sy0), r2(R), r2(min(end_y1, bot(end_page)))]}]
    regs = [{"page": start_page, "bbox": [r2(L), r2(sy0), r2(R), r2(bot(start_page))]}]
    for mp in range(start_page + 1, end_page):
        regs.append({"page": mp, "bbox": [r2(L), r2(T), r2(R), r2(bot(mp))]})
    regs.append({"page": end_page,
                 "bbox": [r2(L), r2(T), r2(R), r2(min(end_y1, bot(end_page)))]})
    return regs


def page_content_rects(doc, pi, cache, footer_y=None):
    """一页内矢量图形与图片块的矩形 [(y1, y0, x0, x1), ...]。"""
    fy = FOOTER_Y if footer_y is None else footer_y
    if pi not in cache:
        rows = []
        try:
            for dr in doc[pi].get_drawings():
                db = dr["rect"]
                if (db.width <= 0 and db.height <= 0) or db.y1 > fy:
                    continue
                if db.y0 < HEADER_Y:
                    continue
                rows.append((db.y1, db.y0, db.x0, db.x1))
        except Exception:
            pass
        for b in doc[pi].get_text("dict")["blocks"]:
            if b.get("type") == 1 and HEADER_Y <= b["bbox"][1] <= fy:
                bb = b["bbox"]
                rows.append((bb[3], bb[1], bb[0], bb[2]))
        cache[pi] = rows
    return cache[pi]


def last_content(doc, rg, pages_content, content_cache, footer_y=None):
    """region 内最低一项内容 → (y_bottom, kind, text)。

    kind: "text" 正文 / "dots" 作答点线 / "line" 矢量（表格线、图形）/ "image"。
    ⚠️ 化学的点线就是作答横线，kind="dots" 必须算内容且**不许封顶保留**：
    动它等于把答题区切掉一半。
    """
    bb = rg["bbox"]
    last = (None, None, "")
    for ln in pages_content[rg["page"]]:
        lb = ln["bbox"]
        if lb[3] < bb[1] - 1 or lb[1] > bb[3] + 1:
            continue
        if lb[2] < bb[0] or lb[0] > bb[2]:
            continue
        if last[0] is None or lb[3] > last[0]:
            kind = "dots" if ln.get("is_dots") else "text"
            last = (lb[3], kind, (ln["raw"] or "").strip())
    for y1, _y0, x0, x1 in page_content_rects(doc, rg["page"], content_cache, footer_y):
        if y1 < bb[1] - 1 or y1 > bb[3] + 1:
            continue
        if x1 < bb[0] or x0 > bb[2]:
            continue
        if last[0] is None or y1 >= last[0]:
            last = (y1, "line", "")
    return last


def trim_regions(doc, regions, pages_content, content_cache, footer_y=None, log=None,
                 allow_cap=True):
    """页尾空白裁剪（原地修改），返回 (裁剪段数, 封顶段数)。

    化学的统一规则（不分「锚点终点 / 版心底兜底」——化学的 [Total] 之后
    仍然常剩半页排版空白，物理那套「锚点终点不裁」在这里会留下大块白）：
      · 末项是作答点线 / 矢量线 / 表格 → 硬裁到 末项 + TRIM_PAD；
      · 末项是正文散文（≥4 词、非选项行）且是末段 → 封顶保留
        min(B, 末项 + TRIM_CAP)，给"还没印横线的作答"留 4 行；
        中间段一律硬裁（题续下页，本页尾部空白无意义）。
      · gap ≤ TRIM_MIN_GAP 不动（本来就贴边）。
    """
    n_trim = n_cap = 0
    for i, rg in enumerate(regions):
        is_last = i == len(regions) - 1
        last_y, kind, text = last_content(doc, rg, pages_content, content_cache, footer_y)
        if last_y is None:
            continue
        gap = rg["bbox"][3] - last_y
        if gap <= TRIM_MIN_GAP:
            continue
        prose = (kind == "text" and len(text.split()) >= 4
                 and not OPTION_LABEL_RE.match(text)
                 and not TOTAL_RE.search(text))
        if allow_cap and is_last and prose:
            new_y1 = min(rg["bbox"][3], last_y + TRIM_CAP)
            if new_y1 > rg["bbox"][1] + 20:
                if new_y1 < rg["bbox"][3] - 0.5:
                    n_cap += 1
                if log is not None:
                    log.append(("cap", rg["page"], rg["bbox"][3], last_y, kind, text[:28]))
                rg["bbox"] = [rg["bbox"][0], rg["bbox"][1], rg["bbox"][2], r2(new_y1)]
                n_trim += 1
        else:
            new_y1 = last_y + TRIM_PAD
            if new_y1 > rg["bbox"][1] + 20:
                if log is not None:
                    log.append(("cut", rg["page"], rg["bbox"][3], last_y, kind, text[:28]))
                rg["bbox"] = [rg["bbox"][0], rg["bbox"][1], rg["bbox"][2], r2(new_y1)]
                n_trim += 1
    return n_trim, n_cap


def parse_subhead(raw):
    """解析 "(a)(i) …" 小问标签行 → (tokens, tail) 或 None。多字符 token 只收罗马数字。"""
    m = SUBHEAD_RE.match(raw.strip())
    if not m:
        return None
    tokens = re.findall(r"\(([a-z]{1,4})\)", m.group(2))
    if any(len(t) > 1 and t not in ROMAN_SUB for t in tokens):
        return None
    return tuple(tokens), m.group(3).strip()


def sub_label(tokens):
    return " ".join(tokens)


# ── 主流程 ──────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="CIE 9701 化学卷按题切割")
    ap.add_argument("session_dir", help="如 data/cie/alevel/physics/2025-june-qp21")
    args = ap.parse_args()
    sdir = Path(args.session_dir).resolve()
    if not sdir.is_dir():
        sys.exit(f"目录不存在: {sdir}")

    try:
        board = sdir.parents[2].name
        qualification = sdir.parents[1].name
        subject = sdir.parents[0].name
        year_s, session, paper_code = sdir.name.split("-", 2)
        year = int(year_s)
        assert board in BOARDS and qualification in QUALS and session in SESSION_TOKENS
    except Exception:
        sys.exit(f"session 目录路径不符合 data/<board>/<qual>/<subject>/<year>-<session>-<paper>/ 约定: {sdir}")
    if board != "cie":
        sys.exit(f"cut_paper_cie_chem.py 只服务 CIE 卷（board={board}）")
    if subject != "chemistry":
        sys.exit(f"cut_paper_cie_chem.py 只服务 CIE 化学（subject={subject}）"
                 f"——物理请用 cut_paper_cie.py")
    paper_code_l = paper_code.lower()
    if paper_code_l not in PAPER_OF_CODE:
        sys.exit(f"不支持的 paper_code: {paper_code}（只支持 qp11–14 / qp21–24 / "
                 f"qp41–44 / qp51–54；P3 实验卷不切割）")
    paper_num, paper_total, paper_nq = PAPER_OF_CODE[paper_code_l]
    is_mcq_paper = paper_num == 1

    src = sdir / "source.pdf"
    if not src.exists():
        sys.exit(f"缺少 source.pdf: {src}")
    sha = hashlib.sha256(src.read_bytes()).hexdigest()

    paper_total_marks = None
    paper_md = sdir / "paper.md"
    if paper_md.exists():
        m = re.search(r"^total_marks:\s*(\d+)", paper_md.read_text(encoding="utf-8"), re.M)
        if m:
            paper_total_marks = int(m.group(1))

    doc = pymupdf.open(src)
    n_pages = doc.page_count
    # 页眉安全线按页高换算：A4/Letter 仍是 50，A3（1191pt）抬到 70——A3 卷的
    # 页码行 y1 到 67.5，用 50 会把页码切进题图顶部。
    global HEADER_Y
    HEADER_Y = max(HEADER_Y, doc[0].rect.height * 0.059)

    # ── 整卷乱码判定（化学：整卷位移或整卷干净，无部分错位） ──
    shifted, n_x03, n_other_ctrl = scan_shift_flag(doc)

    pages_lines, pages_content, pages_pairs = [], [], []
    for page in doc:
        pages_lines.append(build_page_lines(page, shifted))
    if shifted:
        print(f"    −0x1D 位移卷：整卷解码（\\x03×{n_x03}）")
    elif n_other_ctrl:
        print(f"    干净卷（占位控制符 \\x07/\\x08 已换空白；残余异常控制符 {n_other_ctrl} 个）")
    # 页脚 y 自适应：© UCLES 版权行 y 众数 − 1.5（每页都有页脚 → 众数即正文
    # 页脚位置；封面页版权行 y 可能不同，不能取 min）。化学正文页 799.8。
    # 页脚上沿：© UCLES / 卷号 / [Turn over 三者的最小 y（[Turn over 比版权行高
    # 十几 pt，只看 UCLES 会让 A3 卷的 [Turn over 落进最后一题的图底）。
    ucles_ys, turn_ys = [], []
    for lines in pages_lines:
        for ln in lines:
            raw = ln["raw"].strip()
            if "UCLES" in raw:
                ucles_ys.append(round(ln["bbox"][1]))
            elif raw.startswith("[Turn over") or raw.startswith("Turn over"):
                turn_ys.append(round(ln["bbox"][1]))
    # 各自取众数（封面页的 y 与正文页不同，直接取 min 会被封面拉偏），再取小者
    modes = []
    for ys in (ucles_ys, turn_ys):
        if ys:
            modes.append(Counter(ys).most_common(1)[0][0])
    footer_y = float(min(modes)) - 1.5 if modes else FOOTER_Y
    pages_content = [content_lines(lines, footer_y) for lines in pages_lines]
    for pi in range(n_pages):
        words = doc[pi].get_text("words")
        pages_pairs.append(assign_words_to_lines(pages_lines[pi], words))

    # ── 无文本层 / 乱码残留 → 交给视觉，不硬切 ──
    n_text_lines = sum(len(x) for x in pages_content)
    if n_text_lines < 20:
        sys.exit(f"NEEDS_VISION  {sdir.name}: 版心文本行仅 {n_text_lines} 行"
                 f"（扫描卷或无文本层）——请用视觉切")
    bad_ctrl = sum(1 for lines in pages_content for ln in lines
                   if re.search(r"[\x00-\x06\x08\x0b-\x1f]", ln["raw"]))
    if bad_ctrl > max(5, n_text_lines * 0.01):
        sys.exit(f"NEEDS_VISION  {sdir.name}: 解码后仍有 {bad_ctrl} 行含异常控制字符"
                 f"（占 {bad_ctrl / n_text_lines:.1%}）——乱码未解，请用视觉切")

    # ── 逐页有效下界：版权声明 / 周期表附录之上 ──
    pages_bottom = {}
    for pi, lines in enumerate(pages_lines):
        top = None
        for ln in lines:
            raw = ln["raw"].strip()
            if COPYRIGHT_RE.match(raw) or APPENDIX_RE.match(raw):
                top = ln["bbox"][1] if top is None else min(top, ln["bbox"][1])
        if top is not None:
            # top−20：版权段上方还有一条整宽分隔横线（y0≈698.6，首行 709.5），
            # 只减 8 会把那条线留在图底
            pages_bottom[pi] = max(HEADER_Y, top - 20)

    # ── Section B 共享选项表（2016–2021 P1 Q31–40） ──
    section_b_pos = None
    for _pi, _lines in enumerate(pages_content):
        for _ln in _lines:
            if "responses A to D" in _ln["raw"]:
                section_b_pos = (_pi, _ln["bbox"][1])
                break
        if section_b_pos:
            break
    has_shared_options_table = section_b_pos is not None

    # ── 题号列 QCOL：题号行 x0 众数（老卷 49.6 / 2024+ 卷 72.8） ──
    # ⚠️ 只收「页内小数字」：化学 P5 的坐标轴刻度（90/80/70… 在 x≈86.7）
    # 数量比题号还多，纯众数会把题号列定到坐标轴上（2016 qp52 实测）。
    # 题号上限 = P1 的 40 / 结构化卷的 15。
    max_qn = paper_nq if is_mcq_paper else 15
    digit_x0 = []
    for lines in pages_content:
        for ln in lines:
            b = ln["bbox"]
            m = re.match(r"^(\d{1,2})(?:\s|\(|$)", ln["raw"].strip())
            if b[0] < 80.0 and m and int(m.group(1)) <= max_qn:
                digit_x0.append(round(b[0]))
    if len(digit_x0) < 2:
        # P5 只有 2 道大题（20 页），门槛不能按 P1 的 4 个来卡
        sys.exit(f"NEEDS_VISION  {sdir.name}: 题号列定位失败（数字起始行仅 "
                 f"{len(digit_x0)} 个）——疑似扫描卷或版式异常，请用视觉切")
    # 不能只取众数：2017 qp51 的表格序号列（x=81.4，一行一个数字）出现次数
    # 比真题号还多。真题号的特征是「多落在页顶 y<140」（每题新页起），
    # 表格序号列只在页面中段。按「页顶占比」优先挑列。
    _col = {}
    for lines in pages_content:
        for ln in lines:
            b = ln["bbox"]
            m = re.match(r"^(\d{1,2})(?:\s|\(|$)", ln["raw"].strip())
            if b[0] < 80.0 and m and int(m.group(1)) <= max_qn:
                _col.setdefault(round(b[0]), []).append(b[1])
    # 先筛「有足够比例落在页顶」（≥15%）的列，再取出现次数最多、x 最小的：
    # P1 一页 3 题，题号列的页顶占比只有 ~35%，但绝对次数最多（40 次）；
    # 表格序号列次数少且几乎不在页顶。
    _cand = [(len(ys), sum(1 for y in ys if y < 140) / len(ys), x)
             for x, ys in _col.items()]
    _ok = [t for t in _cand if t[1] >= 0.15] or _cand
    qcol = float(sorted(_ok, key=lambda t: (-t[0], -t[1], t[2]))[0][2])

    # ── 版心：内容行坐标分位数 ──
    # ⚠️ 化学卷有三种页面尺寸：A4 595×842（2016–2022）、US Letter 612×792
    # （2023 起全部）、A3 842×1191（个别 2016 oct 卷，内容 x 到 776、y 到 1010）。
    # 写死 575/790 会把 Letter 的右边和 A3 的右半页整块切掉（2016-oct-qp42
    # 实测：小问行 x1=776 超出 region 右界，layout 归一化越界）。
    # 因此版心一律按实际页宽/页高换算，A4 下与原来的 575/790 等价。
    PW = doc[0].rect.width
    PH = doc[0].rect.height
    # ⚠️ **封面页不参与版心统计**（2026-10-18 实测）：封面左上角有防伪条码
    # （`* 1 3 4 2 1 3 4 6 2 9 *` x0=20.6）和几行密文块（x0=26.9），
    # 只有 4 行却能拉到 0.5 分位以下 —— 2025 qp41/qp43 版心 L 被推到 25.0，
    # 题图左边多出 20pt，会切进左侧竖栏区。
    allc = [ln for pi, lines in enumerate(pages_content) if pi > 0 for ln in lines] or \
           [ln for lines in pages_content for ln in lines]
    xs0 = sorted(ln["bbox"][0] for ln in allc)
    xs1 = sorted(ln["bbox"][2] for ln in allc)
    ys0 = sorted(ln["bbox"][1] for ln in allc)
    L = max(25.0, pct(xs0, 0.005) - 6)
    R = min(PW - 20.0, pct(xs1, 0.995) + 6)
    T = max(45.0, pct(ys0, 0.005) - 4)
    B = min(PH - 52.0, footer_y)

    # ── 题号起点候选（序列约束 1..N） ──
    # P1: 题号可在页内任意位置（每页约 3 题）。
    # P2/P4/P5: 大题**多数**从新页顶开始，排满时下一题同页续排。
    # 化学题号恒为独立行（"1\t" / "11\t"），合并行极少；误报由序列约束挡。
    y_max = footer_y
    cands = []  # (page, y, n, bbox, line)
    for pi, lines in enumerate(pages_content):
        for ln in lines:
            b = ln["bbox"]
            if not (HEADER_Y < b[1] < y_max) or b[0] > qcol + QCOL_SLACK or b[0] < qcol - 2:
                continue
            raw = ln["raw"].strip()
            m = QSTART_RE.match(raw)
            if not m:
                continue
            n = int(m.group(1) or m.group(3))
            if n > max_qn:      # P1 → 40；结构化 → 15（P2/P4/P5 题数不固定）
                continue
            if not is_mcq_paper:
                # P2/P4/P5 防误报：独立行放行；合并行只接受「题号后跟 (a… 小问标签」
                # 或粗体行（正文/图表里以数字开头的行不粗体且不带标签）。
                # ⚠️ 2026-10-18 补：新渠道 A4 源把题号与题干**排在同一行**
                # （`10 Propanoic acid, methanoic acid …`），且三个字体名
                # （AllAndNone / AllAndNone2 / AllAndNone3）**都不含 "bold"**，
                # 于是 bold=False、tail 非空 → 末题被当误报跳过，
                # 分值合计 93≠100 而判 NEEDS_VISION（2025 qp41/qp43、2026 qp44）。
                # 放宽为：尾随文字以大写字母开头即认作题干行。
                if not ln["bold"] and not re.match(r"^\d{1,2}\s*\(", raw):
                    tail = re.match(r"^\d{1,2}\s*(.*)$", raw).group(1)
                    if tail and not re.match(r"^[A-Z]", tail):  # 有尾随文字却非题干 → 不当题号
                        continue
            cands.append((pi, b[1], n, b, ln))
    cands.sort(key=lambda c: (c[0], c[1]))

    # 无文本层但含整页图片的页 = 源 PDF 的扫描替代页。
    # 这类页提取不到题号行，会让题号序列断裂；仅在结构化卷上按「紧邻下一页」
    # 兜底成一道题的起点（P1 一页多题，兜底会漏，不适用）。
    scan_pages = []
    if not is_mcq_paper:
        for pi, page in enumerate(doc):
            if pages_lines[pi]:
                continue
            for img in page.get_images(full=True):
                try:
                    bb = page.get_image_bbox(img)
                except Exception:
                    continue
                if bb.width * bb.height >= 0.5 * page.rect.width * page.rect.height:
                    scan_pages.append(pi)
                    break

    qstarts = {}
    expected, last = 1, (-1, -1.0)
    while True:
        hit = None
        for pi, y, n, b, ln in cands:
            if n == expected and (pi, y) > last:
                hit = (pi, y, b, ln)
                break
        if hit:
            pi, y, b, ln = hit
            qstarts[expected] = {"page": pi, "y0": y, "bbox": b, "line": ln}
            last = (pi, y)
            expected += 1
            continue
        nxt_page = last[0] + 1 if last[0] >= 0 else None
        if nxt_page is not None and nxt_page in scan_pages:
            qstarts[expected] = {"page": nxt_page, "y0": T, "bbox": [r2(L), r2(T), r2(R), r2(T)],
                                 "line": None, "scan_fallback": True}
            last = (nxt_page, T)
            expected += 1
            continue
        break
    n_questions = expected - 1
    if n_questions == 0:
        sys.exit(f"NEEDS_VISION  {sdir.name}: 未定位到任何题号起点，请用视觉切")
    # ⚠️ 化学 P2/P4 的题数**不固定**（实测 P2 4–5 题、P4 7–10 题、P5 2 题），
    # 不能用固定题数校验；只有 P1 恒 40 题。
    if is_mcq_paper and n_questions != paper_nq:
        sys.exit(f"NEEDS_VISION  {sdir.name}: P1 题号序列只到 {n_questions}/{paper_nq}"
                 f"（断裂于 Q{n_questions + 1}）——不硬切，请用视觉")

    # ── [Total: N] 终点锚点（P2/P4）；P1 无 ──
    totals_all = []
    if not is_mcq_paper:
        for pi, lines in enumerate(pages_content):
            for ln in lines:
                b = ln["bbox"]
                m = TOTAL_RE.search(ln["raw"])
                if m:
                    totals_all.append({"page": pi, "bbox": b, "marks": int(m.group(1)),
                                       "y1": b[3]})

    def totals_after(page, y):
        return [t for t in totals_all if (t["page"], t["y1"]) > (page, y)]

    # ── 逐题构建 ──
    auto_questions = []
    trim_stats_n = 0
    trim_stats_cap = 0
    content_cache = {}
    marks_sum_all = 0
    for n in range(1, n_questions + 1):
        q = {
            "number": str(n),
            "status": "auto",
            "confidence": 0.0,
            "regions": [],
            "anchors": {},
            "review_notes": [],
            "subquestions": [],
            "question_type": "mcq" if is_mcq_paper else "structured",
        }
        notes = q["review_notes"]
        st = qstarts[n]
        q["anchors"]["start"] = {"page": st["page"], "bbox": [r2(v) for v in st["bbox"]]}
        conf = 0.4
        if st.get("scan_fallback"):
            notes.append("起始页为扫描页（无文本层），起点按版心顶兜底——请复核该题范围")
            conf = 0.2

        # 终点：P2/P4 优先 [Total]；P1 用下一题起点 / Space for working
        end_page, end_y1 = None, None
        anchor_end = False
        nxt = qstarts.get(n + 1)
        if not is_mcq_paper:
            mine = [t for t in totals_after(st["page"], st["y0"] - 1)
                    if nxt is None or (t["page"], t["y1"]) < (nxt["page"], nxt["y0"])]
            if mine:
                en = mine[0]
                q["anchors"]["end"] = {"page": en["page"], "bbox": [r2(v) for v in en["bbox"]]}
                q["total_marks"] = en["marks"]
                marks_sum_all += en["marks"]
                end_page, end_y1 = en["page"], en["y1"] + 6   # +6：Total 行的 descent 在 bbox 外，+2 会切下缘
                anchor_end = True
                conf += 0.3
            else:
                # 兜底：本题范围内的行内 [n] 分值合计（CIE 每题末必有分值标记；
                # 2021 qp41 Q4 无 [Total] 行，分值 [5] 标在末行点线右端）。
                hi = (nxt["page"], nxt["y0"]) if nxt else (n_pages, 1e9)
                inline = []
                for pi in range(st["page"], min(hi[0] + 1, n_pages)):
                    for ln in pages_content[pi]:
                        pos = (pi, ln["bbox"][1])
                        if pos <= (st["page"], st["y0"]) or pos >= hi:
                            continue
                        inline.extend(int(mm.group(1)) for mm in MARK_RE.finditer(ln["raw"]))
                if inline and 0 < sum(inline) <= 30:
                    q["total_marks"] = sum(inline)
                    marks_sum_all += sum(inline)
                    notes.append(f"无 [Total] 行，用题内行内 [n] 合计兜底 = {sum(inline)}")
                    conf += 0.2
                else:
                    notes.append("缺 [Total] 终点锚点，边界由下一题起点/版心底推出")
        end_ok = False
        if end_page is None:
            if nxt is not None:
                if nxt["page"] == st["page"]:
                    end_page, end_y1 = nxt["page"], nxt["y0"] - 2
                else:
                    end_page, end_y1 = nxt["page"] - 1, B
                end_ok = True
            elif is_mcq_paper:
                # 化学 P1 末题：**不用**「Space for working」锚点。
                # 物理那份用它是因为卷尾有专门的 working 区；化学 P1 卷尾是
                # 「Additional page」，而末题的 1/2/3 陈述常常正好排在
                # "space for working" 那行**之后**，用它会从中间截断
                # （2016 qp11 Q40 实测：region 只到 y=256，后面 3 条陈述全丢）。
                # 直接取「卷内最后一个有内容的页」的版心底，页尾空白交给 trim。
                blank_pis = {pi for pi, lines in enumerate(pages_lines)
                             if any(BLANK_PAGE_RE.match(l["raw"].strip()) for l in lines)}
                last_pi = max((pi for pi in range(n_pages)
                               if pi not in blank_pis and pages_content[pi]),
                              default=st["page"])
                end_page, end_y1 = last_pi, B
                end_ok = True
            else:
                # 末题且本题没有 [Total] 锚点（2021 qp41 实测）：
                # 边界一路取到卷末有效底（版权段/周期表由 pages_bottom 挡住），
                # 页尾空白交给 trim_regions。**不能留 None**，否则 build_regions 崩。
                end_page, end_y1 = n_pages - 1, B
                notes.append("末题无 [Total] 锚点，边界取至卷末有效底——请复核")
            if not is_mcq_paper and not end_ok:
                q["status"] = "review"
                conf = min(conf, 0.3)
        if is_mcq_paper and end_ok:
            conf += 0.2

        sy0 = st["y0"] - 4
        q["regions"] = build_regions(st["page"], sy0, end_page, end_y1, L, T, R, B,
                                    bottoms=pages_bottom)
        # 全部 region 一律裁剪：化学的 [Total] 之后常剩半页排版空白，
        # 与物理「锚点终点不裁」的假设相反。
        n_t, n_c = trim_regions(doc, q["regions"], pages_content, content_cache, footer_y,
                                allow_cap=not is_mcq_paper)
        trim_stats_n += n_t
        trim_stats_cap += n_c

        if is_mcq_paper:
            conf += extract_mcq_options_cie(q, st, nxt, pages_content, pages_pairs, R, B, notes,
                                            shared_table=section_b_pos)
        else:
            # 小问标签锚点
            subqs = []
            parent = None
            for rg in q["regions"]:
                for ln in pages_content[rg["page"]]:
                    lb = ln["bbox"]
                    if lb[1] < rg["bbox"][1] - 1 or lb[1] > rg["bbox"][3] + 1:
                        continue
                    if lb[0] > qcol + SUBQ_SLACK:
                        continue
                    parsed = parse_subhead(ln["raw"])
                    if not parsed:
                        continue
                    tokens = parsed[0]
                    if tokens[0] in ROMAN_TOKENS:
                        if parent is None:
                            continue
                        tokens = (parent,) + tokens
                    else:
                        parent = tokens[0]
                    subqs.append({"label": sub_label(tokens), "page": rg["page"],
                                  "bbox": [r2(v) for v in lb]})
            q["subquestions"] = subqs
            if subqs:
                conf += 0.2
            # 行内 [n] 分值合计 vs [Total] 锚点
            if "total_marks" in q:
                s = 0
                for rg in q["regions"]:
                    for ln in pages_content[rg["page"]]:
                        if rg["bbox"][1] - 1 <= ln["bbox"][1] <= rg["bbox"][3] + 1:
                            for mm in MARK_RE.finditer(ln["raw"]):
                                s += int(mm.group(1))
                if s != q["total_marks"]:
                    notes.append(f"行内 [n] 合计 {s} ≠ [Total] 锚点 {q['total_marks']}，请复核")
                    q["status"] = "review"
                else:
                    conf += 0.2

        q["confidence"] = round(min(conf, 1.0), 2)
        if conf < 1.0 and q["status"] == "auto":
            q["status"] = "review"
        auto_questions.append(q)

    # 分值合计校验（P2/P4/P5 用 [Total] 锚点合计，P1 每题 1 分）
    if is_mcq_paper:
        marks_sum_all = n_questions
        for q in auto_questions:
            q["total_marks"] = 1
    expect_total = paper_total_marks if paper_total_marks is not None else paper_total
    if marks_sum_all != expect_total:
        # 化学每卷题数、每题 [Total] 十年恒定 → 合计不符必是锚点读错，
        # 不写 cut.json（硬切出来的题图边界一定错），整卷交视觉。
        detail = "、".join(f"Q{q['number']}={q.get('total_marks', '?')}"
                          for q in auto_questions)
        sys.exit(f"NEEDS_VISION  {sdir.name}: 分值合计 {marks_sum_all} ≠ 卷面总分 "
                 f"{expect_total}（{detail}）——不硬切，请用视觉")

    # ── 与已有 cut.json 合并（人工修改保护） ──
    cut_path = sdir / "cut.json"
    existing = None
    if cut_path.exists():
        existing = json.loads(cut_path.read_text(encoding="utf-8"))
        if existing.get("source", {}).get("sha256") != sha:
            sys.exit("source.pdf 的 sha256 与 cut.json 记录不符——源头已变，请人工处理。")
    questions, merge_report = merge_with_existing(auto_questions, existing)

    cut = {
        "schema_version": 1,
        "source": {"file": "source.pdf", "sha256": sha, "pages": n_pages},
        "page_size": {"w": r2(doc[0].rect.width), "h": r2(doc[0].rect.height)},
        "render": {"dpi": 200, "format": "webp", "quality": 85},
        "questions": [{k: v for k, v in q.items() if not k.startswith("_")} for q in questions],
    }
    season_letter = {"june": "s", "oct": "w", "mar": "m"}.get(session, "?")
    cut["source"]["paper_ref"] = f"9701_{season_letter}{str(year)[2:]}_qp{paper_code_l[2:]}"
    cut_path.write_text(json.dumps(cut, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")

    # ── 每题产物 qNN.md ──
    assets = sdir / "assets"
    assets.mkdir(exist_ok=True)
    q_by_num = {q["number"]: q for q in questions}
    for n in range(1, n_questions + 1):
        q = q_by_num.get(str(n))
        if q is None:
            continue
        write_question_md(sdir, n, q, board, qualification, subject, year, session,
                          paper_code, pages_content, pages_pairs)
    for q in questions:
        q.pop("_options", None)

    # ── 报告 ──
    print(f"OK  {sdir.name}: {n_questions} 题 | QCOL={qcol:.0f} 版心 L={L:.1f} R={R:.1f} T={T:.1f} B={B:.0f}")
    if trim_stats_n:
        print(f"    页尾空白裁剪: {trim_stats_n} 段")
    for line in merge_report:
        print("   ", line)
    by_status = Counter(q["status"] for q in questions)
    print("    status 分布:", " / ".join(f"{k}×{v}" for k, v in sorted(by_status.items())))
    for q in questions:
        if q["review_notes"]:
            print(f"    [review] Q{q['number']}: {'; '.join(q['review_notes'])}")


# ── P1 选项热区 ─────────────────────────────────────────────────────────────

def extract_mcq_options_cie(q, st, nxt, pages_content, pages_pairs, R, B, notes,
                            shared_table=None):
    """CIE P1 选项：独立 [A-D] 标签行（可同行四列），标签下界=下一选项行/题末。

    ⚠️ 化学 P1 从 Q31 起换成「1/2/3 陈述 → A–D 组合表」题型，题干在本页、
    组合表**翻到下一页顶部**（y≈93）。只在起始页找标签会 9 题全空
    （2016 qp12 Q31–Q40 实测）。因此按本题 regions 逐页扫描，取第一个凑齐
    A–D 的页。
    下界钳制到该 region 底（末段可能被页尾空白裁剪过），否则渲染层
    归一化坐标会越界（layout.schema h≤1）。
    返回置信度增量（4 选项齐全 +0.4）。
    """
    def scan(pi, y_lo, y_hi):
        labels = []
        for ln in pages_content[pi]:
            b = ln["bbox"]
            if b[1] < y_lo or b[1] >= y_hi:
                continue
            m = OPTION_LABEL_RE.match(ln["raw"].strip())
            if m:
                labels.append({"label": m.group(1), "y0": b[1], "x0": b[0], "x1": b[2]})
        uniq, seen = [], set()
        for lb in sorted(labels, key=lambda x: x["y0"]):
            if lb["label"] not in seen:
                seen.add(lb["label"])
                uniq.append(lb)
        return uniq

    # Section B（2016–2021 P1 Q31–40）：A–D 是「1/2/3 陈述」组合表，全页共用一份
    # 印在页顶，题目自己没有选项行。这类题不去扫标签——否则题干里的结构式原子
    # "C"／"H" 会被当成选项标签（2016 qp11 Q40 只提到 ['C']）。
    if shared_table and (st["page"], st["y0"]) > shared_table:
        q["_options"] = []
        q["option_hotzones"] = []
        q["_options_shared"] = True
        return 0.4

    best = None                     # (pi, uniq, limit_y)
    for rg in q["regions"]:
        pi = rg["page"]
        y_lo = max(st["y0"] if pi == st["page"] else 0, rg["bbox"][1])
        y_hi = rg["bbox"][3]
        if nxt and nxt["page"] == pi:
            y_hi = min(y_hi, nxt["y0"] - 2)
        uniq = scan(pi, y_lo, y_hi)
        if len({o["label"] for o in uniq}) == 4:
            best = (pi, uniq, y_hi)
            break
        if best is None and uniq:
            best = (pi, uniq, y_hi)

    opts = []
    if best:
        pi, uniq, limit_y = best
        for lb in uniq:
            nxt_row = next((o for o in uniq if o["y0"] > lb["y0"] + 3), None)
            y_end = nxt_row["y0"] - 2 if nxt_row else limit_y
            x1 = R
            for other in uniq:
                if other is not lb and abs(other["y0"] - lb["y0"]) <= 3 and other["x0"] > lb["x0"]:
                    x1 = min(x1, other["x0"] - 6)
            bbox = [r2(lb["x0"] - 6), r2(lb["y0"] - 2), r2(x1), r2(y_end)]
            rows = []
            for ln2 in pages_content[pi]:
                b2 = ln2["bbox"]
                if lb["y0"] - 1 <= b2[1] <= y_end + 1 and b2[0] >= lb["x1"] - 1:
                    rows.append(ln2)
            rows.sort(key=lambda l: (round(l["bbox"][1], 1), l["bbox"][0]))
            text = " ".join(re.sub(r"\s+", " ", ln2["raw"]).strip() for ln2 in rows).strip()
            opts.append({"label": lb["label"], "text": text, "bbox": bbox})
    q["_options"] = opts
    q["option_hotzones"] = [{"label": o["label"], "bbox": o["bbox"]} for o in opts]
    labels_got = {o["label"] for o in opts}
    if labels_got == {"A", "B", "C", "D"}:
        return 0.4
    if not opts and shared_table:
        # Section B（2016–2021）：A–D 是「1/2/3 陈述」组合表，全页共用一份，
        # 印在页顶（y≈93）而非每题末尾。题图里本来就没有独立选项行，
        # 不生成热区、也不算缺陷。
        q["_options_shared"] = True   # 下划线前缀：不进 cut.json（schema 严格）
        return 0.4
    notes.append(f"MCQ 选项不全（提取到 {sorted(labels_got)}），请人工补热区")
    return 0.0


# ── 文本/产物（与 cut_paper.py 同构） ───────────────────────────────────────

def regions_contain(regions, page, cx, cy):
    for rg in regions:
        if rg["page"] != page:
            continue
        b = rg["bbox"]
        if b[0] <= cx <= b[2] and b[1] <= cy <= b[3]:
            return True
    return False


def question_text_lines(q, pages_content, pages_pairs):
    out = []
    for rg in q["regions"]:
        pi = rg["page"]
        for ln in pages_content[pi]:
            if ln.get("is_dots"):
                continue                     # 作答点线不进正文
            b = ln["bbox"]
            cx, cy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
            if not regions_contain(q["regions"], pi, cx, cy):
                continue
            text = " ".join(w[4] for w, ref in pages_pairs[pi] if ref is ln)
            text = text or ln["raw"]
            text = re.sub(r"[\u2009\u00a0\t]+", " ", text).strip()
            text = re.sub(r" {2,}", " ", text)
            if text:
                out.append(text)
    return out


def write_question_md(sdir, n, q, board, qualification, subject, year, session,
                      paper_code, pages_content, pages_pairs):
    qid = f"{board}-{qualification}-{subject}-{year}-{session}-{paper_code}-q{n:02d}"
    path = sdir / f"q{n:02d}.md"
    fm = [
        ("id", qid),
        ("board", board),
        ("qualification", qualification),
        ("subject", subject),
        ("paper_code", paper_code.upper()),
        ("year", year),
        ("session", session),
        ("question_number", str(n)),
    ]
    if "total_marks" in q:
        fm.append(("total_marks", q["total_marks"]))
    if "question_type" in q:
        fm.append(("question_type", q["question_type"]))
    fm += [
        ("image", f"assets/q{n:02d}.webp"),
        ("layout", f"assets/q{n:02d}.layout.json"),
    ]
    body = "\n".join(question_text_lines(q, pages_content, pages_pairs))
    if path.exists():
        update_front_matter(path, fm, q)
        return
    options = q.get("_options")
    lines = ["---"]
    for k, v in fm:
        lines.append(f"{k}: {yaml_scalar(v)}")
    if options:
        lines.append("options:")
        for o in options:
            lines.append(f"  - label: {o['label']}")
            lines.append(f"    text: {yaml_scalar(o.get('text', ''))}")
    lines.append("source: " + yaml_scalar(f"CIE 9701/{paper_code.upper()}, {year} {session}"))
    lines.append("cut_status: " + q["status"])
    lines.append("language: en")
    lines.append("---")
    lines.append("")
    lines.append(body)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def update_front_matter(path, fm_pairs, q):
    text = path.read_text(encoding="utf-8")
    m = re.match(r"^---\n(.*?)\n---\n?(.*)$", text, re.S)
    if not m:
        print(f"    [warn] {path.name}: front-matter 无法解析，已跳过更新")
        return
    fm_text, body = m.group(1), m.group(2)
    blocks, order, cur = {}, [], None
    for line in fm_text.split("\n"):
        km = re.match(r"^([A-Za-z_][A-Za-z0-9_]*):", line)
        if km and not line.startswith((" ", "-")):
            cur = km.group(1)
            blocks[cur] = [line]
            order.append(cur)
        elif cur is not None:
            blocks[cur].append(line)
    updates = dict(fm_pairs)
    updates["cut_status"] = q["status"]
    for k, v in updates.items():
        new = f"{k}: {yaml_scalar(v)}"
        blocks[k] = [new]
        if k not in order:
            order.append(k)
    options = q.get("_options")
    if options is not None and q.get("question_type") == "mcq" and q["status"] != "manual":
        blk = ["options:"]
        for o in options:
            blk.append(f"  - label: {o['label']}")
            blk.append(f"    text: {yaml_scalar(o.get('text', ''))}")
        blocks["options"] = blk
        if "options" not in order:
            order.append("options")
    new_fm = "\n".join(line for k in order for line in blocks[k])
    path.write_text("---\n" + new_fm + "\n---\n" + body, encoding="utf-8")


def merge_with_existing(auto_questions, existing):
    report = []
    if not existing:
        return auto_questions, ["首次运行，全部采用自动结果"]
    old_by_num = {q["number"]: q for q in existing.get("questions", [])}
    result = []
    for q in auto_questions:
        old = old_by_num.get(q["number"])
        if old is None:
            report.append(f"Q{q['number']}: 新增")
            result.append(q)
            continue
        status = old.get("status", "auto")
        if status in ("auto", "review"):
            q["_options"] = q.get("_options") or old.get("_options")
            result.append(q)
            continue
        if status == "verified":
            if regions_equal(old.get("regions", []), q["regions"]):
                for k in ("confidence", "total_marks", "question_type", "subquestions"):
                    if k in q:
                        old[k] = q[k]
                old.setdefault("review_notes", [])
                old["_options"] = q.get("_options") or old.get("_options")
                report.append(f"Q{q['number']}: verified 一致，刷新可再生字段")
                result.append(old)
            else:
                old["status"] = "review"
                old.setdefault("review_notes", []).append("重跑的自动结果与人工确认不一致，请复核")
                result.append(old)
            continue
        if status == "manual":
            for k in ("total_marks", "question_type"):
                if k in q and q[k] != old.get(k):
                    old[k] = q[k]
            report.append(f"Q{q['number']}: manual，regions/anchors 保留人工值")
            result.append(old)
    have = {q["number"] for q in result}
    for num, old in old_by_num.items():
        if num not in have:
            report.append(f"Q{num}: 自动结果未覆盖（保留人工题）")
            result.append(old)
    result.sort(key=lambda q: int(q["number"]))
    return result, report


def regions_equal(a, b):
    if len(a) != len(b):
        return False
    for ra, rb in zip(a, b):
        if ra.get("page") != rb.get("page"):
            return False
        for va, vb in zip(ra.get("bbox", []), rb.get("bbox", [])):
            if abs(va - vb) > 1.0:
                return False
    return True


if __name__ == "__main__":
    main()
