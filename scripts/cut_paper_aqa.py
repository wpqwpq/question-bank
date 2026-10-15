#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""cut_paper_aqa.py — 牛津 AQA 国际 A-Level 物理 / 化学卷（PH01–PH05 / CH01–CH05）按题自动切割

⚠️ 专用脚本，服务于 OxfordAQA International AS/A-level（物理 PH01–PH05、化学 CH01–CH05）；
   不动 cut_paper.py（Edexcel）与 cut_paper_cie.py（CIE 9702）。产出与两者
   **完全同构**（cut.json + qNN.md，符合 schema/cut.schema.json），可直接进
   render_questions.py / validate_cut.py / 打标台。

背景见 docs/AQA物理切割可行性分析.html（2026-10-15 全 72 卷实测）。与 Edexcel / CIE
的版式差异决定了本脚本必须另起一套锚点策略：

    · **没有逐题 Total 终点锚点**（0/72 卷）——Edexcel 的
      `(Total for Question N = X marks)` 与 CIE 的 `[Total: N]` 都不存在。
      现有两套脚本的 region 收尾全靠分值锚点，AQA 只能改用
      「下一题徽章 + END OF SECTION / END OF QUESTIONS 硬终点」。
    · **题号是两位数徽章** `0 1`…`2 6`：两位数字各占一个方框，文本层还原成
      `0 1 `（x0≈51–54、size≈11、粗体）。⚠️ **同一题的徽章会随每个小问重复**
      （`0 1 / . 1`、`0 1 / . 2`）→ 扫题号必须按「序列 1..N 取首次出现」去重，
      否则题数会翻倍（2026-05 U1：26 题出 51 个徽章）。
    · **分值锚点是行内 `[N mark(s)]`**，右对齐（x1≈530/535）、粗体、**逐小问**给出；
      全卷整行匹配求和 ≡ 80（72/72 成立）。⚠️ 不能按 x0 过滤分值行——存在带
      前导空白、x0 远在左侧的行（2021-01 U3 / 2025-05 U3 曾因此少算 1–4 分）。
    · **三节结构**：U1/U2 是 Section A / B / C（C 整节是选择题）；U3/U4/U5 只有
      Section A / B，选择题在 B 节尾部与结构化题**同节混排**。
      ⚠️ 因此**题型不能按节判**，必须按题判（题段内 ≥3 个不同选项字母行）。
    · **页框是 4 条 0.48pt 细线**（不是单个大矩形）：横线 x0≈39.5 x1≈539 y≈53.5/765.5，
      竖线 x0≈39/538.5 h≈711。沿用 Edexcel 的 `width>400 and height>600` 判据会
      全部漏过 → 上/下框线被当内容、"最后一项内容" 恒等于框底、**页尾裁剪静默失效**。
      判据须写成 `width>490 或 height>600 或 (width≤2 且 height≤2)`（角点残段）。
      ⚠️ 另有静默地雷：`CambriaMath` 的空格 span 会把**整行 bbox 撑到 ±33pt**
      （2026-05 U1 p2 的镶数学题首行 bbox 高 67pt）→ 行 bbox 必须只用含可见字符
      的 span 重建，否则题区起点/页尾内容底全被带偏。
    · **无答题册** → answer_regions / answer_block / 分区式拼接全不产生，
      渲染稳定走「单 region 顺序拼接」（与 CIE 同）。
    · 插入页 `DO NOT WRITE ON THIS PAGE` / `There are no questions printed on this
      page` / 卷尾 `Additional page, if required` 会落在题区间内 → 整页跳过。

页尾空白（2026-10-15 用户定稿的「封顶保留」规则）：
    region 下界 = min(版心底 B, 最后一项内容底 + CAP)，CAP = 260pt = 10 行 × 26pt
    （AQA 相邻作答横线行距实测恒为 25.8pt）。**不足 CAP 全保留、超了才切**。
    仅当「该页是该题最后一页 + 末项是正文散文 + 页面上无导航提示」三条同时成立；
    否则（图形/横线/表格/选项行/单位收尾、有 `continues…` / `Turn over for the
    next question`、题还续下页）一律裁到末项 + 8pt。中间段（非末段）一律裁到 +8pt。
    附带收益：封顶**天然作用于末段**，所以本脚本不需要 CIE 版的 `anchor_end`
    二元标志——原「要么全留、要么裁到 8pt」的两难被一个连续量替换。

坐标约定（全仓库统一）: PDF 点 pt，页面左上角原点，y 向下；bbox=[x0,y0,x1,y1]。

用法:
    python scripts/cut_paper_aqa.py <session_dir>
    python scripts/cut_paper_aqa.py --probe <session_dir>   # 只打印锚点诊断，不写盘

