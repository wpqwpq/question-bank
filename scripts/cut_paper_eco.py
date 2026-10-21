#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""cut_paper_eco.py — Edexcel IAL 经济卷（WEC11–14）按题切割

为什么单独一份：
    经济卷的「三段」结构与会计相似但顺序相反，且本次**不切作答区**。
      · 普通国际卷（80 份，单遍）：QP（题面与点线作答区逐小问交错）+ Source Booklet；
      · 01A 中国区卷（12 份，两遍）：紧凑 QP（只有题面）+ Source Booklet + Answer Booklet。
    2026-10-19 王培琪定：**题图只含题面（＋该题资料册），不含点线作答区**，
    拼接顺序 **题面 → 资料**（会计是 资料 → 题面 → 作答区）。
    故 cut.json 用 `regions`（题面）+ `material_regions`（资料），
    并写 `material_order: "after"` 让渲染层把资料拼在题面之后；
    不写 answer_block / answer_regions。

切点靠什么：
    每题必以 `(Total for Question N = X marks)` 收尾（普通卷 1 次、01A 2 次）。
    普通卷里作答区与题面同页交错，所以终点的最终形态是：
      ① 逐页判定「作答页」——页首（y<220）是小问标签行、或整页只有点线 → 整页丢弃；
      ② 起点页内，从**第一行点线**往下全裁（Section B 的 4 分题即此类，
         连带裁掉点线下方那行 Total——分值在 front-matter，前端也显示）。

资料册归属：
    优先解析 QP 里的 `... in the Source Booklet before answering Question N`；
    21 卷缺这句 → 按单元固定（WEC11/12 → Q12，WEC13/14 → Q7）。

用法:
    python scripts/cut_paper_eco.py <session_dir>            # 切割 + 写 cut.json/qNN.md
    python scripts/cut_paper_eco.py <session_dir> --analyze  # 只打印锚点表，不落盘

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
    FOOTER_Y,
    HEADER_Y,
    LEFT_MARGIN_MAX,
    QNUM_ONLY_RE,
    QSTART_RE,
    ROMAN_TOKENS,
    SECTION_RE,
    TOTAL_RE,
    TRIM_MIN_GAP,
    TRIM_PAD,
    assign_words_to_lines,
    build_page_lines,
    build_regions,
    merge_with_existing,
    parse_bare_subhead,
    parse_subhead,
    r2,
    sub_label,
    write_question_md,
    extract_mcq_options,
)

# ── 版心：按本卷 page_size 自适应 ────────────────────────────────────────────
# 经济卷实测**两种页面**：595×842（90 卷）与 652×899（2019-june/oct 共 3 卷）。
# 早先把 L39/R556/T40/B788 写死是按 595 推的，对 652 宽的卷右侧白丢 96pt
# （2019-june-wec11 Q4 的 "Quantity of education" 被切成 "educat"），
# 且 FOOTER_Y=790 会把 899 高页面上 790–845 的正文当页脚丢掉。
# A4 分支保持旧值，避免 90 卷因 0.28pt 的差异全部重渲。
L_DEF, T_DEF = 39.0, 40.0
R_DEF, B_DEF = 556.0, 788.0
A4_W, A4_H = 595.28, 841.89
G = {"L": L_DEF, "T": T_DEF, "R": R_DEF, "B": B_DEF, "footer": FOOTER_Y}


# 页脚行：页码、"Turn over"、条码
FOOT_LINE_RE = re.compile(r"^(\d{1,3}|Turn over|\*?[A-Z]\d{5}[A-Z]\*?)$", re.I)


