#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""cut_paper_aqa_biz.py — 牛津国际 AQA 商务（9725 / BU01–BU04）切割 · 摸底版

口径（2026-10-22 王培琪定，摸底阶段）：
  · 题图 = **相关资料（若有）+ 题干 + 分值**；**答题区整段砍掉**
  · 资料：U3 按 Section 挂 Source One / Source Two（各 1–2 页）；
          U4 挂 case study 全部内容页；U1/U2 无资料
  · 资料拼在题面前（material_order="before"），渲染时画一条分隔线
  · 选择题的作答椭圆**涂白**（--keep-ovals 可保留），选项 A–D 文字保留
  · Section 字头、MCQ 填涂说明框、"Extra space"、纯作答页全部丢弃
  · 卷面题号（01 / 01.3 / 08.1）与内部序号分离：cut.json 用 1..N 连续序号
    （schema 要求 int 连续），qNN.md 里记 printed_label 保留卷面号
  · 整题 stem（如 U4 的 08「Min plc has employed you…」）并入其首个小问 08.1

版面锚点（2024-jan 四卷实测）：
  · 页框 x 39.7–538.6、y 53.9–765.4；正文左界 92.7，题号数字块 x 52–99
  · 题号 = 左栏 45≤x0、x1≤105 的 [0-9.] 单词，按 y 聚簇（容差 7pt）拼成
    "01" / "01.3" / "08.1"
  · 答案横线 = 高<2.2pt、宽>300pt 的横线，≥3 条成串且串内行间无文字
  · MCQ 选项圈 = 宽 7–12pt、高 3–6.5pt 的椭圆；涂白不改坐标

用法:
    python scripts/cut_paper_aqa_biz.py --analyze 2024-jan/qp-202401-business-u3.pdf
    python scripts/cut_paper_aqa_biz.py --only 202401-u1 [,202401-u2 ...]
    python scripts/cut_paper_aqa_biz.py --only 202401-u3 --no-render
