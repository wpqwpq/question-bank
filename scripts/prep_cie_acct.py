#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""prep_cie_acct.py — 把 CIE 9706 源 PDF 铺成 data/cie/alevel/accounting/<year>-<session>-qpNN/

只做目录与元数据，不切割。接受显式清单（--list）或单份（--pdf），绝不扫全量自动开工。

源目录：downloadPapers/CIE_ALevel_Accounting/ ，文件名已归一为
        9706_<s|m|w><yy>_qp_<paper><variant>.pdf
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
SRC_ROOT = ROOT / "downloadPapers/CIE_ALevel_Accounting"
DST_ROOT = ROOT / "data/cie/alevel/accounting"

SESS = {"s": "june", "m": "march", "w": "oct"}
# 官方卷名/分值/时长（从封面读的，2026-10-22 核准）
PAPER_TITLE = {
    "1": "Paper 1 Multiple Choice",
    "2": "Paper 2 Fundamentals of Accounting",
    "3": "Paper 3 Structured Questions",
    "4": "Paper 4 Cost and Management Accounting",
}
# 兜底值；实际以封面文字为准（P3 在 2023 改版：150 分/3 小时 → 75 分/1.5 小时；
# P2 卷名也从 Structured Questions 改成 Fundamentals of Accounting，别写死）
PAPER_MARKS = {"1": 30, "2": 90, "3": 75, "4": 50}
PAPER_MINUTES = {"1": 60, "2": 105, "3": 90, "4": 60}


def parse(name: str):
    m = re.match(r"9706_([smw])(\d{2})_qp_(\d)(\d)\.pdf$", name, re.I)
    if not m:
        return None
    s, yy, paper, var = m.groups()
    return dict(year=2000 + int(yy), session=SESS[s.lower()],
                paper=paper, variant=var,
                code="qp%s%s" % (paper, var))


def cover_info(doc, paper: str):
    """从封面读真实卷名 / 总分 / 时长（各年改版口径不同，别依赖静态表）。"""
    text = doc[0].get_text()
    title = PAPER_TITLE[paper]
    for line in text.splitlines():
        s = line.strip()
        if re.match(r"^Paper\s+%s\b" % paper, s):
            title = s
            break
    m = re.search(r"The total mark for this paper is (\d+)", text)
    marks = int(m.group(1)) if m else PAPER_MARKS[paper]
    m = re.search(r"(\d+)\s*hour(?:s)?\s*(?:(\d+)\s*minutes)?", text)
    if m:
        minutes = int(m.group(1)) * 60 + (int(m.group(2)) if m.group(2) else 0)
    else:
        minutes = PAPER_MINUTES[paper]
    return title, marks, minutes


def merge_insert(src_qp: Path, pdf: Path, paper: str):
    """P3/P4 把 INSERT 插页接到 source.pdf 尾部，返回 (插页起始页, 插页页数)；无插页返回 (None, 0)。

    插进同一个 source.pdf 是为了让渲染器只读一个 PDF（cut.json 的 page 是文件内绝对页号）。
    """
    if paper not in ("3", "4"):
        return None, 0
    ins = src_qp.with_name(src_qp.name.replace("_qp_", "_in_"))
    if not ins.exists():
        return None, 0
    qp = pymupdf.open(pdf)
    n_qp = qp.page_count
    insd = pymupdf.open(ins)
    n_in = insd.page_count
    qp.insert_pdf(insd)
    tmp = pdf.with_name("source.merged.tmp.pdf")
    qp.save(str(tmp), deflate=True, garbage=3)   # 不能直接覆盖已打开的源文件
    qp.close(); insd.close()
    tmp.replace(pdf)
    return n_qp, n_in


def prep(pdf: Path) -> str:
    info = parse(pdf.name)
    if not info:
        sys.exit("文件名不合约定: %s" % pdf.name)
    paper = info["paper"]
    if paper not in PAPER_MARKS:
        sys.exit("P%s 尚未建元数据（先做 P1）: %s" % (paper, pdf.name))

    sdir = DST_ROOT / ("%d-%s-%s" % (info["year"], info["session"], info["code"]))
    sdir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(pdf, sdir / "source.pdf")

    doc = pymupdf.open(pdf)
    pages = doc.page_count
    title, marks, minutes = cover_info(doc, paper)
    doc.close()

    ins_start, ins_pages = merge_insert(pdf, sdir / "source.pdf", paper)

    pid = "cie-alevel-accounting-%d-%s-%s" % (info["year"], info["session"], info["code"])
    fm = [
        "---",
        "id: %s" % pid,
        "board: cie",
        "qualification: alevel",
        "subject: accounting",
        "paper_code: %s" % info["code"],
        "paper_unit: 9706/%s%s" % (paper, info["variant"]),
        "year: %d" % info["year"],
        "session: %s" % info["session"],
        'title: "%s"' % title,
        "total_marks: %d" % marks,
        "duration_minutes: %d" % minutes,
        "source_pdf: %s" % pdf.name,
        "paper_ref: %s" % pdf.stem,
        "cut: cut.json",
        "language: en",
    ]
    if ins_start is not None:
        fm.append("insert_start_page: %d" % ins_start)
        fm.append("insert_pages: %d" % ins_pages)
    fm += [
        "---",
        "",
        "CIE 9706 · Accounting · %s · %s %d" % (title, info["session"], info["year"]),
        "源文件 `%s`（%d 页）%s。" % (
            pdf.name, pages,
            "，另附 INSERT %d 页已并入 source.pdf 尾部" % ins_pages if ins_start is not None else ""),
    ]
    (sdir / "paper.md").write_text("\n".join(fm) + "\n", encoding="utf-8")
    return str(sdir)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", default="", help="文件名清单（相对源目录），一行一个")
    ap.add_argument("--pdf", default="", help="单个文件名")
    args = ap.parse_args()
    names = []
    if args.list:
        names = [l.strip() for l in Path(args.list).read_text(encoding="utf-8").splitlines()
                 if l.strip() and not l.startswith("#")]
    elif args.pdf:
        names = [args.pdf]
    else:
        sys.exit("必须给 --list 或 --pdf（不扫全量）")
    for n in names:
        p = SRC_ROOT / n
        if not p.exists():
            print("MISSING", p)
            continue
        print("prep", prep(p))


if __name__ == "__main__":
    main()