def set_geom(doc):
    """按页面尺寸定版心与页脚线。整卷页面尺寸一致（93 卷实测无混排）。

    A4（595×842，90 卷）走原值，一个字节都不变。
    非 A4（652×899，2019-june/oct 共 3 卷）**按实测页脚行推**，不能按高度减固定值：
    这卷页码在 y=827.7，而 899−52=847 会把它当正文扫进来——页码竖排在 x=77.6
    且是粗体，`find_qstart` 直接把它认成题号 12（2026-10-19 踩过：Q12 起点跑到 p12、
    Q14 抢了 Q12 的段）。
    """
    r = doc[0].rect
    if abs(r.width - A4_W) < 1.5 and abs(r.height - A4_H) < 1.5:
        G.update(L=L_DEF, T=T_DEF, R=R_DEF, B=B_DEF, footer=FOOTER_Y)
        return G
    cands = []
    for pi in range(doc.page_count):
        for w in doc[pi].get_text("words"):
            if w[1] < r.height * 0.9:
                continue
            t = w[4].strip()
            if FOOT_LINE_RE.match(t) or t.startswith("*P"):
                cands.append(w[1])
    foot = min(cands) if cands else r.height - 72.0
    # 右侧边栏（竖排 "DO NOT WRITE IN THIS AREA" 的横线组，x≈597–652）必须排除，
    # 否则 652−39=613 的右沿会把它切进来，图右侧露出几条黑短线。
    xr = r.width
    for pi in range(min(doc.page_count, 6)):
        for dr in doc[pi].get_drawings():
            db = dr["rect"]
            if db.x0 > r.width * 0.85 and db.x1 > r.width - 4:
                xr = min(xr, db.x0)
    right = min(r2(r.width - 39.0), r2(xr - 2.0)) if xr < r.width else r2(r.width - 39.0)
    G.update(L=39.0, T=40.0, R=right, B=r2(foot - 13.0), footer=r2(foot))
    return G

# 作答页判定：页首内容行 y0 低于此值才算「页首」
ANSWER_TOP_Y = 220.0

# 资料册归属：缺 `before answering Question N` 时的兜底
MATERIAL_DEFAULT = {"wec11": 12, "wec12": 12, "wec13": 7, "wec14": 7}

SRC_TITLE_RE = re.compile(r"^\s*Source Booklet\s*$", re.I)
ANS_COVER_RE = re.compile(r"Please check the examination details", re.I)
# "… in the Source Booklet before answering Question 12" 这句常跨行
# （`before answering` 与 `Question 12` 分处两行），故各词之间一律用 \s+。
STUDY_RE = re.compile(r"before\s+answering\s+Question\s*(\d+)", re.I)
BLANK_PAGE_RE = re.compile(r"^\s*BLANK PAGE\s*$", re.I)


def sanitize_lines(pages_lines):
    """把行首/行中的 \x07（原卷勾选框符号 "☒" 的文本层编码）换成空格。

    2020-jan-wec11 Q3 的选项写成 `\x07A\t \x07Government provision…`，
    OPTION_RE 要求行首是 A–D，被这个控制字符顶掉后只认出 D 一个选项，
    该题被标成 review（2026-10-19 全量核查发现）。
    """
    n = 0
    for ls in pages_lines:
        for ln in ls:
            if "\x07" in ln["raw"]:
                ln["raw"] = ln["raw"].replace("\x07", " ")
                n += 1
    return n


def is_dotted(ln):
    """作答点线行：整行只有 '.' 与空白，且够长（表格里的 '.' 不会这么长）。"""
    t = ln["raw"].strip()
    return len(t) >= 20 and set(t) <= set(". ")


def content_lines(page_lines):
    """去掉页眉页脚后的正文行。"""
    out = []
    for ln in page_lines:
        b = ln["bbox"]
        if b[1] < HEADER_Y or b[3] > G["footer"]:
            continue
        out.append(ln)
    return out


def is_subhead_line(ln, n):
    """该行是否小问标签行（"N (a) …" 或裸 "(b) …"）。"""
    p = parse_subhead(ln["raw"])
    if p and p[0] == n and p[2]:
        return True
    bare = parse_bare_subhead(ln["raw"])
    return bool(bare and bare[1])


def dotted_rows(page_lines):
    return sum(1 for ln in page_lines
               if is_dotted(ln) and HEADER_Y <= ln["bbox"][1] < G["footer"])


