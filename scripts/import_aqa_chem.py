#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""import_aqa_chem.py — 把牛津 AQA 国际 A-Level 化学真题导入 data/aqa/a-level/chemistry/

⚠️ 与 import_aqa.py（物理）**各自独立**：卷号体系、封面措辞、单元标题都不同，
   共享的只有「data/ 目录规范」这一层。按项目定下的规矩，每个考试局/资格/学科
   各用一份脚本，改动互不牵连（物理那份一行不动）。

用法:
    python scripts/import_aqa_chem.py --source downloadPapers/OxfordAQA_Chemistry --dry-run
    python scripts/import_aqa_chem.py --source ... --apply
    python scripts/import_aqa_chem.py --source ... --apply --only 2025-june-ch01,2025-june-ch02

规则:
    · 收集 `qp-<YYYYMM>-chemistry-u<N>.pdf`（`ms-…` 评分标准不匹配即排除）；
    · 考季：月份 01 → jan，05/06 → june；
    · 卷号：u1..u5 → CH01..CH05；
    · 目录：data/aqa/a-level/chemistry/<year>-<session>-<ch0N>/；
    · 卷级元数据（单元标题 / 考试日期 / 时长 / 总分 / 卷号水印）从封面文本提取；
    · 源文件内容变了 → 旧 cut 产物挪 `_prev/`（收工须移出 data/）。
