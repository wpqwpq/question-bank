#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""import_aqa_acct.py — 把牛津 AQA 国际 A-Level 会计（9615）真题导入
data/aqa/a-level/accounting/

⚠️ 与 import_aqa_chem.py（化学 9620）、import_aqa.py（物理）**各自独立**：
   卷号体系、封面措辞、单元标题都不同，共享的只有「data/ 目录规范」这一层。
   按项目定下的规矩，每个考试局/资格/学科各用一份脚本。

⚠️ 本学科只收 **9615 国际卷**（封面 `INTERNATIONAL AS` / `INTERNATIONAL A-LEVEL`）。
   英国本土 AQA 7127 卷（页脚 `IB/…/7127/N`、`A-level` 抬头、120 分、17 题）
   一律排除——那是另一套大纲，混进来会污染题库（2026-10-21 实测踩到）。

用法:
    python scripts/import_aqa_acct.py --source downloadPapers/_new/AQA_9615_Accounting --dry-run
    python scripts/import_aqa_acct.py --source ... --apply
    python scripts/import_aqa_acct.py --source ... --apply --only 2025-june-ac01

规则:
    · 收集 `qp-<YYYYMM>-accounting-u<N>[-local].pdf`；
      **带 `-local` 后缀的通通排除**（2020-oct~2024-june 那 10 卷全是 7127）；
    · 考季：月份 01 → jan，05/06 → june，10 → oct；
    · 卷号：u1..u4 → AC01..AC04；
    · 目录：data/aqa/a-level/accounting/<year>-<session>-<ac0N>/；
    · 卷级元数据从封面文本提取（单元标题 / 考试日期 / 时长 / 总分 / 卷号水印）；
    · 源文件内容变了 → 旧 cut 产物挪 `_prev/`。
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
BASE = ROOT / "data" / "aqa" / "a-level" / "accounting"

QP_RE = re.compile(r"^qp-(\d{6})-accounting-u(\d)(-local)?\.pdf$", re.I)
UNIT_CODE = {1: "ac01", 2: "ac02", 3: "ac03", 4: "ac04"}
MONTH_TOK = {"01": "jan", "05": "june", "06": "june", "10": "oct"}

MONTHS = ["January", "February", "March", "April", "May", "June", "July",
          "August", "September", "October", "November", "December"]
DATE_RE = re.compile(
    r"(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\s+"
    r"(\d{1,2})\s+(" + "|".join(MONTHS) + r")\s+(\d{4})")


def parse_qp(name):
    m = QP_RE.match(name)
    if not m:
        return None
    return m.group(1), int(m.group(2)), bool(m.group(3))


def collect(src):
    found, skipped = {}, []
    for root, _dirs, files in os.walk(src):
        for f in files:
            parsed = parse_qp(f)
            if parsed is None:
                continue
            p = Path(root) / f
            if parsed[2]:
                skipped.append(p.relative_to(src).as_posix())
                continue
            found[p.relative_to(src).as_posix()] = p
    return sorted(found.items()), sorted(skipped)


def extract_meta(path):
    """封面页 → 卷级元数据。

    9615 封面：`INTERNATIONAL AS` / `INTERNATIONAL A-LEVEL` + `ACCOUNTING` +
    `Paper 1  Introduction to Financial Accounting`，右上角粗体 `AC01`，
    页脚 `*Jun25ac0101*`，总分句 `The maximum mark for this paper is 80.`
    """
    doc = pymupdf.open(path)
    pages = doc.page_count
    head = doc[0].get_text()
    doc.close()
    flat = re.sub(r"[ \t]+", " ", head)

    # 资格层级：先判，非国际卷直接拒（7127 会落到这里）
    if "INTERNATIONAL AS" in flat:
        level = "AS"
    elif "INTERNATIONAL A-LEVEL" in flat:
        level = "A2"
    else:
        raise ValueError("不是 9615 国际卷（封面无 INTERNATIONAL AS / A-LEVEL）")

    paper_code = ""
    m = re.search(r"^AC0(\d)\s*$", flat, re.M)
    if m:
        paper_code = f"AC0{m.group(1)}"
    if not paper_code:
        m = re.search(r"\*[A-Za-z]{3}\d{2}(ac0\d)\d{2}\*", flat, re.I)
        if m:
            paper_code = m.group(1).upper()

    title = ""
    m = re.search(r"Paper\s*(\d)\s+(.+?)\s*$", flat, re.M)
    if m:
        t = re.sub(r"\s+", " ", m.group(2)).strip()
        title = f"Paper {m.group(1)}: {t}"

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

    return dict(paper_code=paper_code, title=title, total_marks=total_marks,
                exam_date=exam_date, exam_time=exam_time, paper_ref=paper_ref,
                pages=pages, level=level)


def session_name(rel):
    parsed = parse_qp(Path(rel).name)
    if not parsed:
        raise ValueError(f"文件名不符命名规则: {rel}")
    yyyymm, unit, is_local = parsed
    if is_local:
        raise ValueError(f"7127 本土卷，不收: {rel}")
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
    files, skipped = collect(src)
    print(f"源目录: {src}")
    print(f"收集到 9615 qp 卷 {len(files)} 份；跳过 -local（7127）{len(skipped)} 份\n")

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
    print(f"\n合计 {len(rows)} 份；总分未提取 {len([r for r in rows if r[2]['total_marks'] is None])}；"
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
                f"id: aqa-a-level-accounting-{sess}",
                "board: aqa",
                "qualification: a-level",
                "subject: accounting",
                f"paper_code: {meta['paper_code']}",
                f"paper_unit: {meta['paper_code']}",
                f"year: {sess.split('-')[0]}",
                f"session: {sess.split('-')[1]}",
                'spec: "9615"',
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
                f"Oxford AQA International A-Level · Accounting (9615) · {meta['paper_code']} · {meta['title']}",
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