def is_answer_page(page_lines, n):
    """作答页判定。两条判据命中任一即为作答页：

    ① 该页有 ≥3 行作答点线——最硬的判据。普通卷的作答页一定铺满点线；
    ② 页首（y<220）是小问标签行、或整页只剩点线。

    ② 单独用会漏：2021-june-wec14 的 Q7 作答页（p7）首行是**题干的续尾**
    （"In 2018, the EU's rate of inflation was 1.6%."），接着才是 "(a)…"，
    于是整页被并进题面（2026-10-19 实测 2756pt / 7656px）。所以以 ① 为主。

    只在「题起点页之后」的页面上调用——起点页本身有点线是正常的
    （Section B 的 4 分小题：题面 + 点线同页），由页内点线裁剪处理。
    """
    rows = [ln for ln in content_lines(page_lines) if ln["bbox"][3] > HEADER_Y]
    if not rows:
        return False
    if dotted_rows(page_lines) >= 3:
        return True
    text = [ln for ln in rows if not is_dotted(ln)]
    if not text:
        return True                      # 整页只有点线
    first = text[0]
    return first["bbox"][1] < ANSWER_TOP_Y and is_subhead_line(first, n)


def find_qstart(n, lo, hi, pages_lines):
    """块内找第 n 题的题号行。返回 line 或 None。

    经济卷题号行一律粗体、顶格（x0≈42.5），故以粗体 + 左缘为准；
    表格里的数字、坐标轴刻度既不在左缘也不粗体。
    """
    cands = []
    for pi in range(lo, min(hi, len(pages_lines) - 1) + 1):
        for ln in pages_lines[pi]:
            b = ln["bbox"]
            if b[1] < HEADER_Y or b[1] >= G["footer"] or b[0] > LEFT_MARGIN_MAX:
                continue
            if not ln["bold"]:
                continue
            t = ln["raw"].strip()
            m = QSTART_RE.match(t) or QNUM_ONLY_RE.match(t)
            if m and int(m.group(1)) == n:
                cands.append((pi, b[1], ln))
    if cands:
        cands.sort(key=lambda c: (c[0], c[1]))
        return {**cands[0][2], "page": cands[0][0]}
    # 兜底：不要求粗体，但只认页面最上方三行内的题号（避免抓到表格数字）
    for pi in range(lo, min(hi, len(pages_lines) - 1) + 1):
        top3 = [ln for ln in content_lines(pages_lines[pi])
                if ln["bbox"][0] <= LEFT_MARGIN_MAX][:3]
        for ln in top3:
            t = ln["raw"].strip()
            m = QSTART_RE.match(t) or QNUM_ONLY_RE.match(t)
            if m and int(m.group(1)) == n:
                return {**ln, "page": pi}
    return None


def section_map(pages_lines, lo, hi):
    """[(page, y, 'A'), …]，文档序。"""
    out = []
    for pi in range(lo, min(hi, len(pages_lines) - 1) + 1):
        for ln in pages_lines[pi]:
            b = ln["bbox"]
            if b[1] < HEADER_Y or b[1] >= G["footer"]:
                continue
            m = SECTION_RE.match(ln["raw"].strip())
            if m:
                out.append((pi, b[1], m.group(1).upper(), ln))
    return out


def head_has_study(pages_lines, sec_ent, st):
    """SECTION 头与本节首题题号行之间，是否有 "Study … Source Booklet … before answering" 指引。

    这是「要不要连带 SECTION 头」的唯一判据（2026-10-19 王培琪定）：
      · 有 → 连带（这句是答题必需指引，如 WEC11/12 的 SECTION C、WEC13/14 的 SECTION B）；
      · 无 → 整个头块丢掉（只剩 "Answer ONE question…" / "Write your answer…" /
        "EITHER" 这类纯版式说明，题卡头已显示 Section 字段，图里再留一遍是噪音）。
    ⚠️ 指引句常跨行（`before answering` 与 `Question 12` 分处两行），故把区间内
       所有行拼成一句再 search，不做逐行匹配（01A 卷 100% 是跨行的）。
    """
    sp, sy = sec_ent[0], sec_ent[1]
    buf = []
    for pi in range(sp, min(st["page"], len(pages_lines) - 1) + 1):
        for ln in pages_lines[pi]:
            b = ln["bbox"]
            if pi == sp and b[1] < sy - 1:
                continue
            if pi == st["page"] and b[1] > st["bbox"][1] + 1:
                continue
            buf.append(ln["raw"])
    return bool(STUDY_RE.search(" ".join(buf)))


