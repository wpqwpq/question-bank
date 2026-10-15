#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""cut_paper_cie.py — CIE (9702) 物理卷按题自动切割

⚠️ 专用脚本，服务于 CIE A Level 物理（9702）P1/P2/P4；Edexcel 的
    cut_paper.py 不做任何改动。产出与 cut_paper.py **完全同构**
    （cut.json + qNN.md，符合 schema/cut.schema.json），可直接进
    render_questions.py / validate_cut.py / 打标台。

CIE 与 Edexcel 的版式差异（2026-10-15 实测 2016–2025 共 30 卷 s 季样张）：
    · P2/P4 每道大题从新页顶部开始（x0≈49.6 或 72.8，y0≤84，各年左右浮动）；
    · P2/P4 分值锚点是行内 [1] [2]（每条作答线尾）+ 每题一条右对齐
      [Total: N]（x≈504–546）——全十年均有，合计恰等于卷面总分
      （P2=60、P4=100），这是比 Edexcel Total 锚点更强的边界信号；
    · 无答题册：P2/P4 直接答在题卷点线上，answer_booklet / answer_regions
      全链路不存在；
    · P1 单列 40 题，题号行为独立行（"1 "）或与题干合并行（"11 The diagram…"），
      选项 A–D 各占一行或同行四列（x≈70–492 不等）；
    · 题号列位置逐年浮动（49.6 / 72.4 / 72.8），因此按每卷题号行 x0 众数
      自适应推导（QCOL），不用硬编码；
    · 正文前有封面 + Data/Formulae 页（P1 共 2 页、P4 共 3 页），另有
      BLANK PAGE 页，序列约束（1..N 递增）天然跳过；
    · A 型乱码（2021–2023 P1，fraft 渠道剥 ToUnicode）：全部字符 -0x1D
      位移（含数字与空格→\x03 控制符），按 span 分治解码还原
      （判据：控制字符 / 还原后英文词频上升），解码后与正常卷同一管线。

坐标约定（全仓库统一）: PDF 点 pt，页面左上角原点，y 向下；bbox=[x0,y0,x1,y1]。

用法:
    python scripts/cut_paper_cie.py <session_dir>

<session_dir> 指向 data/cie/alevel/physics/<year>-<session>-qpNN/，
目录内须已放入 source.pdf 与 paper.md（含 total_marks 更佳）。
paper_code 约定：qp11–13 → Paper 1 (MCQ)；qp21–23 → Paper 2；qp41–43 → Paper 4。
P3（实验操作）/ P5（设计题）不支持，直接拒绝。

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

# ── 排版常量（CIE 9702 实测） ────────────────────────────────────────────────
HEADER_Y = 50.0     # 页眉安全线：页码/条码/DFD 全在 y<50
FOOTER_Y = 790.0    # 页脚：© UCLES / 卷号 / [Turn over / 水印全在 y≥793
QCOL_SLACK = 12.0   # 题号行 x0 允许偏离题号列的余量
SUBQ_SLACK = 70.0   # 小问标签行 x0 允许偏离题号列的余量（嵌套 (i) 缩进 ~46pt）
TOTAL_RE = re.compile(r"\[\s*Total\s*:?\s*(\d+)\s*\]")
MARK_RE = re.compile(r"\[(\d{1,2})\]")
# 第二分支的题号后分隔符放宽到 [ \t=~]：字体 cmap 局部错位的行（如
# 2016 w11 "15=cour=be~ms=of=the=s~me…"，视觉是 "15 Four beams…"）
# 空格被映射成 '='，用 \s+ 会漏掉整行 → 题号序列断裂。
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

# paper_code → (paper 号, 卷面总分)
PAPER_OF_CODE = {}
for _p, _tot in ((1, 40), (2, 60), (4, 100)):
    for _v in (1, 2, 3):
        PAPER_OF_CODE[f"qp{_p}{_v}"] = (_p, _tot)

TRIM_MIN_GAP = 20.0
TRIM_PAD = 8.0

