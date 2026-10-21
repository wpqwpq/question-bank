#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""prep_cie_eco.py — 把 CIE 9708 源 PDF 铺成 data/cie/alevel/economics/<year>-<session>-qpNN/

只做目录与元数据，不切割。接受显式清单（--list）或单份（--pdf），绝不扫全量自动开工。
"""
import argparse
import re
import shutil
import sys
from pathlib import Path

import pymupdf

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
SRC_ROOTS = [
    ROOT / "downloadPapers/_new/CIE_9708_Economics",
    ROOT / "downloadPapers/CIE_Alevel_Economy",
]
SRC_ROOT = SRC_ROOTS[0]
DST_ROOT = ROOT / "data/cie/alevel/economics"
PAPER_TITLE = {
    "1": "Paper 1 AS Level Multiple Choice",
    "2": "Paper 2 AS Level Data Response and Essays",
    "3": "Paper 3 A Level Multiple Choice",
    "4": "Paper 4 A Level Data Response and Essays",
}
PAPER_MARKS = {"1": 30, "2": 60, "3": 30, "4": 60}


def parse(rel: str):
    """两套源都能解析：
        ① _new/…/2024-oct/9708_w24_qp_22.pdf        —— 年季在目录名
        ② CIE_Alevel_Economy/2025/冬季/9708_w25_qp_11.pdf —— 年季在文件名
    """
    p = None
    for root in SRC_ROOTS:
        if (root / rel).exists():
            p = root / rel
            break
    if p is None:
        p = SRC_ROOTS[0] / rel
    fn = p.name
    m = re.search(r"9708_([smw])(\d{2})_qp_(\d{2})", fn, re.I)
    if m:
        yr = 2000 + int(m.group(2))
        sess = {"s": "june", "w": "oct", "m": "mar"}[m.group(1).lower()]
        return yr, sess, f"qp{m.group(3)}", p
    m = re.match(r"^(\d{4})-(jan|mar|june|oct)$", p.parent.name, re.I)
    if not m:
        return None
    year, sess = int(m.group(1)), m.group(2).lower()
    # 老命名 CIE_2020_Jun_9708-22_qp.pdf（卷号在 qp 前）
    # 新命名 9708_s24_qp_41.pdf（卷号在 qp 后）
    m2 = re.search(r"9708[-_](\d{2})[-_]qp", fn, re.I)
    if m2:
        code = f"qp{m2.group(1)}"
    else:
        m3 = re.search(r"9708[-_][smw]\d{2}[-_]qp[-_]?(\d{2})", fn, re.I)
        if not m3:
            return None
        code = f"qp{m3.group(1)}"
    return year, sess, code, p


def duration(doc):
    t = doc[0].get_text()
    m = re.search(r"(\d+)\s*hours?(?:\s*(\d+)\s*minutes?)?", t)
    if not m:
        m2 = re.search(r"(\d+)\s*minutes?", t)
        return int(m2.group(1)) if m2 else 0
    return int(m.group(1)) * 60 + int(m.group(2) or 0)


def prep(rel: str) -> str:
    got = parse(rel)
    if not got:
        return f"SKIP 解析失败: {rel}"
    year, sess, code, src = got
    if not src.exists():
        return f"SKIP 源不存在: {rel}"
    paper_no = code[2]
    if paper_no not in PAPER_TITLE:
        return f"SKIP 卷号异常: {rel}"
    d = DST_ROOT / f"{year}-{sess}-{code}"
    if (d / "cut.json").exists():
        return f"SKIP 已有 cut.json: {d.name}"
    d.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, d / "source.pdf")
    doc = pymupdf.open(d / "source.pdf")
    dur = duration(doc) or {"1": 60, "2": 120, "3": 75, "4": 120}[paper_no]
    pages = doc.page_count
    doc.close()
    fm = [
        "---",
        f"id: cie-alevel-economics-{year}-{sess}-{code}",
        "board: cie",
        "qualification: alevel",
        "subject: economics",
        f"paper_code: {code}",
        f"paper_unit: 9708/{code[2:]}",
        f"year: {year}",
        f"session: {sess}",
        f'title: "{PAPER_TITLE[paper_no]}"',
        *([f"total_marks: {PAPER_MARKS[paper_no]}"] if paper_no in ("1", "3") else []),
        f"duration_minutes: {dur}",
        f"source_pdf: {src.name}",
        f"paper_ref: {src.stem}",
        "cut: cut.json",
        "language: en",
        "---",
        "",
        f"CIE 9708 · Economics · {PAPER_TITLE[paper_no]} · {sess} {year}",
        f"源文件 `{src.name}`（{pages} 页）。",
        "",
    ]
    (d / "paper.md").write_text("\n".join(fm), encoding="utf-8")
    return f"OK  {d.name}  ({pages}p, {dur}min)"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", default="")
    ap.add_argument("--pdf", default="")
    args = ap.parse_args()
    items = []
    if args.pdf:
        items = [args.pdf]
    elif args.list:
        items = [l.strip().replace("\\", "/") for l in
                 Path(args.list).read_text(encoding="utf-8").splitlines() if l.strip()]
    else:
        sys.exit("需要 --list 或 --pdf")
    ok = 0
    for it in items:
        r = prep(it)
        print(r)
        ok += r.startswith("OK")
    print(f"完成 {ok}/{len(items)}")


if __name__ == "__main__":
    main()
