#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""import_aqa_math.py — 把牛津 AQA 国际 A-Level 数学（9660）真题导入
data/aqa/a-level/mathematics/

口径（王培琪 2026-10-21 定）：
    · **只收 2020-jan 及以后的卷**（2019 两季不切）；
    · 只收 `AQA_Alevel_Math/9660_<YYMM>_<UNIT>.pdf` 这一族 QP，`_MS` 不导入；
    · 0 页坏卷（9660_2101_PSM1.pdf）跳过并报出来，不阻断整批。

单元（封面右上角卷码 + 文件名标记）：
    P1   → MA01   80 分 / 1h30   AS
    PSM1 → MA02   80 分 / 1h30   AS
    P2   → MA03  120 分 / 2h     A2
    S2   → MA04   80 分 / 1h30   A2
    M2   → MA05   80 分 / 1h30   A2

用法:
    python scripts/import_aqa_math.py --source downloadPapers/AQA_Alevel_Math --dry-run
    python scripts/import_aqa_math.py --source downloadPapers/AQA_Alevel_Math --apply
    python scripts/import_aqa_math.py --source ... --apply --only 2024-june-ma01
"""

import argparse
import hashlib
import re
import sys
from collections import Counter
from pathlib import Path

import pymupdf

ROOT = Path(__file__).resolve().parent.parent
BASE = ROOT / "data" / "aqa" / "a-level" / "mathematics"

QP_RE = re.compile(r"^9660_(\d{2})(\d{2})_([A-Za-z0-9]+)\.pdf$")
MIN_YEAR = 2020

UNIT_TO_CODE = {"P1": "ma01", "PSM1": "ma02", "P2": "ma03",
                "S2": "ma04", "M2": "ma05"}
UNIT_TITLE = {
    "P1": "Pure Mathematics",
    "PSM1": "Pure, Statistics and Mechanics",
    "P2": "Pure Mathematics",
    "S2": "Statistics",
    "M2": "Mechanics",
}
UNIT_LEVEL = {"P1": "AS", "PSM1": "AS", "P2": "A2", "S2": "A2", "M2": "A2"}

MONTH_TOK = {"01": "jan", "05": "june", "06": "june"}
MONTHS = ["January", "February", "March", "April", "May", "June", "July",
          "August", "September", "October", "November", "December"]
DATE_RE = re.compile(
    r"(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\s+"
    r"(\d{1,2})\s+(" + "|".join(MONTHS) + r")\s+(\d{4})")
CODE_RE = re.compile(r"\((9660)/(MA0\d)\)")


def parse_qp(name):
    """9660_2406_P1.pdf → (yyyymm, UNIT)。_MS 与其它族返回 None。"""
    if name.upper().endswith("_MS.PDF"):
        return None
    m = QP_RE.match(name)
    if not m:
        return None
    yy, mm, unit = m.group(1), m.group(2), m.group(3).upper()
    if unit not in UNIT_TO_CODE:
        return None
    return f"20{yy}{mm}", unit


def collect(src: Path):
    found, skipped, bad = {}, [], []
    for p in sorted(src.glob("*.pdf")):
        parsed = parse_qp(p.name)
        if parsed is None:
            continue
        yyyymm, unit = parsed
        if int(yyyymm[:4]) < MIN_YEAR:
            skipped.append(p.name)
            continue
        try:
            doc = pymupdf.open(p)
            n = doc.page_count
            if n == 0:
                bad.append((p.name, "0 页（文件损坏）"))
                continue
            head = doc[0].get_text()
            doc.close()
        except Exception as e:                                   # noqa: BLE001
            bad.append((p.name, f"打不开：{e}"))
            continue
        found[f"{yyyymm}-{unit}"] = (p, head, n)
    return found, skipped, bad


def extract_meta(name, head, pages):
    flat = re.sub(r"[ \t]+", " ", head)
    m = CODE_RE.search(flat)
    paper_code = m.group(2) if m else ""
    if not paper_code:                       # 2019 版封面只有角标水印
        z = re.findall(r"\*([A-Za-z]{3}\d{2}(ma0\d)\d{2})\*", flat, re.I)
        if z:
            paper_code = z[0][1].upper()
    unit = parse_qp(name)[1]
    if not paper_code:
        paper_code = UNIT_TO_CODE[unit].upper()

    level = "AS" if "INTERNATIONAL AS" in flat else (
        "A2" if "INTERNATIONAL A-LEVEL" in flat else "")

    total_marks = None
    m = re.search(r"maximum mark for this paper is (\d+)", flat, re.I)
    if m:
        total_marks = int(m.group(1))

    exam_date = ""
    m = DATE_RE.search(flat)
    if m:
        exam_date = f"{m.group(4)}-{MONTHS.index(m.group(3)) + 1:02d}-{int(m.group(2)):02d}"

    exam_time = ""
    m = re.search(r"Time allowed:\s*([^\n]+)", flat)
    if m:
        exam_time = re.sub(r"\s+", " ", m.group(1)).strip()

    paper_ref = ""
    z = re.findall(r"\*([^*\n]{4,24})\*", flat)
    if z:
        paper_ref = Counter(z).most_common(1)[0][0].upper()

    return dict(paper_code=paper_code, paper_unit=unit, level=level,
                title=UNIT_TITLE[unit], total_marks=total_marks,
                exam_date=exam_date, exam_time=exam_time,
                paper_ref=paper_ref, pages=pages)


def session_name(yyyymm, unit):
    year, mm = yyyymm[:4], yyyymm[4:]
    if mm not in MONTH_TOK:
        raise ValueError(f"未知考季月份 {mm}")
    return f"{year}-{MONTH_TOK[mm]}-{UNIT_TO_CODE[unit]}"


def yaml_scalar(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    s = str(v)
    if s == "" or re.search(r"[:#\[\]{}&*!|>'\"%@`]|^\s|\s$|^[-?]", s):
        return '"' + s.replace('"', '\\"') + '"'
    return s


def paper_md(sess, meta, src_name):
    sid = f"aqa-a-level-mathematics-{sess}"
    rows = [
        ("id", sid), ("board", "aqa"), ("qualification", "a-level"),
        ("subject", "mathematics"), ("paper_code", meta["paper_code"]),
        ("paper_unit", meta["paper_unit"]),
        ("year", int(sess[:4])), ("session", sess.split("-")[1]),
        ("spec", "9660"), ("level", meta["level"]), ("title", meta["title"]),
        ("exam_date", meta["exam_date"]), ("exam_time", meta["exam_time"]),
        ("total_marks", meta["total_marks"]), ("source_pdf", src_name),
        ("paper_ref", meta["paper_ref"]), ("cut", "cut.json"),
        ("language", "en"),
    ]
    lines = ["---"]
    for k, v in rows:
        if v is None or v == "":
            continue
        lines.append(f"{k}: {yaml_scalar(v)}")
    lines += ["---", ""]
    lines.append(f"Oxford AQA International A-Level · Mathematics (9660) · "
                 f"{meta['paper_code']} · Unit {meta['paper_unit']} {meta['title']}")
    lines.append(f"源文件 `{src_name}`（{meta['pages']} 页）。")
    return "\n".join(lines) + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", required=True)
    ap.add_argument("--apply", action="store_true", help="真正落盘")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--only", default=None, help="逗号分隔的目录名过滤")
    args = ap.parse_args()

    src = Path(args.source).resolve()
    if not src.is_dir():
        sys.exit(f"源目录不存在: {src}")

    found, skipped, bad = collect(src)
    print(f"源目录: {src}")
    print(f"收集到 9660 QP（2020+）{len(found)} 卷；"
          f"跳过 2019 及以前 {len(skipped)} 份；坏卷 {len(bad)} 份\n")

    want = set(args.only.split(",")) if args.only else None
    rows = []
    for key, (p, head, pages) in sorted(found.items()):
        yyyymm, unit = key.split("-")
        try:
            sess = session_name(yyyymm, unit)
        except ValueError as e:
            print(f"  ! {p.name}: {e}")
            continue
        if want and sess not in want:
            continue
        meta = extract_meta(p.name, head, pages)
        rows.append((sess, p, meta))

    print(f"{'会话目录':<22}{'卷号':<6}{'级':<4}{'总分':<5}{'日期':<12}{'页':<4}单元")
    print("-" * 82)
    for sess, p, meta in rows:
        print(f"{sess:<22}{meta['paper_code']:<6}{meta['level']:<4}"
              f"{str(meta['total_marks']):<5}{meta['exam_date']:<12}"
              f"{meta['pages']:<4}{meta['paper_unit']} {meta['title']}")
    miss = [r for r in rows if r[2]["total_marks"] is None]
    print(f"\n合计 {len(rows)} 卷；总分未提取 {len(miss)}；"
          f"卷号未提取 {len([r for r in rows if not r[2]['paper_code']])}")
    for name, why in bad:
        print(f"  ! 坏卷 {name}: {why}")

    if not args.apply:
        print("\n（dry-run，未落盘）")
        return

    n_new = n_same = 0
    for sess, p, meta in rows:
        d = BASE / sess
        d.mkdir(parents=True, exist_ok=True)
        dst = d / "source.pdf"
        sha = hashlib.sha256(p.read_bytes()).hexdigest()
        if dst.exists():
            if hashlib.sha256(dst.read_bytes()).hexdigest() == sha:
                n_same += 1
            else:
                (d / "source.pdf.bak").write_bytes(dst.read_bytes())
                dst.write_bytes(p.read_bytes())
        else:
            dst.write_bytes(p.read_bytes())
            n_new += 1
        (d / "paper.md").write_text(paper_md(sess, meta, p.name), encoding="utf-8")
    print(f"\n落盘完成：新建 {n_new} 卷，已存在且一致 {n_same} 卷 → {BASE}")


main()
