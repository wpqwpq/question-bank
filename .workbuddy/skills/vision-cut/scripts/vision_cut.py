# -*- coding: utf-8 -*-
"""vision_cut.py — Step 1：文本层坏掉的卷，纯视觉切到题（卷级）

用法：
    python vision_cut.py <卷目录> [--work DIR] [--plan '1:1-9;2:2-17'] [--force]
    python vision_cut.py data/cie/alevel/chemistry/2025-june-qp42

产出（写到 <work>/<卷名>/out/）：
    assets/qNN.webp           拼接后的整题图（200 DPI）
    assets/qNN.layout.json    src_map.segs（y 区间↔页）+ 空 subquestions
    cut.json                  regions / total_marks（subquestions 由 vision_subs.py 补）

判据硬校验：各题 total_marks 之和 == 卷面满分（paper.md 的 total_marks），否则报错退出。
"""
import argparse
import hashlib
import re
import sys
from pathlib import Path

import pymupdf
import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).parent))
import vlib as V

DPI = 200
QUALITY = 88


def _is_content_row(r):
    """像「题目内容」的行：子问标签 (a)/(ii) 或 分值标记 [N]。"""
    t = r["text"].strip()
    return bool(V.RE_TAG1.match(t) or V.RE_TAG2.match(t) or V.RE_MARK.search(t))


def question_bounds(pages, g, yb):
    """从 OCR 行序列推题界。

    每题 = 「上一个 [Total: N] 之后第一个左顶格行」→「本题 [Total: N] 行」。
    返回 [{number, marks, start:(page,y0,y1), end:(page,y0,y1)}]。

    ⚠️ 两个坑（2026-10-18 实测修掉）：
    ① **封面页会抢首题起点**：p1「Cambridge International AS & A Level」x0=79 < 100，
       于是 q01 从封面开始，把整页说明裁进来。→ 起点页必须是「含子问标签或分值标记」的页。
    ② **题间的数据参考页会被吞进下一题**：2025-june-qp42 的 p19 只有 Table 8.2（化学位移表），
       整页无 (a) 无 [N]，属于考试提供的数据，不属于任何题。同一个判据顺手跳过。
    """
    seq = []
    by_page = {}
    for pg in pages:
        rs = [r for r in V.rows_of(pg) if r["y0"] >= g.yt and r["y1"] <= yb]
        by_page[pg["page"]] = rs
        for r in rs:
            seq.append((pg["page"], r))

    tot_idx = [i for i, (p, r) in enumerate(seq) if V.RE_TOTAL.match(r["text"].strip())]
    out, prev = [], -1
    for k, ti in enumerate(tot_idx):
        s = None
        for j in range(prev + 1, ti + 1):
            p, r = seq[j]
            t = r["text"].strip()
            if V.APPENDIX.search(t) or V.RE_TOTAL.match(t):
                continue
            if r["x0"] < 100:          # 左顶格 = 题号行
                s = j
                break
        if s is None:
            s = prev + 1
        # 起点页前推：跳过封面页 / 纯数据表页
        sp = seq[s][0]
        while sp < seq[ti][0] and not any(_is_content_row(r) for r in by_page.get(sp, [])):
            sp += 1
        if sp != seq[s][0]:
            # 起点行改为该页第一行
            s = next(j for j in range(s, ti + 1) if seq[j][0] == sp)
        m = V.RE_TOTAL.match(seq[ti][1]["text"].strip())
        sp2, sr = seq[s]
        ep, er = seq[ti]
        out.append(dict(number=k + 1, marks=int(m.group(1)),
                        start=(sp2, sr["y0"], sr["y1"]), end=(ep, er["y0"], er["y1"])))
        prev = ti
    return out


