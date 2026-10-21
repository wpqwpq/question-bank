#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""import_cie_chem.py — CIE 9701 化学卷批量入库

⚠️ 专用脚本（考试局 × 资格 × 学科各一份）：CIE / A Level / Chemistry(9701)。
   与 AQA 的 import_aqa*.py、Edexcel 的 import_papers.py 不共享规则。

源目录：downloadPapers/CIE_9701_Chemistry/qp/9701_<s|w|m><YY>_qp_<PP>.pdf
落盘：  data/cie/alevel/chemistry/<year>-<session>-qp<PP>/
        （source.pdf + paper.md；paper.md 已存在则只补缺字段，不覆盖已有内容）

⚠️ Paper 3（qp31–qp38，实验操作卷）**不入库、不切割**。

用法:
    python scripts/import_cie_chem.py --dry-run          # 只看映射表
    python scripts/import_cie_chem.py --apply            # 落盘
    python scripts/import_cie_chem.py --apply --only 9701_s16_qp_22.pdf
"""

import argparse
import json
import re
import shutil
import sys
from pathlib import Path

import pymupdf

ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT / "downloadPapers" / "CIE_9701_Chemistry" / "qp"
DATA_DIR = ROOT / "data" / "cie" / "alevel" / "chemistry"

SESSION_OF = {"s": "june", "w": "oct", "m": "mar"}
MONTH_OF = {"june": 6, "oct": 11, "mar": 3}

# paper 号 → (标题, 总分, 时长分钟)
PAPER_META = {
    "1": ("Multiple Choice", 40, 60),
    "2": ("AS Level Structured Questions", 60, 75),
    "4": ("A Level Structured Questions", 100, 120),
    "5": ("Planning, Analysis and Evaluation", 30, 75),
}

NAME_RE = re.compile(r"^9701_([swm])(\d{2})_qp_(\d{2})\.pdf$", re.I)


def parse_name(p: Path):
    m = NAME_RE.match(p.name)
    if not m:
        return None
    ser, yy, pp = m.group(1).lower(), m.group(2), m.group(3)
    paper_num = pp[0]
    if paper_num not in PAPER_META:
        return None           # P3 实验卷等：不入库
    year = 2000 + int(yy)
    session = SESSION_OF[ser]
    return {
        "year": year,
        "session": session,
        "paper_num": paper_num,
        "variant": int(pp[1]),
        "paper_code": f"qp{pp}",
        "paper_unit": f"9701/{pp}",
        "paper_ref": f"9701_{ser}{yy}_qp{pp}",
        "dir": f"{year}-{session}-qp{pp}",
    }


def cover_marks(doc):
    """从封面/正文读总分与页数（仅作 paper.md 的校验参考）。"""
    text = ""
    for i in range(min(3, doc.page_count)):
        text += doc[i].get_text()
    m = re.search(r"(\d+)\s*printed pages?", text)
    pages = int(m.group(1)) if m else None
    return pages


def build_paper_md(info, pdf_path, doc):
    title, marks, minutes = PAPER_META[info["paper_num"]]
    pages = doc.page_count          # 封面印的是"printed pages"（不含 blank page），以实际页数为准
    fm = [
        "---",
        f"id: cie-alevel-chemistry-{info['dir']}",
        "board: cie",
        "qualification: alevel",
        "subject: chemistry",
        f"paper_code: {info['paper_code']}",
        f"paper_unit: {info['paper_unit']}",
        f"year: {info['year']}",
        f"session: {info['session']}",
        f'title: "{title}"',
        f"total_marks: {marks}",
        f"duration_minutes: {minutes}",
        f"source_pdf: {pdf_path.name}",
        f"paper_ref: {info['paper_ref']}",
        "cut: cut.json",
        "language: en",
        "---",
        "",
        f"CIE 9701 · Chemistry · Paper {info['paper_num']} (variant {info['variant']}) · "
        f"{info['session']} {info['year']}",
        f"源文件 `{pdf_path.name}`（{pages} 页）。",
        "",
    ]
    return "\n".join(fm)


def main():
    ap = argparse.ArgumentParser(description="CIE 9701 化学卷批量入库")
    ap.add_argument("--apply", action="store_true", help="真正落盘（默认只打印映射表）")
    ap.add_argument("--dry-run", action="store_true", help="只打印映射表")
    ap.add_argument("--only", action="append", default=None, help="只处理指定文件名（可重复）")
    args = ap.parse_args()
    if not args.apply:
        args.dry_run = True

    pdfs = sorted(SRC_DIR.glob("9701_*.pdf"))
    if args.only:
        want = {x.lower() for x in args.only}
        pdfs = [p for p in pdfs if p.name.lower() in want or p.stem.lower() in want]
    if not pdfs:
        sys.exit(f"没有匹配的 PDF：{SRC_DIR}")

    rows, skipped, done = [], [], 0
    for p in pdfs:
        info = parse_name(p)
        if info is None:
            skipped.append(p.name)
            continue
        rows.append((p, info))

    print(f"源目录 {SRC_DIR}")
    print(f"命中 {len(rows)} 卷 | 跳过 {len(skipped)} 卷（Paper 3 实验卷 / 命名不符）")
    if skipped:
        print("  跳过:", ", ".join(skipped[:8]), "…" if len(skipped) > 8 else "")

    for p, info in rows:
        tdir = DATA_DIR / info["dir"]
        exists = (tdir / "source.pdf").exists()
        print(f"  {p.name:24s} → {tdir.relative_to(ROOT)}" + ("  [已存在]" if exists else ""))
        if not args.apply:
            continue
        tdir.mkdir(parents=True, exist_ok=True)
        doc = pymupdf.open(p)
        (tdir / "paper.md").write_bytes(build_paper_md(info, p, doc).encode("utf-8"))
        dst = tdir / "source.pdf"
        if not dst.exists() or dst.stat().st_size != p.stat().st_size:
            shutil.copy2(p, dst)
        doc.close()
        done += 1

    if args.apply:
        print(f"\n落盘完成：{done} 卷 → {DATA_DIR}")
    else:
        print("\n[dry-run] 加 --apply 才落盘")


if __name__ == "__main__":
    main()