# ── A 型乱码解码（span 分治） ────────────────────────────────────────────────
# fraft 渠道的加工卷把整支字体（或部分 span）的字符统一 -0x1D 位移：
# UCLES→8&/(6、"Formulae"→)RUPXODH、空格→\x03。判据有三：
#   1. span 含控制字符（除 \t）→ 整 span 位移还原（2021+ 型，连数字一起移）；
#   2. 还原后英文常用词命中数上升 → 还原（常规字母移位型）；
#   3. 原 span 完全无字母而还原后有 → 还原（短标签 span，如 "$"→"A"）。
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
page physics paper candidate centre name number alpha beta gamma metal glass water block
ball box ring disc rod wire beam pulley trolley satellite planet student teacher teacher
area length width diameter density moment impulse momentum kinetic potential thermal
nuclear magnetic electric gravitational elastic strain stress young modulus
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


def decode_shift_back(text):
    """−0x1D 位移还原（存储 = 原文 + 0x1D 型）。

    与 decode_shift 方向相反：2016 w11 Q15 的 "=cour=be~ms=of=the=s~me"
    还原后是 " Four beams of the same"（空格→'='、'a'→'~'、'F'→'c'，
    均 +0x1D）。此类行只在部分 span 出现，是字体子集错位所致。
    """
    out = []
    for c in text:
        o = ord(c) - 0x1D
        out.append(chr(o) if 0x20 <= o <= 0x7E else c)
    return "".join(out)


def word_hits(text):
    return sum(1 for w in re.findall(r"[A-Za-z]{2,}", text.lower()) if w in COMMON_WORDS)


# 乱码签名：大写字母 -0x1D 后落进的标点区（健康文本的字母不会整批变成这些）
SIG_PUNCT = frozenset("$%&'()*+,-./:;<=>?@")


def decode_span(text):
    """按判据决定是否对 span 做 +0x1D 还原；返回 (解码后文本, 是否解码)。

    判据（按序）：
      1. 含控制字符（除 \t）→ 还原（2021+ 型：数字/空格也位移，空格→\x03）；
      2. 原文含乱码签名标点且还原后英文常用词命中上升 → 还原
         （健康卷的 "21"→"ON"、"25"→"OR" 误不中——数字不是签名标点）；
      3. 单字符 span 且还原后恰为 A–D → 还原（P1 选项标签 "$"→"A" 场景）。
    """
    if not text:
        return text, False
    if any(0x03 <= ord(c) < 0x20 and c != "\t" for c in text):
        return decode_shift(text), True
    if any(c in SIG_PUNCT or c in "~|\x7f{" for c in text):
        base = word_hits(text)
        fwd, bwd = decode_shift(text), decode_shift_back(text)
        hs = sorted(((word_hits(fwd), fwd), (word_hits(bwd), bwd)), key=lambda t: -t[0])
        # 强特征：'=' 密集（正常排版里 '=' 两侧必有空格、一行至多一两个）。
        # 这类行（如 2016 w11 Q15 "=cour=be~ms=of=the=s~me"）部分 span 未被
        # 编码、混合常见词，词频判据会失效（原文命中数不低），必须靠标点密度。
        strong = text.count("=") >= 3
        if hs[0][0] > base or (strong and hs[0][0] >= base and hs[0][0] > 0):
            return hs[0][1], True
    if len(text.strip()) == 1 and text.strip() in SIG_PUNCT and decode_shift(text).strip() in "ABCD":
        return decode_shift(text), True
    return text, False


# ── 页面文本结构提取 ────────────────────────────────────────────────────────

def build_page_lines(page):
    """一页的全部横排文本行（span 级 A 型乱码解码后），按 (y0, x0) 排序。"""
    lines = []
    for block in page.get_text("dict")["blocks"]:
        if block.get("type") != 0:
            continue
        for ln in block["lines"]:
            if tuple(ln.get("dir", (1, 0))) != (1.0, 0.0):
                continue
            # 不能按 strip() 判空：乱码年题号 span 形如 "\x1c"（'9'−0x1D），
            # 0x1C–0x1F 恰是 Python 空白字符 → strip() 后为空、整行题号被丢
            # （2022/2023 qp11 曾只切出 8/40 题，序列断在 9→10）。
            # 改为「非空字符串即收」，行级再判可见性。
            spans = [s for s in ln["spans"] if s["text"]]
            if not spans:
                continue
            parts, decoded_any = [], False
            for s in ln["spans"]:
                # 显式列可见装饰字符，**不能用 \s**——Python 的 \s 把 \x1c–\x1f
                # 也算空白，会把乱码题号 span（'9'−0x1D = \x1c）当装饰跳过、
                # 导致整行题号丢失（2022/2023 qp11 只切 8/40 题的真正根因）。
                if re.fullmatch(r"[ \t,.*\-]*", s["text"] or ""):
                    parts.append(s["text"])  # 可见装饰/点线垃圾：不解码，交给 content_lines 过滤
                    continue
                t, dec = decode_span(s["text"])
                parts.append(t)
                decoded_any = decoded_any or dec
            raw = "".join(parts)
            # 行级可见性：剥掉控制字符后仍须有可见字符（纯点线/装饰行丢弃）；
            # 乱码行解码后是正常文本，不受影响。
            if not re.sub(r"[\x00-\x1f]", "", raw).strip():
                continue
            lines.append({
                "bbox": list(ln["bbox"]),
                "raw": raw,
                "bold": any("bold" in s["font"].lower() for s in ln["spans"]),
                "size": max(s["size"] for s in ln["spans"]),
                "decoded": decoded_any,
            })
    lines.sort(key=lambda l: (round(l["bbox"][1], 1), round(l["bbox"][0], 1)))
    return lines


