#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""import_aqa_fmath.py — 把牛津 AQA 国际 A-Level **进阶数学**（9665）真题导入
data/aqa/a-level/further-mathematics/

口径（王培琪 2026-10-21 定）：**只收 2020-jan 及以后的卷**，只收 `qp-*` 一族的 QP，`ms-*` 不导入。

源文件名两族并存，且**同一份卷常被下两次**（2026-10-21 摸底：2023-june 3 对、2024-jan 5 对）：
    qp-202401-further+math-ufp1.pdf    ← 希腊字母是正常 Unicode（π θ μ α）
    qp-202401-furthermath-ufp1.pdf     ← 文字层是乱码（ʌ ș ȝ Į ȕ），渲染正常
按定论**优先取 `further+math` 版**，同 (考季, 单元) 只留一份。

单元（封面 `(9665/FM01) Unit FP1 …`）：
    FP1   → FM01  80 分 / 1h30   AS
    FPSM1 → FM02  80 分 / 1h30   AS
    FP2   → FM03 120 分 / 2h     A2
    FS2   → FM04  80 分 / 1h30   A2
    FM2   → FM05  80 分 / 1h30   A2

用法:
    python scripts/import_aqa_fmath.py --source downloadPapers/AQA_Alevel_Furthermath --dry-run
    python scripts/import_aqa_fmath.py --source ... --apply [--only 2024-jan-fm01]
"""

import argparse
import hashlib
import re
import sys
from collections import Counter
from pathlib import Path

import pymupdf

ROOT = Path(__file__).resolve().parent.parent
BASE = ROOT / "data" / "aqa" / "a-level" / "further-mathematics"

QP_RE = re.compile(r"^qp-(\d{6})-further(\+)?math-(uf\w+)\.pdf$", re.I)
MIN_YEAR = 2020

UNIT_TO_CODE = {"UFP1": "fm01", "UFPSM1": "fm02", "UFP2": "fm03",
                "UFS2": "fm04", "UFM2": "fm05"}
CODE_TO_UNIT = {v.upper(): k for k, v in UNIT_TO_CODE.items()}
UNIT_TITLE = {
    "UFP1": "Pure Mathematics",
    "UFPSM1": "Pure, Statistics and Mechanics",
    "UFP2": "Pure Mathematics",
    "UFS2": "Statistics",
    "UFM2": "Mechanics",
}
UNIT_KEY = {"UFP1": "FP1", "UFPSM1": "FPSM1", "UFP2": "FP2", "UFS2": "FS2", "UFM2": "FM2"}
UNIT_LEVEL = {"UFP1": "AS", "UFPSM1": "AS", "UFP2": "A2", "UFS2": "A2", "UFM2": "A2"}

MONTH_TOK = {"01": "jan", "05": "june", "06": "june"}
MONTHS = ["January", "February", "March", "April", "May", "June", "July",
          "August", "September", "October", "November", "December"]
DATE_RE = re.compile(
    r"(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\s+"
    r"(\d{1,2})\s+(" + "|".join(MONTHS) + r")\s+(\d{4})")
CODE_RE = re.compile(r"\((9665)/(FM0\d)\)")


def parse_qp(name):
    """qp-2401-further(+math)-ufp1.pdf → (yyyymm, UNIT, 是否 further+math)。"""
    m = QP_RE.match(name)
    if not m:
        return None
    yyyymm, plus, unit = m.group(1), bool(m.group(2)), m.group(3).upper()
    if unit not in UNIT_TO_CODE:
        return None
    return yyyymm, unit, plus


def collect(src: Path):
    """按 (考季, 单元) 去重，further+math 优先。返回 kept / 丢弃的冗余 / 2019 及以前 / 坏卷。"""
    kept, dup, skipped, bad = {}, [], [], []
    for p in sorted(src.glob("qp-*.pdf")):
        parsed = parse_qp(p.name)
        if parsed is None:
            continue
        yyyymm, unit, plus = parsed
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
        key = (yyyymm, unit)
        if key in kept:
            old_p, old_head, old_n, old_plus = kept[key]
            if plus and not old_plus:                # 新来的可读性更好 → 换掉旧的
                dup.append((old_p.name, p.name))
                kept[key] = (p, head, n, plus)
            else:
                dup.append((p.name, old_p.name))
            continue
        kept[key] = (p, head, n, plus)
    return kept, dup, skipped, bad


def extract_meta(name, head, pages):
    flat = re.sub(r"[ \t]+", " ", head)
    unit = parse_qp(name)[1]
    m = CODE_RE.search(flat)
    paper_code = m.group(2) if m else UNIT_TO_CODE[unit].upper()
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

    return dict(paper_code=paper_code, paper_unit=UNIT_KEY[unit], level=level,
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
    sid = f"aqa-a-level-further-mathematics-{sess}"
    rows = [
        ("id", sid), ("board", "aqa"), ("qualification", "a-level"),
        ("subject", "further-mathematics"), ("paper_code", meta["paper_code"]),
        ("paper_unit", meta["paper_unit"]),
        ("year", int(sess[:4])), ("session", sess.split("-")[1]),
        ("spec", "9665"), ("level", meta["level"]), ("title", meta["title"]),
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
    lines.append(f"Oxford AQA International A-Level · Further Mathematics (9665) · "
                 f"{meta['paper_code']} · Unit {meta['paper_unit']} {meta['title']}")
    lines.append(f"源文件 `{src_name}`（{meta['pages']} 页）。")
    return "\n".join(lines) + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", required=True)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--only", default=None)
    args = ap.parse_args()

    src = Path(args.source).resolve()
    if not src.is_dir():
        sys.exit(f"源目录不存在: {src}")

    kept, dup, skipped, bad = collect(src)
    print(f"源目录: {src}")
    print(f"2020+ 唯一卷 {len(kept)}；重复丢弃 {len(dup)}；跳过 2019 及以前 {len(skipped)}；坏卷 {len(bad)}\n")

    want = set(args.only.split(",")) if args.only else None
    rows = []
    for (yyyymm, unit), (p, head, pages, plus) in sorted(kept.items()):
        sess = session_name(yyyymm, unit)
        if want and sess not in want:
            continue
        rows.append((sess, p, extract_meta(p.name, head, pages)))

    print(f"{'会话目录':<22}{'卷号':<6}{'级':<4}{'总分':<5}{'日期':<12}{'页':<4}单元  源文件")
    print("-" * 108)
    for sess, p, meta in rows:
        print(f"{sess:<22}{meta['paper_code']:<6}{meta['level']:<4}"
              f"{str(meta['total_marks']):<5}{meta['exam_date']:<12}{meta['pages']:<4}"
              f"{meta['paper_unit']:<6}{p.name}")
    print(f"\n合计 {len(rows)} 卷；总分未提取 {len([r for r in rows if r[2]['total_marks'] is None])}")
    for name, why in bad:
        print(f"  ! 坏卷 {name}: {why}")
    for a, b in dup:
        print(f"  · 重复丢弃 {a}（保留 {b}）")

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
