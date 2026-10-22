#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""cut_paper_aqa_fmath.py — 牛津 AQA 国际 A-Level **进阶数学**（9665，FM01–FM05）按题切割

⚠️ **独立脚本，不与 cut_paper_math.py（Edexcel）/ cut_paper_aqa_acct.py（会计）共享代码**。
   项目规矩：每个考试局 × 资格 × 学科各一份。产出与全仓库同构
   （cut.json + qNN.md，符合 schema/cut.schema.json）。

🔴 与 Edexcel 数学（cut_paper_math.py）**口径相反**：
   Edexcel 是「只切问题，把作答横线全剃掉」；AQA 数学（王培琪 2026-10-21 定）是
   **小问自带的作答区整块保留**——题干 → 一整片答题横线 → 下一小问 → 再一片。
   唯一要丢的是「**整页纯横线 + 末尾一个 Answer 小空**」那种页（它只是草稿空间）。

版面实测（2026-10-21，126 个有效卷 / 3,076 页，全 A4 595×842，只有一代）：
    · 题号 = Arial-BoldMT 11pt 贴左字块，x₀ 42–49（2019 版 53）；形态 `N` / `N (a)` /
      `N (a) (i)`，**有时题干跟在同一行**（`8 (a) (i) Show that …`）。
    · 正文缩进 x=99.1；分值行 `[4 marks]` 右对齐 x≈484；作答线是填充黑条
      h≈0.2pt、宽 434pt、行距 25.8pt；填空短线宽 <200pt。
    · 页脚 y>765（条码 + `IB/G/Jun24/MA01` + 页码）、右侧竖排
      `Do not write outside the box`（x≈546）、页顶页码 y≈27.5 —— 全部在版心外。
    · **图表是 image block**（不是矢量）：算「内容底」必须带图片 bbox，
      否则 `3(b) Sketch the graph` 的坐标网格会被整块切掉（第一版踩到）。
    · PSM1（MA02）有 `Section A/B/C` 标题（25 卷）→ 并入该 Section 首题。
    · 卷末：`END OF QUESTIONS` 之后固定跟 4–5 页（`There are no questions printed…` +
      `Additional page, if required` 方格纸）→ 整段丢弃；END 页本身保留，内容底截到该行上方。

切块规则：
    起点 = 题号徽章行 y−3（Section 行 y−8）；同一题内的 (a)(b)(i) 徽章**不另起一段**
    （否则一页被切三四刀、渲染出三四条缝）。
    终点 = 同页下一题起点；换页则取该页「内容底 + 6pt」——内容底 = max(正文行 y1,
    图形 y1, 图片 bbox y1, 作答线 y)。用「内容底」而不是「页底」，否则大片空白进图。
    空段（区间内没有任何内容）不产出——换页时页顶那条 5–8pt 空隙曾让段数虚高 1,103 段。

丢弃：
    ① 整页纯作答线：无题号徽章 + 无 `[N marks]` + 正文净字符 <40 + 长横线 ≥8 +
       无大图形 + 无图片 → 整页不进任何题。
    ② `END OF QUESTIONS` 之后的所有页。
    ③ 页眉页脚 / 右侧竖排提示（在版心外，裁切时自然排除）。

两道自检（126/126 全过，干切实测）：
    · 封面 `For Examiner's Use` 表格的 1…N 与正文题号序列必须完全一致；
    · 正文 `[N marks]` 累加必须等于 paper.md 的 total_marks。
  任一不符 → 该卷标 review，不静默通过。

坐标约定（全仓库统一）: PDF 点 pt，页面左上角原点，y 向下；bbox=[x0,y0,x1,y1]。

用法:
    python scripts/cut_paper_aqa_math.py <session_dir> [--dry-run] [--force]
    python scripts/cut_paper_aqa_math.py <session_dir> --audit   # 只体检，不落盘
