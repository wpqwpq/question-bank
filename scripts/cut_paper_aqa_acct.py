#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""cut_paper_aqa_acct.py — 牛津 AQA 国际 A-Level **会计**（9615，AC01–AC04）按题切割

⚠️ **独立脚本，不与 cut_paper_aqa_chem.py（化学）/ cut_paper_aqa.py（物理）共享代码**。
   项目规矩：每个考试局 × 资格 × 学科各一份，改动互不牵连。产出与
   cut_paper.py / cut_paper_cie.py / cut_paper_aqa_chem.py **完全同构**
   （cut.json + qNN.md，符合 schema/cut.schema.json）。

2026-10-21 用 6 卷（2025-june AC01/AC02、2026-jan AC01/AC02、2026-june AC03/AC04）实测：

    · **无独立答题册**：题面与作答区同页（Answer Book 式）→ 不产生
      answer_block / answer_regions / material_regions。
    · **无 `(Total for Question N = X marks)`**，也没有分节 Total → 题界只能靠
      「下一题徽章首次出现」。
    · 题号徽章 `0 1` 是**两个独立粗体 span**（x0 52.4 / 68.9，size 11）；
      小问号 `. 1` 与徽章**同一行同 y**，x0 ≈ 82.8。
    · Section A = MCQ（A–D 选项 + 涂圈），B/C = 结构化；Section 头
      （`Section B` + `Answer all questions in this section.`）必须归**下一节第一题**。
    · 卷尾：`DO NOT WRITE ON THIS PAGE / ANSWER IN THE SPACES PROVIDED` 插入页 +
      `Additional page, if required` 横线页（横线是图片），整页跳过。

**本脚本的核心增量：剥答题区（2026-10-21 王培琪定「不保留答题区，小问直接拼接」）**

    会计卷的作答区里印着**答题骨架**（试算表的科目名、固定资产明细表的
    `At 1 April 2019`/`Depreciation`/`Net book value`），那些是题面，不能删。
    所以口径是「**只删没有字的行带**」：

      ① 横线夹层：相邻两条横线之间没有任何文字行中心 → 整带删（含线）。
         相邻删除带必须合并——否则 21 条作答横线会压成一条黑条。
      ② 纯空白：无文字、无横线、无图片的纵向区间 ≥ BLANK_MIN → 压到 KEEP_BLANK。
      ③ 保护：文字行 / 图片 / 题号徽章 / 分值行 / Section 头 一律不删。
    删除后同页拆成多段 region；渲染层原生支持多段拼接（Edexcel 会计点线压缩在用），
    **render_questions.py 不用改**。

    实测：2025-june AC01 整卷 16 757pt → 7 614pt（45%），最长单题 1 161pt。

坐标约定（全仓库统一）: PDF 点 pt，页面左上角原点，y 向下；bbox=[x0,y0,x1,y1]。

用法:
    python scripts/cut_paper_aqa_acct.py <session_dir>
    python scripts/cut_paper_aqa_acct.py <session_dir> --analyze   # 只打印锚点/压缩表，不落盘
    python scripts/cut_paper_aqa_acct.py <session_dir> --gap 12    # 删除带留 12pt 呼吸位
    python scripts/cut_paper_aqa_acct.py <session_dir> --no-compress  # 关掉剥离，只看原始切段