def extend_over_rule(doc, page, y, content_cache):
    """终点 y 若正好切在一条横线上，把终点推到线下方 2pt。

    Total 行下方常压着一条分隔横线（宽度接近版心）。终点取 Total 底 +2 时
    会横穿这条线，渲染出来是半条灰线，看着像字被切了（2026-10-19 踩过）。
    宁可把整条线包进来——完整的细线像题目的收尾，半条像 bug。
    """
    for y1, y0, x0, x1, is_hline in content_rects(doc, page, content_cache):
        if not is_hline or (x1 - x0) < 300:
            continue
        if y0 - 3 <= y <= y1 + 1:
            return r2(y1 + 2)
    return y


def collect_subquestions(regions, pages_lines):
    """题面段内的标签行 → [{label,page,bbox}]。罗马数字接在父字母后（a i）。"""
    subs = []
    parent = None
    for rg in regions:
        bb = rg["bbox"]
        for ln in pages_lines[rg["page"]]:
            lb = ln["bbox"]
            if lb[1] < HEADER_Y or lb[1] >= G["footer"] or lb[0] > LEFT_MARGIN_MAX:
                continue
            if lb[1] < bb[1] - 1 or lb[3] > bb[3] + 1:
                continue
            tokens = starred = None
            parsed = parse_subhead(ln["raw"])
            if parsed and parsed[2]:
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
            # 同一标签重复出现（续页复述）只留第一次
            if any(s["label"] == ent["label"] for s in subs):
                continue
            subs.append(ent)
    return subs


def trim_all(doc, regions, pages_lines, content_cache, word_cache, allow_extend=False):
    """把每段（含末段）的下界收到最后一项内容 + TRIM_PAD（原地修改）。

    经济卷没有「以 Total 行收尾」的段之外的东西——题面段要么止于点线上沿，
    要么止于 Total 行，要么止于页底；页底那种都该收。

    ⚠️ 图表 x 轴的年份标签是**旋转文字**，`build_page_lines` 会跳过它们，
    只按行算 last_y 会把资料页底边切在年份中间（2021-june-wec14 Q7 实测
    19 处压字）。故补一遍 `get_text("words")` 的词框，旋转与否一并兜住。
    """
    trimmed = 0
    for rg in regions:
        bb = rg["bbox"]
        last_y = None
        for ln in pages_lines[rg["page"]]:
            lb = ln["bbox"]
            if lb[3] > G["footer"] or lb[1] < bb[1] - 1:
                continue
            if lb[2] < bb[0] or lb[0] > bb[2]:
                continue
            if last_y is None or lb[3] > last_y:
                last_y = lb[3]
        for w in page_words(doc, rg["page"], word_cache):
            wx0, wy0, wx1, wy1 = w[0], w[1], w[2], w[3]
            if wy0 < bb[1] - 1:
                continue
            if wy0 >= (G["footer"] + 30 if allow_extend else G["footer"]):
                continue
            # 页码/条码/Turn over 不算内容；但**压在页脚带上的正文要算**——
            # 资料末页的致谢常一直排到 y≈820，超过版心底 788，
            # 只按版心底切会把它切一半（2023-june-wec14 Q7 实测）。
            t = w[4].strip()
            if t and FOOT_LINE_RE.match(t) and wy0 >= G["footer"] - 12:
                continue
            if wx1 < bb[0] or wx0 > bb[2]:
                continue
            if last_y is None or wy1 > last_y:
                last_y = wy1
        for y1, _y0, x0, x1, _h in content_rects(doc, rg["page"], content_cache):
            if y1 > G["footer"] or y1 < bb[1] - 1:
                continue
            if x1 < bb[0] or x0 > bb[2]:
                continue
            if last_y is None or y1 > last_y:
                last_y = y1
        if last_y is None:
            continue
        if allow_extend and last_y > bb[3] + 1:
            # 内容超出段底（压在页脚带上的正文）→ 扩底，别切字
            rg["bbox"] = [bb[0], bb[1], bb[2],
                          r2(min(last_y + TRIM_PAD, doc[rg["page"]].rect.height - 2))]
            trimmed += 1
            continue
        if bb[3] - last_y <= TRIM_MIN_GAP:
            continue
        rg["bbox"] = [bb[0], bb[1], bb[2], r2(last_y + TRIM_PAD)]
        trimmed += 1
    return trimmed