# 页脚行按内容过滤（y 阈值兜底）：各年页脚 y 不一（750.5 / 793），
# 文本特征恒定：版权行、卷号行、[Turn over、整页占位 BLANK PAGE。
FOOTER_LINE_RE = re.compile(
    r"^(?:\xa9?\s*UCLES|\d{4}/\d{2}/|\[?\s*Turn over\s*\]?)", re.I)
BLANK_PAGE_RE = re.compile(r"^BLANK PAGE$", re.I)


def content_lines(lines, footer_y=None):
    """版心内容行：滤掉页眉（页码/条码）、页脚（UCLES/卷号/Turn over/水印/
    BLANK PAGE 占位）、点线装饰垃圾（', ,' 等）。footer_y 缺省用全局 FOOTER_Y。"""
    fy = FOOTER_Y if footer_y is None else footer_y
    out = []
    for ln in lines:
        b = ln["bbox"]
        if b[1] < HEADER_Y or b[1] > fy:
            continue
        raw = ln["raw"].strip()
        if not raw or JUNK_RE.fullmatch(raw):
            continue
        if FOOTER_LINE_RE.match(raw) or BLANK_PAGE_RE.match(raw):
            continue
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


def build_regions(start_page, sy0, end_page, end_y1, L, T, R, B):
    if end_page == start_page:
        return [{"page": start_page, "bbox": [r2(L), r2(sy0), r2(R), r2(end_y1)]}]
    regs = [{"page": start_page, "bbox": [r2(L), r2(sy0), r2(R), r2(B)]}]
    for mp in range(start_page + 1, end_page):
        regs.append({"page": mp, "bbox": [r2(L), r2(T), r2(R), r2(B)]})
    regs.append({"page": end_page, "bbox": [r2(L), r2(T), r2(R), r2(end_y1)]})
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