def build_regions(doc, g, qb, nxt):
    """把题界转成每页的裁 y 区间。

    ⚠️ 一页多题时必须按题界 y 切，不能整页 span（cut3.py 版的缺陷，已修）：
       起始页从 min(题首行 y, 本页该区间首墨迹) 起，结束页到 结束 y 为止。
    """
    sp, sy0, _ = qb["start"]
    ep, _, ey1 = qb["end"]
    segs = []
    for pno in range(sp, ep + 1):
        b0, b1 = V.page_span(doc, pno, g)
        if b0 == 0.0 and b1 == 0.0:
            continue                                  # 空白页
        top = b0
        bot = b1
        if pno == sp:                                  # 首页：不低于题首行
            top = min(b0, max(sy0 - 6, g.yt))
        if pno == ep:                                  # 末页：不高于本题末尾
            bot = max(min(b1, ey1 + 8), top + 8)
        if pno == sp and pno == ep and bot <= top:
            bot = top + 8
        # 上下各留余量，别贴着墨迹切（下缘留 8pt 才不会切掉 [N] 的 descent）
        segs.append((pno, top - 6, bot + 8))
    return segs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("vol", help="卷目录（含 source.pdf）")
    ap.add_argument("--work", default=None, help="工作目录，默认 <root>/.vision_work")
    ap.add_argument("--plan", default=None,
                    help="手工覆盖题界，形如 '1:p1-p3;2:p4-p6'（只给页区间，y 仍自动）")
    ap.add_argument("--dpi", type=int, default=DPI)
    ap.add_argument("--force", action="store_true", help="文本层正常也强行视觉切")
    args = ap.parse_args()

    vol = Path(args.vol).resolve()
    paper = vol.name
    # data/<board>/<qual>/<subject>/<session> → 上溯 5 层到项目根
    root = vol.parents[4] if len(vol.parents) > 4 else vol
    work = Path(args.work) if args.work else root / ".vision_work"
    out = work / paper / "out"
    (out / "assets").mkdir(parents=True, exist_ok=True)

    # ---- 0. 体检 ----
    v = V.textlayer_verdict(vol / "source.pdf")
    print(f"[体检] {paper}: 文本层 {v['cls']}  "
          f"({v['pages']}p {v['w']}x{v['h']}, 控制字符 {v['ctrl_ratio']*100:.1f}%, 虚词 {v['common_words']})")
    if v["cls"] == "OK" and not args.force:
        print("  文本层可用 → 应该走 cut_paper_cie_chem.py，不是视觉切割。"
              "确实要视觉切请加 --force。")
        return 2

    doc, g = V.open_paper(vol)
    print(f"[几何] {g}")
    # 裁图一律用固定版心 g.x0/g.x1（见 vlib.Geom 的说明）。
    # detect_content_x 只做诊断打印：差得多说明版式变了，该人工看一眼。
    cx0, cx1 = g.x0, g.x1
    _dx0, _dx1 = V.detect_content_x(doc, g)
    if abs(_dx0 - cx0) > 12 or abs(_dx1 - cx1) > 12:
        print(f"[版心] ⚠️ 实测内容 x {_dx0:.1f}–{_dx1:.1f} 与固定版心 {cx0:.0f}–{cx1:.0f} 差 >12pt，确认版式是否异常")

    # ---- 1. OCR 定题界 ----
    print("[OCR] 300 DPI（首次约 2.7s/页，之后走缓存）…")
    pages = V.ocr_paper(vol, work)
    qbs = question_bounds(pages, g, g.yb)
    print(f"[题界] {len(qbs)} 题，分值 {[q['marks'] for q in qbs]}，合计 {sum(q['marks'] for q in qbs)}")

    # 手工覆盖
    if args.plan:
        over = {}
        for part in args.plan.split(";"):
            n, rng = part.split(":")
            a, b = rng.replace("p", "").split("-")
            over[int(n)] = (int(a), int(b))          # 1-based 页码
        for qb in qbs:
            if qb["number"] in over:
                a, b = over[qb["number"]]
                qb["start"] = (a - 1, qb["start"][1], qb["start"][2])
                qb["end"] = (b - 1, qb["end"][1], qb["end"][2])
        print(f"[题界] 已按 --plan 覆盖 {len(over)} 题")

    # ---- 2. 裁图 + 拼接 ----
    cut = {"schema_version": 1,
           "source": {"file": "source.pdf",
                      "sha256": hashlib.sha256((vol / "source.pdf").read_bytes()).hexdigest(),
                      "pages": doc.page_count},
           "page_size": {"w": g.W, "h": g.H},
           "render": {"dpi": args.dpi, "format": "webp", "quality": QUALITY},
           "questions": []}

    for qi, qb in enumerate(qbs):
        qn = f"q{qb['number']:02d}"
        segs = build_regions(doc, g, qb, None)
        if not segs:
            print(f"  ⚠️ {qn} 没算出任何区间，跳过")
            continue
        tiles, lay_segs, regions = [], [], []
        for (pno, top, bot) in segs:
            pix = doc[pno].get_pixmap(dpi=args.dpi,
                                      clip=pymupdf.Rect(cx0, top, cx1, bot))
            img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            tiles.append(img)
            lay_segs.append({"kind": "qp", "page": pno,
                             "pt": [round(top, 2), round(bot, 2)],
                             "img": [0, 0]})          # 占位，下面回填
            regions.append({"page": pno, "bbox": [round(cx0, 2), round(top, 2),
                                                  round(cx1, 2), round(bot, 2)]})
        W = max(t.width for t in tiles)
        H = sum(t.height for t in tiles)
        canvas = Image.new("RGB", (W, H), "white")
        off = 0
        for i, t in enumerate(tiles):
            canvas.paste(t, (0, off))
            lay_segs[i]["img"] = [off, off + t.height]
            off += t.height
        canvas.save(out / "assets" / f"{qn}.webp", "WEBP", quality=QUALITY, method=6)

        lay = {"schema_version": 1, "image": f"{qn}.webp",
               "coords": "normalized 0-1: x/w, y/h relative to this image",
               "size": {"w": W, "h": H},
               "src_map": {"dpi": args.dpi, "notice_h": 0, "sep_h": 0, "segs": lay_segs},
               "subquestions": []}
        V.write_json(out / "assets" / f"{qn}.layout.json", lay)

        cut["questions"].append({"number": str(qb["number"]), "status": "auto",
                                 "confidence": 1.0, "regions": regions,
                                 "review_notes": [], "subquestions": [],
                                 "question_type": "structured",
                                 "total_marks": qb["marks"]})
        print(f"  {qn} {qb['marks']:>3}分  p{qb['start'][0]+1}–p{qb['end'][0]+1}  "
              f"{len(segs)}段 {W}x{H}px")

    V.write_json(out / "cut.json", cut)
    total = sum(q["total_marks"] for q in cut["questions"])
    print(f"\n[完成] {len(cut['questions'])} 题，分值合计 {total} → {out}")

    # ---- 3. 硬校验：分值合计 ----
    pm = vol / "paper.md"
    if pm.exists():
        m = re.search(r"^total_marks:\s*(\d+)", pm.read_text(encoding="utf-8"), re.M)
        if m:
            want = int(m.group(1))
            if total != want:
                print(f"❌ 分值对不上：切出 {total}，卷面 {want}。题界错了，用 --plan 手工指定页区间。")
                return 3
            print(f"✅ 分值合计 {total} == 卷面满分 {want}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
