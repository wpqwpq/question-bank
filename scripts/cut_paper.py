#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""cut_paper.py — 真题试卷按大题自动切割（Edexcel 策略）

⚠️ 适用范围：**Edexcel IAL 物理 / 化学共用同一套脚本**（2026-10-14 合并为单份超集，
    原先 scripts/chemistry/ 的副本已撤销）；生物待实测；AQA / CIE 尚不支持。
    脚本内嵌的策略来自 Edexcel 物理卷（WPH11–WPH16，2026-june 实测）与
    化学卷（WCH11–WCH16，2025-oct 实测）的版式经验：
    · 锚点格式：题号行 "N (a)…" / Total 分值行的正则按理化卷题面归纳；
    · 版心与页框假设基于 Pearson 版式：每页 y≈794 的整页圆角装饰边框、
      左下角竖排卷号（P87xxx A）在文本层还原为 y≈770–794 的假横排行；
    · 小问标签体系（字母 + 罗马数字接续行）与答题册另附卷（/01A 中国区卷）
      的检测逻辑亦按理化卷结构观测归纳。
    学科/卷式差异一律在**同一份脚本内用条件触发**表达（如化学的纯分组头
    "~group" 映射：仅当册内出现独立 "(a)" 行、而 QP 是 "(a)(i)…" 连排时生效，
    物理卷不满足条件即走原路径），不按学科分叉目录——避免同一修复在多份副本
    间漂移（"~group" 曾只因目录分叉而漏同步到物理侧）。
    AQA / CIE（双栏 + 方括号分值）版式不同，将来需另写锚点策略层，
    届时可复用本仓库统一的坐标约定与 cut.json 数据结构（schema/question.schema.json）。

用法:
    python scripts/cut_paper.py <session_dir>

<session_dir> 指向 data/<board>/<qualification>/<subject>/<session>/，
目录内须已放入 source.pdf（原卷无损副本）与 paper.md（含 total_marks 更佳）。

产出:
    cut.json               每题页码+边界+状态（权威数据，人工校对改这里）
    qNN.md                 题目脚手架（front-matter 元数据 + 提取的题干文本）
    assets/qNN.layout.json 由 render_questions.py 产出：题图内小问作答区 /
                           MCQ 选项框的归一化坐标（学习机交互层）

特性:
    · 页尾空白裁剪：非末段 region 的页尾排版空白（内容放不下流到下一页，
      本页底部剩下的空白）一律裁到该页最后一项内容（文本/图形/图片）之下
      TRIM_PAD 处；整页装饰边框（页面圆角边框）不算内容。
    · 小问标签锚点：结构化题检测 QP 内粗体 "N (a)(i)…" 行，写入
      subquestions（label + 页码 + 坐标）；接续行省略父前缀时（U4/U5 卷式
      裸 "(i)" 行）自动拼上最近的父标签（→ "a i"），与答题册标题对齐。
    · 答题册另附卷（如 WPH12/01A）：检测 Total 锚点二次成套出现，
      按册内小问标题提取 answer_regions（去掉册内题头行与分值行，
      只取作答内容，带 label）；MCQ 不提取（QP 选项即可勾选）。
      同时存 answer_block = 册内题号行顶 → 册内 Total 底的整块连续
      区域（2026-10-14 起渲染分区式：题干在上、册内整块在下）。

坐标约定（全仓库统一）: PDF 点 pt，页面左上角原点，y 向下；bbox=[x0,y0,x1,y1]。
像素换算: 像素 = pt × DPI ÷ 72。