def content_rects(doc, pi, cache):
    """page_content_rects 的本地版：页脚线按本卷 page_h 算（原版写死 790）。

    652×899 的 3 卷内容区到 y≈845，用 790 当页脚会把下半页的正文/图形全丢掉，
    底边裁剪随之算错（2026-10-19 实测）。
    """
    if pi not in cache:
        rows = []
        footer = G["footer"]
        try:
            for dr in doc[pi].get_drawings():
                db = dr["rect"]
                if (db.width <= 0 and db.height <= 0) or db.y1 > footer:
                    continue
                if db.width > 400 and db.height > 600:
                    continue          # 整页装饰边框，非内容
                rows.append((db.y1, db.y0, db.x0, db.x1,
                             db.height <= 1.5 and db.width >= 50))
        except Exception:
            pass
        for b in doc[pi].get_text("dict")["blocks"]:
            if b.get("type") == 1 and b["bbox"][3] <= footer:
                bb = b["bbox"]
                rows.append((bb[3], bb[1], bb[0], bb[2], False))
        cache[pi] = rows
    return cache[pi]


def page_words(doc, pi, cache):
    """该页所有词框（含旋转文字）。裁白时用来兜住 build_page_lines 漏掉的旋转标签。"""
    if pi not in cache:
        cache[pi] = [w for w in doc[pi].get_text("words")]
    return cache[pi]