"""

import argparse
import csv as _csv
import hashlib
import os
import re
import sys
from collections import Counter
from pathlib import Path

import pymupdf

ROOT = Path(__file__).resolve().parent.parent
BASE = ROOT / "data" / "aqa" / "a-level" / "chemistry"

QP_RE = re.compile(r"^qp-(\d{6})-chemistry-u(\d)\.pdf$", re.I)
UNIT_CODE = {1: "ch01", 2: "ch02", 3: "ch03", 4: "ch04", 5: "ch05"}
MONTH_TOK = {"01": "jan", "05": "june", "06": "june"}

MONTHS = ["January", "February", "March", "April", "May", "June", "July",
          "August", "September", "October", "November", "December"]
DATE_RE = re.compile(
    r"(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\s+"
    r"(\d{1,2})\s+(" + "|".join(MONTHS) + r")\s+(\d{4})")


def parse_qp(name):
    m = QP_RE.match(name)
    if not m:
        return None
    return m.group(1), int(m.group(2))


def collect(src):
    found = {}
    for root, _dirs, files in os.walk(src):
        for f in files:
            if parse_qp(f) is None:
                continue
            p = Path(root) / f
            found[p.relative_to(src).as_posix()] = p
    return sorted(found.items())


def extract_meta(path):
    """封面页 → 卷级元数据。

    ⚠️ 化学封面与物理有别：卷号 `CH01` 单独一行（物理是 PH0N），单元标题写作
    `Unit 1:  Inorganic 1 and Physical 1`（带冒号、双空格），总分句为
    `The maximum mark for this paper is 70.`。
    """
    doc = pymupdf.open(path)
    pages = doc.page_count
    head = doc[0].get_text()
    doc.close()
    flat = re.sub(r"[ \t]+", " ", head)

    paper_code = ""
    m = re.search(r"^CH0(\d)\s*$", flat, re.M)
    if m:
        paper_code = f"CH0{m.group(1)}"

    title = ""
    m = re.search(r"Unit\s*(\d)\s*[:\u2236]?\s*([^\n]+)", flat)
    if m:
        t = re.sub(r"\s+", " ", m.group(2)).strip()
        t = t.replace("  ", " ")
        title = f"Unit {m.group(1)}: {t}"

    total_marks = None
    m = re.search(r"maximum mark for this paper is (\d+)", flat, re.I)
    if m:
        total_marks = int(m.group(1))

    exam_date = ""
    m = DATE_RE.search(flat)
    if m:
        exam_date = f"{m.group(4)}-{MONTHS.index(m.group(3)) + 1:02d}-{int(m.group(2)):02d}"
    if not exam_date:
        parsed = parse_qp(path.name)
        if parsed:
            exam_date = f"{parsed[0][:4]}-{parsed[0][4:]}"

    exam_time = ""
    m = re.search(r"Time allowed:\s*([^\n]+)", flat)
    if m:
        exam_time = re.sub(r"\s+", " ", m.group(1)).strip()

    paper_ref = ""
    m = re.search(r"\*([A-Za-z]*\d{2}CH0\d{2})\*", flat)
    if m:
        paper_ref = m.group(1).upper()
    else:
        z = re.findall(r"\*([^*\n]{4,20})\*", flat)
        if z:
            paper_ref = Counter(z).most_common(1)[0][0]

    # 封面上「INTERNATIONAL AS / A-LEVEL」决定资格层级（U1–U3 = AS，U4–U5 = A2）
    level = "AS" if "INTERNATIONAL AS" in flat else ("A2" if "INTERNATIONAL A-LEVEL" in flat else "")

    return dict(paper_code=paper_code, title=title, total_marks=total_marks,
                exam_date=exam_date, exam_time=exam_time, paper_ref=paper_ref,
                pages=pages, level=level)


def session_name(rel):
    parsed = parse_qp(Path(rel).name)
    if not parsed:
        raise ValueError(f"文件名不符命名规则: {rel}")
    yyyymm, unit = parsed
    year, mm = yyyymm[:4], yyyymm[4:]
    if mm not in MONTH_TOK:
        raise ValueError(f"未知考季月份代码 {mm}（{rel}）")
    if unit not in UNIT_CODE:
        raise ValueError(f"未知单元号 u{unit}（{rel}）")
    return f"{year}-{MONTH_TOK[mm]}-{UNIT_CODE[unit]}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", required=True)
    ap.add_argument("--apply", action="store_true", help="真正落盘（建目录+拷 PDF+写 paper.md）")
    ap.add_argument("--meta-only", action="store_true", help="只重写 paper.md")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--only", default=None, help="逗号分隔的 session 目录名过滤")
    ap.add_argument("--csv", default=None)
    args = ap.parse_args()

    src = Path(args.source).resolve()
    if not src.is_dir():
        sys.exit(f"源目录不存在: {src}")
    files = collect(src)
    print(f"源目录: {src}")
    print(f"收集到 qp 卷 {len(files)} 份\n")

    want = set(args.only.split(",")) if args.only else None
    rows, seen, problems = [], {}, []
    for rel, p in files:
        try:
            meta = extract_meta(p)
            sess = session_name(rel)
        except Exception as e:
            problems.append((rel, str(e)))
            continue
        if want and sess not in want:
            continue
        if sess in seen:
            problems.append((rel, f"目录名冲突，已被 {seen[sess]} 占用"))
            continue
        seen[sess] = rel
        sha = hashlib.sha256(p.read_bytes()).hexdigest()
        rows.append((sess, rel, meta, sha))

    rows.sort()
    print(f"{'会话目录':<22}{'卷号':<6}{'级':<4}{'总分':<5}{'日期':<12}{'页':<4}标题")
    print("-" * 110)
    for sess, rel, meta, _ in rows:
        print(f"{sess:<22}{str(meta['paper_code']):<6}{meta['level']:<4}"
              f"{str(meta['total_marks']):<5}{meta['exam_date']:<12}{meta['pages']:<4}{meta['title']}")
    miss = [r for r in rows if r[2]["total_marks"] is None]
    print(f"\n合计 {len(rows)} 份；总分未提取 {len(miss)}；"
          f"日期未提取 {len([r for r in rows if not r[2]['exam_date']])}；"
          f"卷号未提取 {len([r for r in rows if not r[2]['paper_code']])}")
    for rel, why in problems:
        print(f"  ! PROBLEM {rel}: {why}")

    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8-sig") as fh:
            w = _csv.writer(fh)
            w.writerow(["session_dir", "src_rel", "paper_code", "level", "title", "total_marks",
                        "exam_date", "exam_time", "paper_ref", "pages", "sha256"])
            for sess, rel, meta, sha in rows:
                w.writerow([sess, rel, meta["paper_code"], meta["level"], meta["title"],
                            meta["total_marks"], meta["exam_date"], meta["exam_time"],
                            meta["paper_ref"], meta["pages"], sha])
        print(f"\n映射表已写入 {args.csv}")

    if args.apply or args.meta_only:
        meta_only = args.meta_only and not args.apply
        n_new = n_same = n_backup = 0
        for sess, rel, meta, sha in rows:
            d = BASE / sess
            d.mkdir(parents=True, exist_ok=True)
            dst = d / "source.pdf"
            if meta_only:
                pass
            elif dst.exists():
                if hashlib.sha256(dst.read_bytes()).hexdigest() == sha:
                    n_same += 1
                else:
                    prev = d / "_prev"
                    prev.mkdir(exist_ok=True)
                    for item in list(d.iterdir()):
                        if item.name in ("_prev", "paper.md"):
                            continue
                        if item.name == "assets":
                            (prev / "assets").mkdir(exist_ok=True)
                            for f in item.iterdir():
                                os.replace(f, prev / "assets" / f.name)
                        else:
                            os.replace(item, prev / item.name)
                    n_backup += 1
            else:
                n_new += 1
            if not meta_only:
                dst.write_bytes((src / rel).read_bytes())
            lines = [
                "---",
                f"id: aqa-a-level-chemistry-{sess}",
                "board: aqa",
                "qualification: a-level",
                "subject: chemistry",
                f"paper_code: {meta['paper_code']}",
                f"paper_unit: {meta['paper_code']}",
                f"year: {sess.split('-')[0]}",
                f"session: {sess.split('-')[1]}",
                f'spec: "9620"',
                f'level: {meta["level"] or "AS"}',
                f'title: "{meta["title"]}"',
                (f"exam_date: {meta['exam_date']}" if meta["exam_date"] else "exam_date: null"),
                f'exam_time: "{meta["exam_time"]}"',
                (f"total_marks: {meta['total_marks']}" if meta["total_marks"] is not None
                 else "total_marks: null"),
                f"source_pdf: {Path(rel).name}",
                f"paper_ref: {meta['paper_ref']}",
                "cut: cut.json",
                "language: en",
                "---",
                "",
                f"Oxford AQA International A-Level · Chemistry (9620) · {meta['paper_code']} · {meta['title']}",
                f"源文件 `{Path(rel).name}`（{meta['pages']} 页）。",
                "",
            ]
            (d / "paper.md").write_text("\n".join(lines), encoding="utf-8")
        if meta_only:
            print(f"已刷新 {len(rows)} 卷的 paper.md")
        else:
            print(f"已落盘 {len(rows)} 卷到 {BASE}"
                  f"（新建 {n_new}，源文件一致 {n_same}，源头变更备份 {n_backup}）")


if __name__ == "__main__":
    main()