重跑保护: source.sha256 不符→硬停；status=verified/manual 的题其 regions/anchors
不被自动结果覆盖（manual 永不覆盖，verified 仅在 ±1pt 一致时刷新可再生字段）。
"""

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

import pymupdf

# ── 排版常量（Edexcel IAL 实测） ────────────────────────────────────────────
FOOTER_Y = 790.0          # 页脚条带起点（条码/页码/Turn over 均在 y>790）
HEADER_Y = 40.0           # 页眉安全线（本卷无页眉，内容最早 y≈58）
LEFT_MARGIN_MAX = 100.0   # 题号行 x0 上限（题号顶格 x≈42.5；页脚数字不在候选内）
SIDEBAR_X = 30.0          # 左侧竖排边栏 "DO NOT WRITE IN THIS AREA" x1 上限

# 页尾空白裁剪参数
TRIM_MIN_GAP = 20.0       # 尾空白不超过此值视为正常排版间距，不裁
TRIM_PAD = 8.0            # 裁剪后保留的底部余量（pt）

# 题号行：允许题号前星号（*16 …）、题号后可选冒号（2025-june 卷式 "1:\t A cube…"）
QSTART_RE = re.compile(r"^\*?(\d{1,2}):?[\t\u0020\u00a0\u2009]+(\S.*)$")
QNUM_ONLY_RE = re.compile(r"^\*?(\d{1,2}):?[\t\u0020\u00a0\u2009]*$")
# Total 锚点：允许题号前星号（星号题写作 "(Total for Question *15 = 6 marks)"）
TOTAL_RE = re.compile(r"\(\s*Total\s+for\s+Question\s*\*?\s*(\d+)\s*=\s*(\d+)\s*marks?\s*\)")
TOTAL_SQUASH_RE = re.compile(r"TotalforQuestion\*?(\d+)=(\d+)marks?")
SECTION_RE = re.compile(r"^SECTION\s+([A-Z])[\s\.]*$", re.I)
OPTION_RE = re.compile(r"^([A-D])[\t\u0020\u00a0\u2009]*(.*)$")
# 小问标题行：QP 侧 "13\t (a)\t Calculate…" / 册内 "14\t (a)\t (i)" / 星号延伸题 "16\t*(b)…"
SUBHEAD_RE = re.compile(
    r"^(\d{1,2})[\t\u0020\u00a0\u2009]*(\*?)"
    r"((?:[\t\u0020\u00a0\u2009]*\([a-z]{1,4}\)){1,3})"
    r"[\t\u0020\u00a0\u2009]*(.*)$")
# 无题号前缀的小问标签行（WPH11 等 U1 卷式）："(a)\t State what is meant…"；
# 2026-jan 答题册式为独立成行、无尾随文字的 "(a)" / "(i)"，尾组可为空。
BARE_SUBHEAD_RE = re.compile(
    r"^(\*?)((?:\([a-z]{1,4}\)[\t\u0020\u00a0\u2009]*){1,3})"
    r"[\t\u0020\u00a0\u2009]*(.*)$")
MARKS_ONLY_RE = re.compile(r"\(\s*\d+\s*\)")
ROMAN_SUB = {"ii", "iii", "iv", "vi", "vii", "viii", "ix", "xi", "xii"}
ROMAN_TOKENS = ROMAN_SUB | {"i", "v", "x"}  # 含单字符罗马数字（区分字母标签）

SESSION_TOKENS = {"jan", "mar", "may-jun", "june", "oct", "oct-nov"}
BOARDS = {"edexcel", "aqa", "cie"}
QUALS = {"a-level", "alevel", "igcse", "gcse"}

SCRIPT_OWNED_KEYS = ("total_marks", "question_type", "section", "image", "layout", "cut_status")


def r2(v):
    return round(float(v), 2)


def pct(sorted_vals, q):
    if not sorted_vals:
        return 0.0
    i = min(int(len(sorted_vals) * q), len(sorted_vals) - 1)
    return sorted_vals[i]


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


# ── 页面文本结构提取 ────────────────────────────────────────────────────────

def build_page_lines(page):
    """一页的全部横排文本行（dict 模式），含字体信息，按 (y0, x0) 排序。"""
    lines = []
    for block in page.get_text("dict")["blocks"]:
        if block.get("type") != 0:
            continue
        bno = block.get("number", -1)
        for li, ln in enumerate(block["lines"]):
            if tuple(ln.get("dir", (1, 0))) != (1.0, 0.0):
                continue  # 竖排边栏等旋转文本不参与切割
            spans = [s for s in ln["spans"] if s["text"].strip()]
            if not spans:
                continue
            raw = "".join(s["text"] for s in ln["spans"])
            lines.append({
                "page_bno": bno,
                "page_lno": li,
                "bbox": list(ln["bbox"]),
                "raw": raw,
                "bold": any("bold" in s["font"].lower() for s in spans),
                "size": max(s["size"] for s in spans),
            })
    lines.sort(key=lambda l: (round(l["bbox"][1], 1), round(l["bbox"][0], 1)))
    return lines


def assign_words_to_lines(lines, words):
    """把 get_text('words') 的词按中心点归入横排行；返回 [(word, line_ref), ...]。

    竖排边栏（DO NOT WRITE IN THIS AREA）的词没有可归入的横排行，
    兜底匹配要求词的 x 中心也落在行框内，避免边栏词污染相邻横排行的文本。
    """
    out = []
    for w in words:
        cx, cy = (w[0] + w[2]) / 2.0, (w[1] + w[3]) / 2.0
        hit = None
        for ln in lines:
            b = ln["bbox"]
            if b[0] <= cx <= b[2] and b[1] <= cy <= b[3]:
                hit = ln
                break
        if hit is None:  # 中心落在行框外（跨行数学碎片等）：y 最近且 x 同列的行
            best, best_d = None, 1e9
            for ln in lines:
                b = ln["bbox"]
                if b[0] <= cx <= b[2]:
                    d = abs(cy - (b[1] + b[3]) / 2.0)
                    if d < best_d:
                        best, best_d = ln, d
            if best is not None and best_d <= 20.0:
                hit = best
        out.append((w, hit))
    return out


# ── region 构建与页尾空白裁剪 ───────────────────────────────────────────────

def build_regions(start_page, sy0, end_page, end_y1, L, T, R, B):
    """从起点(页, y0)到终点(页, y1)构建有序 region 列表（跨页自动分段）。"""
    if end_page == start_page:
        return [{"page": start_page, "bbox": [r2(L), r2(sy0), r2(R), r2(end_y1 + 2)]}]
    regs = [{"page": start_page, "bbox": [r2(L), r2(sy0), r2(R), r2(B)]}]
    for mp in range(start_page + 1, end_page):
        regs.append({"page": mp, "bbox": [r2(L), r2(T), r2(R), r2(B)]})
    regs.append({"page": end_page, "bbox": [r2(L), r2(T), r2(R), r2(end_y1 + 2)]})
    return regs


def parse_subhead(raw):
    """解析 "N (a)(i)…" 小问标题行 → (n, starred, tokens, tail) 或 None。

    tokens 为标签序列（如 ("a", "i")）；多字符 token 只接受罗马数字。
    题号行 "N 题干…"、Total 行等均不匹配。
    """
    m = SUBHEAD_RE.match(raw.strip())
    if not m:
        return None
    tokens = re.findall(r"\(([a-z]{1,4})\)", m.group(3))
    if any(len(t) > 1 and t not in ROMAN_SUB for t in tokens):
        return None
    return int(m.group(1)), bool(m.group(2)), tuple(tokens), m.group(4).strip()


def parse_bare_subhead(raw):
    """解析无题号前缀的小问标签行 "(a)\t …"（U1 卷式）→ (starred, tokens, tail) 或 None。"""
    m = BARE_SUBHEAD_RE.match(raw.strip())
    if not m:
        return None
    tokens = re.findall(r"\(([a-z]{1,4})\)", m.group(2))
    if any(len(t) > 1 and t not in ROMAN_SUB for t in tokens):
        return None
    return bool(m.group(1)), tuple(tokens), m.group(3).strip()


def sub_label(tokens):
    """标签元组 → cut.json 里的 label 字符串，如 ("a", "i") → "a i"。"""
    return " ".join(tokens)


def booklet_content_y0(hdr, pages_lines):
    """册内小问的内容起点：题头块（题头行 + 紧邻右侧分值行 "(N)"）底界 + 6pt。

    分值行与题头行大致同行（x0≈496，题头 x0≈42.5），故按 y 区间 + 右侧定位。
    """
    y = hdr["bbox"][3]
    for ln in pages_lines[hdr["page"]]:
        lb = ln["bbox"]
        if hdr["bbox"][1] - 2 <= lb[1] <= hdr["bbox"][3] + 25 and lb[0] > 400:
            if MARKS_ONLY_RE.fullmatch(ln["raw"].strip()):
                y = max(y, lb[3])
    return r2(y + 6.0)


def page_content_rects(doc, pi, cache):
    """一页内矢量图形与图片块的矩形，[(y1, y0, x0, x1, is_hline), ...]。

    图片块（get_text dict 的 type=1）与任何矢量图形都算内容；
    整页装饰边框（跨几乎整页的圆角矩形，Pearson 卷每页都有）不算。
    """
    if pi not in cache:
        rows = []
        try:
            for dr in doc[pi].get_drawings():
                db = dr["rect"]
                # 只跳过点状退化矩形（w、h 双 0）；一维线段（h=0 横线 /
                # w=0 竖线，如表格栅格、表格空白行）是真实内容，必须保留
                if (db.width <= 0 and db.height <= 0) or db.y1 > FOOTER_Y:
                    continue
                if db.width > 400 and db.height > 600:
                    continue  # 整页装饰边框，非内容
                rows.append((db.y1, db.y0, db.x0, db.x1,
                             db.height <= 1.5 and db.width >= 50))
        except Exception:
            pass
        for b in doc[pi].get_text("dict")["blocks"]:
            if b.get("type") == 1 and b["bbox"][3] <= FOOTER_Y:
                bb = b["bbox"]
                rows.append((bb[3], bb[1], bb[0], bb[2], False))
        cache[pi] = rows
    return cache[pi]


def trim_regions_trailing_blank(doc, regions, pages_lines, content_cache):
    """非末段 region 的页尾排版空白裁剪（原地修改，返回裁剪统计）。

    非末段 region 以版心底界收尾，页底到该页最后一项内容（文本行/矢量
    图形/图片，装饰边框除外）之间的空白是排版残留（内容放不下换页留下
    的），一律裁到最后一项内容之下 TRIM_PAD 处。末段 region 的下界是
    Total 行/边界锚点，无排版空白，不处理。
    """
    trimmed = []
    for rg in regions[:-1]:
        bb = rg["bbox"]
        last_y = None
        for ln in pages_lines[rg["page"]]:
            lb = ln["bbox"]
            if lb[3] > FOOTER_Y or lb[1] < bb[1] - 1:
                continue
            if lb[2] < bb[0] or lb[0] > bb[2]:
                continue
            if last_y is None or lb[3] > last_y:
                last_y = lb[3]
        for y1, _y0, x0, x1, _is_hline in page_content_rects(doc, rg["page"], content_cache):
            if y1 > FOOTER_Y or y1 < bb[1] - 1:
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
        trimmed.append((rg["page"], round(gap - TRIM_PAD, 1)))
    return trimmed


SCORED_LABEL_RE = re.compile(r"\([ivx]+\)|\([a-z]\)")
MARKS_TOKEN_RE = re.compile(r"\(\d{1,2}\)")


def trim_answer_block_blank(doc, regions, pages_lines, content_cache):
    """answer_block 非末段的页尾空白处理（原地修改，返回裁剪统计）。

    与 trim_regions_trailing_blank 的区别：册内页尾空白有两种性质
    （2026-10-14 王培琪确认规则）：
    · 最后内容为点线/横线/表格/正文（作答范围已由内容界定）→ 排版
      残留，裁到最后一项内容+TRIM_PAD；
    · 最后内容为「带分值的小问标签行」（最近标签行之下只有分值行，
      如 "(ii)" 与右对齐 "(5)"）→ 其下的整页纯空白是真实预留作答区，
      保留不裁；
    · 最后内容为无分值裸标签（分组头如 "(b)"）→ 非作答区，裁。
    """
    trimmed = []
    for rg in regions[:-1]:
        bb = rg["bbox"]
        last_y, last_text = None, None
        for ln in pages_lines[rg["page"]]:
            lb = ln["bbox"]
            if lb[3] > FOOTER_Y or lb[1] < bb[1] - 1:
                continue
            if lb[2] < bb[0] or lb[0] > bb[2]:
                continue
            if last_y is None or lb[3] > last_y:
                last_y, last_text = lb[3], ln["raw"]
        for y1, _y0, x0, x1, _is_hline in page_content_rects(doc, rg["page"], content_cache):
            if y1 > FOOTER_Y or y1 < bb[1] - 1:
                continue
            if x1 < bb[0] or x0 > bb[2]:
                continue
            if last_y is None or y1 > last_y:
                last_y, last_text = y1, None  # 矢量/图片垫底 → 非标签行
        if last_y is None:
            continue
        gap = bb[3] - last_y
        if gap <= TRIM_MIN_GAP:
            continue
        scored = False
        if last_text is not None:
            seg_lines = sorted(
                (ln for ln in pages_lines[rg["page"]]
                 if ln["bbox"][3] <= FOOTER_Y and ln["bbox"][1] >= bb[1] - 1
                 and ln["bbox"][2] >= bb[0] and ln["bbox"][0] <= bb[2]),
                key=lambda l: l["bbox"][3])
            # 自底向上找最近的标签行：其下若只有分值行（标签行与右对齐
            # 分值行可相距 ~90pt，如 wch11a "(a)"→"(4)"）→ 带分标签行，
            # 页尾空白是真实预留作答区；其下还有点线/正文等内容 → 作答区
            # 已由内容界定，裁。
            for ln in reversed(seg_lines):
                if not SCORED_LABEL_RE.search(ln["raw"]):
                    continue
                below = [m["raw"] for m in seg_lines if m["bbox"][3] > ln["bbox"][3]]
                if (ln["bbox"][3] >= last_y - 150 and below
                        and all(re.fullmatch(r"[\s()\d]*", r) and MARKS_TOKEN_RE.search(r)
                                for r in below)):
                    scored = True
                break
        if scored:
            continue
        rg["bbox"] = [bb[0], bb[1], bb[2], r2(last_y + TRIM_PAD)]
        trimmed.append((rg["page"], round(gap - TRIM_PAD, 1)))
    return trimmed


def _region_rows(pages_lines, doc, content_cache, rg):
    """region 内的内容行：文本行 yield (y0, y1)，图形/图片 yield (y0, y1)。"""
    bb, pi = rg["bbox"], rg["page"]
    for ln in pages_lines[pi]:
        lb = ln["bbox"]
        if not ln["raw"].strip():
            continue
        if lb[3] <= bb[1] or lb[1] >= bb[3]:
            continue
        if lb[2] <= bb[0] or lb[0] >= bb[2]:
            continue
        yield lb[1], lb[3]
    for y1, y0, x0, x1, _is_hline in page_content_rects(doc, pi, content_cache):
        if y1 <= bb[1] or y0 >= bb[3]:
            continue
        if x1 <= bb[0] or x0 >= bb[2]:
            continue
        yield y0, y1


def region_has_content(pages_lines, doc, content_cache, regs):
    """答题册作答区是否有任何内容（文本/图形/图片）。整段空白 → False。"""
    for rg in regs:
        for _ in _region_rows(pages_lines, doc, content_cache, rg):
            return True
    return False


# ── 主流程 ──────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="真题按大题自动切割（Edexcel 策略）")
    ap.add_argument("session_dir", help="如 data/edexcel/a-level/physics/2026-june-wph11")
    args = ap.parse_args()
    sdir = Path(args.session_dir).resolve()
    if not sdir.is_dir():
        sys.exit(f"目录不存在: {sdir}")

    # 路径即分类：解析 board / qualification / subject / year / session / paper_code
    try:
        board = sdir.parents[2].name
        qualification = sdir.parents[1].name
        subject = sdir.parents[0].name
        year_s, session, paper_code = sdir.name.split("-", 2)
        year = int(year_s)
        assert board in BOARDS and qualification in QUALS and session in SESSION_TOKENS
    except Exception:
        sys.exit(f"session 目录路径不符合 data/<board>/<qual>/<subject>/<year>-<session>-<paper>/ 约定: {sdir}")

    src = sdir / "source.pdf"
    if not src.exists():
        sys.exit(f"缺少 source.pdf: {src}")
    sha = hashlib.sha256(src.read_bytes()).hexdigest()

    # paper.md 卷级信息（总分校验 / 溯源）
    paper_total_marks, paper_ref = None, None
    paper_md = sdir / "paper.md"
    if paper_md.exists():
        m = re.search(r"^total_marks:\s*(\d+)", paper_md.read_text(encoding="utf-8"), re.M)
        if m:
            paper_total_marks = int(m.group(1))
        m = re.search(r"^paper_ref:\s*(\S+)", paper_md.read_text(encoding="utf-8"), re.M)
        if m:
            paper_ref = m.group(1)

    doc = pymupdf.open(src)

    # ── Step 0 · 预检：文本层覆盖率与锚点命中 ──
    pages_lines, pages_pairs = [], []
    for page in doc:
        lines = build_page_lines(page)
        words = page.get_text("words")
        pages_lines.append(lines)
        pages_pairs.append(assign_words_to_lines(lines, words))
    n_pages = doc.page_count
    covered = sum(1 for lines in pages_lines if lines) / n_pages
    anchor_count = sum(
        1 for lines in pages_lines for ln in lines
        if TOTAL_RE.search(ln["raw"]) or TOTAL_SQUASH_RE.search(re.sub(r"\s+", "", ln["raw"]))
    )
    if covered < 0.9 or anchor_count < 2:
        out = {
            "schema_version": 1,
            "source": {"file": "source.pdf", "sha256": sha, "pages": n_pages},
            "needs_ocr": True,
            "questions": [],
        }
        (sdir / "cut.json").write_text(json.dumps(out, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        sys.exit(f"预检未通过（文本覆盖率 {covered:.0%}，锚点 {anchor_count} 个）——疑似扫描卷，"
                 f"已写入 needs_ocr 标记的 cut.json，需走人工画框/OCR 路径。")

    # ── 版心：横排内容行的坐标分位数（自动避开页眉页脚/边栏） ──
    # 左下角竖排卷号（"P87910A"）会被文本层还原成 y≈770–794、x1<150 的
    # 假横排行，把版心底界推到页面装饰边框（y≈794）之下，须排除。
    content = [ln for lines in pages_lines for ln in lines
               if HEADER_Y < ln["bbox"][1] < FOOTER_Y and ln["bbox"][0] > SIDEBAR_X
               and not (ln["bbox"][1] > 750.0 and ln["bbox"][2] < 150.0)]
    xs0 = sorted(ln["bbox"][0] for ln in content)
    xs1 = sorted(ln["bbox"][2] for ln in content)
    ys0 = sorted(ln["bbox"][1] for ln in content)
    ys1 = sorted(ln["bbox"][3] for ln in content)
    L = max(25.0, pct(xs0, 0.005) - 6)
    R = min(575.0, pct(xs1, 0.995) + 6)
    T = max(30.0, pct(ys0, 0.005) - 4)
    B = min(pct(ys1, 0.995) + 4, 788.0)  # 硬上限：不吞页面装饰边框（y1≈794）

    # ── SECTION 标记 ──
    sections = []
    for pi, lines in enumerate(pages_lines):
        for ln in lines:
            m = SECTION_RE.match(ln["raw"].strip())
            if m and ln["bold"]:
                sections.append((pi, ln["bbox"][1], m.group(1).upper()))

    def section_of(pi, y):
        for sp, sy, letter in sections:
            if (sp, sy) <= (pi, y):
                if letter == "B":
                    return "B"
        return "A" if sections else None

    # ── 题号起点：题号开头 + 左缘带 + 序列约束（粗体优先，受阻时非粗体补位） ──
    # 粗体不能当硬条件：老卷（如 2021-jan U5）文本层里个别题号行丢失 bold 标记，
    # 一旦该行被跳过，严格递增的序列就永久卡死、其后所有题被丢弃。
    # 改法：先按文档序收集全部候选（记录 bold 与位置），序列推进时优先取粗体候选；
    # 若该题号在当前位置之后没有粗体候选，则接受最早的那个非粗体候选补位
    # （前提是它早于下一题的首个粗体候选，避免误收答题册/正文里的数字行）。
    qsts_cand = []   # [(page, y, n, bold, bbox, line)]，文档序
    for pi, lines in enumerate(pages_lines):
        for ln in lines:
            b = ln["bbox"]
            if b[1] >= FOOTER_Y or b[0] > LEFT_MARGIN_MAX:
                continue
            m = QSTART_RE.match(ln["raw"].strip()) or QNUM_ONLY_RE.match(ln["raw"].strip())
            if not m:
                continue
            qsts_cand.append((pi, b[1], int(m.group(1)), bool(ln["bold"]), b, ln))

    def _pick(n, min_pos, bold_only):
        for pi, y, nn, bold, b, ln in qsts_cand:
            if nn != n or (pi, y) <= min_pos:
                continue
            if bold_only and not bold:
                continue
            return (pi, y, bold, b, ln)
        return None

    qstarts = {}  # n -> dict
    expected = 1
    last_pos = (-1, -1.0)
    while True:
        pick = _pick(expected, last_pos, bold_only=True)
        if pick is None:
            nb = _pick(expected, last_pos, bold_only=False)
            if nb is not None and not nb[2]:
                # 非粗体补位：必须早于下一题（expected+1）的首个粗体候选
                nxt = _pick(expected + 1, last_pos, bold_only=True)
                if nxt is None or (nb[0], nb[1]) < (nxt[0], nxt[1]):
                    pick = nb
        if pick is None:
            break
        pi, y, _bold, b, ln = pick
        qstarts[expected] = {"page": pi, "bbox": b, "line": ln}
        last_pos = (pi, y)
        expected += 1

    # ── 终点锚点：双模式匹配（常规 + 去空格兜底，对付 PDF 拆词） ──
    totals = {}  # n -> dict（每题首个命中 = Question Paper 内的）
    total_occ = []  # 全部命中（含答题册重印），供答题册检测
    for pi, lines in enumerate(pages_lines):
        for ln in lines:
            b = ln["bbox"]
            if b[1] >= FOOTER_Y:
                continue
            m = TOTAL_RE.search(ln["raw"])
            squashed = False
            if not m:
                sq = re.sub(r"\s+", "", ln["raw"])
                m = TOTAL_SQUASH_RE.search(sq)
                squashed = bool(m)
            if m:
                n = int(m.group(1))
                total_occ.append({"n": n, "page": pi, "bbox": b, "marks": int(m.group(2))})
                if n not in totals:
                    totals[n] = {"page": pi, "bbox": b, "marks": int(m.group(2)),
                                 "squashed": squashed, "line": ln}

    n_questions = expected - 1
    if n_questions == 0:
        sys.exit("未定位到任何题号起点，请人工检查 source.pdf。")

    # ── 答题册检测：Total 锚点二次成套出现（答题册另附卷型，如 WPH12/01A） ──
    # 答题册把每题的题号与 Total 行重印一遍（作答区在其间）。
    # QP 锚点靠“每题首次命中”取得；二次命中若成套出现即判为答题册。
    occ_by_n = {}
    for e in total_occ:
        occ_by_n.setdefault(e["n"], []).append(e)
    booklet_anchors = {}  # n -> 第二次出现（答题册内的 Total 行）
    for n in range(1, n_questions + 1):
        occ = occ_by_n.get(n, [])
        if len(occ) >= 2 and occ[1]["page"] > occ[0]["page"]:
            booklet_anchors[n] = occ[1]
    booklet_starts = {}   # n -> 首个标题行（answer_start 锚点）
    booklet_headers = {}  # n -> [全部标题行（含小问级），文档序]
    if len(booklet_anchors) >= max(2, n_questions // 2):
        # 答题册始于 QP 最后一个 Total 锚点之后。不能用「首个册内 Total
        # 锚点页」当扫描起点：首题的册内标题可以在其 Total 锚点之前的页上
        # （如 WPH16A Q1 的 (a)(i)/(a)(ii) 标题在 p17，Total 锚点在 p18）。
        scan_from = max(t["page"] for t in totals.values()) + 1
        cand = []  # (page, y0, n, tokens, starred, bbox, bold)：册内题号/小问标题行
        # 两种册式题头：① 6 月卷式——题号+小问同一行（粗体 "1\t (a)\t(i)"）；
        # ② 1 月卷式——分行：题号行粗体顶格，"(a)"/"(i)" 各占一行且可不粗体，
        #    裸标签行的题号继承最近扫到的粗体题号行，罗马行拼当前父字母。
        # 粗体同样不能当硬条件：册内题号行也会丢 bold 标记（如 2025-oct WPH11A
        # Q13 的 "13" 行 bold=False）→ 该题定位不到册内标题、拿不到 answer_block。
        # 改为候选带 bold 标记，选标题时粗体优先、无粗体则接受非粗体。
        cur_n, cur_letter = None, None
        for pi in range(scan_from, n_pages):
            for ln in pages_lines[pi]:
                b = ln["bbox"]
                if b[1] >= FOOTER_Y or b[0] > LEFT_MARGIN_MAX:
                    continue
                parsed = parse_subhead(ln["raw"])
                if parsed and parsed[2]:
                    cand.append((pi, b[1], parsed[0], parsed[2], parsed[1], b, bool(ln["bold"])))
                    cur_n = parsed[0]
                    cur_letter = parsed[2][0] if parsed[2][0] not in ROMAN_TOKENS else None
                    continue
                m = QNUM_ONLY_RE.match(ln["raw"].strip())
                if m:
                    cand.append((pi, b[1], int(m.group(1)), (), False, b, bool(ln["bold"])))
                    cur_n = int(m.group(1))
                    cur_letter = None
                    continue
                bare = parse_bare_subhead(ln["raw"])
                if bare and bare[1] and cur_n is not None:
                    toks = bare[1]
                    if toks[0] not in ROMAN_TOKENS:  # 字母标签行：更新当前父字母
                        cur_letter = toks[0]
                    elif cur_letter is not None:  # 罗马接续行：拼父字母
                        toks = (cur_letter,) + toks
                    cand.append((pi, b[1], cur_n, toks, bare[0], b, bool(ln["bold"])))
        cand.sort(key=lambda x: (x[0], x[1]))
        for n in sorted(booklet_anchors):
            ba = booklet_anchors[n]
            prev = booklet_anchors.get(n - 1)
            # 标题必须位于上一题答题册锚点之后、本题锚点之前
            lo = (prev["page"], prev["bbox"][3]) if prev else (scan_from - 1, 1e9)
            hit = [(pi, b, tok, st, bold) for pi, y0, cn, tok, st, b, bold in cand
                   if cn == n and lo < (pi, y0) < (ba["page"], ba["bbox"][1])]
            if hit:
                hdrs = [{"page": pi, "bbox": b, "tokens": tok, "starred": st}
                        for pi, b, tok, st, _b in hit]
                # 首个粗体标题优先（无粗体则用最早的）
                starts = [h for h, c in zip(hdrs, hit) if c[4]] or hdrs
                booklet_starts[n] = starts[0]
                booklet_headers[n] = hdrs

    # ── 配对、边界与跨页 regions ──
    auto_questions = []
    trim_stats = []
    content_rects_cache = {}
    marks_sum = 0
    for n in range(1, n_questions + 1):
        q = {
            "number": str(n),
            "status": "auto",
            "confidence": 0.0,
            "regions": [],
            "anchors": {},
            "review_notes": [],
            "subquestions": [],
        }
        notes = q["review_notes"]
        st, en = qstarts.get(n), totals.get(n)

        if st is not None:
            q["anchors"]["start"] = {"page": st["page"], "bbox": [r2(v) for v in st["bbox"]]}
        if en is not None:
            q["anchors"]["end"] = {"page": en["page"], "bbox": [r2(v) for v in en["bbox"]]}
            q["total_marks"] = en["marks"]
            marks_sum += en["marks"]

        # 终点缺失：按下一题起点推边界（天然成立），强制 review
        end_page, end_y1 = None, None
        if en is not None:
            end_page, end_y1 = en["page"], en["bbox"][3]
        else:
            nxt = qstarts.get(n + 1)
            if nxt is not None:
                end_page, end_y1 = (nxt["page"], nxt["bbox"][1] - 2) if nxt["page"] == st["page"] \
                    else (nxt["page"] - 1, B) if nxt["page"] > st["page"] else (st["page"], B)
            else:
                end_page, end_y1 = st["page"], B
            notes.append("缺终点锚点，边界由下一题起点/版心底推出，请人工复核")

        if st is None:
            q["status"] = "review"
            notes.append("缺题号起点锚点")
            auto_questions.append(q)
            continue

        sy0 = st["bbox"][1] - 4
        q["regions"] = build_regions(st["page"], sy0, end_page, end_y1, L, T, R, B)
        trim_stats += trim_regions_trailing_blank(doc, q["regions"], pages_lines, content_rects_cache)

        sec = section_of(st["page"], st["bbox"][1])
        if sec:
            q["section"] = sec
        is_mcq = (sec == "A") if sec else (en is not None and en["marks"] == 1)
        q["question_type"] = "mcq" if is_mcq else "structured"

        # ── 共用题干块 MCQ（2026-jan WPH15A Q9/Q10、WPH12A Q6/Q7 式） ──
        # "Questions 9 and 10 refer to the information below." 之后的共用题干
        # 位于两题题干行之前，常落在上一题区域尾部或页首真空区。处理：
        #   · 干块 = marker 行顶 → 两题中较早题干行之上，复制拼给两题；
        #   · 干块内含选项行（WPH15A 式：选项真共用）→ 热区搜索上移到块顶、
        #     下界压到块底；
        #   · 干块内无选项（WPH12A 式：选项各归各题，Q6 陈述句 / Q7 公式）
        #     → 后一题不补拼，热区按默认从自己题干行起找；
        #   · region 标 "shared": true（validate 据此跳过重叠检查）。
        mcq_shared_top = mcq_shared_bottom = None
        if is_mcq:
            for ln in pages_lines[st["page"]]:
                lb = ln["bbox"]
                if lb[1] >= st["bbox"][1] or lb[1] >= FOOTER_Y:
                    continue
                m = re.search(r"Questions?\s+(\d{1,2})\s+and\s+(\d{1,2})\s+refer",
                              ln["raw"], re.I)
                if not m or n not in (int(m.group(1)), int(m.group(2))):
                    continue
                other_n = int(m.group(2)) if n == int(m.group(1)) else int(m.group(1))
                other_st = qstarts.get(other_n)
                pair_top = lb[1] - 4
                first_y = st["bbox"][1]
                if other_st is not None and other_st["page"] == st["page"]:
                    first_y = min(first_y, other_st["bbox"][1])
                if first_y - 2 <= pair_top:
                    break
                mcq_shared_top = pair_top
                q["regions"].insert(0, {"page": st["page"],
                                        "bbox": [r2(L), r2(pair_top), r2(R), r2(first_y - 2)],
                                        "shared": True})
                # 干块内的选项行（较早题干行之上）→ 真共用选项，热区搜索扩展到块内
                opts_above = [l2["bbox"] for l2 in pages_lines[st["page"]]
                              if l2["bold"] and pair_top <= l2["bbox"][1] < first_y
                              and OPTION_RE.match(l2["raw"].strip())]
                if opts_above:
                    mcq_shared_bottom = first_y - 2
                break

        # ── QP 侧小问标签锚点（结构化题；MCQ 无小问） ──
        # 两种卷式：U2/U6 式粗体 "N (a)(i)……"（带题号前缀）；U1/U4/U5 式
        # "(a)\t ……"（无前缀、非粗体）。接续行省略父前缀（"(a)" 之后的裸
        # "(i)" 行）→ 拼上最近的父标签，记作 "a i"，与答题册标题对齐。
        subqs = []
        parent_letter = None
        if not is_mcq:
            for rg in q["regions"]:
                rbb = rg["bbox"]
                for ln in pages_lines[rg["page"]]:
                    lb = ln["bbox"]
                    if lb[1] >= FOOTER_Y or lb[0] > LEFT_MARGIN_MAX:
                        continue
                    if lb[1] < rbb[1] - 1 or lb[1] > rbb[3] + 1:
                        continue
                    tokens = starred = None
                    parsed = parse_subhead(ln["raw"])
                    if parsed and parsed[0] == n and parsed[2]:
                        tokens, starred = parsed[2], parsed[1]
                    else:
                        bare = parse_bare_subhead(ln["raw"])
                        if bare and bare[1]:
                            tokens, starred = bare[1], bare[0]
                    if tokens is None:
                        continue
                    if tokens[0] in ROMAN_TOKENS:  # 罗马接续：(i) → (父, i)
                        if parent_letter is not None:
                            tokens = (parent_letter,) + tokens
                    else:  # 字母标签：(a) / (b)(i) → 更新当前父标签
                        parent_letter = tokens[0]
                    ent = {"label": sub_label(tokens), "page": rg["page"],
                           "bbox": [r2(v) for v in lb]}
                    if starred:
                        ent["starred"] = True
                    subqs.append(ent)
        q["subquestions"] = subqs

        # ── 答题册另附卷：提取该题在答题册中的作答区 ──
        # MCQ 不拼接（QP 选项本身即可勾选）；结构化题另存 answer_block：
        # 册内题号行顶 → 册内 Total 底的整块连续区域（小问标签/预留空白/
        # 横线/分值全保留），渲染时作为「答题册部分」整块拼在题干之后。
        if n in booklet_headers:
            if is_mcq:
                pass
            else:
                bs, ba = booklet_starts[n], booklet_anchors[n]
                q["anchors"]["answer_start"] = {"page": bs["page"], "bbox": [r2(v) for v in bs["bbox"]]}
                q["anchors"]["answer_end"] = {"page": ba["page"], "bbox": [r2(v) for v in ba["bbox"]]}
                q["answer_block"] = build_regions(bs["page"], r2(bs["bbox"][1]),
                                                  ba["page"], r2(ba["bbox"][3] + 2),
                                                  L, T, R, B)
                # 非末段页尾空白按内容性质处理（规则见函数 docstring）：
                # 内容尾裁掉；带分值标签行的整页预留作答区保留。
                trim_stats += trim_answer_block_blank(
                    doc, q["answer_block"], pages_lines, content_rects_cache)
                if "total_marks" in q and ba["marks"] != q["total_marks"]:
                    notes.append(f"答题册分值 {ba['marks']} 与 QP {q['total_marks']} 不一致")
                    q["status"] = "review"

                labeled = [h for h in booklet_headers[n] if h["tokens"]]
                qp_labels = {s["label"] for s in subqs}

                def _map_label(bl):
                    # 册内标签 → QP 标签：先精确匹配，再按后缀唯一匹配
                    # （2026-jan WPH15A Q21 册内无 "(a)" 行，裸 "i" ↔ QP "a i"）
                    if bl in qp_labels:
                        return bl
                    cands = [qp for qp in qp_labels if qp.endswith(" " + bl)]
                    if len(cands) == 1:
                        return cands[0]
                    # 纯分组头：QP 无此标签、但 QP 有以其为前缀的子标签
                    # （2025-oct 化学：册内独立 "(a)" 行，QP 是 "(a)(i)…" 连排）
                    if any(qp.startswith(bl + " ") for qp in qp_labels):
                        return bl + "~group"
                    return None

                mapped = [(h, _map_label(sub_label(h["tokens"]))) for h in labeled]
                if labeled and all(ml is not None for _, ml in mapped) \
                        and len({ml for _, ml in mapped}) == len(mapped):
                    # 册内小问标题与 QP 小问标签逐一匹配 → 按小问生成作答区。
                    # 先剔除纯分组头：1 月册式 "(a)" 下无内容、仅作为 "(i)(ii)"
                    # 的父级存在，其分值行又排得低会把内容起点推过下一头
                    # （内容起点 ≥ 下一头顶 → 无独立作答区）。
                    # 被剔除的分组头行仍要作为前一问作答区的下边界，否则前一问
                    # 区域会延伸到再下一个保留头，把分组头行包进渲染图
                    # （2026-jan 多卷 "孤零零 (b)" 行的来源）。
                    kept = []  # (h, ml, mapped 下标)
                    for i, (h, ml) in enumerate(mapped):
                        if ml.endswith("~group"):
                            continue  # 纯分组头：不生成作答区，仅经 mapped 顺序作边界
                        sy = booklet_content_y0(h, pages_lines)
                        nxt = mapped[i + 1][0] if i + 1 < len(mapped) else None
                        if nxt is not None and (h["page"], sy) >= (nxt["page"], nxt["bbox"][1]):
                            continue
                        kept.append((h, ml, i))
                    entries = []
                    for h, ml, oi in kept:
                        sy = booklet_content_y0(h, pages_lines)
                        if oi + 1 < len(mapped):
                            nxt = mapped[oi + 1][0]  # 紧随的下一个头（保留/分组均作边界）
                            ep, ey = nxt["page"], r2(nxt["bbox"][1] - 2)
                        else:
                            ep, ey = ba["page"], r2(ba["bbox"][3] + 2)  # 末问连带册内 Total 行
                        regs = build_regions(h["page"], sy, ep, ey, L, T, R, B)
                        trim_stats += trim_regions_trailing_blank(doc, regs, pages_lines,
                                                                  content_rects_cache)
                        if not region_has_content(pages_lines, doc, content_rects_cache, regs):
                            continue  # 整段空白（如 0 分引导段的预留区），不生成作答区
                        entries.append({"label": ml, "regions": regs})
                    q["answer_regions"] = entries
                else:
                    if labeled:
                        bl = ", ".join(sub_label(h["tokens"]) for h in labeled)
                        notes.append(f"答题册小问标题（{bl}）与 QP 小问标签不匹配，作答区退化为整题拼接")
                        q["status"] = "review"
                    elif subqs:
                        notes.append("QP 有小问标签但答题册无小问标题，作答区整题拼接")
                    h0 = booklet_headers[n][0]
                    regs = build_regions(h0["page"], booklet_content_y0(h0, pages_lines),
                                         ba["page"], r2(ba["bbox"][3] + 2), L, T, R, B)
                    trim_stats += trim_regions_trailing_blank(doc, regs, pages_lines,
                                                              content_rects_cache)
                    q["answer_regions"] = [{"label": None, "regions": regs}]
        elif n in booklet_anchors:
            notes.append("答题册中未定位到该题题号标题，未生成 answer_regions")

        # ── 置信度 ──
        conf = 0.0
        if st is not None and en is not None:
            conf += 0.4
            if st["page"] > en["page"] or (st["page"] == en["page"] and st["bbox"][1] >= en["bbox"][1]):
                notes.append("起点/终点锚点次序异常")
                conf = min(conf, 0.3)
                q["status"] = "review"
            if not en["squashed"]:
                conf += 0.2
            else:
                conf += 0.1
        else:
            conf = min(conf + 0.3, 0.3)
            q["status"] = "review"
        # 相邻题无缝检查（同页时）
        nxt = qstarts.get(n + 1)
        if nxt is None:
            conf += 0.2
        elif nxt["page"] == (en["page"] if en else end_page):
            gap = nxt["bbox"][1] - (en["bbox"][3] if en else end_y1)
            if gap < -6:
                notes.append(f"与下一题区域重叠 {abs(gap):.1f}pt")
                q["status"] = "review"
            else:
                conf += 0.2
        else:
            conf += 0.2

        # ── MCQ 选项热区 ──
        if is_mcq:
            q.setdefault("_options", [])
            # 干块内真有共用选项（mcq_shared_bottom 非 None）才上移搜索范围；
            # 否则按默认从本题题干行起找（WPH12A Q7 式，选项各归各题）
            opts = extract_mcq_options(
                n, q, pages_lines, pages_pairs, st, en, R, B, notes,
                min_y=mcq_shared_top if mcq_shared_bottom is not None else None,
                max_y=mcq_shared_bottom)
            q["_options"] = opts
            q["option_hotzones"] = [{"label": o["label"], "bbox": o["bbox"]} for o in opts]
            labels = {o["label"] for o in opts}
            if labels == {"A", "B", "C", "D"}:
                conf += 0.2
            else:
                notes.append(f"MCQ 选项不全（提取到 {sorted(labels)}），请人工补热区")
                q["status"] = "review"
        else:
            conf += 0.2  # 结构化题无选项检查，该项记满分

        q["confidence"] = round(conf, 2)
        if conf < 1.0 and q["status"] == "auto":
            q["status"] = "review"
        auto_questions.append(q)

    # 分值合计校验
    if paper_total_marks is not None and marks_sum != paper_total_marks:
        for q in auto_questions:
            q["status"] = "review"
            if f"全卷分值合计 {marks_sum} ≠ 试卷总分 {paper_total_marks}" not in q["review_notes"]:
                q["review_notes"].append(f"全卷分值合计 {marks_sum} ≠ 试卷总分 {paper_total_marks}")

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
        # 序列化时剔除内存临时字段（_options 已写入 front-matter；option_hotzones 保留给渲染层）
        "questions": [{k: v for k, v in q.items() if not k.startswith("_")} for q in questions],
    }
    if paper_ref:
        cut["source"]["paper_ref"] = paper_ref
    if booklet_anchors:
        cut["answer_booklet"] = {
            "pages": [min(e["page"] for e in booklet_anchors.values()),
                      max(e["page"] for e in booklet_anchors.values())],
        }
    cut_path.write_text(json.dumps(cut, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")

    # ── 每题产物：qNN.md（layout.json 由 render_questions.py 产出） ──
    assets = sdir / "assets"
    assets.mkdir(exist_ok=True)
    q_by_num = {q["number"]: q for q in questions}
    for n in range(1, n_questions + 1):
        q = q_by_num.get(str(n))
        if q is None:
            continue
        write_question_md(sdir, n, q, board, qualification, subject, year, session,
                          paper_code, pages_lines, pages_pairs)
    # 全部产物写完后，才把内存用的临时字段从权威数据里剔除
    for q in questions:
        q.pop("_options", None)

    # ── 报告 ──
    print(f"OK  {sdir.name}: {n_questions} 题 | 版心 L={L:.1f} R={R:.1f} T={T:.1f} B={B:.1f}")
    if booklet_anchors:
        bp = cut["answer_booklet"]["pages"]
        n_ans = sum(1 for q in questions if q.get("answer_regions"))
        print(f"    答题册: p{bp[0]}–p{bp[1]}，{len(booklet_starts)}/{len(booklet_anchors)} 题定位标题，"
              f"{n_ans} 题生成作答区与 answer_block（其余整题拼接；MCQ 不拼接）")
    if trim_stats:
        print(f"    页尾空白裁剪: {len(trim_stats)} 段，共 {sum(s for _, s in trim_stats):.0f}pt")
    for line in merge_report:
        print("   ", line)
    by_status = {}
    for q in questions:
        by_status[q["status"]] = by_status.get(q["status"], 0) + 1
    print("    status 分布:", " / ".join(f"{k}×{v}" for k, v in sorted(by_status.items())))
    for q in questions:
        if q["review_notes"]:
            print(f"    [review] Q{q['number']}: {'; '.join(q['review_notes'])}")


def extract_mcq_options(n, q, pages_lines, pages_pairs, st, en, R, B, notes,
                        min_y=None, max_y=None):
    """MCQ 选项热区：粗体 A–D 行为标签，向下吸收到下一选项/Total 行。

    min_y/max_y：选项搜索的 y 范围扩展（默认题干行顶 → Total 行顶）。
    共用题干块（如 WPH15A Q9/Q10 的 "Questions 9 and 10 refer…"）的选项
    在题干行上方、位于拼接的干块 region 内，调用方传入块顶/块底。
    标签列先按窄列（x∈[60,140]）找；不足 4 个时放宽到整页宽——
    两列选项卷（如 WPH11A Q2 的 B/D 在右列 x≈396）需要宽列才能收齐。
    """
    opts = []
    # 只在起始页（MCQ 不跨页；若跨页仍只处理首页）找标签行。
    # 候选行上界 = 本题 Total 锚点顶（双列选项卷：窄列只收齐 A/C 时，
    # 下一题的 B/D 行会被当首现字母凑数 → 跨题错配，2025-oct 化学实测）
    pi = st["page"]
    y_top = st["bbox"][1] if min_y is None else min_y
    scan_bottom = en["bbox"][1] if en and en["page"] == pi else FOOTER_Y
    if max_y is not None:
        scan_bottom = min(scan_bottom, max_y)
    labels = []
    for wide in (False, True):
        labels = []
        for ln in pages_lines[pi]:
            b = ln["bbox"]
            if b[1] >= FOOTER_Y or not ln["bold"] or not (60.0 <= b[0] <= (R if wide else 140.0)):
                continue
            if b[1] < y_top or b[1] >= scan_bottom:
                continue
            m = OPTION_RE.match(ln["raw"].strip())
            if m and m.group(2) is not None:
                labels.append({"label": m.group(1), "y0": b[1], "x0": b[0], "x1": b[2],
                               "tail": m.group(2).strip(), "page": pi})
        # 每个字母取首次出现，按 y 排序
        seen, uniq = set(), []
        for lb in sorted(labels, key=lambda x: x["y0"]):
            if lb["label"] not in seen:
                seen.add(lb["label"])
                uniq.append(lb)
        if len(uniq) >= 4:
            break
    if not uniq:
        return opts
    # 每个选项的下界与右界（两列选项：下界取下一「行」选项；右邻列分割 x）
    limit_y = en["bbox"][1] if en and en["page"] == pi else B
    if max_y is not None:
        limit_y = min(limit_y, max_y)
    for i, lb in enumerate(uniq):
        nxt_row = next((o for o in uniq if o["y0"] > lb["y0"] + 3), None)
        y_end = nxt_row["y0"] - 2 if nxt_row else limit_y - 2
        x1 = R
        for other in uniq:
            if other is not lb and abs(other["y0"] - lb["y0"]) <= 3 and other["x0"] > lb["x0"]:
                x1 = min(x1, other["x0"] - 6)
        bbox = [r2(lb["x0"] - 6), r2(lb["y0"] - 2), r2(x1), r2(y_end)]
        # 文本：标签行尾部 + 同页 y 范围内 x 更靠右的行；同 y 的列用 " / " 连接
        parts = []
        if lb["tail"]:
            parts.append(lb["tail"])
        rows = []
        for ln in pages_lines[pi]:
            b = ln["bbox"]
            if lb["y0"] - 1 <= b[1] <= y_end + 1 and b[0] > lb["x0"] + 4:
                rows.append(ln)
        rows.sort(key=lambda l: (round(l["bbox"][1], 1), l["bbox"][0]))
        grouped, last_y = [], None
        for ln in rows:
            y = round(ln["bbox"][1], 1)
            if last_y is not None and abs(y - last_y) <= 2:
                grouped[-1].append(ln)
            else:
                grouped.append([ln])
                last_y = y
        for grp in grouped:
            parts.append(" / ".join(
                " ".join(w[4] for w, ref in pages_pairs[pi]
                         if ref is grp_item and w[1] >= grp_item["bbox"][1] - 1)
                or grp_item["raw"].strip()
                for grp_item in grp))
        text = " ".join(p for p in parts if p).strip()
        word_indices = []
        for wi, (w, ref) in enumerate(pages_pairs[pi]):
            cx, cy = (w[0] + w[2]) / 2, (w[1] + w[3]) / 2
            if bbox[0] <= cx <= bbox[2] and bbox[1] <= cy <= bbox[3]:
                word_indices.append(wi)
        opts.append({"label": lb["label"], "text": text, "bbox": bbox,
                     "word_indices": word_indices})
    return opts


def regions_contain(regions, page, cx, cy):
    for rg in regions:
        if rg["page"] != page:
            continue
        b = rg["bbox"]
        if b[0] <= cx <= b[2] and b[1] <= cy <= b[3]:
            return True
    return False


def question_text_lines(n, q, pages_lines, pages_pairs):
    out = []
    for pi, lines in enumerate(pages_lines):
        for ln in lines:
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
                      paper_code, pages_lines, pages_pairs):
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
    if "section" in q:
        fm.append(("section", q["section"]))
    fm += [
        ("image", f"assets/q{n:02d}.webp"),
        ("layout", f"assets/q{n:02d}.layout.json"),
    ]
    body = "\n".join(question_text_lines(n, q, pages_lines, pages_pairs))

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
    lines.append("source: " + yaml_scalar(f"Pearson Edexcel IAL {paper_code.upper()}/01, {year}"))
    lines.append("cut_status: " + q["status"])
    lines.append("language: en")
    lines.append("---")
    lines.append("")
    lines.append(body)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def update_front_matter(path, fm_pairs, q):
    """qNN.md 已存在：只更新脚本拥有的键，人工内容（正文/其他键）不动。

    ⚠️ front-matter 解析不了时**不能静默返回**：曾出现编辑器（WYSIWYG 保存）
    把 `---` 转义成 `\\---`、并在键之间插空行，导致本函数永远匹配失败、
    脚本却一切照常报 OK，直到 validate_cut.py 报「缺 front-matter」才暴露。
    所以这里显式打警告，提示该文件需人工修复或删除后重跑。
    """
    text = path.read_text(encoding="utf-8")
    m = re.match(r"^---\n(.*?)\n---\n?(.*)$", text, re.S)
    if not m:
        print(f"    [warn] {path.name}: front-matter 无法解析（首行非 '---'？被编辑器重排过？），"
              f"已跳过更新；该文件需人工修复或删除后重跑本脚本")
        return
    fm_text, body = m.group(1), m.group(2)
    blocks, order = {}, []
    cur = None
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
    # 版本更迭后废弃的键：从已有 front-matter 中移除
    for stale in ("words",):
        if stale in blocks and stale not in updates:
            del blocks[stale]
            order.remove(stale)
    options = q.get("_options")
    for k, v in updates.items():
        new = f"{k}: {yaml_scalar(v)}"
        if k in blocks:
            blocks[k] = [new]
        else:
            blocks[k] = [new]
            order.append(k)
    if options is not None and q["question_type"] == "mcq" and q["status"] != "manual":
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
    """重跑合并：auto/review 覆盖；verified 一致才刷新；manual 永不覆盖 regions/anchors。"""
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
            if (regions_equal(old.get("regions", []), q["regions"])
                    and answer_regions_equal(old.get("answer_regions", []), q.get("answer_regions", []))):
                for k in ("confidence", "total_marks", "section", "question_type", "subquestions",
                          "answer_block"):
                    if k in q:
                        old[k] = q[k]
                old.setdefault("review_notes", [])
                old["_options"] = q.get("_options") or old.get("_options")
                report.append(f"Q{q['number']}: verified 一致，刷新可再生字段")
                result.append(old)
            else:
                old["status"] = "review"
                old.setdefault("review_notes", []).append("重跑的自动结果与人工确认不一致，请复核")
                old["_options"] = q.get("_options") or old.get("_options")
                report.append(f"Q{q['number']}: verified 不一致 → 保留人工 regions，降级 review")
                result.append(old)
            continue
        if status == "manual":
            for k in ("total_marks", "section", "question_type"):
                if k in q and q[k] != old.get(k):
                    report.append(f"Q{q['number']}: manual，{k} 刷新为 {q[k]}")
                    old[k] = q[k]
            old["_options"] = old.get("_options") or q.get("_options")
            report.append(f"Q{q['number']}: manual，regions/anchors 保留人工值")
            result.append(old)
    for num, old in old_by_num.items():
        if num not in {q["number"] for q in result}:
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


def answer_regions_equal(a, b):
    """answer_regions（[{label, regions}]）逐条比较 label 与 regions。"""
    if len(a) != len(b):
        return False
    for ea, eb in zip(a, b):
        if ea.get("label") != eb.get("label"):
            return False
        if not regions_equal(ea.get("regions", []), eb.get("regions", [])):
            return False
    return True


if __name__ == "__main__":
    main()
