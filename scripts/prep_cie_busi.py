#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""prep_cie_busi.py — 把 CIE 9609 商务源 PDF 铺成 data/cie/alevel/business/<year>-<session>-qpNN/

只做目录与元数据，不切割。接受显式清单（--list）或单份（--pdf），绝不扫全量自动开工。

源目录：downloadPapers/CIE_ALevel_Business/ ，文件名已归一为
        9609_<s|m|w><yy>_qp_<paper><variant>.pdf

P3（Business Decision-Making / 旧 Case Study）**必须**配 INSERT：案例在独立材料册里，
不并入 source.pdf 的话题面全是「Refer to lines 69–70」而无处可查。
并入方式与 9706 会计一致：接在 QP 尾部，front-matter 记 `insert_start_page`。
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
SRC_ROOT = ROOT / "downloadPapers/CIE_ALevel_Business"
DST_ROOT = ROOT / "data/cie/alevel/business"

SESS = {"s": "june", "m": "march", "w": "oct"}
PAPER_TITLE = {
    "1": "Paper 1 Business Concepts 1",
    "2": "Paper 2 Business Concepts 2",
    "3": "Paper 3 Business Decision-Making",
    "4": "Paper 4 Business Strategy",
}
PAPER_MARKS = {"1": 40, "2": 60, "3": 60, "4": 40}
PAPER_MINUTES = {"1": 75, "2": 90, "3": 105, "4": 75}


def parse(name: str):
    m = re.match(r"9609_([smw])(\d{2})_qp_(\d)(\d)\.pdf$", name, re.I)
    if not m:
        return None
    s, yy, paper, var = m.groups()
    return dict(year=2000 + int(yy), session=SESS[s.lower()],
                paper=paper, variant=var,
                code="qp%s%s" % (paper, var))


def cover_info(doc, paper: str):
    """从封面读真实卷名 / 总分 / 时长 / 是否选做（各年改版口径不同，别写死）。"""
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
    minutes = (int(m.group(1)) * 60 + (int(m.group(2)) if m.group(2) else 0)) if m else PAPER_MINUTES[paper]
    # 选做卷：Section B 只做一题（P1 全季、P3 2020–2022）→ 不能写 total_marks
    choose_one = bool(re.search(r"answer\s+one\s+question", text, re.I))
    return title, marks, minutes, choose_one


def _is_red(col):
    if col is None:
        return False
    try:
        return col[0] > 0.55 and col[1] < 0.35 and col[2] < 0.35
    except Exception:
        return False


def clean_red_marks(pdf: Path):
    """涂掉印前核对漏删的**红色批注**（框 / 箭头 / 说明文字），返回被处理的页号列表。

    CIE 偶尔把给排版看的 red proofing note 印进正式卷（2022-june/9609_s22_qp_22 p2：
    红框里写着 "Please ensure text is fully justified throughout."，还盖住了 (a)(i) 行）。
    那是给编辑看的话，不能进题图，留着还会把切割边界拽歪。
    """
    doc = pymupdf.open(pdf)
    touched = []
    for pi, pg in enumerate(doc):
        rects = [pymupdf.Rect(d["rect"]) for d in pg.get_drawings()
                 if _is_red(d.get("color")) or _is_red(d.get("fill"))]
        if not rects:
            continue
        for rc in rects:
            # fill=False：只删内容，不额外画白框——画了会留下不可见的白色矢量块，
            # audit_cut_edges 仍把它当图形，边界照样报冲突
            pg.add_redact_annot(rc, fill=False)
        pg.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE,
                            graphics=pymupdf.PDF_REDACT_LINE_ART_REMOVE_IF_TOUCHED)
        touched.append(pi + 1)
    if touched:
        tmp = pdf.with_name("source.clean.tmp.pdf")
        doc.save(str(tmp), deflate=True, garbage=3)
        doc.close()
        tmp.replace(pdf)
    else:
        doc.close()
    return touched


def merge_insert(src_qp: Path, pdf: Path):
    """有 INSERT 就接到 source.pdf 尾部，返回 (插页起始页, 插页页数)；无插页 (None, 0)。"""
    ins = src_qp.with_name(src_qp.name.replace("_qp_", "_in_"))
    if not ins.exists():
        return None, 0
    qp = pymupdf.open(pdf)
    n_qp = qp.page_count
    insd = pymupdf.open(ins)
    n_in = insd.page_count
    qp.insert_pdf(insd)
    tmp = pdf.with_name("source.merged.tmp.pdf")
    qp.save(str(tmp), deflate=True, garbage=3)
    qp.close()
    insd.close()
    tmp.replace(pdf)
    return n_qp, n_in


def prep(pdf: Path) -> str:
    info = parse(pdf.name)
    if not info:
        sys.exit("文件名不合约定: %s" % pdf.name)
    paper = info["paper"]
    if paper not in PAPER_TITLE:
        sys.exit("P%s 未知: %s" % (paper, pdf.name))

    sdir = DST_ROOT / ("%d-%s-%s" % (info["year"], info["session"], info["code"]))
    sdir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(pdf, sdir / "source.pdf")

    doc = pymupdf.open(pdf)
    pages = doc.page_count
    title, marks, minutes, choose_one = cover_info(doc, paper)
    doc.close()

    ins_start, ins_pages = merge_insert(pdf, sdir / "source.pdf")
    red_pages = clean_red_marks(sdir / "source.pdf")

    pid = "cie-alevel-business-%d-%s-%s" % (info["year"], info["session"], info["code"])
    fm = [
        "---",
        "id: %s" % pid,
        "board: cie",
        "qualification: alevel",
        "subject: business",
        "paper_code: %s" % info["code"],
        "paper_unit: 9609/%s%s" % (paper, info["variant"]),
        "year: %d" % info["year"],
        "session: %s" % info["session"],
        'title: "%s"' % title,
    ]
    if choose_one:
        # 选做卷：官方写的总分包含「都做」的情况，与逐题加总对不上 → 只记录不校验
        fm.append("printed_total_marks: %d" % marks)
    else:
        fm.append("total_marks: %d" % marks)
    fm += [
        "duration_minutes: %d" % minutes,
        "source_pdf: %s" % pdf.name,
        "paper_ref: %s" % pdf.stem,
        "cut: cut.json",
        "language: en",
    ]
    if ins_start is not None:
        fm.append("insert_start_page: %d" % ins_start)
        fm.append("insert_pages: %d" % ins_pages)
    if red_pages:
        fm.append("redacted_pages: [%s]" % ", ".join(str(p) for p in red_pages))
    fm += [
        "---",
        "",
        "CIE 9609 · Business · %s · %s %d" % (title, info["session"], info["year"]),
        "源文件 `%s`（%d 页）%s。" % (
            pdf.name, pages,
            "，另附 INSERT %d 页已并入 source.pdf 尾部" % ins_pages if ins_start is not None else ""),
    ]
    if choose_one:
        fm.append("本卷为**选做卷**（Section B 只做一题），总分记 `printed_total_marks`，逐题分值不做合计校验。")
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