def trim_top(doc, regions, pages_lines, content_cache, word_cache):
    """把每段的上界收到第一项内容之上 6pt（原地修改，返回收缩的段数）。

    资料册页面常见两种顶部空白：① 章节标题前的排版留白；
    ② 「BLANK PAGE」标签压在页面下半部、上面半页全空（引文页 p32 那种）。
    不收的话题图会出现半页白。**只用于资料段**——题面段的起点是锚点，不能动。
    """
    n = 0
    for rg in regions:
        bb = rg["bbox"]
        first_y = None
        for ln in pages_lines[rg["page"]]:
            lb = ln["bbox"]
            if lb[1] < HEADER_Y or lb[3] > G["footer"]:
                continue
            if BLANK_PAGE_RE.match(ln["raw"].strip()):
                continue                      # 「BLANK PAGE」标签不算内容
            if first_y is None or lb[1] < first_y:
                first_y = lb[1]
        # "BLANK PAGE" 标签所占据的带：words 兜底时要跳过它，否则整页白顶收不掉
        # （引文页那个 "BLANK PAGE" 印在页面中部、下面才是 Acknowledgements）
        blank_bands = [(ln["bbox"][1] - 1, ln["bbox"][3] + 1)
                       for ln in pages_lines[rg["page"]]
                       if BLANK_PAGE_RE.match(ln["raw"].strip())]
        # 旋转文字（图表轴标签常竖排）不进 pages_lines，用词框兜底
        for w in page_words(doc, rg["page"], word_cache):
            if w[1] < HEADER_Y or w[1] >= G["footer"]:
                continue
            if w[2] < bb[0] or w[0] > bb[2]:
                continue
            if any(a <= w[1] <= b for a, b in blank_bands):
                continue
            if first_y is None or w[1] < first_y:
                first_y = w[1]
        for y1, y0, _x0, _x1, _h in content_rects(doc, rg["page"], content_cache):
            if y1 > G["footer"] or y1 - y0 <= 20:
                continue                      # 细线（表格栅格/横线）不算段落起点
            if first_y is None or y0 < first_y:
                first_y = y0
        if first_y is None:
            continue
        new_top = r2(max(G["T"], first_y - 6))
        if new_top - bb[1] > 2:
            rg["bbox"] = [bb[0], new_top, bb[2], bb[3]]
            n += 1
    return n


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
    ap = argparse.ArgumentParser(description="Edexcel IAL 经济卷按题切割")
    ap.add_argument("session_dir")
    ap.add_argument("--analyze", action="store_true", help="只打印锚点表，不落盘")
    ap.add_argument("--questions", default=None, help="只处理这些题号（逗号分隔，调试用）")
    args = ap.parse_args()

    sdir = Path(args.session_dir).resolve()
    src = sdir / "source.pdf"
    if not src.is_file():
        sys.exit(f"缺少 source.pdf: {sdir}")

    doc = pymupdf.open(src)
    set_geom(doc)                     # 版心/页脚线按本卷页面尺寸定，必须先于一切取行
    pages_lines = [build_page_lines(pg) for pg in doc]
    sanitize_lines(pages_lines)
    pages_pairs = [assign_words_to_lines(pages_lines[i], doc[i].get_text("words"))
                   for i in range(doc.page_count)]
    pages_text = [pg.get_text() for pg in doc]
    content_cache = {}
    word_cache = {}
    n_pages = doc.page_count

    meta = read_paper_meta(sdir)
    paper_code = (meta.get("paper_code") or sdir.name.split("-")[-1])
    if sdir.name.endswith("a") and not paper_code.lower().endswith("a"):
        paper_code += "a"
    # 卷号必须小写传下去：qid 是 `...-<paper_code>-qNN`，schema 的 id 模式只允许小写；
    # write_question_md 内部再 .upper() 还原 front-matter 的 paper_code 字段。
    paper_code = paper_code.lower()
    unit_key = paper_code.rstrip("a")
    year = int(meta.get("year") or re.search(r"(\d{4})", sdir.name).group(1))
    session = meta.get("session") or sdir.name.split("-")[1]

    # ── 1 段划分：资料册标题页 / 答题册封面 ──
    src_title = None
    for i, t in enumerate(pages_text):
        if i == 0:
            continue                      # 封面可能也印一行 "Source Booklet"
        if not TOTAL_RE.search(t) and any(
                SRC_TITLE_RE.match(ln) for ln in t.splitlines()):
            src_title = i
            break
    if src_title is None:
        sys.exit("未找到 Source Booklet 标题页")

    ans_cover = None
    for i in range(src_title + 1, n_pages):
        if ANS_COVER_RE.search(pages_text[i]):
            ans_cover = i
            break

    qp_lo, qp_hi = 0, src_title - 1
    mat_lo = src_title + 1
    mat_hi = (ans_cover - 1) if ans_cover is not None else n_pages - 1

    # ── 2 Total 锚点（只在 QP 段内） ──
    by_n = {}
    for pi in range(0, qp_hi + 1):
        for ln in pages_lines[pi]:
            m = TOTAL_RE.search(ln["raw"])
            if m:
                n, mk = int(m.group(1)), int(m.group(2))
                if n not in by_n:
                    by_n[n] = {"page": pi, "n": n, "marks": mk,
                               "bbox": [r2(v) for v in ln["bbox"]]}
    if not by_n:
        sys.exit("未识别出任何 Total 锚点")

    n_list = sorted(by_n)
    secs = section_map(pages_lines, qp_lo, qp_hi)

    # ── 3 资料归属 ──
    material_for = None
    for pi in range(0, qp_hi + 1):
        m = STUDY_RE.search(pages_text[pi])
        if m:
            material_for = int(m.group(1))
            break
    mat_src = "卷面 Study 句"
    if material_for is None:
        material_for = MATERIAL_DEFAULT.get(unit_key)
        mat_src = f"单元固定规则（{unit_key} → Q{material_for}）"

    # 资料块：整页
    mat_regions = []
    for pi in range(mat_lo, min(mat_hi, n_pages - 1) + 1):
        rows = content_lines(pages_lines[pi])
        if not rows:
            continue                       # 纯空白页不进资料段
        if all(BLANK_PAGE_RE.match(ln["raw"].strip()) for ln in rows):
            continue                       # 整页只有「BLANK PAGE」
        mat_regions.append({"page": pi, "bbox": [r2(G["L"]), r2(G["T"]), r2(G["R"]), r2(G["B"])]})
    if mat_regions:
        trim_top(doc, mat_regions, pages_lines, content_cache, word_cache)
        trim_all(doc, mat_regions, pages_lines, content_cache, word_cache, allow_extend=True)

    print(f"=== {sdir.name}  {n_pages} 页  题数 {len(n_list)} ===")
    print(f"    段: qp p1-{qp_hi + 1} | material p{mat_lo + 1}-{mat_hi + 1}"
          + (f" | answer p{ans_cover + 1}-{n_pages}（不用）" if ans_cover is not None else ""))
    print(f"    资料归属: Q{material_for}（{mat_src}）")

    notes = []
    questions = []

    # 本节首题 = 该节最小题号
    sec_first = {}
    q_sec = {}
    for n in n_list:
        st = find_qstart(n, qp_lo, qp_hi, pages_lines)
        if st is None:
            notes.append(f"Q{n}: 未找到题号行")
            continue
        best = None
        for sp, sy, sname, sln in secs:
            if (sp, sy) < (st["page"], st["bbox"][1]):
                best = (sp, sy, sname, sln)
        q_sec[n] = (best[2] if best else None, best)
        if best and best[2] not in sec_first:
            sec_first[best[2]] = n

    only = {int(x) for x in args.questions.split(",")} if args.questions else None
    for n in n_list:
        if only and n not in only:
            continue
        st = find_qstart(n, qp_lo, qp_hi, pages_lines)
        en = by_n.get(n)
        if st is None:
            continue
        q = {"number": str(n), "status": "auto", "confidence": 0.95,
             "question_type": "structured", "total_marks": en["marks"], "anchors": {}}

        start_p, start_y = st["page"], r2(st["bbox"][1] - 1)
        # 本节首题：只有头块里带 Study 指引才上移到 SECTION 头（见 head_has_study）
        sec_name, sec_ent = q_sec.get(n, (None, None))
        if sec_name and sec_first.get(sec_name) == n and sec_ent is not None:
            if head_has_study(pages_lines, sec_ent, st):
                start_p, start_y = sec_ent[0], r2(sec_ent[1] - 1)
                q["anchors"]["section_head"] = {"page": sec_ent[0],
                                                "bbox": [r2(v) for v in sec_ent[3]["bbox"]]}
            else:
                q["anchors"]["section_head_dropped"] = {
                    "page": sec_ent[0], "bbox": [r2(v) for v in sec_ent[3]["bbox"]]}
        q["anchors"]["start"] = {"page": st["page"], "bbox": [r2(v) for v in st["bbox"]]}
        q["anchors"]["end"] = {"page": en["page"], "bbox": [0.0, 0.0, 0.0, r2(en["bbox"][3])]}
        if sec_name:
            q["section"] = sec_name

        # ── 终点：先到 Total；再被「作答页整页丢弃 / 页内点线全裁」收紧 ──
        end_p, end_y = en["page"], r2(en["bbox"][3])
        cut_reason = "total"

        # ① 起点页之后的作答页 → 截到该页上沿（前一段页底）
        for pi in range(start_p + 1, en["page"] + 1):
            if is_answer_page(pages_lines[pi], n):
                end_p, end_y = pi - 1, G["B"]
                cut_reason = f"作答页 p{pi + 1} 起整页丢弃"
                break

        # ② 起点页内第一行点线 → 从那里往下全裁
        first_dot = None
        for ln in pages_lines[start_p]:
            lb = ln["bbox"]
            if lb[1] < HEADER_Y or lb[1] >= G["footer"]:
                continue
            if lb[3] <= start_y:
                continue
            if is_dotted(ln):
                first_dot = lb[1]
                break
        if first_dot is not None and end_p == start_p:
            if cut_reason == "total":
                cut_reason = "页内点线全裁"
            end_y = r2(min(end_y, first_dot - 2))

        if (start_p, start_y) >= (end_p, end_y):
            q["status"] = "review"
            notes.append(f"Q{n}: 起点不早于终点，跳过")
            continue

        if cut_reason == "total":
            end_y = extend_over_rule(doc, end_p, end_y, content_cache)

        regs = build_regions(start_p, start_y, end_p, end_y, G["L"], G["T"], G["R"], G["B"])
        trim_all(doc, regs, pages_lines, content_cache, word_cache)
        q["regions"] = regs
        q["anchors"]["cut"] = cut_reason

        # ── 资料 ──
        if material_for == n and mat_regions:
            q["material_regions"] = [dict(r) for r in mat_regions]

        # ── 小问标签 ──
        subs = collect_subquestions(regs, pages_lines)
        if subs:
            q["subquestions"] = subs

        # ── 选择题热区 ──
        is_mcq = (sec_name == "A") if sec_name else (en["marks"] == 1)
        q["question_type"] = "mcq" if is_mcq else "structured"
        if is_mcq:
            opts = extract_mcq_options(n, q, pages_lines, pages_pairs, st, en,
                                       G["R"], G["B"], notes)
            q["_options"] = opts
            q["option_hotzones"] = [{k: o[k] for k in ("label", "bbox", "part") if k in o}
                                    for o in opts]
            labels = {o["label"] for o in opts}
            if labels != {"A", "B", "C", "D"}:
                q["status"] = "review"
                notes.append(f"Q{n}: MCQ 选项不全 {sorted(labels)}")
        questions.append(q)

        if args.analyze:
            span = f"p{regs[0]['page'] + 1}"
            if regs[-1]["page"] != regs[0]["page"]:
                span += f"-{regs[-1]['page'] + 1}"
            pt = sum(r["bbox"][3] - r["bbox"][1] for r in regs)
            extra = ""
            if q.get("material_regions"):
                mp = q["material_regions"]
                extra += f" +资料 p{mp[0]['page'] + 1}-{mp[-1]['page'] + 1}"
                pt += sum(r["bbox"][3] - r["bbox"][1] for r in mp)
            print(f"  Q{n:>2} {en['marks']:>3}分 sec={sec_name or '-':<2} {span:<9}"
                  f"{pt:6.0f}pt ({pt * 200 / 72:5.0f}px){extra:<16}"
                  f" sub={len(subs)} {q['question_type']:<10} {cut_reason}")

    if args.analyze:
        if notes:
            print("\n[提示]")
            for x in notes:
                print("  -", x)
        print(f"\n共 {len(questions)} 题（analyze 模式，未落盘）")
        return

    # ── 落盘 ──
    sha = hashlib.sha256(src.read_bytes()).hexdigest()
    cut_path = sdir / "cut.json"
    existing = None
    if cut_path.exists():
        try:
            existing = json.loads(cut_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            existing = None
        if existing and existing.get("source", {}).get("sha256") != sha:
            print("  [warn] source.pdf 与既有 cut.json 的 sha256 不符，丢弃旧坐标重新生成")
            existing = None

    merged, report = merge_with_existing(questions, existing)
    for q in merged:
        for k in [k for k in q if k.startswith("_")]:
            q.pop(k, None)

    out = {
        "schema_version": 1,
        "source": {"file": "source.pdf", "sha256": sha, "pages": n_pages,
                   "paper_ref": meta.get("paper_ref") or ""},
        "page_size": page_size(doc),
        "render": {"dpi": 200, "format": "webp", "quality": 85},
        "blocks": ["qp", "material"],
        "material_order": "after",
        "questions": merged,
    }
    cut_path.write_text(json.dumps(out, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")

    for q in merged:
        write_question_md(sdir, int(q["number"]), {**q, "status": q.get("status", "auto")},
                          "edexcel", "a-level", "economics", year, session,
                          paper_code, pages_lines, pages_pairs)

    # 01A 卷的 source 行修成 "WEC11/01A"——write_question_md 只会拼 "<CODE>/01"，
    # 而 paper_code 字段必须带 A（与普通卷区分 id），两者不能共用一种写法。
    if paper_code.endswith("a") and paper_code[:-1]:
        base = paper_code[:-1].upper()
        pat = re.compile(r'^source: .*$', re.M)
        for q in merged:
            p = sdir / f"q{int(q['number']):02d}.md"
            if not p.exists():
                continue
            txt = p.read_text(encoding="utf-8")
            new = pat.sub(f'source: "Pearson Edexcel IAL {base}/01A, {year}"', txt, count=1)
            if new != txt:
                p.write_text(new, encoding="utf-8")

    print(f"\ncut.json 已写入（{len(merged)} 题）")
    for line in report:
        print("  ·", line)
    if notes:
        print("[提示]")
        for x in notes:
            print("  -", x)


if __name__ == "__main__":
    main()
