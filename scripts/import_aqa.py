#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""import_aqa.py — 把牛津 AQA 国际 A-Level 物理真题导入 data/aqa/

⚠️ 为什么单独一份而不是改 import_papers.py：后者整套规则是 Edexcel 命名
    （`que`/`试卷` + `<YYYY.M>` 考季目录 + WPH11 卷号 + Pearson 卷面元数据），
    AQA 是扁平命名 `qp-<YYYYMM>-physics-u<N>.pdf`、考季代码 01/05/06、
    卷号 PH01–PH05、卷面是 AQA 自己的措辞。两者的「收集规则」没有共享段，
    硬塞进同一份只会让两边互相牵制（与切割层不同，切割层共享的是算法）。
    故意**不动** import_papers.py。

用法:
    python scripts/import_aqa.py --source "C:/Users/Administrator/Desktop/AQA物理试卷" --dry-run
    python scripts/import_aqa.py --source ... --apply

规则:
    · 收集 `qp-<YYYYMM>-physics-u<N>.pdf`（`ms-…` 评分标准一律排除）；
    · 考季：月份 01 → jan，05/06 → june（AQA 5–6 月季站点用码不一致，统一记 june）；
    · 卷号：u1..u5 → PH01..PH05；
    · 目录：data/aqa/a-level/physics/<year>-<session>-<ph0N>/；
    · 卷级元数据（单元标题 / 考试日期 / 时长 / 总分 / 卷号水印）从封面文本提取；
    · 重新导入时旧 cut 产物挪 `_prev/`（收工须移出 data/）。