def trim_regions_trailing_blank(doc, regions, pages_content, content_cache, footer_y=None):
    """region 的页尾排版空白裁剪（原地修改，返回裁剪段数）。

    版心底 B 是全卷上限，题内容在页尾留下的排版空白裁到
    最后一项内容 + TRIM_PAD（CIE 无答题册，无需区分作答区性质）。
    调用方决定传全部 region（无锚点兜底的末段也要裁）还是 [:-1]。
    """
    n_trimmed = 0
    for rg in regions:
        bb = rg["bbox"]
        last_y = None
        for ln in pages_content[rg["page"]]:
            lb = ln["bbox"]
            if lb[3] < bb[1] - 1 or lb[1] > bb[3] + 1:
                continue
            if lb[2] < bb[0] or lb[0] > bb[2]:
                continue
            if last_y is None or lb[3] > last_y:
                last_y = lb[3]
        for y1, _y0, x0, x1 in page_content_rects(doc, rg["page"], content_cache, footer_y):
            if y1 < bb[1] - 1 or y1 > bb[3] + 1:
                continue
            if x1 < bb[0] or x0 > bb[2]:
                continue
            if last_y is None or y1 > last_y:
                last_y = y1
        if last_y is None:
            continue
        gap = bb[3] - last_y
        if gap <= TRIM_MIN_GAP:
            continue
        rg["bbox"] = [bb[0], bb[1], bb[2], r2(last_y + TRIM_PAD)]
        n_trimmed += 1
    return n_trimmed


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
    ap = argparse.ArgumentParser(description="CIE 9702 物理卷按题切割")
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
        sys.exit(f"cut_paper_cie.py 只服务 CIE 卷（board={board}）")
    paper_code_l = paper_code.lower()
    if paper_code_l not in PAPER_OF_CODE:
        sys.exit(f"不支持的 paper_code: {paper_code}（只支持 qp11–13 / qp21–23 / qp41–43，"
                 f"P3 实验卷与 P5 设计卷不切割）")
    paper_num, paper_total = PAPER_OF_CODE[paper_code_l]
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

    pages_lines, pages_content, pages_pairs = [], [], []
    n_decoded = 0
    for page in doc:
        lines = build_page_lines(page)
        n_decoded += sum(1 for ln in lines if ln["decoded"])
        pages_lines.append(lines)
    # 页脚 y 自适应：© UCLES 版权行 y 众数 − 1.5（每页都有页脚 → 众数即正文
    # 页脚位置；封面页版权行 y 可能不同，不能取 min——2022 P1 封面 716.7 /
    # 正文 749.6，取 min 会误杀正文底部内容）。2021 页脚 750.5，早年 793。
    ucles_ys = [round(ln["bbox"][1]) for lines in pages_lines for ln in lines
                if "UCLES" in ln["raw"]]
    footer_y = float(Counter(ucles_ys).most_common(1)[0][0]) - 1.5 if ucles_ys else FOOTER_Y
    pages_content = [content_lines(lines, footer_y) for lines in pages_lines]
    for pi in range(n_pages):
        words = doc[pi].get_text("words")
        pages_pairs.append(assign_words_to_lines(pages_lines[pi], words))
    if n_decoded:
        print(f"    A 型乱码解码: {n_decoded} 个 span 已还原")

    # ── 题号列 QCOL：题号行 x0 众数（各年在 49.6 / 72.4 / 72.8 之间浮动） ──
    digit_x0 = []
    for lines in pages_content:
        for ln in lines:
            b = ln["bbox"]
            if b[0] < 100.0 and re.match(r"^\d{1,2}(\s|\(|$)", ln["raw"].strip()):
                digit_x0.append(round(b[0]))
    if len(digit_x0) < 4:
        sys.exit("题号列定位失败（数字起始行过少）——疑似扫描卷或版式异常。")
    qcol = float(Counter(digit_x0).most_common(1)[0][0])

    # ── 版心：内容行坐标分位数 ──
    allc = [ln for lines in pages_content for ln in lines]
    xs0 = sorted(ln["bbox"][0] for ln in allc)
    xs1 = sorted(ln["bbox"][2] for ln in allc)
    ys0 = sorted(ln["bbox"][1] for ln in allc)
    L = max(25.0, pct(xs0, 0.005) - 6)
    R = min(575.0, pct(xs1, 0.995) + 6)
    T = max(45.0, pct(ys0, 0.005) - 4)
    B = min(790.0, footer_y)

    # ── 题号起点候选（序列约束 1..N） ──
    # P1: 题号可在页内任意位置（每页约 3 题）。
    # P2/P4: 大题**多数**从新页顶开始，但内容排满时下一题同页续排
    #（2025 qp41 Q7 紧跟 Q6 的 [Total: 12] 之后，y=298），不能限 y<130。
    # 独立行（"1 "）与合并行（"10 (a) State…"）两种形态；误报由序列约束挡。
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
            if n > (40 if is_mcq_paper else 20):
                continue
            if not is_mcq_paper:
                # P2/P4 防误报：独立行放行；合并行只接受「题号后跟 (a… 小问标签」
                # 或粗体行（正文/图表里以数字开头的行不粗体且不带标签）
                if not ln["bold"] and not re.match(r"^\d{1,2}\s*\(", raw):
                    tail = re.match(r"^\d{1,2}\s*(.*)$", raw).group(1)
                    if tail:  # 有尾随文字却既非标签也非粗体 → 不当题号
                        continue
            cands.append((pi, b[1], n, b, ln))
    cands.sort(key=lambda c: (c[0], c[1]))

    # 无文本层但含整页图片的页 = 源 PDF 的扫描替代页（如 2024 s42 p9）。
    # 这类页提取不到题号行，会让题号序列断裂；仅在 P2/P4 上按「紧邻下一页」
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
        sys.exit("未定位到任何题号起点，请人工检查 source.pdf。")

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
                end_page, end_y1 = en["page"], en["y1"] + 2
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
                # 末题：Space for working 或本页最后一个内容行
                sw = None
                for pi in range(st["page"], n_pages):
                    for ln in pages_content[pi]:
                        if (pi, ln["bbox"][1]) > (st["page"], st["y0"]) and \
                                SPACE_FOR_WORKING_RE.search(ln["raw"]):
                            sw = (pi, ln["bbox"][1])
                            break
                    if sw:
                        break
                if sw:
                    end_page, end_y1 = sw[0], sw[1] - 2
                    end_ok = True
                else:
                    end_page, end_y1 = st["page"], B
            if not is_mcq_paper and not end_ok:
                q["status"] = "review"
                conf = min(conf, 0.3)
        if is_mcq_paper and end_ok:
            conf += 0.2

        sy0 = st["y0"] - 4
        q["regions"] = build_regions(st["page"], sy0, end_page, end_y1, L, T, R, B)
        # 锚点终点（[Total]/Space for working/下一题同页起点）之下无排版空白，
        # 末段不裁；B 兜底的末段连页尾空白一起裁。
        trim_stats_n += trim_regions_trailing_blank(
            doc, q["regions"][:-1] if anchor_end else q["regions"],
            pages_content, content_cache, footer_y)

        if is_mcq_paper:
            conf += extract_mcq_options_cie(q, st, nxt, pages_content, pages_pairs, R, B, notes)
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

    # 分值合计校验（P2/P4 用 [Total] 锚点合计，P1 每题 1 分）
    if is_mcq_paper:
        marks_sum_all = n_questions
        for q in auto_questions:
            q["total_marks"] = 1
    expect_total = paper_total_marks if paper_total_marks is not None else paper_total
    if marks_sum_all != expect_total:
        # 优先只归咎「无分值来源」的题（缺 [Total] 且行内 [n] 兜底也失败）；
        # 若每题都有分值却合计不符 → 疑锚点误读，整卷 review。
        culprits = [q for q in auto_questions if not q.get("total_marks")]
        for q in (culprits or auto_questions):
            q["status"] = "review"
            q["review_notes"].append(
                f"全卷分值合计 {marks_sum_all} ≠ 试卷总分 {expect_total}")

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
    cut["source"]["paper_ref"] = f"9702_{season_letter}{str(year)[2:]}_{paper_code_l}"
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