"""

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

import pymupdf

ROOT = Path(__file__).resolve().parent.parent
QP_ROOT = ROOT / "downloadPapers" / "AQA_9725_Business"
DATA = ROOT / "data" / "aqa" / "a-level" / "business"
RENDER = ROOT / "scripts" / "render_questions.py"

X0, X1 = 41.0, 538.0        # 版心（页框 39.7–538.6；X1 收到 538.0 避开右框线）
Y_TOP, Y_BOT = 56.0, 764.0  # 页框 53.9–765.4，底部留 1.4pt 不吃框线
PAD_TOP, PAD_BOT = 8.0, 6.0

DROP_TXT = re.compile(
    r"^(Section [A-C]\b|Answer all questions in the spaces|Read Source (One|Two)|"
    r"Read the case study|^(and then )?answer the questions\.?$|^questions\.?$|"
    r"Only one answer per question|For each multiple-choice|CORRECT METHOD|WRONG METHODS|"
    r"If you want to change|If you wish to return|Do not write|Turn over|Extra space|"
    r"END OF QUESTIONS|There are no questions|DO +NOT +WRITE|ANSWER +IN +THE|"
    r"Question$|number$|Additional page|For Examiner|Copyright|For confidentiality|"
    r"IB/[MG]/|M/(Jan|Jun|Oct)\d\d|BU0\d|\*[0-9A-Za-z/*]+|Page \d+|\d{1,2}$)", re.I)

UNIT_NAME = {
    "1": "Business and Markets",
    "2": "Managing Operations, Human Resources and Finance",
    "3": "Business Strategy",
    "4": "Business Decision Making",
}

BADGE_X0, BADGE_X1 = 45.0, 105.0
BADGE_Y_TOL = 7.0


# ───────────────────────── 版面解析 ─────────────────────────
def _norm(t):
    return re.sub(r"\s+", " ", t).strip()


def page_elements(pg):
    """(rect, kind, text)；kind ∈ text / draw / rule / img"""
    els = []
    for b in pg.get_text("dict")["blocks"]:
        if b.get("type") != 0:
            continue
        for line in b["lines"]:
            txt = "".join(s["text"] for s in line["spans"]).strip()
            if not txt:
                continue
            r = pymupdf.Rect(line["bbox"])
            if r.y1 > Y_BOT or r.y0 < Y_TOP or r.x0 > X1 - 2:
                continue
            if DROP_TXT.match(_norm(txt)):
                continue
            els.append((r, "text", txt))
    for dr in pg.get_drawings():
        r = pymupdf.Rect(dr["rect"])
        if r.y1 > Y_BOT or r.y0 < Y_TOP or r.x0 > X1 - 2:
            continue
        if r.width < 1 and r.height < 1:
            continue
        kind = "rule" if (r.height < 2.2 and r.width > 300) else "draw"
        els.append((r, kind, ""))
    for info in pg.get_image_info():
        r = pymupdf.Rect(info["bbox"])
        if r.y0 < Y_TOP or r.y1 > Y_BOT or r.x0 > X1 - 2:
            continue
        els.append((r, "img", ""))
    els.sort(key=lambda e: (e[0].y0, e[0].x0))
    return els


def rule_runs(els):
    """答案横线成串：≥3 条、间距 ≤45pt、相邻两条之间没有文字"""
    rules = [r for r, k, _ in els if k == "rule"]
    others = [(r.y0, r.y1) for r, k, _ in els if k in ("text", "img")]
    runs, cur = [], []
    for r in rules:
        if cur:
            gap_top, gap_bot = cur[-1].y1, r.y0
            has_text = any(t0 < gap_bot - 1 and t1 > gap_top + 1 for t0, t1 in others)
            if r.y0 - cur[-1].y0 > 45 or has_text:
                runs.append(cur)
                cur = []
        cur.append(r)
    if cur:
        runs.append(cur)
    return [x for x in runs if len(x) >= 3]


def all_text_lines(pg):
    out = []
    for b in pg.get_text("dict")["blocks"]:
        if b.get("type") != 0:
            continue
        for line in b["lines"]:
            if "".join(sp["text"] for sp in line["spans"]).strip():
                r = pymupdf.Rect(line["bbox"])
                if Y_TOP <= r.y0 and r.y1 <= Y_BOT:
                    out.append(r)
    return out


def chunks_of_page(pg, y_from, y_to, page_i):
    """一页内切成若干内容块，跳过答题区/大空白"""
    els = page_elements(pg)
    # ⚠️ 只取版心内的文本行做「防切字」边界修正。右栏「Do not write outside the box」
    #    在 x≈543–570（X1 之外），它的 bbox 会跨过切区上沿，逐行把 y0 往下顶：
    #    2021-jan BU01 Q13 就是这么把「1 3」徽章和首行题干一起顶掉的（题号消失）。
    lines = [lr for lr in all_text_lines(pg) if lr.x0 < X1 - 2]
    rule_ys = sorted(r.y0 for run in rule_runs(els) for r in run)
    seq = [(r, k, t) for r, k, t in els if y_from - 4 <= r.y0 < y_to]
    out, cur, prev_b = [], [], y_from
    for r, k, t in seq:
        if k == "rule":
            continue
        if cur and any(abs(r.y0 - ry) < 6 for ry in rule_ys):
            out.append(cur)
            cur = []
            prev_b = r.y1
            continue
        if cur and (r.y0 - prev_b > 110):
            out.append(cur)
            cur = []
        cur.append(r)
        prev_b = max(prev_b, r.y1)
    if cur:
        out.append(cur)
    slices = []
    for g in out:
        y0 = max(y_from, min(r.y0 for r in g) - PAD_TOP)
        y1 = min(y_to, max(r.y1 for r in g) + PAD_BOT)
        # 单次收敛：跨上沿的行取最大 y1，跨下沿的行取最小 y0。
        # 逐行迭代会「一行顶一行」连锁下移（旧写法在 Q13 上连跳三级）。
        strad = [lr for lr in lines if lr.y0 < y0 < lr.y1]
        if strad:
            y0 = max(lr.y1 for lr in strad) + 1
        strad = [lr for lr in lines if lr.y0 < y1 < lr.y1]
        if strad:
            y1 = min(lr.y0 for lr in strad) - 1
        rect = pymupdf.Rect(X0, y0, X1, y1)
        if rect.height > 8 and (rect.height > 30 or pg.get_text(clip=rect).strip()):
            slices.append((page_i, rect))
    return slices


def badges(doc):
    """[(page, y0, label, main, sub, bbox)] —— 左栏题号数字块"""
    out = []
    for i, pg in enumerate(doc):
        if i == 0:
            continue
        toks = [w for w in pg.get_text("words")
                if re.fullmatch(r"[\d.]", w[4])
                and BADGE_X0 <= w[0] and w[2] <= BADGE_X1
                and Y_TOP - 2 <= w[1] <= Y_BOT]
        clusters = []
        for w in sorted(toks, key=lambda w: (w[1], w[0])):
            for c in clusters:
                if abs(w[1] - c[0][1]) <= BADGE_Y_TOL:
                    c.append(w)
                    break
            else:
                clusters.append([w])
        for c in clusters:
            c.sort(key=lambda w: w[0])
            label = "".join(w[4] for w in c)
            m = re.fullmatch(r"(\d{2})(?:\.(\d))?", label)
            if not m:
                continue
            main, sub = m.group(1), m.group(2)
            bb = pymupdf.Rect(min(w[0] for w in c), min(w[1] for w in c),
                              max(w[2] for w in c), max(w[3] for w in c))
            out.append((i, bb.y0, label, main, sub, tuple(bb)))
    return sorted(out)


def paper_end(doc):
    for i, pg in enumerate(doc):
        for b in pg.get_text("dict")["blocks"]:
            if b.get("type") != 0:
                continue
            for line in b["lines"]:
                if "END OF QUESTIONS" in _norm("".join(s["text"] for s in line["spans"])):
                    return (i, line["bbox"][1] - 4)
    return (len(doc) - 1, Y_BOT)


def sections(doc):
    """[(page,y,letter)]"""
    out = []
    for i, pg in enumerate(doc):
        for b in pg.get_text("dict")["blocks"]:
            if b.get("type") != 0:
                continue
            for line in b["lines"]:
                t = _norm("".join(s["text"] for s in line["spans"]))
                m = re.match(r"^Section ([A-C])\b", t)
                if m:
                    out.append((i, line["bbox"][1], m.group(1)))
    return sorted(out)


def source_directives(doc):
    """[(page,y,'one'|'two')] —— 'Read Source One: … in the insert booklet'"""
    out = []
    for i, pg in enumerate(doc):
        for b in pg.get_text("dict")["blocks"]:
            if b.get("type") != 0:
                continue
            for line in b["lines"]:
                t = _norm("".join(s["text"] for s in line["spans"]))
                m = re.match(r"^Read Source (One|Two)\b", t)
                if m:
                    out.append((i, line["bbox"][1], m.group(1).lower()))
    return sorted(out)


def material_blocks(ins):
    """资料册内容块：{'one': [页], 'two': [页], 'case': [页]}（0 基，原册页号）"""
    blocks, cur = {}, None
    for i in range(1, ins.page_count):
        t = ins[i].get_text()
        if "There are no sources printed on this page" in t:
            continue
        m = re.search(r"Source (One|Two):", t)
        if m:
            cur = m.group(1).lower()
            blocks.setdefault(cur, []).append(i)
            continue
        if re.search(r"Case study:", t):
            cur = "case"
            blocks.setdefault(cur, []).append(i)
            continue
        if cur:
            blocks[cur].append(i)
    return blocks


MAT_PAD_TOP, MAT_PAD_BOT = 20.0, 14.0


def content_bbox(pg):
    """资料页内容外框（**按本页实测的横向范围**，不用卷面版心）。

    资料册的左右界跟卷面不一样，而且逐年在变：
      2024-jan u3 文字到 x=547.6、2023-june u3 到 553.1、2026-june u4 到 539.4。
    套用卷面 X1=538 会把正文右侧整列截掉（2024-jan BU03 实测踩过）。
    """
    ys, xs = [], []
    for b in pg.get_text("dict")["blocks"]:
        if b.get("type") != 0:
            continue
        for ln in b["lines"]:
            if not "".join(sp["text"] for sp in ln["spans"]).strip():
                continue
            bb = pymupdf.Rect(ln["bbox"])
            if Y_TOP <= bb.y0 and bb.y1 <= Y_BOT + 2 and bb.x0 < 575:
                ys.append((bb.y0, bb.y1))
                xs.append((bb.x0, bb.x1))
    for dr in pg.get_drawings():
        r = pymupdf.Rect(dr["rect"])
        if r.y0 >= Y_TOP and r.y1 <= Y_BOT and r.x0 < 575 and (r.width > 5 or r.height > 5):
            ys.append((r.y0, r.y1))
            xs.append((r.x0, r.x1))
    # 栅格图也算内容：2024-jan BU04 资料 p3 的 Figure 1 是整张位图
    # (121,475)-(463,740)，只看文字/矢量会把外框收到图注上面，整张图丢掉。
    for info in pg.get_image_info():
        r = pymupdf.Rect(info["bbox"])
        if r.y0 >= Y_TOP and r.y1 <= Y_BOT:
            ys.append((r.y0, r.y1))
            xs.append((r.x0, r.x1))
    if not ys:
        return pymupdf.Rect(X0, Y_TOP, X1, 700)
    x0 = max(30.0, min(a for a, _ in xs) - 6.0)
    x1 = min(575.0, max(b for _, b in xs) + 6.0)
    return pymupdf.Rect(x0, max(Y_TOP, min(a for a, _ in ys) - MAT_PAD_TOP),
                        x1, min(Y_BOT, max(b for _, b in ys) + MAT_PAD_BOT))


def ovals_in(pg, rects):
    """rects 范围内的作答椭圆（宽 7–12、高 3–6.5）。

    ⚠️ **必须按几何去重**：AQA 有些卷把同一个圈画两遍（描边层 + 填充层各一个
    drawing），get_drawings() 会返回两个 bbox 完全相同的椭圆。若直接按序号
    分配 A/B/C/D，从重复那一项起全部错位——C 拿到 B 的框、真正的 D 被丢掉。
    2026-june BU01 Q1 实测：4 个选项出 5 个椭圆（B 重复），21 卷里 12 题中招。
    """
    seen, out = set(), []
    for dr in pg.get_drawings():
        r = pymupdf.Rect(dr["rect"])
        if 6.5 <= r.width <= 12 and 3.0 <= r.height <= 6.5:
            if any(r.y0 >= q.y0 - 2 and r.y1 <= q.y1 + 2 for q in rects):
                key = (round(r.x0), round(r.y0), round(r.x1), round(r.y1))
                if key in seen:
                    continue
                seen.add(key)
                out.append(r)
    return out


def answer_widgets(pg, rects):
    """MCQ 作答块 = **黑色细边框 + 内部椭圆**（不是只有椭圆）。

    2024 起 AQA 商务的选择题作答位画的是「23×15pt 黑框 + 框内 9×3.6pt 椭圆」，
    只涂椭圆会留下一圈黑框（实测踩过）。这里以椭圆为中心把相交的细线并进来，
    得到整个作答框的外框。
    """
    drs = [(pymupdf.Rect(d["rect"]), d) for d in pg.get_drawings()]
    out = []
    for o in rects:
        probe = pymupdf.Rect(o.x0 - 10, o.y0 - 10, o.x1 + 10, o.y1 + 10)
        bb = pymupdf.Rect(o)
        for d, _raw in drs:
            if d.intersects(probe) and d.width < 40 and d.height < 30:
                bb |= d
        out.append(bb)
    return out


def sum_marks(text):
    ms = [int(x) for x in re.findall(r"\[(\d+) marks?\]", text)]
    return sum(ms), ms


def group_questions(spots):
    """按**主号**分组：题 = 01 / 02 / 08，点号小问只作锚点。

    AQA 每印一个小问就重印一次徽章（`0 8 . 1`、`0 8 . 2`…），若把每个徽章
    当一道题，08 就会被拆成三题、01 被拆成五题。经济 9625 与物理/化学/会计
    的既有脚本统一按「主问题为单位」，这里保持一致：
        q01 含 01.1–01.5，q08 含 08.1–08.3。
    """
    order, groups = [], {}
    for s in spots:
        if s[3] not in groups:
            groups[s[3]] = []
            order.append(s[3])
        groups[s[3]].append(s)
    return [(m, groups[m]) for m in order]


def region_slices(doc, start, stop):
    """[start, stop) 区间内的内容切片（(page,y) 元组，左闭右开到 stop 为止）"""
    p0, y0 = start
    if stop is None:
        end = paper_end(doc)
        p1, y1 = (end[0] + 1, Y_BOT) if end > start else (len(doc) - 1, Y_BOT)
        if end > start:
            p1, y1 = end
    else:
        end = paper_end(doc)
        p1, y1 = min(stop, end) if end > start else stop
    slices = []
    for p in range(p0, min(p1, len(doc) - 1) + 1):
        yf = y0 - 10 if p == p0 else Y_TOP
        yt = y1 - 6 if p == p1 else Y_BOT
        if yt > yf:
            slices += chunks_of_page(doc[p], yf, yt, p)
    return slices


# ───────────────────────── 建卷 ─────────────────────────
def build(qp_path: Path, ins_path: Path | None, out_root: Path,
          wipe_ovals=False, overwrite=False):
    m = re.search(r"qp-(\d{6})-business-u(\d)", qp_path.name)
    yyyymm, unit = m.group(1), m.group(2)
    year, mm = int(yyyymm[:4]), yyyymm[4:]
    session = "jan" if mm == "01" else "june"
    code = f"BU0{unit}"
    sid = f"{year}-{session}-bu0{unit}"
    out_dir = out_root / sid
    if (out_dir / "cut.json").exists() and not overwrite:
        print(f"  SKIP {sid}（已有 cut.json）")
        return None

    qp = pymupdf.open(str(qp_path))
    ins = pymupdf.open(str(ins_path)) if ins_path else None

    # 1) 资料页 → 合并卷前置
    blocks = material_blocks(ins) if ins else {}
    mat_pages = []            # 原册页号（按 one → two 顺序）
    for key in ("one", "two", "case"):
        mat_pages += blocks.get(key, [])
    OFF = len(mat_pages)
    pos = {}
    k = 0
    for key in ("one", "two", "case"):
        for p in blocks.get(key, []):
            pos[p] = k
            k += 1

    merged = pymupdf.open()
    if ins:
        for p in mat_pages:
            merged.insert_pdf(ins, from_page=p, to_page=p)
    merged.insert_pdf(qp)
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp = out_dir / "source.pdf"
    merged.save(str(tmp), garbage=3, deflate=True)
    merged.close()
    doc = pymupdf.open(str(tmp))

    # 2) 解析题号（题 = 主号；.N 小问只作锚点）
    spots = badges(qp)
    if not spots:
        raise SystemExit(f"{sid}: 未识别到题号")
    groups = group_questions(spots)
    starts = [(g[0][0], g[0][1]) for _m, g in groups]
    secs = sections(qp)
    directs = source_directives(qp)

    qs, texts, labels = [], {}, {}
    for n, (main, grp) in enumerate(groups, 1):
        p, y = grp[0][0], grp[0][1]
        stop = starts[n] if n < len(starts) else None
        regs = region_slices(qp, (p, y), stop)
        if not regs:
            print(f"  ! {sid} Q{main} 空切块，跳过")
            continue
        txt = "\n".join(qp[pi].get_text(clip=r) for pi, r in regs)
        marks, _ = sum_marks(txt)
        cur_sec = ""
        for sp, sy, letter in secs:
            if (sp, sy) <= (p, y):
                cur_sec = letter
        src = None
        for sp, sy, key in directs:
            if (sp, sy) <= (p, y):
                src = key
        ovs, hots = [], []
        for pi, r in regs:
            raw_o = ovals_in(qp[pi], [r])
            if raw_o:
                ovs += [(pi, o) for o in raw_o]
                hots += answer_widgets(qp[pi], raw_o)

        # 资料挂载（U3 按 Source，U4 整份 case study）
        mat_regs = []
        if unit == "3" and src in ("one", "two"):
            use = blocks.get(src, [])
        elif unit == "4":
            use = blocks.get("case", [])
        else:
            use = []
        for pg_i in use:
            mat_regs.append({"page": pos[pg_i],
                             "bbox": [round(v, 1) for v in content_bbox(ins[pg_i])]})

        qtype = "mcq" if ovs else "structured"
        hotz = []
        if qtype == "mcq":
            for j, lab in enumerate("ABCD"):
                if j < len(hots[:4]):
                    o = hots[:4][j]
                    hotz.append({"label": lab,
                                 "bbox": [round(v, 1) for v in
                                          (o.x0 - 2, o.y0 - 2, o.x1 + 2, o.y1 + 2)]})
        # 小问 label 只存**点号后的序号**（"1".."5"），不存卷面全号 "08.1"。
        # schema 的小问 label 是「空格连接的标签序列」（'a' / 'a i'），且切分时
        # 子题 id 拼成 `{qid}-{label}`，正则 ^[a-z0-9]+(-[a-z0-9]+)+$ 不认点号——
        # 存 "08.1" 会让「按小问保存」直接 500（2026-10-22 实测）。
        subs = [{"label": str(b[4]), "page": b[0] + OFF,
                 "bbox": [round(v, 1) for v in b[5]]}
                for b in grp if b[4]]
        qs.append({
            "number": str(n),
            "status": "auto",
            "confidence": 0.95,
            "total_marks": marks,
            "section": cur_sec,
            "question_type": qtype,
            "option_hotzones": hotz,
            "regions": [{"page": pi + OFF, "bbox": [round(v, 1) for v in rg]}
                        for pi, rg in regs],
            "material_regions": mat_regs,
            "anchors": {
                "start": {"page": p + OFF,
                          "bbox": [round(v, 1) for v in grp[0][5]]},
                "end": {"page": regs[-1][0] + OFF,
                        "bbox": [round(v, 1) for v in regs[-1][1]]},
            },
            "subquestions": subs,
            "_label": main,
            "_text": txt,
            "_ovals": ovs,
        })

    # 5a) 资料页上的翻页提示（"Turn over for Source Two"）涂白：
    #     它夹在两页资料之间，不涂会在成图里留下一句莫名其妙的指令。
    for pi in range(OFF):
        pg = doc[pi]
        for b in pg.get_text("dict")["blocks"]:
            if b.get("type") != 0:
                continue
            for ln in b["lines"]:
                t = _norm("".join(sp["text"] for sp in ln["spans"]))
                if re.match(r"^(Turn over|DO NOT WRITE)", t, re.I):
                    r = pymupdf.Rect(ln["bbox"])
                    pg.draw_rect(pymupdf.Rect(r.x0 - 2, r.y0 - 2, r.x1 + 2, r.y1 + 2),
                                 color=(1, 1, 1), fill=(1, 1, 1), width=0.6, overlay=True)

    # 5) MCQ 选项圈涂白（只改合并卷，不碰原卷）
    if wipe_ovals:
        by_page = {}
        for q in qs:
            for pi, o in q["_ovals"]:
                by_page.setdefault(pi, []).append(o)
        for pi, ovs in by_page.items():
            pg = doc[pi + OFF]
            for o in ovs:
                # 白块覆盖（不是删除）：背景纯白，覆盖后与涂白等价，
                # 又不触碰原内容流里的任何文字/图形。
                pg.draw_rect(pymupdf.Rect(o.x0 - 1.2, o.y0 - 1.2,
                                          o.x1 + 1.2, o.y1 + 1.2),
                             color=(1, 1, 1), fill=(1, 1, 1), width=0.6,
                             overlay=True)
        tmp2 = out_dir / "source.tmp.pdf"
        doc.save(str(tmp2), garbage=3, deflate=True)
        doc.close()
        tmp2.replace(tmp)
        doc = pymupdf.open(str(tmp))

    sha = hashlib.sha256(tmp.read_bytes()).hexdigest()
    cut = {
        "schema_version": 1,
        "source": {"file": "source.pdf", "sha256": sha, "pages": doc.page_count,
                   "paper_ref": f"{code}/{yyyymm[2:]}"},
        "booklet_pages": OFF,
        "sources": ([{"role": "booklet", "file": ins_path.name,
                      "sha256": hashlib.sha256(ins_path.read_bytes()).hexdigest(),
                      "pages": ins.page_count, "used_pages": mat_pages}] if ins else [])
        + [{"role": "qp", "file": qp_path.name,
            "sha256": hashlib.sha256(qp_path.read_bytes()).hexdigest(),
            "pages": qp.page_count}],
        "page_size": {"w": round(doc[0].rect.width, 2), "h": round(doc[0].rect.height, 2)},
        "render": {"dpi": 200, "format": "webp", "quality": 85},
        "material_order": "before",
        "questions": qs,
    }
    for q in qs:
        texts[q["number"]] = q.pop("_text")
        q.pop("_ovals", None)
    labels = {q["number"]: q.pop("_label") for q in qs}
    (out_dir / "cut.json").write_text(
        json.dumps(cut, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")

    # 6) paper.md
    total = sum(q["total_marks"] for q in qs)
    printed = parse_printed_total(qp)
    exam_date, exam_time = parse_cover(qp)
    pm = [
        "---",
        f"id: aqa-a-level-business-{sid}",
        "board: aqa",
        "qualification: a-level",
        "subject: business",
        f"paper_code: {code}",
        f"paper_unit: U{unit}",
        f"year: {year}",
        f"session: {session}",
        'spec: "9725"',
        f"level: {'AS' if unit in ('1', '2') else 'A2'}",
        f'title: "Unit {unit}: {UNIT_NAME[unit]}"',
    ]
    if exam_date:
        pm.append(f'exam_date: "{exam_date}"')
    if exam_time:
        pm.append(f'exam_time: "{exam_time}"')
    if printed:
        pm.append(f"printed_total_marks: {printed}")
    pm.append(f"total_marks: {total}")
    pm += [
        f"source_pdf: {qp_path.name}",
    ]
    if ins:
        pm.append(f"insert_pdf: {ins_path.name}")
        pm.append(f"insert_pages: {OFF}")
    pm += [
        "cut: cut.json",
        "language: en",
        "---",
        "",
        f"Oxford AQA International A-Level · Business · {code} · Unit {unit}: {UNIT_NAME[unit]}",
        f"源卷 `{qp_path.name}`（{qp.page_count} 页）"
        + (f" + 资料册 `{ins_path.name}`（用第 {len(mat_pages)} 页内容" if ins else "")
        + (f"，合并为 `source.pdf` 前置 {OFF} 页）。" if ins else "。"),
        f"口径：题图 = 资料（若有）+ 题干 + 分值，答题区整段丢弃；共 {len(qs)} 题 / {total} 分"
        + (f"（卷面印 {printed} 分）。" if printed else "。"),
        "",
    ]
    (out_dir / "paper.md").write_text("\n".join(pm), encoding="utf-8")

    # 7) qNN.md
    for q in qs:
        label = labels[q["number"]]
        qid = f"aqa-a-level-business-{sid}-q{int(q['number']):02d}"
        fm = [
            "---",
            f"id: {qid}",
            "board: aqa",
            "qualification: a-level",
            "subject: business",
            f"paper_code: {code}",
            f"paper_unit: U{unit}",
            f"year: {year}",
            f"session: {session}",
            f"question_number: {q['number']}",
            f'printed_label: "{label}"',
            f"total_marks: {q['total_marks']}",
            f"question_type: {q['question_type']}",
            f"section: {q['section'] or '-'}",
            f"image: assets/q{int(q['number']):02d}.webp",
            f"layout: assets/q{int(q['number']):02d}.layout.json",
            f'source: "Oxford AQA International A-Level Business {code}, {year} {session}"',
            "cut_status: auto",
            "language: en",
            "---",
            "",
            texts[q["number"]].strip(),
            "",
        ]
        (out_dir / f"q{int(q['number']):02d}.md").write_text("\n".join(fm), encoding="utf-8")
    print(f"  OK  {sid}: {len(qs)} 题 / {total} 分 / 卷 {qp.page_count} 页 + 资料 {OFF} 页")
    return out_dir


def parse_printed_total(qp):
    t = _norm(qp[0].get_text())
    m = re.search(r"maximum mark for this paper is (\d+)", t)
    return int(m.group(1)) if m else None


def parse_cover(qp):
    t = _norm(qp[0].get_text())
    date = None
    m = re.search(r"(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\s+(\d{1,2} \w+ \d{4})", t)
    if m:
        try:
            from datetime import datetime
            date = datetime.strptime(m.group(2), "%d %B %Y").strftime("%Y-%m-%d")
        except ValueError:
            date = None
    tm = re.search(r"Time allowed:\s*([0-9]+ hour[s]?(?: [0-9]+ minutes?)?)", t)
    return date, (tm.group(1) if tm else None)


def analyze(qp_path, ins_path=None):
    qp = pymupdf.open(str(qp_path))
    ins = pymupdf.open(str(ins_path)) if ins_path else None
    spots = badges(qp)
    print(f"{qp_path.name}  {qp.page_count} 页  题号锚点 {len(spots)} 个")
    secs = sections(qp)
    directs = source_directives(qp)
    print("  sections:", [(p + 1, round(y), s) for p, y, s in secs])
    print("  sources :", [(p + 1, round(y), s) for p, y, s in directs])
    if ins:
        print("  insert blocks:", {k: [p + 1 for p in v] for k, v in material_blocks(ins).items()})
    for i, (p, y, label, main, sub, bb) in enumerate(spots):
        regs = question_regions(qp, spots, i)
        txt = "\n".join(qp[pi].get_text(clip=r) for pi, r in regs)
        marks, ms = sum_marks(txt)
        ovs = sum(len(ovals_in(qp[pi], [r])) for pi, r in regs)
        head = _norm(txt)[:70]
        print(f"  {label:>5} p{p+1:<3} y={y:6.1f} marks={marks:<2} ovals={ovs:<2} "
              f"slices={[(pi+1, round(r.y0), round(r.y1)) for pi, r in regs]} | {head}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="*", help="如 202401-u3")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--analyze", nargs="*", help="只打印锚点，接受相对 QP_ROOT 的路径")
    ap.add_argument("--overwrite", action="store_true")
    ap.add_argument("--wipe-ovals", action="store_true",
                    help="把 MCQ 作答框涂白（默认保留圆圈）")
    ap.add_argument("--no-render", action="store_true")
    args = ap.parse_args()

    if args.analyze:
        for rel in args.analyze:
            p = (QP_ROOT / rel)
            m = re.search(r"qp-(\d{6})-business-u(\d)", p.name)
            ins = p.parent / f"in-{m.group(1)}-business-u{m.group(2)}.pdf"
            analyze(p, ins if ins.exists() else None)
        return

    todo = []
    for qp in sorted(QP_ROOT.glob("*/qp-*-business-u*.pdf")):
        m = re.search(r"qp-(\d{6})-business-u(\d)", qp.name)
        key = f"{m.group(1)}-u{m.group(2)}"
        if args.only and key not in args.only:
            continue
        ins = qp.parent / f"in-{m.group(1)}-business-u{m.group(2)}.pdf"
        # U3/U4 必须带 insert；2021-jan / 2022-jan / 2022-june / 2023-jan 这 8 卷
        # 站点上没有 insert（2023-06 才开始发行），整卷跳过（2026-10-22 培琪定）。
        if m.group(2) in ("3", "4") and not ins.exists():
            print(f"  ! 缺资料册，整卷跳过 {key}")
            continue
        todo.append((qp, ins if ins.exists() else None, key))
    if args.limit:
        todo = todo[:args.limit]

    print(f"待切 {len(todo)} 卷")
    made = []
    for i, (qp, ins, key) in enumerate(todo, 1):
        print(f"[{i}/{len(todo)}] {key}")
        d = build(qp, ins, DATA, wipe_ovals=args.wipe_ovals, overwrite=args.overwrite)
        if d:
            made.append(d)
    if made and not args.no_render:
        for d in made:
            r = subprocess.run([sys.executable, str(RENDER), str(d)],
                               capture_output=True, text=True)
            print(f"  render {d.name}: {(r.stdout or r.stderr).strip()[:300]}")


if __name__ == "__main__":
    main()