"""

import argparse
import hashlib
import os
import re
import sys
from collections import Counter
from pathlib import Path

import pymupdf

ROOT = Path(__file__).resolve().parent.parent
BASE = ROOT / "data" / "aqa" / "a-level" / "physics"

QP_RE = re.compile(r"^qp-(\d{6})-physics-u(\d)\.pdf$", re.I)
UNIT_CODE = {1: "ph01", 2: "ph02", 3: "ph03", 4: "ph04", 5: "ph05"}
MONTH_TOK = {"01": "jan", "05": "june", "06": "june"}

MONTHS = ["January", "February", "March", "April", "May", "June", "July",
          "August", "September", "October", "November", "December"]
DATE_RE = re.compile(
    r"(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\s+"
    r"(\d{1,2})\s+(" + "|".join(MONTHS) + r")\s+(\d{4})")


def collect(src: Path):
    """收集 qp 卷，返回 [(relpath, Path)]。"""
    found = {}
    for root, _dirs, files in os.walk(src):
        for f in files:
            if not QP_RE.match(f):
                continue
            p = Path(root) / f
            found[p.relative_to(src).as_posix()] = p
    return sorted(found.items())


def extract_meta(path: Path):
    """从封面页提取卷级元数据。"""
    doc = pymupdf.open(path)
    pages = doc.page_count
    head = doc[0].get_text()
    doc.close()
    flat = re.sub(r"[ \t]+", " ", head)

    paper_code = ""
    m = re.search(r"^PH0(\d)\s*$", flat, re.M)
    if m:
        paper_code = f"PH0{m.group(1)}"

    title = ""
    m = re.search(r"Unit\s*(\d)\s+([^\n]+)", flat)
    if m:
        title = f"Unit {m.group(1)} {re.sub(r'\s+', ' ', m.group(2)).strip()}"

    total_marks = None
    m = re.search(r"maximum mark for this paper is (\d+)", flat, re.I)
    if m:
        total_marks = int(m.group(1))

    exam_date = ""
    m = DATE_RE.search(flat)
    if m:
        exam_date = f"{m.group(4)}-{MONTHS.index(m.group(3)) + 1:02d}-{int(m.group(2)):02d}"
    if not exam_date:
        # 少数卷（2021–2022 疫情季）封面不印考试日期 → 退回文件名里的年月，
        # 只精确到月，不臆造具体日（与 import_papers.py 同一策略）。
        m = QP_RE.match(path.name)
        if m:
            exam_date = f"{m.group(1)[:4]}-{m.group(1)[4:]}"

    exam_time = ""
    m = re.search(r"Time allowed:\s*([^\n]+)", flat)
    if m:
        exam_time = re.sub(r"\s+", " ", m.group(1)).strip()

    paper_ref = ""
    m = re.search(r"\*([A-Za-z]*\d{2}PH0\d{2})\*", flat)
    if m:
        paper_ref = m.group(1).upper()
    else:
        z = re.findall(r"\*([^*\n]{4,20})\*", flat)
        if z:
            paper_ref = Counter(z).most_common(1)[0][0]

    return dict(paper_code=paper_code, title=title, total_marks=total_marks,
                exam_date=exam_date, exam_time=exam_time, paper_ref=paper_ref,
                pages=pages)


def session_name(rel: str, meta: dict) -> str:
    m = QP_RE.match(Path(rel).name)
    yyyymm, unit = m.group(1), int(m.group(2))
    year, mm = yyyymm[:4], yyyymm[4:]
    if mm not in MONTH_TOK:
        raise ValueError(f"未知考季月份代码 {mm}（{rel}）")
    return f"{year}-{MONTH_TOK[mm]}-{UNIT_CODE[unit]}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", required=True)
    ap.add_argument("--apply", action="store_true", help="真正落盘（建目录+拷 PDF+写 paper.md）")
    ap.add_argument("--meta-only", action="store_true", help="只重写 paper.md")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--csv", default=None)
    args = ap.parse_args()

    src = Path(args.source).resolve()
    if not src.is_dir():
        sys.exit(f"源目录不存在: {src}")
    files = collect(src)
    print(f"源目录: {src}")
    print(f"收集到 qp 卷 {len(files)} 份\n")

    rows, seen, problems = [], {}, []
    for rel, p in files:
        try:
            meta = extract_meta(p)
            sess = session_name(rel, meta)
        except Exception as e:
            problems.append((rel, str(e)))
            continue
        if sess in seen:
            problems.append((rel, f"目录名冲突，已被 {seen[sess]} 占用"))
            continue
        seen[sess] = rel
        sha = hashlib.sha256(p.read_bytes()).hexdigest()
        rows.append((sess, rel, meta, sha))

    rows.sort()
    print(f"{'会话目录':<22}{'卷号':<6}{'总分':<5}{'日期':<12}{'页':<4}标题")
    print("-" * 108)
    for sess, rel, meta, _ in rows:
        print(f"{sess:<22}{str(meta['paper_code']):<6}{str(meta['total_marks']):<5}"
              f"{meta['exam_date']:<12}{meta['pages']:<4}{meta['title']}")
    miss = [r for r in rows if r[2]["total_marks"] is None]
    print(f"\n合计 {len(rows)} 份；总分未提取 {len(miss)}；"
          f"日期未提取 {len([r for r in rows if not r[2]['exam_date']])}；"
          f"卷号未提取 {len([r for r in rows if not r[2]['paper_code']])}")
    for rel, why in problems:
        print(f"  ! PROBLEM {rel}: {why}")
    for sess, rel, meta, _ in miss:
        print(f"  ! 无总分 {sess}  <- {rel}")

    if args.csv:
        import csv as _csv
        with open(args.csv, "w", newline="", encoding="utf-8-sig") as fh:
            w = _csv.writer(fh)
            w.writerow(["session_dir", "src_rel", "paper_code", "title", "total_marks",
                        "exam_date", "exam_time", "paper_ref", "pages", "sha256"])
            for sess, rel, meta, sha in rows:
                w.writerow([sess, rel, meta["paper_code"], meta["title"], meta["total_marks"],
                            meta["exam_date"], meta["exam_time"], meta["paper_ref"],
                            meta["pages"], sha])
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
                f"id: aqa-a-level-physics-{sess}",
                "board: aqa",
                "qualification: a-level",
                "subject: physics",
                f"paper_code: {meta['paper_code']}",
                f"paper_unit: {meta['paper_code']}",
                f"year: {sess.split('-')[0]}",
                f"session: {sess.split('-')[1]}",
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
                f"Oxford AQA International A-Level · Physics · {meta['paper_code']} · {meta['title']}",
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