def extract_mcq_options_cie(q, st, nxt, pages_content, pages_pairs, R, B, notes):
    """CIE P1 选项：独立 [A-D] 标签行（可同行四列），标签下界=下一选项行/题末。

    下界钳制到本题 region 底（末段可能被页尾空白裁剪过），否则渲染层
    归一化坐标会越界（layout.schema h≤1）。
    返回置信度增量（4 选项齐全 +0.4）。
    """
    pi = st["page"]
    y_top = st["y0"]
    scan_bottom = nxt["y0"] - 2 if (nxt and nxt["page"] == pi) else B
    if q["regions"] and q["regions"][-1]["page"] == pi:
        scan_bottom = min(scan_bottom, q["regions"][-1]["bbox"][3])
    labels = []
    for ln in pages_content[pi]:
        b = ln["bbox"]
        if b[1] < y_top or b[1] >= scan_bottom:
            continue
        m = OPTION_LABEL_RE.match(ln["raw"].strip())
        if m:
            labels.append({"label": m.group(1), "y0": b[1], "x0": b[0], "x1": b[2]})
    uniq, seen = [], set()
    for lb in sorted(labels, key=lambda x: x["y0"]):
        if lb["label"] not in seen:
            seen.add(lb["label"])
            uniq.append(lb)
    opts = []
    if uniq:
        limit_y = scan_bottom
        for i, lb in enumerate(uniq):
            nxt_row = next((o for o in uniq if o["y0"] > lb["y0"] + 3), None)
            y_end = nxt_row["y0"] - 2 if nxt_row else limit_y
            x1 = R
            for other in uniq:
                if other is not lb and abs(other["y0"] - lb["y0"]) <= 3 and other["x0"] > lb["x0"]:
                    x1 = min(x1, other["x0"] - 6)
            bbox = [r2(lb["x0"] - 6), r2(lb["y0"] - 2), r2(x1), r2(y_end)]
            parts, rows = [], []
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
    lines.append("source: " + yaml_scalar(f"CIE 9702/{paper_code.upper()}, {year} {session}"))
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