⚠️ 只服务 `data/aqa/a-level/accounting/<year>-<session>-ac0N/`。
重跑保护: source.sha256 不符硬停；status=verified/manual 的题其 regions 不被覆盖。
"""

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

import pymupdf

# ── 版心（2026-10-21 六卷实测：页框竖线 39.2–39.7 / 538.6–539.0） ────────────
HEADER_Y = 50.0          # 页眉安全线（页码 y≈27.2）
FOOTER_Y = 766.0         # 页脚安全线（Turn over ► y≈770、*NN* y≈789）
L_DEF, R_DEF = 40.5, 537.5
T_DEF, B_DEF = 55.0, 763.0

BADGE_X_MIN, BADGE_X_MAX = 45.0, 62.0    # 徽章首位数字列（实测 52.4）
BADGE2_X_MIN, BADGE2_X_MAX = 60.0, 80.0  # 徽章第二位数字列（实测 68.9）
BADGE_SPLIT_DY = 4.0
SUBQ_X_MIN, SUBQ_X_MAX = 78.0, 106.0     # 小问号 ". N"（实测 82.8）

# ── 剥离参数 ────────────────────────────────────────────────────────────────
MIN_HL_W = 60.0          # 只有 ≥60pt 宽的横线才算「表格/作答横线」（挡掉分数线）
MIN_VL_H = 8.0           # 竖线最短高度（表格列分隔线）
MIN_HL_GAP = 4.0         # 两条横线间距 < 4pt → 重线，不成带
MAX_HL_GAP = 80.0        # 间距 > 80pt → 交给空白规则，不当夹层
BLANK_MIN = 40.0         # 纯空白带 ≥40pt 才压
KEEP_BLANK = 12.0        # 压到 12pt
KEEP_EDGE = 4.0          # 删除带边界让出 4pt：**必须比线宽大**，否则段边界贴着横线，
                         # 渲染出半条抗锯齿的浅灰线（看着像多了一条「拼接线」，2026-10-21 培琪指出）
ORPHAN_PAD = 24.0        # 孤立作答横线：向下连带删 24pt
ORPHAN_NEAR = 15.0       # 孤立横线判定：上下 15pt 内无文字
MIN_SEG = 6.0            # 短于此的碎段丢弃

BADGE_PAIR_RE = re.compile(r"^(\d)$")
BADGE_FULL_RE = re.compile(r"^(\d)\s(\d)(?:\s*\.\s*(\d{1,2}))?$")
SUBQ_RE = re.compile(r"^\.\s*(\d{1,2})$")
MARK_RE = re.compile(r"\[\s*(\d{1,2})\s*marks?\s*\]")
SEC_RE = re.compile(r"^Section\s+([A-C])$")
SEC_SUB_RE = re.compile(r"^Answer all questions in this section", re.I)
ENDQ_RE = re.compile(r"END\s+OF\s+QUESTIONS", re.I)
NAV_RE = re.compile(r"continues? on the next page|Turn over for the next", re.I)
DNW_RE = re.compile(r"DO\s+NOT\s+WRITE\s+ON\s+THIS\s+PAGE", re.I)
NOQ_RE = re.compile(r"no questions printed on this page", re.I)
ADDPG_RE = re.compile(r"Additional page, if required", re.I)
EXTRA_RE = re.compile(r"^Extra space$", re.I)
JUNK_RE = re.compile(r"^[\s,.\-*\u0000-\u001f]*$")
FOOTER_LINE_RE = re.compile(r"^(\*[^*]*\*|IB/[A-Za-z0-9/_]*|Turn over\s*►?)\s*$")
RIGHTBOX_RE = re.compile(r"^(Do not write|outside the|box)$", re.I)


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


def visible_bbox(ln):
    """只用含可见字符的 span 求并集（右对齐的 [N marks] 前面常带空 span）。"""
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


def content_lines(lines):
    """版心内容行：滤掉页眉页码 / 页脚条码 / 右上角「Do not write outside the box」。"""
    out = []
    for ln in lines:
        b = ln["bbox"]
        if b[1] < HEADER_Y or b[1] > FOOTER_Y:
            continue
        raw = ln["raw"].strip()
        if not raw or JUNK_RE.fullmatch(raw):
            continue
        if FOOTER_LINE_RE.match(raw):
            continue
        # 版心右外侧：右上角竖排「Do not write outside the box」(x≈546) 与
        # 阅卷角标数字（`23` / `12` / `55`，x≈556）。角标是给阅卷人看的，
        # 留在题区间里会撑住一整段，让「纯横线尾段」不被丢弃，最终渲染出一截
        # 灰线残段（2026-10-21 图 2 的多余线就是这么来的）。
        if b[0] > 542:
            continue
        out.append(ln)
    return out


def page_flags(page):
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


def option_label_of(ln):
    """行首 span 粗体且以单个 A–D 字母起头 → 'A'–'D'。题干里的 'A ' 是非粗体。"""
    t, bold, x0 = ln["first"]
    t = t.strip()
    if not bold or not t or t[0] not in "ABCD":
        return None
    if len(t) > 1 and t[1] not in " .":
        return None
    if not (40.0 <= x0 <= R_DEF):
        return None
    return t[0]


# ── 徽章 ────────────────────────────────────────────────────────────────────

def find_badges(lines):
    """题号徽章 → [{n, sub, page?, y0, x1}]。

    两种印刷形态都要认：
      · 单个 span `0 1`（旧卷/部分页）；
      · 拆成两个粗体 span `0`（x≈52.4）+ `1`（x≈68.9），同 y 差 <4pt。
    """
    seq, i = [], 0
    while i < len(lines):
        ln = lines[i]
        b = ln["bbox"]
        t = ln["raw"].strip()
        m = BADGE_FULL_RE.match(t)
        if m and ln["bold"] and BADGE_X_MIN <= b[0] <= BADGE_X_MAX:
            seq.append({"n": int(m.group(1) + m.group(2)), "sub": bool(m.group(3)),
                        "y0": b[1], "y1": b[3], "x0": b[0], "x1": b[2], "line": ln})
            i += 1
            continue
        if (ln["bold"] and BADGE_PAIR_RE.match(t) and BADGE_X_MIN <= b[0] <= BADGE_X_MAX):
            nx = lines[i + 1] if i + 1 < len(lines) else None
            if nx is not None:
                nb = nx["bbox"]
                if (nx["bold"] and BADGE_PAIR_RE.match(nx["raw"].strip())
                        and abs(nb[1] - b[1]) < BADGE_SPLIT_DY
                        and BADGE2_X_MIN <= nb[0] <= BADGE2_X_MAX):
                    seq.append({"n": int(t + nx["raw"].strip()), "sub": False,
                                "y0": b[1], "y1": max(b[3], nb[3]), "x0": b[0],
                                "x1": nb[2], "line": ln})
                    i += 2
                    continue
        i += 1
    return seq


def find_subq_marks(lines):
    """小问号 `. N`（与徽章同行同 y，x0≈82.8）→ [{n, y0, y1, x0, x1}]"""
    out = []
    for ln in lines:
        b = ln["bbox"]
        if not (SUBQ_X_MIN <= b[0] <= SUBQ_X_MAX):
            continue
        m = SUBQ_RE.match(ln["raw"].strip())
        if m:
            out.append({"n": int(m.group(1)), "y0": b[1], "y1": b[3],
                        "x0": b[0], "x1": b[2]})
    return out


# ── 图形 / 横线 / 图片 ──────────────────────────────────────────────────────

def is_frame_rect(r):
    """页框线 / 角点残段（不算内容、也不当作答横线）。"""
    w, h = r.width, r.height
    if w <= 3 and h <= 3:
        return True
    if w > 450 and h > 600:
        return True
    if h <= 3 and w > 450 and (r.y0 < 57 or r.y1 > 763):
        return True
    if w <= 3 and h > 400 and (r.x0 < 42 or r.x1 > 537):
        return True
    if r.x0 >= R_DEF - 1.0 and r.width < 120:      # 右上角 Do not write 方框
        return True
    if r.x1 <= L_DEF + 1.0 and r.width < 120:
        return True
    return False


def page_geometry(doc, pi, cache):
    """一页的横线、竖线、图片：
    {"hlines": [(y0,y1,x0,x1)], "vlines": [(x,y0,y1)], "images": [(y0,y1)]}
    """
    if pi in cache:
        return cache[pi]
    hs, vs, imgs = [], [], []
    try:
        for dr in doc[pi].get_drawings():
            r = dr["rect"]
            if r.width <= 0 and r.height <= 0:
                continue
            if is_frame_rect(r):
                continue
            if r.width >= MIN_HL_W and r.height < 2.0 \
                    and L_DEF - 2 <= r.x0 and r.x1 <= R_DEF + 2:
                hs.append((r2(r.y0), r2(r.y1), r2(r.x0), r2(r.x1)))
            elif r.height >= MIN_VL_H and r.width < 2.0 \
                    and L_DEF - 2 <= r.x0 <= R_DEF + 2:
                vs.append((r2((r.x0 + r.x1) / 2), r2(r.y0), r2(r.y1)))
    except Exception:
        pass
    for b in doc[pi].get_text("dict")["blocks"]:
        if b.get("type") == 1:
            bb = b["bbox"]
            if bb[2] - bb[0] > 25 and bb[3] - bb[1] > 25:
                imgs.append((bb[1], bb[3]))
    hs.sort()
    vs.sort()
    cache[pi] = {"hlines": hs, "vlines": vs, "images": imgs}
    return cache[pi]


def table_spans(geo):
    """表格的纵向区间 [(y0, y1)]，按「横线被竖线穿过」判定。

    ⚠️ 这是本脚本判「表格 / 纯作答横线」的唯一依据：**有竖线围格 = 表格**。
    会计卷的答题表格（试算表、账户表、财务报表）必须整块保留——空格子要给学生填，
    表头列名（Date / Details / $ / Dr / Cr）是格式的一部分，删了题就做不了
    （2026-10-21 培琪指出：图 1/3/4 的表只剩表头是错的）。
    纯作答横线（银行对账单那种一条条长线）没有竖线，照删。
    """
    if not geo["vlines"]:
        return []
    tbl = []
    for h in geo["hlines"]:
        y0, _y1, x0, x1 = h
        for vx, vy0, vy1 in geo["vlines"]:
            if x0 - 1.5 <= vx <= x1 + 1.5 and vy0 - 1.5 <= y0 <= vy1 + 1.5:
                tbl.append((y0, x0, x1))
                break
    if not tbl:
        return []
    tbl.sort()
    spans = [[tbl[0][0], tbl[0][0]]]
    for y, _x0, _x1 in tbl[1:]:
        if y - spans[-1][1] <= 120:      # 同一张表内相邻行
            spans[-1][1] = y
        else:
            spans.append([y, y])
    return [(a, b) for a, b in spans if b - a > 2]


def in_spans(y, spans):
    return any(a - 1.5 <= y <= b + 1.5 for a, b in spans)


# ── 剥离：把「无字的行带」从区间里挖掉 ──────────────────────────────────────

def _clean_ranges(iv):
    """合并重叠区间。"""
    iv = sorted(iv)
    out = []
    for a, b in iv:
        if out and a <= out[-1][1] + 0.5:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return out


def find_cuts(ls, geo, y0, y1, extra_cuts=(), gap=0.0):
    """返回该区间的删除带 [(a, b)]（已合并、已让边）。

    ls: 该页内容行（已滤页眉页脚）
    extra_cuts: 额外要挖掉的行（导航行），格式 [(row_top, row_bottom)]

    三条规则：
      ① 横线夹层（不含表格线）
      ② 纯空白（表格区间视为「有内容」，不压）
      ③ 孤立作答横线（单条、上下无字）
    """
    tbl = table_spans(geo)
    hs_all = [(a, b, x0, x1) for a, b, x0, x1 in geo["hlines"] if y0 - 1 <= a <= y1 + 1]
    # 表格线：落在表格纵向区间内的横线 —— 一律不动
    hs = [(a, b, x0, x1) for a, b, x0, x1 in hs_all if not in_spans(a, tbl)]
    hs_tbl = [(a, b, x0, x1) for a, b, x0, x1 in hs_all if in_spans(a, tbl)]
    imgs = geo["images"]
    cuts = []

    def has_text(ya, yb, pad=1.0):
        return any(ya + pad < (l["bbox"][1] + l["bbox"][3]) / 2 < yb - pad for l in ls)

    def near_text(ya, yb):
        """行区间与 [ya,yb] 有任何重叠就算「附近有字」。

        ⚠️ 规则③ 必须用这个而不是中心判定：表格表头常在上边框线下方 8pt
        就开始（`Account(s) to be debited` 顶沿距线 7.9pt），中心判定会漏掉，
        把表头连线上一起当「孤立作答横线」删掉（2026-10-21 图 3/4 的表格只剩
        表头就是这么来的）。
        """
        return any(l["bbox"][3] > ya and l["bbox"][1] < yb for l in ls)

    # ① 非表格横线之间的空带
    raw = []
    raw_ranges = []
    for (a0, a1, _x0, _x1), (b0, b1, _u0, _u1) in zip(hs, hs[1:]):
        if b0 - a1 < MIN_HL_GAP or b0 - a0 > MAX_HL_GAP:
            continue
        if has_text(a1, b0):
            continue
        if any(not (i1 <= a1 or i0 >= b0) for i0, i1 in imgs):
            continue
        raw.append([a1, b0])
        raw_ranges.append((a0, b0))
    for a, b in _clean_ranges(raw):
        if b - a > 3.0:
            cuts.append((a + KEEP_EDGE, b - KEEP_EDGE))
    # 每组作答横线的**第一条线**也要连根删：否则它留在段尾，渲染出半条灰线
    # （2026-10-21 图 2/图 5 的「拼接线」就是这么来的）。
    # ⚠️ 上下 14pt 内有字就不动它——表头文字常紧贴表格第一根线，硬删会切到字。
    for a0, b0 in raw_ranges:
        if near_text(a0 - 14, a0 + 4):
            continue
        cuts.append((max(y0, a0 - 3), a0 + 1))

    # ③ 孤立作答横线：上下近旁都没字 → 整条连下面的空白一起删
    for a0, a1, _x0, _x1 in hs:
        if any(a - 2 <= a0 <= b + 2 for a, b in raw):
            continue
        if near_text(a0 - ORPHAN_NEAR, a0 + ORPHAN_NEAR + 4):
            continue
        cuts.append((a0 - KEEP_EDGE, min(a1 + ORPHAN_PAD, y1)))

    # ② 纯空白带（文字 / 横线 / 表格 / 图片 / 额外行都算「有内容」）
    occ = [(l["bbox"][1], l["bbox"][3]) for l in ls if y1 > l["bbox"][1] > y0 - 5]
    occ += [(a, b) for a, b, _x0, _x1 in hs_all if y1 > a > y0 - 5]
    occ += list(tbl)
    occ += [(a, b) for a, b in imgs if y1 > a > y0 - 5]
    occ += [(a, b) for a, b in extra_cuts]
    cur = y0
    for a, b in _clean_ranges(occ):
        if a - cur >= BLANK_MIN:
            # 起点让 4pt：段尾贴着前一项（表格底边、最后一行字）只有 0.5pt 时，
            # 200dpi 渲染后那一像素正好落在边缘，底线看着像被削掉（2026-10-21 图 3 底部）
            cuts.append((cur + 4, a - KEEP_BLANK + gap))
        cur = max(cur, b)
    # 尾部空白
    if y1 - cur >= BLANK_MIN:
        cuts.append((cur + 4, y1 - KEEP_BLANK + gap))

    # 额外挖掉的行（导航行）
    for a, b in extra_cuts:
        cuts.append((a - KEEP_EDGE, b + KEEP_EDGE))

    merged = _clean_ranges([[max(y0, a), min(y1, b)] for a, b in cuts if b > a])
    return [(a, b) for a, b in merged if b - a > 2.0]


def subtract(y0, y1, cuts):
    """区间减删除带 → 保留段 [(a, b)]"""
    segs, cur = [], y0
    for a, b in cuts:
        if a - cur > MIN_SEG:
            segs.append((r2(cur), r2(a)))
        cur = max(cur, b)
    if y1 - cur > MIN_SEG:
        segs.append((r2(cur), r2(y1)))
    return segs


# ── region 组装 ─────────────────────────────────────────────────────────────

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
    for pi in sorted({rg["page"] for rg in q["regions"]}):
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
        # 重切后正文也要跟着换：否则去掉 Section 头、压缩掉作答区之后，md 里还是
        # 上一版抓到的旧文本（2026-10-21 踩到：图里 Section 头没了，md 里还在）。
        # 只对 auto 题做——verified / manual 的正文是人工看过的，一律不动。
        if q.get("status") == "auto":
            _sync_body(path, body)
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
        f"Oxford AQA International A-Level Accounting {paper_code.upper()}, {year} {session}"))
    lines.append("cut_status: " + q["status"])
    lines.append("language: en")
    lines.append("---")
    lines.append("")
    lines.append(body)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _sync_body(path, body):
    """把已存在 qNN.md 的正文换成最新自动结果，front-matter 原样保留。"""
    text = path.read_text(encoding="utf-8")
    m = re.match(r"^(---\n.*?\n---\n)(.*)$", text, re.S)
    if not m:
        return
    path.write_text(m.group(1) + "\n" + body + "\n", encoding="utf-8")


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
        blocks[k] = [f"{k}: {yaml_scalar(v)}"]
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


# ── 主流程 ──────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Oxford AQA 9615 会计卷按题切割（剥答题区）")
    ap.add_argument("session_dir")
    ap.add_argument("--analyze", action="store_true", help="只打印锚点/压缩表，不落盘")
    ap.add_argument("--gap", type=float, default=0.0, help="删除带保留的呼吸间距 pt（默认 0）")
    ap.add_argument("--no-compress", action="store_true", help="关闭答题区剥离")
    args = ap.parse_args()

    sdir = Path(args.session_dir).resolve()
    src = sdir / "source.pdf"
    if not src.is_file():
        sys.exit(f"缺少 source.pdf: {sdir}")

    doc = pymupdf.open(src)
    pages_lines = [build_page_lines(pg) for pg in doc]
    pages_content = [content_lines(ls) for ls in pages_lines]
    pages_pairs = [assign_words_to_lines(pages_content[i], doc[i].get_text("words"))
                   for i in range(doc.page_count)]
    geo_cache, flags = {}, []
    for pi, pg in enumerate(doc):
        flags.append(page_flags(pg))

    meta = read_paper_meta(sdir)
    paper_code = (meta.get("paper_code") or sdir.name.split("-")[-1]).lower()
    year = int(meta.get("year") or re.search(r"(\d{4})", sdir.name).group(1))
    session = meta.get("session") or sdir.name.split("-")[1]
    paper_total = int(meta["total_marks"]) if meta.get("total_marks", "").isdigit() else None

    # ── 题号序列（含跨页重印）──
    all_badges = []
    for pi, ls in enumerate(pages_content):
        for b in find_badges(ls):
            b["page"] = pi
            all_badges.append(b)
    all_badges.sort(key=lambda b: (b["page"], b["y0"]))
    if not all_badges:
        sys.exit("未识别出任何题号徽章")

    qnums, seen = [], set()
    for b in all_badges:
        if b["n"] in seen:
            continue
        seen.add(b["n"])
        qnums.append(b)
    nums = sorted({b["n"] for b in all_badges})
    if nums != list(range(1, len(nums) + 1)):
        print(f"  [warn] 题号不连续: {nums}")

    # ── 卷尾硬界：优先 `END OF QUESTIONS`，退回「最后一个内容页的底」 ──
    # ⚠️ 不能用「第一个 DNW 页」当卷尾：这科的 DNW 页既有卷尾的，也有**插在题中间**
    #    的（2025-june AC01 p7 就夹在 Q11 里），用它截断会把最后一题区间算成负数。
    #    题区间内的 DNW 页在走页时整页跳过即可。
    end_page, end_y = None, None
    for pi, ls in enumerate(pages_content):
        hit = [l for l in ls if ENDQ_RE.search(l["raw"])]
        if not hit:
            continue
        # 该页除 `END OF QUESTIONS` 与阅卷角标数字外还有正文？有则截到该行上方，
        # 没有则整页不算（卷尾声明页不承载题目）。
        others = [l for l in ls
                  if not ENDQ_RE.search(l["raw"]) and not re.fullmatch(r"\d{1,3}", l["raw"].strip())]
        if others:
            end_page, end_y = pi, r2(hit[0]["bbox"][1] - 4)
        else:
            end_page, end_y = pi - 1, B_DEF
        break
    if end_page is None:
        for pi in range(doc.page_count - 1, -1, -1):
            if flags[pi] & {"addpg", "blank", "dnw"}:
                continue
            end_page, end_y = pi, B_DEF
            break
    if end_page is None:
        sys.exit("未找到卷尾边界")

    # ── Section 头（归下一节第一题）──
    secs = []
    for pi, ls in enumerate(pages_content):
        for i, ln in enumerate(ls):
            m = SEC_RE.match(ln["raw"].strip())
            if not m:
                continue
            top = ln["bbox"][1]
            # 连带下面的 "Answer all questions in this section."
            for nx in ls[i + 1:i + 4]:
                if SEC_SUB_RE.match(nx["raw"].strip()):
                    top = min(top, ln["bbox"][1])
            secs.append({"letter": m.group(1), "page": pi, "y0": r2(top - 4)})
    secs.sort(key=lambda s: (s["page"], s["y0"]))

    def section_of(page, y0):
        cur = None
        for s in secs:
            if (s["page"], s["y0"]) <= (page, y0 + 1):
                cur = s
            else:
                break
        return cur

    # ── 每题区间 ──
    questions, notes = [], []
    tot_raw = tot_new = 0.0
    for i, b in enumerate(qnums):
        n = b["n"]
        sp, sy = b["page"], r2(b["y0"] - 2)
        sec = section_of(sp, sy)
        # ⚠️ Section 头（`Section B` + `Answer all questions in this section.` + 下面那条线）
        #    **不进题图**（2026-10-21 培琪定）。它只用来给 `section` 字段打标，
        #    题区间一律从题号徽章起算；Section 头及其分隔线落在上一题的终点之外，
        #    由「终点收到 Section 头之前」挖掉。

        if i + 1 < len(qnums):
            nb = qnums[i + 1]
            ep, ey = nb["page"], r2(nb["y0"] - 2)
            nsec = section_of(nb["page"], ey)
            if nsec and nsec["letter"] != (sec["letter"] if sec else None) \
                    and (nsec["page"], nsec["y0"]) < (ep, ey):
                d = (ep - nsec["page"]) * 700 + (ey - nsec["y0"])
                if 0 <= d < 140:      # 下一题的 Section 头不能本吞
                    ep, ey = nsec["page"], nsec["y0"]
        else:
            ep, ey = end_page, end_y

        if (ep, ey) <= (sp, sy):
            notes.append(f"Q{n}: 区间非法 p{sp+1}/{sy} → p{ep+1}/{ey}")
            continue

        # 待挖掉的行：只挖导航行。
        # ⚠️ **不要挖跨页重印的题号徽章**：`. N` 小问号与重印徽章同 y、同处一行
        #    （如 p11 的 `1 3` x=52.4 与 `. 1` x=82.8），按行挖会把小问号连同题面
        #    一起切掉（2026-10-21 实测踩到：Q13 四个小问全丢）。原卷重印就让它重印。
        extra_by_page = {}
        for pi in range(sp, ep + 1):
            for ln in pages_content[pi]:
                if NAV_RE.search(ln["raw"]):
                    extra_by_page.setdefault(pi, []).append((ln["bbox"][1], ln["bbox"][3]))

        regions, anchors = [], {}
        # 题区间内整页跳过：插入页 / 附加页 / **纯答题续页**。
        # 9615 的长答题后面常跟「只剩横线的空页」或只有一行 `Extra space` 的续答页
        # （2025-june AC01 的 p23/p24）—— 它们是作答空间，剥答题区时就该整页丢，
        # 否则压完还会剩一小截横线在题尾（2026-10-21 视觉抽检抓到）。
        pages_of_q = []
        for pi in range(sp, ep + 1):
            if flags[pi] & {"dnw", "addpg", "blank"}:
                continue
            if pi != sp:
                content = pages_content[pi]
                # 整页跳过：完全无字的纯横线页 / 只有一行 `Extra space` 的续答页。
                # ⚠️ 不能写成「内容行 ≤1」——`Workings` 单独占一页顶部的续页很常见，
                #    那行是作答提示，要留（2026-10-21 ac02 Q12 被误跳过）。
                if not content:
                    continue
                if all(EXTRA_RE.match(l["raw"].strip()) for l in content):
                    continue
            pages_of_q.append(pi)
        for pi in pages_of_q:
            y0 = sy if pi == sp else T_DEF
            y1 = ey if pi == ep else B_DEF
            if y1 - y0 < 5:
                continue
            geo = page_geometry(doc, pi, geo_cache)
            if args.no_compress:
                segs = [(r2(y0), r2(y1))]
                cuts = []
            else:
                cuts = find_cuts(pages_content[pi], geo, y0, y1,
                                 extra_cuts=extra_by_page.get(pi, []), gap=args.gap)
                segs = subtract(y0, y1, cuts)
            for a, b2 in segs:
                # 无文字的碎段一律丢（页底框线残段、只剩一条线的尾巴）；
                # 但**表格段例外**——纯空表格本身无文字，却是要保留的作答模板。
                if not any(a <= (ln["bbox"][1] + ln["bbox"][3]) / 2 <= b2
                           for ln in pages_content[pi]):
                    if not any(a <= t0 and t1 <= b2 + 1
                               for t0, t1 in table_spans(geo)):
                        continue
                regions.append({"page": pi, "bbox": [L_DEF, a, R_DEF, b2]})
            tot_raw += y1 - y0
            tot_new += sum(b2 - a for a, b2 in segs)

        if not regions:
            notes.append(f"Q{n}: 剥离后无剩余段")
            continue

        # 分值：题内所有 [N marks] 求和
        marks = []
        for rg in regions:
            pi, bb = rg["page"], rg["bbox"]
            for ln in pages_content[pi]:
                yc = (ln["bbox"][1] + ln["bbox"][3]) / 2
                if not (bb[1] <= yc <= bb[3]):
                    continue
                for mm in MARK_RE.finditer(ln["raw"]):
                    marks.append(int(mm.group(1)))
        total_marks = sum(marks) if marks else 1

        # 小问：徽章行同 y 的 ". N"
        subs = []
        for rg in regions:
            pi, bb = rg["page"], rg["bbox"]
            for sq in find_subq_marks(pages_content[pi]):
                if not (bb[1] - 1 <= sq["y0"] <= bb[3] + 1):
                    continue
                subs.append({"label": str(sq["n"]), "page": pi,
                             "bbox": [r2(sq["x0"]), r2(sq["y0"]), r2(sq["x1"]), r2(sq["y1"])]})
        uniq, sseen = [], set()
        for s in subs:
            if s["label"] in sseen:
                continue
            sseen.add(s["label"])
            uniq.append(s)

        q = {"number": str(n), "status": "auto", "confidence": 0.95,
             "total_marks": total_marks, "regions": regions,
             "anchors": {
                 "start": {"page": sp, "bbox": [L_DEF, sy, R_DEF, r2(sy + 16)]},
                 "end": {"page": ep, "bbox": [L_DEF, r2(ey - 4), R_DEF, ey]},
             },
             "subquestions": uniq}
        if sec:
            q["section"] = sec["letter"]

        # 题型 + 选项热区
        qtype, opts = question_type_of(regions, pages_content)
        q["question_type"] = qtype
        if opts:
            q["_options"] = [{"label": o["label"], "text": o["text"]} for o in opts]
            q["option_hotzones"] = [{"label": o["label"],
                                     "bbox": [r2(v) for v in o["bbox"]]} for o in opts]

        questions.append(q)
        if args.analyze:
            span = sum(r["bbox"][3] - r["bbox"][1] for r in regions)
            print(f"  Q{n:>2} sec={q.get('section','-')} {q['question_type']:<10} "
                  f"marks={total_marks:>2} regs={len(regions):>2} "
                  f"p{sp+1}-{ep+1} h={span:>6.0f} subs={len(uniq)} opts={len(opts)}")

    print(f"=== {sdir.name}  {doc.page_count} 页  题数 {len(nums)}  "
          f"高度 {tot_raw:.0f} → {tot_new:.0f}pt ({100*tot_new/max(tot_raw,1):.0f}%) ===")
    msum = sum(q["total_marks"] for q in questions)
    print(f"    分值合计 {msum}" + (f" / 卷面 {paper_total}" +
          ("  ✓" if msum == paper_total else "  ✗ 不符") if paper_total else ""))
    if notes:
        print("[提示]")
        for x in notes:
            print("  -", x)

    if args.analyze:
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

    payload = questions
    merged, report = merge_with_existing(payload, existing)
    for q in merged:
        for k in [k for k in q if k.startswith("_")]:
            q.pop(k, None)

    out = {
        "schema_version": 1,
        "source": {"file": "source.pdf", "sha256": sha, "pages": doc.page_count,
                   "paper_ref": meta.get("paper_ref") or ""},
        "page_size": {"w": r2(doc[0].rect.width), "h": r2(doc[0].rect.height)},
        "render": {"dpi": 200, "format": "webp", "quality": 85},
        "blocks": ["qp"],
        "questions": merged,
    }
    cut_path.write_text(json.dumps(out, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")

    for q in merged:
        write_question_md(sdir, int(q["number"]), q, "aqa", "a-level", "accounting",
                          year, session, paper_code, pages_content, pages_pairs)

    print(f"\ncut.json 已写入（{len(merged)} 题）")
    for line in report:
        print("  ·", line)


def question_type_of(regions, pages_content):
    """判题型 + 选项行。Section A 是 MCQ，但按选项聚类判（不靠 Section 名）。

    选项行：行首粗体 A–D，同列、按 y 升序恰为 A→B→C→D 前缀，≥3 个即 MCQ。
    """
    cands = []
    for rg in regions:
        bb = rg["bbox"]
        pi = rg["page"]
        for ln in pages_content[pi]:
            yc = (ln["bbox"][1] + ln["bbox"][3]) / 2
            if not (bb[1] - 1 <= yc <= bb[3] + 1):
                continue
            lab = option_label_of(ln)
            if not lab:
                continue
            cands.append({"label": lab, "page": pi, "yc": yc,
                          "x0": ln["bbox"][0], "text": ln["raw"].strip(),
                          "bbox": list(ln["bbox"])})
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
            want = "ABCD"[len(seq)]
            if o["label"] == want and abs(o["x0"] - xs[0]) <= 12:
                seq.append(o)
            elif o["label"] == "A":
                break
        if len(seq) >= 4 and len(seq) > len(best):
            best = seq
    if len(best) >= 4:
        return "mcq", best[:4]
    return "structured", []


if __name__ == "__main__":
    main()