重跑保护: source.sha256 不符硬停；status=verified/manual 的题其 regions 不被覆盖。
"""

import argparse
import hashlib
import json
import re
import sys
from collections import Counter
from pathlib import Path

import pymupdf

# ── 排版常量（2026-10-15 全 72 卷实测） ──────────────────────────────────────
HEADER_Y = 50.0          # 页眉安全线：页码居中 y≈27.5，页框顶线 y≈53.5
FOOTER_Y = 768.0         # 页脚安全线：Turn over ► y≈774、*NN* y≈790、IB/… y≈815
L_DEF, R_DEF = 39.0, 539.0   # 页框左右竖线 x0（全库 2277/2278 页一致）
T_DEF, B_DEF = HEADER_Y, 766.0   # 页框上下横线 y（53.5 / 765.5，+0.5 容差）
BADGE_X_MIN, BADGE_X_MAX = 40.0, 80.0   # 题号徽章列（实测 51–54，个别 56/57）
BADGE_MAX_SIZE = 13.0    # 徽章字号 ≈11，正文 11–12；≥13 一律不是徽章
BADGE_SPLIT_DX = (12.0, 24.0)   # 两行拆分徽章：两数字同 y、x0 相差约 17pt（2018-01 CH01 Q4 实测 17.2）
SUBQ_X_MIN, SUBQ_X_MAX = 70.0, 110.0    # 小问号 ". N" 列（实测 81/82/83）
OPT_X_MIN, OPT_X_MAX = 84.0, 106.0      # 选择题选项行 x0（实测众数 93）
CAP = 260.0              # 封顶保留：10 行 × 26pt
TRIM_MIN_GAP = 20.0
TRIM_PAD = 8.0
MAX_REGION_PAGES = 10    # 吞页防御：单题跨页数超此值即降级 review

BADGE_RE = re.compile(r"^(\d)\s(\d)(?:\s*\.\s*(\d{1,2}))?$")
SUBQ_RE = re.compile(r"^\.\s*(\d{1,2})\s*$")
MARK_RE = re.compile(r"\[\s*(\d{1,2})\s*marks?\s*\]")
OPT_RE = re.compile(r"^([A-D])(?:\s+\S.*)?$")
SEC_RE = re.compile(r"^Section\s+([A-Z])\s*$")
ENDQ_RE = re.compile(r"END\s+OF\s+QUESTIONS", re.I)
ENDSEC_RE = re.compile(r"END\s+OF\s+SECTION\s*([A-Z]?)", re.I)
NAV_RE = re.compile(
    r"(continues on the next page"
    r"|turn over for the next question"
    r"|turn to page\s+\d+\s+for the next question)", re.I)
NOQ_RE = re.compile(r"no questions printed on this page", re.I)
DNW_RE = re.compile(r"DO\s*NOT\s*WRITE\s*ON\s*THIS\s*PAGE", re.I)
ADDPG_RE = re.compile(r"Additional page, if required", re.I)
JUNK_RE = re.compile(r"^[\s,.\-*\u0000-\u001f]*$")

# 封面右栏「For Examiner's Use」阅卷表：结构化题逐题列一行数字，选择题整段列成
# 「N–M」区间（分隔符有 en dash / U+2212 减号 / 连字符三种写法）。这张表是
# **机器可读的题型权威来源**（比按选项行启发式判更稳），本脚本用它给
# question_type 定调，并用每题的选项行检测来产热区、交叉校验。
TABLE_CELL_RE = re.compile(r"^(\d{1,2})$")
TABLE_RANGE_RE = re.compile(r"^(\d{1,2})\s*[-\u2013\u2014\u2212]\s*(\d{1,2})$")
TABLE_X_MIN, TABLE_X_MAX = 430.0, 545.0
TABLE_Y_MIN, TABLE_Y_MAX = 420.0, 770.0

BOARDS = {"edexcel", "aqa", "cie"}
QUALS = {"a-level", "alevel", "igcse", "gcse"}
SESSION_TOKENS = {"jan", "mar", "may-jun", "june", "oct", "oct-nov"}
AQAPAPERS = {"ph01": 1, "ph02": 2, "ph03": 3, "ph04": 4, "ph05": 5}   # 物理
AQACHPAPERS = {"ch01": 1, "ch02": 2, "ch03": 3, "ch04": 4, "ch05": 5}  # 化学
SUPPORTED_PAPERS = {**AQAPAPERS, **AQACHPAPERS}

# paper_code → 卷面总分（物理 72/72 及化学 70/80/60 实测；paper.md 有 total_marks 时以之为准）
PAPER_TOTAL = {k: 80 for k in AQAPAPERS}
PAPER_TOTAL.update({"ch01": 70, "ch02": 70, "ch03": 80, "ch04": 80, "ch05": 60})

# 学科目录名 → 卷面学科名（写进 qNN.md 的 source 字段）
SUBJECT_LABEL = {"physics": "Physics", "chemistry": "Chemistry"}


# ── 行提取 ──────────────────────────────────────────────────────────────────

def visible_bbox(ln):
    """只用**含可见字符**的 span 求并集。

    ⚠️ 必须这样算：AQA 的数学式里 CambriaMath 只含一个空格的 span 带有
    异常 ascent/descent，其 bbox 会竖直撑到 ±33pt（2026-05 U1 p2 题首行
    bbox 高 67pt）。若直接用 line["bbox"]，题区起点与「最后一项内容底」
    都会被带偏，页尾裁剪也跟着错。
    """
    xs0, ys0, xs1, ys1 = [], [], [], []
    for s in ln["spans"]:
        if not s["text"].strip():
            continue
        b = s["bbox"]
        xs0.append(b[0]); ys0.append(b[1]); xs1.append(b[2]); ys1.append(b[3])
    if not xs0:
        b = ln["bbox"]
        return [b[0], b[1], b[2], b[3]]
    return [min(xs0), min(ys0), max(xs1), max(ys1)]


def build_page_lines(page):
    """一页的全部横排文本行，按 (y0, x0) 排序。"""
    lines = []
    for block in page.get_text("dict")["blocks"]:
        if block.get("type") != 0:
            continue
        for ln in block["lines"]:
            if tuple(ln.get("dir", (1, 0))) != (1.0, 0.0):
                continue
            raw = "".join(s["text"] for s in ln["spans"])
            if not raw.strip():
                continue
            s0 = ln["spans"][0]
            lines.append({
                "bbox": visible_bbox(ln),
                "raw": raw,
                "bold": any("bold" in s["font"].lower() for s in ln["spans"]),
                "size": max(s["size"] for s in ln["spans"]),
                "first": (s0["text"], "bold" in s0["font"].lower(), s0["bbox"][0]),
            })
    lines.sort(key=lambda l: (round(l["bbox"][1], 1), round(l["bbox"][0], 1)))
    return lines


# 页眉/页脚行按内容兜底过滤（y 阈值之外的补充）
FOOTER_LINE_RE = re.compile(r"^(\*[^*]*\*|IB/[A-Z]/?[A-Za-z0-9/_]*|Turn over\s*►?)\s*$")


def content_lines(lines, footer_y=None):
    """版心内容行：滤掉页眉（页码）、页脚（条码/卷号/IB 号/Turn over ►）
    与点线装饰垃圾。"""
    fy = FOOTER_Y if footer_y is None else footer_y
    out = []
    for ln in lines:
        b = ln["bbox"]
        if b[1] < HEADER_Y or b[1] > fy:
            continue
        raw = ln["raw"].strip()
        if not raw or JUNK_RE.fullmatch(raw):
            continue
        if FOOTER_LINE_RE.match(raw):
            continue
        out.append(ln)
    return out


def page_flags(page):
    """该页是否为插入页 / 卷尾附加页（不承载题目内容，题区间整页跳过）。"""
    t = page.get_text()
    f = set()
    if NOQ_RE.search(t):
        f.add("blank")
    if DNW_RE.search(t):
        f.add("dnw")
    if ADDPG_RE.search(t):
        f.add("addpg")
    return f


def assign_words_to_lines(lines, words):
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


# ── 通用小工具（与 cut_paper.py / cut_paper_cie.py 同名同义） ────────────────

def r2(v):
    return round(float(v), 2)


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


def build_regions(pages, sy0, ey1, L, T, B, R=R_DEF):
    """按有序页列表拼 region；首页起于 sy0、末页止于 ey1，中间页 T→B。

    pages 已剔除插入页（不连续）——不能再用 range(start, end+1) 生成。
    """
    regs = []
    for i, pi in enumerate(pages):
        top = sy0 if i == 0 else T
        bot = ey1 if i == len(pages) - 1 else B
        regs.append({"page": pi, "bbox": [r2(L), r2(top), r2(R), r2(bot)]})
    return regs


def is_frame_rect(r):
    """页框线 / 角点残段（不算题目内容）。

    ⚠️ AQA 页框是 4 条 0.48pt 细线而非单矩形：横线 498.8×0.48、竖线
    0.48×711.5、四角另有 0.48×0.48 的退化残段。Edexcel 的
    `width>400 and height>600` 判据在 AQA 上**全部漏过**，会让「最后一项内容」
    永远等于框底线、页尾裁剪静默失效。
    """
    w, h = r.width, r.height
    return w > 490 or h > 600 or (w <= 2 and h <= 2)


def page_content_rects(doc, pi, cache, footer_y=None):
    """一页内矢量图形与图片块 [(y1, y0, x0, x1, kind), ...]（已剔除页框线）。"""
    fy = FOOTER_Y if footer_y is None else footer_y
    if pi not in cache:
        rows = []
        try:
            for dr in doc[pi].get_drawings():
                db = dr["rect"]
                if db.width <= 0 and db.height <= 0:
                    continue
                if db.y1 > fy or db.y0 < HEADER_Y:
                    continue
                if is_frame_rect(db):
                    continue
                rows.append((db.y1, db.y0, db.x0, db.x1, "line"))
        except Exception:
            pass
        for b in doc[pi].get_text("dict")["blocks"]:
            if b.get("type") == 1 and HEADER_Y <= b["bbox"][1] <= fy:
                bb = b["bbox"]
                rows.append((bb[3], bb[1], bb[0], bb[2], "image"))
        cache[pi] = rows
    return cache[pi]


def option_label_of(ln):
    """若该行「行首 span」是粗体且以单个 A–D 字母起头 → 返回 'A'–'D'，否则 None。

    ⚠️ 判据是「**行首 span 是粗体 + 首字符 A–D + 其后是空格/结束**」，不是
    「整行以 A–D 开头」：题干常以 "A projectile is fired…" 开头（同一列、同一起点），
    按整行正则会把题干行误当选项行，导致选项热区整体错位（2026-06 PH01 Q13/Q16
    踩过）。全库实测选项标签 span **一律粗体**，而题干里句首的 'A ' 是非粗体。

    ⚠️ 这里**不判 x0 列位置**：选项列有两种——常规每行一项时 x0≈92.5，
    但选项排成表格时（如 2019-06 PH03 Q11 的 A/B/C/D 表格行）x0≈122；
    列一致性交给 question_type_of 按「同列顺序 ABCD」聚类判。
    """
    t, bold, x0 = ln["first"]
    t = t.strip()
    if not bold or not t or t[0] not in "ABCD":
        return None
    if len(t) > 1 and t[1] not in " .":
        return None
    if not (40.0 <= x0 <= R_DEF):
        return None
    return t[0]


def cover_exam_table(doc):
    """封面阅卷表 → (选择题题号集合, 诊断信息)；解析不出返回 (None, 原因)。

    结构化题逐题一行、选择题整段一行「N–M」；单列须恰为 1..(区间起点-1) 才采信。
    """
    singles, ranges = [], []
    for block in doc[0].get_text("dict")["blocks"]:
        if block.get("type") != 0:
            continue
        for ln in block["lines"]:
            b = ln["bbox"]
            if not (TABLE_X_MIN <= b[0] <= TABLE_X_MAX
                    and TABLE_Y_MIN <= b[1] <= TABLE_Y_MAX):
                continue
            t = "".join(s["text"] for s in ln["spans"]).strip()
            if TABLE_CELL_RE.match(t):
                singles.append(int(t))
            else:
                m = TABLE_RANGE_RE.match(t)
                if m:
                    ranges.append((int(m.group(1)), int(m.group(2))))
    if not singles:
        return None, "封面阅卷表未解析到题号行"
    if not ranges:
        # U5（Physics in practice）整卷无选择题 → 阅卷表只有单行、没有区间
        if sorted(singles) == list(range(1, len(singles) + 1)):
            return set(), ""
        return None, f"封面阅卷表单列 {singles} 不连续，未采信"
    a, b = ranges[0]
    if sorted(singles) != list(range(1, a)):
        return None, f"封面阅卷表单列 {singles} 与区间 {ranges} 不连续，未采信"
    return set(range(a, b + 1)), ""


def row_top_of(page, y0, pages_content, min_x=105.0, half=12.0, R=R_DEF):
    """题号徽章所在「行」的顶边。

    ⚠️ 徽章数字的 y0 常比同行的题干低 ~9pt（徽章框小、题干含大字号数学式，
    如 2026-06 PH01 p1 的 Q2：徽章 y0=347.9 而题干 'Nitrogen-13 (' y0=338.3）。
    直接拿徽章 y0 当题区顶/上一题底，会把下一题题干的上沿切进上一题的图里
    （页尾会露出半行被切开的字）。取同行候选行（x≥105 的正文列）的最小 y0 才对。
    """
    tops = [y0]
    for ln in pages_content[page]:
        b = ln["bbox"]
        if abs(b[1] - y0) <= half and min_x <= b[0] <= R:
            tops.append(b[1])
    return min(tops)


def region_has_content(pi, y_top, y_bot, pages_content, content_cache, doc,
                       L=L_DEF, R=R_DEF):
    """[y_top, y_bot] 内是否还有题目内容（文本行 / 矢量 / 图片）。

    ⚠️ 必须同时卡 x 范围：页框外的「Do not write outside the box」提示就在
    页顶 y≈52–70（x≈546–586），不卡 x 会让每页都"有内容"，跨页回退判据失效。
    """
    for ln in pages_content[pi]:
        b = ln["bbox"]
        if b[1] < y_top - 1 or b[1] > y_bot + 1:
            continue
        if b[2] < L or b[0] > R:
            continue
        return True
    for y1, y0, x0, x1, _k in page_content_rects(doc, pi, content_cache):
        if y0 > y_bot + 1 or y1 < y_top - 1:
            continue
        if x1 < L or x0 > R:
            continue
        return True
    return False


def last_content(region, pages_content, content_cache, doc):
    """region 内最低一项内容 → (y_bottom, kind, text, is_option)；无内容 (None,None,"",False)。

    kind: "text" 文本行 / "line" 矢量（作答横线、表格、图形）/ "image" 图片。
    """
    bb = region["bbox"]
    last = (None, None, "", False)
    for ln in pages_content[region["page"]]:
        lb = ln["bbox"]
        if lb[3] < bb[1] - 1 or lb[1] > bb[3] + 1:
            continue
        if lb[2] < bb[0] or lb[0] > bb[2]:
            continue
        if last[0] is None or lb[3] > last[0]:
            last = (lb[3], "text", ln["raw"].strip(), bool(option_label_of(ln)))
    for y1, _y0, x0, x1, kind in page_content_rects(doc, region["page"], content_cache):
        if y1 < bb[1] - 1 or y1 > bb[3] + 1:
            continue
        if x1 < bb[0] or x0 > bb[2]:
            continue
        if last[0] is None or y1 >= last[0]:
            last = (y1, kind, "", False)
    return last


def page_has_nav(region, pages_content):
    """该 region 所在页是否带版面导航提示（本页内容已终）。"""
    for ln in pages_content[region["page"]]:
        if NAV_RE.search(ln["raw"]):
            return True
    return False


def trim_regions(doc, regions, pages_content, content_cache, log=None, allow_cap=True):
    """页尾空白裁剪（原地修改），返回 (裁剪段数, 封顶段数)。

    · 中间段（题还续下页）→ 末项 + 8pt；
    · 末段：末项是正文散文 且 页面无导航提示 → 封顶保留 min(B, 末项 + CAP)；
            否则（图形/横线/表格/选项行/单位收尾、短文本、有导航提示）→ +8pt。
    · allow_cap=False（选择题）→ 一律 +8pt：MCQ 无书写区，尾部留白纯属排版残留。
    ⚠️ 末段**必须**纳入裁剪范围（AQA 无逐题 Total 锚点，每道跨页题的末段
    都以版心底收尾）——照抄 Edexcel 的 `regions[:-1]` 会让每道跨页题都拖
    一段版心空白进图。
    """
    n_trim = n_cap = 0
    for i, rg in enumerate(regions):
        is_last = i == len(regions) - 1
        last_y, kind, text, is_opt = last_content(rg, pages_content, content_cache, doc)
        if last_y is None:
            continue
        gap = rg["bbox"][3] - last_y
        if gap <= TRIM_MIN_GAP:
            continue
        prose = (allow_cap and kind == "text" and not is_opt
                 and not OPT_RE.match(text)
                 and len(text.split()) >= 4 and not NAV_RE.search(text))
        if is_last and prose and not page_has_nav(rg, pages_content):
            new_y1 = min(rg["bbox"][3], last_y + CAP)
            if new_y1 > rg["bbox"][1] + 20:
                if new_y1 < rg["bbox"][3] - 0.5:
                    n_cap += 1
                if log is not None:
                    log.append(("cap", i, is_last, rg["bbox"][3], last_y, kind,
                                text[:28], page_has_nav(rg, pages_content)))
                rg["bbox"] = [rg["bbox"][0], rg["bbox"][1], rg["bbox"][2], r2(new_y1)]
                n_trim += 1
        else:
            new_y1 = last_y + TRIM_PAD
            if new_y1 > rg["bbox"][1] + 20:
                if log is not None:
                    log.append(("cut", i, is_last, rg["bbox"][3], last_y, kind,
                                text[:28], page_has_nav(rg, pages_content)))
                rg["bbox"] = [rg["bbox"][0], rg["bbox"][1], rg["bbox"][2], r2(new_y1)]
                n_trim += 1
    return n_trim, n_cap


# ── 主流程 ──────────────────────────────────────────────────────────────────

def anchor_scan(doc, pages_lines, pages_content, filler_pages):
    """扫三类锚点：题号徽章（序列 1..N）/ Section 标题 / END 硬终点。"""
    # ── 题号徽章 ──
    cands = []
    for pi, lines in enumerate(pages_content):
        for ln in lines:
            b = ln["bbox"]
            if not (BADGE_X_MIN <= b[0] <= BADGE_X_MAX) or ln["size"] >= BADGE_MAX_SIZE:
                continue
            m = BADGE_RE.match(ln["raw"].strip())
            if not m:
                continue
            n = int(m.group(1) + m.group(2))
            if n < 1 or n > 60:
                continue
            sub = int(m.group(3)) if m.group(3) else None
            cands.append((pi, round(b[1], 2), n, sub, ln))

    # ── 拆分徽章：两个数字是**独立文本对象**（同一视觉行，y 相同、x0 差 ~17pt）──
    # ⚠️ 措辞：不是「视觉两行」——卷面上看就是一行 `0 4`；拆的是**文本层**：
    # `0` 与 `4` 各是一个独立 line 对象（raw 分别为 '0 ' / '4 '），
    # 而徽章正则假设两个数字连在同一条 line 的 raw 里（'0 4 ' 带空格）→ 直接漏检。
    # 老卷（2018 季）特有。后果不是「少一题」而是**本题起点退到下一个重复徽章处**
    # （AQA 每个小问都重复印徽章），于是上一题的题图吞掉本题的题干与表格
    # （2018-01 CH01 Q4：Q3 图里长出了 Q4 的题干 + Table 1，Q4 图却从 `0 4 . 1` 起）。
    # 全库 87 卷仅 1 处。判据：同页、同 y（±0.8）、各为单个数字、x0 差 BADGE_SPLIT_DX、都粗体。
    for pi, lines in enumerate(pages_content):
        singles = []
        for ln in lines:
            t = ln["raw"].strip()
            if len(t) != 1 or not t.isdigit() or ln["size"] >= BADGE_MAX_SIZE:
                continue
            if not ln["first"][1]:            # 行首 span 非粗体 → 不是徽章数字
                continue
            if not (BADGE_X_MIN <= ln["bbox"][0] <= BADGE_X_MAX):
                continue
            singles.append(ln)
        for i, a in enumerate(singles):
            for c in singles[i + 1:]:
                ba, bc = a["bbox"], c["bbox"]
                if abs(ba[1] - bc[1]) > 0.8:
                    continue
                if not (BADGE_SPLIT_DX[0] <= bc[0] - ba[0] <= BADGE_SPLIT_DX[1]):
                    continue
                n = int(a["raw"].strip()) * 10 + int(c["raw"].strip())
                if n < 1 or n > 60:
                    continue
                merged = {**a, "bbox": [ba[0], ba[1], bc[2], max(ba[3], bc[3])]}
                cands.append((pi, round(min(ba[1], bc[1]), 2), n, None, merged))

    cands.sort(key=lambda c: (c[0], c[1]))
    qstarts = {}
    expected, last = 1, (-1, -1.0)
    for pi, y, n, sub, ln in cands:
        if n == expected and (pi, y) > last:
            qstarts[expected] = {"page": pi, "y0": y, "bbox": list(ln["bbox"]),
                                 "line": ln, "first_sub": sub}
            last = (pi, y)
            expected += 1
    n_questions = expected - 1
    # ── Section 标题 ──
    sections = []
    for pi, lines in enumerate(pages_content):
        for ln in lines:
            m = SEC_RE.match(ln["raw"].strip())
            if m:
                sections.append({"letter": m.group(1), "page": pi,
                                 "y0": round(ln["bbox"][1], 2)})
    sections.sort(key=lambda s: (s["page"], s["y0"]))

    # ── END 硬终点 ──
    ends = []
    for pi, lines in enumerate(pages_content):
        for ln in lines:
            raw = ln["raw"].strip()
            b = ln["bbox"]
            if ENDQ_RE.search(raw):
                ends.append({"kind": "ENDQ", "letter": "", "page": pi,
                             "y0": round(b[1], 2), "y1": round(b[3], 2)})
            elif ENDSEC_RE.search(raw):
                ends.append({"kind": "ENDSEC", "letter": ENDSEC_RE.search(raw).group(1),
                             "page": pi, "y0": round(b[1], 2), "y1": round(b[3], 2)})
    ends.sort(key=lambda e: (e["page"], e["y0"]))

    # ── 小问号 ". N"（独立行形态） ──
    subs = []
    for pi, lines in enumerate(pages_content):
        for ln in lines:
            b = ln["bbox"]
            if not (SUBQ_X_MIN <= b[0] <= SUBQ_X_MAX):
                continue
            m = SUBQ_RE.match(ln["raw"].strip())
            if m:
                subs.append({"page": pi, "y0": round(b[1], 2), "n": int(m.group(1)),
                             "bbox": list(b)})
    subs.sort(key=lambda s: (s["page"], s["y0"]))
    return qstarts, n_questions, sections, ends, subs, cands


def question_type_of(regions, pages_content, cover_mcq=None, n=None):
    """按题判题型（不能按 Section 判：U3/U4/U5 的选择题与结构化题同节混排）。

    优先用**封面阅卷表**（选择题整段列成区间）定调；表不可用时退回启发式：
    选项标签行有两种形态——``A tension and tensile stress``（字母+文本同行）
    与裸字母 ``B``（选项含上下堆叠的分数式时，字母自成一行、文本在其上下方）。
    启发式判据：选项列内 ≥3 个不同字母，且按 y 升序恰为 A→B→C→D 的前缀
    （挡住正文里的表格列标题）。

    ⚠️ 必须按 region 的 y 区间过滤：选择题常两题共处一页（Q14/Q15），不做
    归属会把上一题的 A–D 当成下一题的选项，热区整体跑到题区之外。

    选项行聚类规则：候选行（行首粗体 A–D）中取「标签按 A→B→C→D 升序、且
    行首 x0 同列（差 ≤10pt）」的最长前缀，≥3 个即判 MCQ。不卡绝对列位置——
    常规版式选项列 x0≈92.5，表格版式（A/B/C/D 各占一表格行）x0≈122。

    返回 (题型, 选项行列表)。选项行**总是**检测（热区要用），与题型无关。
    """
    cands = []
    for rg in regions:
        bb = rg["bbox"]
        for ln in pages_content[rg["page"]]:
            b = ln["bbox"]
            if not (bb[1] - 1 <= b[1] <= bb[3] + 1):
                continue
            lab = option_label_of(ln)
            if lab:
                cands.append({"label": lab, "page": rg["page"],
                              "y0": round(b[1], 2), "y1": round(b[3], 2),
                              "yc": (b[1] + b[3]) / 2.0,
                              "x0": ln["first"][2], "x1": b[2]})
    cands.sort(key=lambda o: (o["page"], o["yc"]))
    best = []
    for start in range(len(cands)):
        seq, xs = [], []
        for o in cands[start:]:
            if not seq:
                if o["label"] == "A":
                    seq.append(o)
                    xs.append(o["x0"])
                continue
            if o["label"] == "ABCD"[len(seq)] and abs(o["x0"] - xs[0]) <= 10.0:
                seq.append(o)
                xs.append(o["x0"])
                if len(seq) == 4:
                    break
        if len(seq) > len(best):
            best = seq
        if len(best) == 4:
            break
    ordered = best
    heuristic_mcq = len(ordered) >= 3
    if cover_mcq is not None and n is not None:
        return ("mcq" if n in cover_mcq else "structured"), (ordered if heuristic_mcq else [])
    return ("mcq" if heuristic_mcq else "structured"), (ordered if heuristic_mcq else [])


def fallback_option_rows(regions, content_cache, doc):
    """选项整块是**图形**（文本层里没有 A–D 标签）时的兜底：取题区内最大的图像块，
    按高度四等分当 A–D 条带。

    AQA 有少数选择题把四个选项画在一张图里（如 2024-06 PH02 Q18 的四张图），
    这时文本层没有任何选项标签可归。四等分是这类版式的自然切法（四行等距排布），
    但仍属启发式 → 调用方会留一条 review 说明。
    """
    best = None
    for rg in regions:
        bb = rg["bbox"]
        for y1, y0, x0, x1, kind in page_content_rects(doc, rg["page"], content_cache):
            if kind != "image":
                continue
            if y0 < bb[1] - 1 or y1 > bb[3] + 1:
                continue
            if (y1 - y0) < 100 or (x1 - x0) < 100:
                continue
            if best is None or (y1 - y0) > best[1]:
                best = (rg["page"], y1 - y0, y0, y1, x0, x1)
    if best is None:
        return []
    pi, h, y0, y1, x0, x1 = best
    rows = []
    for i, lab in enumerate("ABCD"):
        a, b = y0 + h * i / 4.0, y0 + h * (i + 1) / 4.0
        rows.append({"label": lab, "page": pi, "y0": round(a, 2), "y1": round(b, 2),
                     "yc": (a + b) / 2.0, "x0": max(85.0, x0), "x1": x1})
    return rows


def extract_option_hotzones(q, opt_rows, pages_content, B, R):
    """选择题选项热区：以选项标签行的 y 中心为基准，取相邻标签的中点作带边界。

    ⚠️ 不能简单用「标签行 → 下一标签行」当带：选项含上下堆叠的分数式时，
    字母居中于两行文字之间，文本会溢到字母行之外（2026-06 PH01 Q22 的
    B/D 选项），按中点分带才能把该选项的正文包住。
    """
    if not opt_rows:
        q["_options"] = []
        q["option_hotzones"] = []
        return False
    # ⚠️ 上下界都要钳：首选项的带上界取 span/2，会伸到 region 顶之上（2018-06 CH05 Q33
    # 越界 63.1pt、2026-06 CH05 Q19 越界 102.8pt）→ layout.json 出现负归一化 y，
    # validate_cut.py 判 FAIL。此前只钳了下界（bot），上界漏钳。
    region_top, region_bottom = {}, {}
    for rg in q["regions"]:
        pi = rg["page"]
        region_top[pi] = min(region_top.get(pi, 1e9), rg["bbox"][1])
        region_bottom[pi] = max(region_bottom.get(pi, 0), rg["bbox"][3])
    x_left = min(o["x0"] for o in opt_rows) - 7
    opts = []
    by_page = {}
    for o in opt_rows:
        by_page.setdefault(o["page"], []).append(o)
    for pi, rows in by_page.items():
        rows.sort(key=lambda o: o["yc"])
        span = rows[1]["yc"] - rows[0]["yc"] if len(rows) > 1 else 34.0
        for i, o in enumerate(rows):
            half_lo = (o["yc"] - rows[i - 1]["yc"]) / 2 if i > 0 else span / 2
            half_hi = (rows[i + 1]["yc"] - o["yc"]) / 2 if i + 1 < len(rows) else span / 2
            top = max(o["yc"] - half_lo, region_top.get(pi, 0.0))
            bot = min(o["yc"] + half_hi, region_bottom.get(pi, B))
            bbox = [r2(x_left), r2(top), r2(R), r2(bot)]
            text_rows = [ln for ln in pages_content[pi]
                         if top - 1 <= ln["bbox"][3] and ln["bbox"][1] <= bot + 1
                         and ln["bbox"][0] >= x_left - 1 and ln["bbox"][2] <= R + 2]
            text_rows.sort(key=lambda l: (round(l["bbox"][1], 1), l["bbox"][0]))
            text = " ".join(re.sub(r"\s+", " ", ln["raw"]).strip() for ln in text_rows).strip()
            opts.append({"label": o["label"], "text": text, "bbox": bbox,
                         "page": pi, "yc": o["yc"]})
    opts.sort(key=lambda o: (o["page"], o["yc"]))
    q["_options"] = opts
    q["option_hotzones"] = [{"label": o["label"], "bbox": o["bbox"]} for o in opts]
    return {o["label"] for o in opts} >= {"A", "B", "C", "D"}


def main():
    ap = argparse.ArgumentParser(description="牛津 AQA IAL 物理 / 化学卷按题切割")
    ap.add_argument("session_dir", help="如 data/aqa/a-level/physics/2026-june-ph01 或 "
                                       "data/aqa/a-level/chemistry/2026-june-ch01")
    ap.add_argument("--probe", action="store_true", help="只打印锚点诊断，不写盘")
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
    if board != "aqa":
        sys.exit(f"cut_paper_aqa.py 只服务 AQA 卷（board={board}）")
    paper_code_l = paper_code.lower()
    if paper_code_l not in SUPPORTED_PAPERS:
        sys.exit(f"不支持的 paper_code: {paper_code}（物理 ph01–ph05 / 化学 ch01–ch05）")

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

    pages_lines = [build_page_lines(p) for p in doc]
    filler_pages = set()
    for pi, page in enumerate(doc):
        if page_flags(page):
            filler_pages.add(pi)
    pages_content = [content_lines(lines) for lines in pages_lines]
    pages_pairs = [assign_words_to_lines(pages_lines[i], doc[i].get_text("words"))
                   for i in range(n_pages)]

    qstarts, n_questions, sections, ends, subs, badges_all = anchor_scan(
        doc, pages_lines, pages_content, filler_pages)
    cover_mcq, cover_note = cover_exam_table(doc)
    if cover_mcq is None:
        print(f"    [warn] {cover_note}，题型改用按题启发式判定")
    elif cover_mcq:
        print(f"    封面阅卷表：选择题 {min(cover_mcq)}–{max(cover_mcq)}"
              f"（{len(cover_mcq)} 题），其余结构化")
    if n_questions == 0:
        sys.exit("未定位到任何题号徽章，请人工检查 source.pdf（疑似扫描卷或版式异常）。")

    L, R, T, B = L_DEF, R_DEF, T_DEF, B_DEF

    # 题号 ──→ Section 归属（题起点之前最近的一个 Section 标题）
    def section_of(pi, y0):
        cur = ""
        for s in sections:
            if (s["page"], s["y0"]) <= (pi, y0):
                cur = s["letter"]
            else:
                break
        return cur

    # ── 逐题构建 ──
    auto_questions = []
    content_cache = {}
    n_trim = n_cap = 0
    for n in range(1, n_questions + 1):
        st = qstarts[n]
        q = {
            "number": str(n),
            "status": "auto",
            "confidence": 0.0,
            "regions": [],
            "anchors": {"start": {"page": st["page"],
                                  "bbox": [r2(v) for v in st["bbox"]]}},
            "review_notes": [],
            "subquestions": [],
        }
        notes = q["review_notes"]
        note_conf = 0.0

        # ── 终点：下一题徽章 / END OF SECTION / END OF QUESTIONS 取最近者 ──
        stop = None   # (page, y0, y1, kind, letter)
        nxt = qstarts.get(n + 1)
        if nxt:
            stop = (nxt["page"], nxt["y0"], nxt["y0"] - 2, "badge", "")
        for e in ends:
            if (e["page"], e["y0"]) <= (st["page"], st["y0"]):
                continue
            if stop is None or (e["page"], e["y0"]) < (stop[0], stop[1]):
                stop = (e["page"], e["y0"], e["y1"] + 2, e["kind"], e["letter"])
        if stop is None:
            end_page, end_y1 = n_pages - 1, B
            notes.append("缺终点锚点（无下一题徽章 / END 标记），边界由版心底兜底，请复核")
            q["status"] = "review"
        else:
            if stop[3] == "badge":
                # 下一题徽章顶 -2pt 就是本题终点；⚠️ 不能一律取「nxt.page-1 的版心底」
                # ——AQA 会把一道题的末几小问排到下一页、下一题再从该页中部开始
                # （2025-06 PH02 Q7 的 .4 小问就在下一题 Q8 之上）。只有当下一题
                # 那一页在徽章之上**没有本题内容**时（= 下一题从页顶起）才回退整页。
                # 边界用「下一题所在行顶 -2」而非徽章 y0-2（见 row_top_of）。
                nxt_top = row_top_of(stop[0], stop[1], pages_content)
                y_end = nxt_top - 2
                if stop[0] == st["page"]:
                    end_page, end_y1 = stop[0], y_end
                elif region_has_content(stop[0], T, y_end, pages_content,
                                        content_cache, doc):
                    end_page, end_y1 = stop[0], y_end
                else:
                    end_page, end_y1 = stop[0] - 1, B
            else:
                # END OF SECTION A/B / END OF QUESTIONS 是硬终点：题区止于标记底，
                # 末项即标记本身，页尾空白问题被显式消解（无需再判空白性质）。
                end_page, end_y1 = stop[0], stop[2]
            q["anchors"]["end"] = {"page": end_page,
                                   "bbox": [r2(L), r2(end_y1 - 16), r2(R), r2(end_y1)]}

        # 剔除插入页（DO NOT WRITE / no questions / Additional page）
        pages = [p for p in range(st["page"], end_page + 1) if p not in filler_pages]
        if not pages:
            pages = [st["page"]]
            notes.append("题区间全为插入页，已回退到起始页，请复核")
            q["status"] = "review"
        sy0 = row_top_of(st["page"], st["y0"], pages_content) - 4
        q["regions"] = build_regions(pages, sy0, end_y1, L, T, B, R)
        # 题型/选项行要在裁剪**前**判（用未裁 region 的 y 区间归属选项行），
        # 热区在裁剪**后**换算（末项热区下界取裁后 region 底，归一化坐标不越界）。
        qtype, opt_rows = question_type_of(q["regions"], pages_content, cover_mcq, n)
        trim_log = []
        t, c = trim_regions(doc, q["regions"], pages_content, content_cache, trim_log,
                            allow_cap=(qtype != "mcq"))
        n_trim += t
        n_cap += c
        if args.probe:
            for kind_d, idx, is_last, before, ly, kk, txt, nav in trim_log:
                print(f"        裁剪 [{kind_d}] region#{idx}{' (末段)' if is_last else ''} "
                      f"p{q['regions'][idx]['page']} {before:.0f}→{q['regions'][idx]['bbox'][3]:.0f} "
                      f"末项={kk} nav={nav} {txt!r}")

        if len(pages) > MAX_REGION_PAGES:
            notes.append(f"题区间跨 {len(pages)} 页（超 {MAX_REGION_PAGES} 页阈值），"
                         f"疑漏检题号徽章，请复核")
            q["status"] = "review"
            note_conf -= 0.2

        sec = section_of(st["page"], st["y0"])
        if sec:
            q["section"] = sec

        # ── 分值：题区间内行内 [N mark(s)] 求和 ──
        marks = 0
        for rg in q["regions"]:
            bb = rg["bbox"]
            for ln in pages_content[rg["page"]]:
                if bb[1] - 1 <= ln["bbox"][1] <= bb[3] + 1:
                    marks += sum(int(mm.group(1)) for mm in MARK_RE.finditer(ln["raw"]))
        if marks > 0:
            q["total_marks"] = marks
            note_conf += 0.3
        else:
            notes.append("题区间内未取到任何 [N marks] 分值标记，请复核")
            q["status"] = "review"

        # ── 小问：合并式徽章（`0 1 . 4`）+ 独立 ". N" 行 ──
        # ⚠️ 同一题的徽章会随每个小问**重复出现**（2025/2026 卷尤其明显），
        # 题号序列扫描只保留首次出现 → 后续重复徽章里的小问号必须在这里捞回来，
        # 否则小问会残缺（2025-06 PH02 Q7 只捞到 .3、丢了 .4）。
        subqs = []
        for pi, y, num, sub, ln in badges_all:
            if num != n or sub is None or pi in filler_pages:
                continue
            subqs.append({"label": str(sub), "page": pi,
                          "bbox": [r2(v) for v in ln["bbox"]]})
        for s in subs:
            if (s["page"], s["y0"]) < (st["page"], st["y0"]):
                continue
            if (s["page"], s["y0"]) > (end_page, end_y1):
                continue
            if s["page"] in filler_pages:
                continue
            subqs.append({"label": str(s["n"]), "page": s["page"],
                          "bbox": [r2(v) for v in s["bbox"]]})
        # 去重（同一题内 label 唯一，取首次出现）
        uniq, seen = [], set()
        for s in subqs:
            if s["label"] in seen:
                continue
            seen.add(s["label"])
            uniq.append(s)
        q["subquestions"] = sorted(uniq, key=lambda s: (s["page"], s["bbox"][1]))
        if q["subquestions"]:
            note_conf += 0.1

        # ── 题型（封面表定调 + 按题启发式交叉校验） + MCQ 选项热区 ──
        q["question_type"] = qtype
        if qtype == "mcq":
            rows_used = opt_rows
            if not rows_used:
                rows_used = fallback_option_rows(q["regions"], content_cache, doc)
                if rows_used:
                    notes.append("该选择题的四个选项是整块图形（文本层无 A–D 标签），"
                                 "热区按图像高度四等分给出，请人工核对")
            if extract_option_hotzones(q, rows_used, pages_content, B, R):
                note_conf += 0.3
                if rows_used is not opt_rows and rows_used:
                    q["status"] = "review"
            else:
                notes.append(f"MCQ 选项不全（提取到 {sorted(o['label'] for o in q['option_hotzones'])}），"
                             f"请人工补热区")
                q["status"] = "review"
            if q["total_marks"] != 1 and marks:
                notes.append(f"选择题分值异常（{q['total_marks']}，通常为 1）")
        elif opt_rows:
            notes.append("封面表判为结构化题，但题段内检出选项行，请复核题型")
        else:
            if q["subquestions"]:
                note_conf += 0.1

        conf = round(min(max(0.4 + note_conf, 0.0), 1.0), 2)
        q["confidence"] = conf
        if conf < 0.6 and q["status"] == "auto":
            q["status"] = "review"
        auto_questions.append(q)

    # ── 全卷不变量校验（72/72 成立，任一条不过即整卷降级 review） ──
    marks_sum = sum(q.get("total_marks", 0) for q in auto_questions)
    expect = paper_total_marks if paper_total_marks is not None else PAPER_TOTAL[paper_code_l]
    invariant_msgs = []
    if marks_sum != expect:
        invariant_msgs.append(f"全卷分值合计 {marks_sum} ≠ 试卷总分 {expect}")
        culprits = [q for q in auto_questions if q.get("total_marks", 0) <= 0]
        for q in (culprits or auto_questions):
            q["status"] = "review"
            q["review_notes"].append(invariant_msgs[-1])
    nums = [int(q["number"]) for q in auto_questions]
    if nums != list(range(1, len(nums) + 1)):
        invariant_msgs.append(f"题号序列不连续: {nums}")
    got_endsq = sum(1 for e in ends if e["kind"] == "ENDQ")
    if got_endsq != 1:
        invariant_msgs.append(f"END OF QUESTIONS 出现 {got_endsq} 次（预期 1 次）")
    if invariant_msgs:
        for q in auto_questions:
            q["status"] = "review"
            for msg in invariant_msgs:
                if msg not in q["review_notes"]:
                    q["review_notes"].append(msg)

    if args.probe:
        print(f"PROBE {sdir.name}: {n_questions} 题 | 分={marks_sum}(期望 {expect}) "
              f"| Sections={[s['letter'] + '@p' + str(s['page']) for s in sections]}")
        print(f"      END 标记: {[(e['kind'], e.get('letter', ''), e['page']) for e in ends]}")
        print(f"      插入页: {sorted(filler_pages)}  裁剪段={n_trim}(封顶 {n_cap})")
        for q in auto_questions:
            print(f"      Q{q['number']:>2} {q['question_type']:<9} sec={q.get('section', '-'):<2} "
                  f"分={q.get('total_marks', '?'):<3} 页={[rg['page'] for rg in q['regions']]} "
                  f"小问={[s['label'] for s in q['subquestions']]} "
                  f"{('[' + '; '.join(q['review_notes']) + ']') if q['review_notes'] else ''}")
        return

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
        "source": {"file": "source.pdf", "sha256": sha, "pages": n_pages,
                   "paper_ref": f"{year}-{session}-{paper_code_l}"},
        "page_size": {"w": r2(doc[0].rect.width), "h": r2(doc[0].rect.height)},
        "render": {"dpi": 200, "format": "webp", "quality": 85},
        "questions": [{k: v for k, v in q.items() if not k.startswith("_")} for q in questions],
    }
    cut_path.write_text(json.dumps(cut, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")

    # ── 每题产物 qNN.md ──
    (sdir / "assets").mkdir(exist_ok=True)
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
    print(f"OK  {sdir.name}: {n_questions} 题 | 分值 {marks_sum}/{expect} | "
          f"版心 L={L:.1f} R={R:.1f} T={T:.0f} B={B:.0f}")
    if n_trim:
        print(f"    页尾裁剪 {n_trim} 段（其中封顶 {n_cap} 段）")
    for line in merge_report:
        print("   ", line)
    by_status = Counter(q["status"] for q in questions)
    by_type = Counter(q.get("question_type", "?") for q in questions)
    print("    status:", " / ".join(f"{k}×{v}" for k, v in sorted(by_status.items())),
          "| 题型:", " / ".join(f"{k}×{v}" for k, v in sorted(by_type.items())))
    for q in questions:
        if q["review_notes"]:
            print(f"    [review] Q{q['number']}: {'; '.join(q['review_notes'])}")


# ── 文本/产物（与 cut_paper.py / cut_paper_cie.py 同构） ─────────────────────

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


def question_id(board, qualification, subject, year, session, paper_code, n):
    # ⚠️ id 必须全小写（question.schema.json 的 pattern 只允许 [a-z0-9-]）
    return f"{board}-{qualification}-{subject}-{year}-{session}-{paper_code.lower()}-q{n:02d}"


def write_question_md(sdir, n, q, board, qualification, subject, year, session,
                      paper_code, pages_content, pages_pairs):
    qid = question_id(board, qualification, subject, year, session, paper_code, n)
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
    lines.append("source: " + yaml_scalar(
        f"Oxford AQA International A-Level {SUBJECT_LABEL.get(subject, subject.title())} "
        f"{paper_code.upper()}, {year} {session}"))
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
    """重跑保护：verified/manual 的题其 regions/anchors 不被自动结果覆盖。"""
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
                for k in ("confidence", "total_marks", "question_type", "subquestions", "section"):
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