"""

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

import pymupdf

# ── 版心常量（AQA 三科统一：页框竖线 39.2–39.7 / 538.6–539.1）────────────────
L = 40.5
R = 538.0
TOP = 54.5           # 页框上沿 53.5–54.0
BOT = 765.0          # 页框下沿 765.4；其下是条码/页码

BOARD_RE = re.compile(r"^(aqa)$")
SUBJECT_RE = re.compile(r"^further-mathematics$")

BADGE_RE = re.compile(r"^(\d{1,2})((?:\s*\([a-h]\))?(?:\s*\([ivx]+\))?)(?=[\s(]|$)")
LABEL_RE = re.compile(r"\(([a-h]|[ivx]+)\)")
MARKS_RE = re.compile(r"\[(\d+)\s*marks?\]")
SEC_RE = re.compile(r"^Section\s+([A-Z])$")
END_RE = re.compile(r"END\s+OF\s+QUESTIONS", re.I)
# 卷中插页（不是题面）："There are no questions printed on this page" /
# "DO NOT WRITE ON THIS PAGE / ANSWER IN THE SPACES PROVIDED"
FILLER_RE = re.compile(r"There are no questions printed|DO\s+NOT\s+WRITE\s+ON\s+THIS\s+PAGE", re.I)

# 页内导航/提示行：不算正文量，也不进 qNN.md 正文
NAV_RE = re.compile(
    r"^(Answer|END\s+OF\s+QUESTIONS|Turn over for the next question|Turn over|"
    r"Do not write\s*outside the box|BLANK PAGE|There are no questions printed.*|"
    r"Additional page, if required.*)$", re.I)
PAGENO_RE = re.compile(r"^\*?\d{1,3}\*?$")


def is_nav(r):
    """导航行；纯数字只在页顶/页底才算页码——题面里的分数分母 `4` 也是纯数字。"""
    if NAV_RE.match(r["t"]):
        return True
    return bool(PAGENO_RE.match(r["t"])) and (r["y0"] < 45 or r["y0"] > 755)


def r2(v):
    return round(float(v), 2)


def yaml_scalar(v):
    """够用的 YAML 标量：含特殊字符就加双引号。"""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    s = str(v)
    if s == "" or re.search(r"[:#\[\]{}&*!|>'\"%@`]|^\s|\s$|^[-?]", s):
        return '"' + s.replace('"', '\\"') + '"'
    return s


# ── 页模型 ──────────────────────────────────────────────────────────────────

def page_lines(pg):
    """行级模型：合并同一 line 的 span（题号 `8` 与 `(b) (i)` 常是两个 span）。"""
    out = []
    for b in pg.get_text("dict")["blocks"]:
        if b["type"] != 0:
            continue
        for ln in b["lines"]:
            spans = [s for s in ln["spans"] if s["text"].strip()]
            if not spans:
                continue
            bb = ln["bbox"]
            s0 = spans[0]
            # bbox 取所有 span 的并集：矩阵/分段函数的大括号是超高字形，
            # 它的 span bbox 会比 line bbox 高出一大截；只用 line bbox 会让
            # 边界从字形中间穿过去（2021-jan-ma03 p4 实测 14 处）。
            x0 = min(s["bbox"][0] for s in spans)
            y0 = min(s["bbox"][1] for s in spans)
            x1 = max(s["bbox"][2] for s in spans)
            y1 = max(s["bbox"][3] for s in spans)
            out.append({
                "t": "".join(s["text"] for s in spans).strip(),
                "x0": r2(x0), "y0": r2(y0), "x1": r2(x1), "y1": r2(y1),
                "sx0": r2(s0["bbox"][0]),
                "bold": "Bold" in s0["font"],
                "sz": round(s0["size"], 1),
            })
    out.sort(key=lambda r: (round(r["y0"], 1), r["x0"]))
    return out


def in_frame(r):
    return not (r["y0"] < TOP or r["y0"] > BOT or r["x0"] > 540)


def page_draws(pg):
    """返回 (长作答线 y 列表, 图形 (y0,y1) 列表)。装饰框/白底/页脚一律排掉。"""
    hl, shapes = [], []
    for dr in pg.get_drawings():
        rc = dr["rect"]
        w, h = rc.width, rc.height
        if rc.y0 > BOT + 2 or rc.y1 < TOP - 2:
            continue
        if rc.x0 < 30 or rc.x1 > 545:
            continue
        if w > 450 and (rc.y0 < TOP or rc.y1 > 760):      # 页框上下边
            continue
        if h > 600 and (rc.x0 < 45 or rc.x1 > 535):       # 页框左右边
            continue
        if w > 450 and h > 650:                            # 整页装饰框
            continue
        if dr.get("fill") == (1.0, 1.0, 1.0) and w > 200 and h > 100:
            continue                                       # 纯白大底
        if h <= 1.2 and w >= 200:
            hl.append(r2(rc.y0))
            continue
        if w < 1.5 and h < 1.5:
            continue
        shapes.append((r2(rc.y0), r2(rc.y1)))
    return sorted(hl), shapes


def visual_rows(lines):
    """把同一「视觉行」的多个 line 合并（题号 `1` 与 `(c)` 常是两个 line）。

    PyMuPDF 会把贴得很近但字号/基线不同的片段拆成两个 line（2020-jan-ma04 p3
    的 `1` + `(c)` 就是这样）。徽章识别必须按合并后的行做，否则小问标签丢失。

    ⚠️ 先按纵向中心聚簇，**簇内按 x 排序后只合并横向相邻的片段**。
    只按 y 排序全局合并会把右侧题干尾部的 `10` 之类吸进题干行
    （2022-jan-ma02 p19 的 `10` 就是这样丢的，整题变成空区域）。
    """
    clusters = []
    for r in sorted(lines, key=lambda x: ((x["y0"] + x["y1"]) / 2, x["x0"])):
        mid = (r["y0"] + r["y1"]) / 2
        for c in clusters:
            if abs(mid - c["mid"]) < 4 and c["bold"] == r["bold"]:
                c["items"].append(r)
                break
        else:
            clusters.append({"mid": mid, "bold": r["bold"], "items": [r]})

    out = []
    for c in clusters:
        cur = None
        for r in sorted(c["items"], key=lambda x: x["x0"]):
            if cur is not None and (r["x0"] - cur["x1"]) <= 25:
                cur["t"] += r["t"]
                cur["x1"] = max(cur["x1"], r["x1"])
                cur["y1"] = max(cur["y1"], r["y1"])
                continue
            cur = {"t": r["t"], "x0": r["x0"], "y0": r["y0"], "x1": r["x1"], "y1": r["y1"],
                   "sx0": r["sx0"], "bold": r["bold"], "sz": r["sz"], "mid": c["mid"]}
            out.append(cur)
    out.sort(key=lambda r: (r["y0"], r["x0"]))
    return out


def badge_of(row):
    """题号徽章行 → (题号, 小问标签)。标签如 'a' / 'a i' / ''。"""
    if row["sx0"] > 56 or not row["bold"] or not (10 <= row["sz"] <= 12):
        return None
    m = BADGE_RE.match(row["t"])
    if not m:
        return None
    labels = LABEL_RE.findall(m.group(2) or "")
    return int(m.group(1)), " ".join(labels)


def build_page(pg, pi):
    lines = [r for r in page_lines(pg) if in_frame(r)]
    hl, shapes = page_draws(pg)
    badges = []
    for r in visual_rows(lines):
        b = badge_of(r)
        if b:
            badges.append({"n": b[0], "label": b[1], "y": r["y0"],
                           "bbox": [r["x0"], r["y0"], r["x1"], r["y1"]]})

    body = [r for r in lines if not is_nav(r)]
    chars = sum(len(re.sub(r"\s", "", r["t"])) for r in body)

    bottom = 0.0
    for r in body:
        bottom = max(bottom, r["y1"])
    for _y0, y1 in shapes:
        bottom = max(bottom, y1)
    for y in hl:
        bottom = max(bottom, y)
    nimg = 0
    for b in pg.get_text("dict")["blocks"]:
        if b["type"] == 1:
            nimg += 1
            bb = b["bbox"]
            if bb[1] < BOT and bb[3] > TOP:
                bottom = max(bottom, min(bb[3], BOT))

    has_marks = any(MARKS_RE.search(r["t"]) for r in lines)
    big_fig = any(y1 - y0 > 80 for y0, y1 in shapes)
    # 卷中插页：AQA 会在题与题之间插「There are no questions printed on this page /
    # DO NOT WRITE ON THIS PAGE」整页（页上常带一条大对角线），不是题面，整页丢。
    filler = any(FILLER_RE.search(r["t"]) for r in lines)

    return {
        "i": pi, "lines": lines, "badges": badges, "chars": chars,
        "nh": len(hl), "nimg": nimg, "bottom": r2(bottom),
        "ys": [r["y0"] for r in lines] + hl,
        "blank": filler or ((not badges) and chars < 40 and len(hl) >= 8
                            and not has_marks and not big_fig and nimg == 0),
        "sections": [{"label": SEC_RE.match(r["t"]).group(1), "y": r["y0"]}
                     for r in lines if SEC_RE.match(r["t"])],
    }


# ── 主分析 ──────────────────────────────────────────────────────────────────

def analyze(doc, paper_marks=None):
    n = doc.page_count
    # 卷末：END OF QUESTIONS 之后全丢；END 页本身保留，内容底截到该行上方
    end_idx = None
    for i in range(1, n):
        if END_RE.search(doc[i].get_text()):
            end_idx = i
            break
    hi = (end_idx + 1) if end_idx is not None else n

    pages = []
    for i in range(1, hi):
        pg = build_page(doc[i], i)
        if end_idx == i:
            e = next((r for r in pg["lines"] if END_RE.search(r["t"])), None)
            if e:
                pg["bottom"] = r2(min(pg["bottom"], e["y0"] - 6))
        pages.append(pg)

    # ── 分组：题号推进 —— 同题内的小节徽章不断段 ──
    qs, dropped, cur = {}, [], None
    sec_of, pending_sec = {}, None
    SEC = "__sec__"          # Section 头：只当断点，本身不进任何题（王培琪 2026-10-21）

    def add(owner, page, y0, y1, ys):
        if owner is None or owner == SEC or y1 - y0 < 1:
            return
        if not any(y0 - 1 <= y <= y1 + 1 for y in ys):
            return                      # 段内无任何内容（页顶/页底空条）
        qs.setdefault(owner, []).append([page, r2(y0), r2(y1)])

    for pg in pages:
        if pg["blank"]:
            dropped.append(pg["i"])
            continue
        markers = [{"n": SEC, "y": s["y"], "y1": s["y"] + 14, "sec": True,
                    "label": s["label"]} for s in pg["sections"]]
        markers += [{"n": b["n"], "y": b["y"], "y1": b["bbox"][3], "sec": False}
                    for b in pg["badges"]]
        markers.sort(key=lambda m: m["y"])

        if not markers:
            add(cur, pg["i"], TOP, min(BOT, pg["bottom"] + 6), pg["ys"])
            continue
        seg, owner = TOP, cur
        for m in markers:
            if m["n"] == owner:
                continue
            # 同一「视觉行」里可能有多个 line（公式的 π、分数线基线不同），
            # 徽章行 bbox 未必盖住它们。取与徽章行纵向相交的所有行的最高上沿，
            # 否则边界会把同行里更高的那个字符劈成两半（2022-jan-ma03 Q5 踩到）。
            tops = [m["y"]]
            for r in pg["lines"]:
                if r["y1"] > m["y"] - 2 and r["y0"] < m["y1"] + 2:
                    tops.append(r["y0"])
            y = max(TOP, r2(min(tops) - 2))
            # 兜底：边界绝不能落在任何字形内部。矩阵大括号这类超高字形，
            # 与徽章行并不「相交」（差几个 pt），但下沿会跨过边界——
            # 沿着跨边界元素往上退，直到边界落在空白里（2021-jan-ma03 p4）。
            for _ in range(8):
                hit = [r["y0"] for r in pg["lines"] if r["y0"] < y < r["y1"]]
                if not hit:
                    break
                y = max(TOP, r2(min(hit) - 2))
            if y > seg:
                add(owner, pg["i"], seg, y, pg["ys"])
            seg, owner = y, m["n"]
            if m["n"] != SEC:            # Section 头不推进「当前题」，否则下一页归属错
                cur = m["n"]
                if pending_sec and m["n"] not in sec_of:
                    sec_of[m["n"]] = pending_sec
                    pending_sec = None
            else:
                pending_sec = m.get("label")
        add(owner, pg["i"], seg, min(BOT, pg["bottom"] + 6), pg["ys"])

    nq = max(qs) if qs else 0
    questions, marks_sum, notes_all = [], 0, []
    for k in range(1, nq + 1):
        q = {"number": str(k), "status": "auto", "confidence": 1.0,
             "regions": [], "anchors": {}, "subquestions": [],
             "question_type": "structured", "review_notes": []}
        regs = sorted(qs.get(k, []), key=lambda r: (r[0], r[1]))
        q["regions"] = [{"page": p, "bbox": [L, y0, R, y1]} for p, y0, y1 in regs]

        # 起点/终点锚点（供校对工具回显）
        st = next((b for pg in pages if not pg["blank"]
                   for b in pg["badges"] if b["n"] == k), None)
        if st is not None:
            q["anchors"]["start"] = {"page": next(pg["i"] for pg in pages
                                                  if any(b is st for b in pg["badges"])),
                                     "bbox": [r2(v) for v in st["bbox"]]}
        if not regs:
            q["status"] = "review"
            q["confidence"] = 0.4
            q["review_notes"].append("本题未取到任何内容段")
        else:
            p, y0, y1 = regs[-1]
            q["anchors"]["end"] = {"page": p, "bbox": [L, y0, R, y1]}

        # 小问锚点：本页徽章里带标签、且落在本题区域内的那些
        subs = []
        for pg in pages:
            if pg["blank"]:
                continue
            for b in pg["badges"]:
                if b["n"] != k or not b["label"]:
                    continue
                inside = any(r["page"] == pg["i"] and r["bbox"][1] - 1 <= b["y"] <= r["bbox"][3] + 1
                             for r in q["regions"])
                if inside:
                    subs.append({"label": b["label"], "page": pg["i"],
                                 "bbox": [r2(v) for v in b["bbox"]]})
        q["subquestions"] = subs

        # 分值：本题区域内的 [N marks] 累加
        mk = 0
        for r in q["regions"]:
            pg = next(x for x in pages if x["i"] == r["page"])
            for ln in pg["lines"]:
                if r["bbox"][1] - 1 <= ln["y0"] <= r["bbox"][3] + 1:
                    for m in MARKS_RE.finditer(ln["t"]):
                        mk += int(m.group(1))
        if mk:
            q["total_marks"] = mk
            marks_sum += mk
        else:
            q["status"] = "review"
            q["confidence"] = min(q["confidence"], 0.6)
            q["review_notes"].append("本题区域内未找到 [N marks]")

        # Section（PSM1 的 A/B/C）：Section 头本身不进题图，但要记住每题属于哪一节
        if sec_of.get(k):
            q["section"] = sec_of[k]

        questions.append(q)

    # 全卷题号必须 1..N 连续（分组天然保证，这里兜一道）
    nums = [int(q["number"]) for q in questions]
    if nums != list(range(1, len(nums) + 1)):
        notes_all.append(f"题号不连续: {nums}")

    meta = {"pages": n, "end_page": end_idx, "nq": len(questions),
            "marks": marks_sum, "dropped": dropped,
            "segments": sum(len(q["regions"]) for q in questions)}
    if paper_marks:
        if marks_sum != paper_marks:
            notes_all.append(f"分值合计 {marks_sum} ≠ 卷面 {paper_marks}")
    meta["notes"] = notes_all
    return questions, meta


def cover_question_count(doc):
    """封面 `For Examiner's Use` 表格竖排 1..N —— 与正文题数对账用。"""
    nums = []
    for b in doc[0].get_text("dict")["blocks"]:
        if b["type"] != 0:
            continue
        for ln in b["lines"]:
            t = "".join(s["text"] for s in ln["spans"]).strip()
            if re.fullmatch(r"\d{1,2}", t) and 450 < ln["bbox"][0] < 500 and 470 < ln["bbox"][1] < 760:
                nums.append((ln["bbox"][1], int(t)))
    nums.sort()
    n = 0
    for _y, v in nums:
        if v == n + 1:
            n = v
        else:
            break
    return n


# ── 落盘 ────────────────────────────────────────────────────────────────────

def question_body(q, pages):
    """把本题区域内的文字行拉成 md 正文（横线是图形，不产生文本）。"""
    out = []
    for r in q["regions"]:
        pg = next(x for x in pages if x["i"] == r["page"])
        for ln in pg["lines"]:
            if not (r["bbox"][1] - 1 <= (ln["y0"] + ln["y1"]) / 2 <= r["bbox"][3] + 1):
                continue
            t = ln["t"].strip()
            if not t or is_nav(ln):
                continue
            if re.fullmatch(r"[A-Z]{2,}/\S+", t):        # IB/G/Jun24/MA01 之类
                continue
            out.append(t)
    return "\n".join(out)


def write_outputs(sdir, cut, questions, pages, board, qual, subject,
                  year, session, paper_code, paper_unit):
    (sdir / "cut.json").write_text(
        json.dumps(cut, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")

    for q in questions:
        n = int(q["number"])
        path = sdir / f"q{n:02d}.md"
        # 人工确认过的（verified / manual）不覆盖；review 是机器给的，重切要跟着更新
        if path.exists():
            head = path.read_text(encoding="utf-8")[:800]
            if re.search(r"^cut_status:\s*(?:verified|manual)\b", head, re.M):
                continue
        qid = f"{board}-{qual}-{subject}-{year}-{session}-{paper_code.lower()}-q{n:02d}"
        fm = [("id", qid), ("board", board), ("qualification", qual),
              ("subject", subject), ("paper_code", paper_code.upper())]
        if paper_unit:
            fm.append(("paper_unit", paper_unit))
        fm += [("year", year), ("session", session), ("question_number", str(n))]
        if "total_marks" in q:
            fm.append(("total_marks", q["total_marks"]))
        if "section" in q:
            fm.append(("section", q["section"]))
        fm += [("question_type", q["question_type"]),
               ("image", f"assets/q{n:02d}.webp"),
               ("layout", f"assets/q{n:02d}.layout.json"),
               ("source", f"Oxford AQA International A-Level Further Mathematics "
                          f"{paper_code.upper()} {paper_unit or ''}, {year} {session}".replace("  ", " ")),
               ("cut_status", q["status"]), ("language", "en")]
        lines = ["---"] + [f"{k}: {yaml_scalar(v)}" for k, v in fm] + ["---", ""]
        body = question_body(q, pages)
        path.write_text("\n".join(lines + [body]) + "\n", encoding="utf-8")


def is_done(sdir):
    return (sdir / ".done").exists() and (sdir / "cut.json").exists()


def run_one(sdir, args):
    sdir = Path(sdir).resolve()
    if not sdir.is_dir():
        sys.exit(f"目录不存在: {sdir}")
    board = sdir.parents[2].name
    qual = sdir.parents[1].name
    subject = sdir.parents[0].name
    if not BOARD_RE.match(board) or not SUBJECT_RE.match(subject):
        sys.exit(f"路径不符约定（应为 data/aqa/a-level/further-mathematics/<session>）: {sdir}")
    try:
        year_s, session, code = sdir.name.split("-", 2)
        year = int(year_s)
    except Exception:
        sys.exit(f"session 目录名不符约定: {sdir.name}")

    src = sdir / "source.pdf"
    if not src.exists():
        sys.exit(f"缺少 source.pdf: {src}")
    if is_done(sdir) and not getattr(args, "force", False):
        print(f"SKIP {sdir.name}: 已完成（--force 才重切）")
        return

    paper_unit = paper_marks = paper_ref = None
    pm = sdir / "paper.md"
    if pm.exists():
        t = pm.read_text(encoding="utf-8")
        m = re.search(r"^paper_unit:\s*(\S+)", t, re.M)
        paper_unit = m.group(1) if m else None
        m = re.search(r"^total_marks:\s*(\d+)", t, re.M)
        paper_marks = int(m.group(1)) if m else None
        m = re.search(r"^paper_ref:\s*(\S+)", t, re.M)
        paper_ref = m.group(1) if m else None

    sha = hashlib.sha256(src.read_bytes()).hexdigest()
    doc = pymupdf.open(src)
    questions, meta = analyze(doc, paper_marks)

    cut = {
        "schema_version": 1,
        "source": {"file": "source.pdf", "sha256": sha, "pages": meta["pages"]},
        "page_size": {"w": r2(doc[0].rect.width), "h": r2(doc[0].rect.height)},
        "render": {"dpi": 200, "format": "webp", "quality": 85},
        "questions": questions,
    }
    if paper_ref:
        cut["source"]["paper_ref"] = paper_ref

    cover_nq = cover_question_count(doc)
    ok_nq = (cover_nq == meta["nq"]) if cover_nq else None
    ok_marks = (meta["marks"] == paper_marks) if paper_marks else None

    flag = "OK " if (ok_nq is not False and ok_marks is not False) else "!! "
    print(f"{flag}{sdir.name}: {meta['nq']} 题 | 段 {meta['segments']} | "
          f"纯线页丢弃 {len(meta['dropped'])} | 分值 {meta['marks']}"
          + (f"/{paper_marks}" if paper_marks else "")
          + (f" | 封面题数 {cover_nq}" if cover_nq else ""))
    rev = [q["number"] for q in questions if q["status"] != "auto"]
    if rev:
        print(f"    review: Q{', Q'.join(rev)}")
    for n in meta["notes"]:
        print(f"    ! {n}")
    for q in questions:
        for n in q["review_notes"]:
            print(f"    ! Q{q['number']}: {n}")

    if args.dry_run or args.audit:
        doc.close()
        return
    pages = [build_page(doc[i], i) for i in range(doc.page_count)]
    write_outputs(sdir, cut, questions, pages, "aqa", "a-level", "further-mathematics",
                  year, session, code, paper_unit)
    print(f"    → cut.json + {len(questions)} 个 qNN.md")


def main():
    ap = argparse.ArgumentParser(description="AQA 进阶数学（9665）按题切割")
    ap.add_argument("session_dir")
    ap.add_argument("--dry-run", action="store_true", help="只分析不落盘")
    ap.add_argument("--audit", action="store_true", help="只体检，不落盘")
    ap.add_argument("--force", action="store_true", help="已完成也重切")
    args = ap.parse_args()
    run_one(args.session_dir, args)


if __name__ == "__main__":
    main()
