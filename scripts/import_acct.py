#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""import_acct.py — 把 Edexcel IAL 会计卷（WAC11/WAC12）导入题库目录规范

用法:
    python scripts/import_acct.py --source downloadPapers/Edexcel_ALevel_Accounting --dry-run
    python scripts/import_acct.py --source downloadPapers/Edexcel_ALevel_Accounting --apply

规则:
    · 只收文件名含 `que` 的 PDF（排除 rms / msc / 答案）；
    · 目录名 `<year>-<session>-<paperCode>`：session = jan|june|oct，01A（中国区）在卷号后加 a；
    · **不收 2017-jan WAC02**（旧大纲、7 题、题型不同，2026-10-19 王培琪定）；
    · 卷级元数据（单元标题 / 考试日期 / 时长 / 总分 / 卷号）从卷面首页提取；
    · PDF 复制为 session 目录下的 `source.pdf`（已存在且 sha256 相同则跳过）；
    · `paper.md` 只在缺失时写入，已存在不覆盖（人工改过的不动）。

⚠️ 与 import_papers.py 的差别：那份是物理专用（UNIT_CODE 写死 wph11..wph16），
   会计卷号是 WAC11/WAC12 且跨年稳定，故单独一份，避免在共用脚本里加学科分叉。
"""

import argparse
import hashlib
import re
import shutil
import sys
from pathlib import Path

import pymupdf

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "edexcel" / "a-level" / "accounting"

BOARD = "edexcel"
QUAL = "a-level"
SUBJECT = "accounting"

MONTH_TOK = {"january": "jan", "jan": "jan", "june": "june", "may": "june",
             "october": "oct", "oct": "oct", "november": "oct", "nov": "oct",
             "march": "mar", "mar": "mar"}
MONTH_NUM = {"jan": 1, "mar": 3, "june": 6, "oct": 10}

ANSWER_RE = re.compile(r"rms|msc|-ms-|answer|答案", re.I)
CODE_RE = re.compile(r"(wac)\s*[-_]?\s*(\d{2})[-_]?01(a)?", re.I)

# 不收：旧大纲（2017-jan WAC02，7 题、Section A 两选 + Section B 五选三）
EXCLUDE_CODES = {"wac02"}

UNIT_TITLES = {
    "WAC11": "The Accounting System and Costing",
    "WAC12": "Corporate and Management Accounting",
}


def norm_session(path: Path, stem: str):
    """从目录名/文件名推出 (year, session)。'2025/Jan' + 'wac11-01-que-20250122' → (2025,'jan')。"""
    year = None
    for seg in path.parts:
        m = re.fullmatch(r"(19|20)\d{2}", seg)
        if m:
            year = int(seg)
        m2 = re.fullmatch(r"(January|Jan|June|May|October|Oct|November|Nov|March|Mar)(?:\s*(19|20)\d{2})?",
                          seg, re.I)
        if m2:
            ses = MONTH_TOK[m2.group(1).lower()]
    # 用文件名里的日期兜底（wac11-01-que-20250122 / WAC11_01_que_20170109）
    # ⚠️ 只在目录名给不出考季时才用：Edexcel 的 que 文件名日期是**发布日**，
    #    不是考试日（2020/Jan 的卷子文件名是 20200305），拿它会错判成 3 月季。
    m = re.search(r"(19|20)\d{2}(0[1-9]|1[0-2])\d{2}", stem)
    if m:
        y, mo = int(m.group(0)[:4]), int(m.group(0)[4:6])
        year = year or y
        if ses is None:
            ses = {1: "jan", 3: "mar", 5: "june", 6: "june", 10: "oct", 11: "oct"}.get(mo)
    return year, ses


def cover_meta(doc):
    """从首页文本提取标题 / 日期 / 时长 / 卷号。"""
    t = doc[0].get_text()
    flat = " | ".join(x.strip() for x in t.split("\n") if x.strip())
    out = {}
    m = re.search(r"(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\s+(\d{1,2})\s+"
                  r"(January|February|March|April|May|June|July|August|September|October|"
                  r"November|December)\s+((?:19|20)\d{2})", flat)
    if m:
        out["exam_date"] = f"{m.group(4)}-{MONTH_NUM.get(MONTH_TOK[m.group(3).lower()], 1):02d}-{int(m.group(2)):02d}"
        out["exam_day"] = m.group(1)
    m = re.search(r"(Morning|Afternoon|Evening)\s*\(Time:\s*([^)]+)\)", flat)
    if m:
        out["exam_time"] = f"{m.group(1)} (Time: {m.group(2).strip()})"
    m = re.search(r"\b([A-Z]\d{5}[A-Z])\b", flat)
    if m:
        out["paper_ref"] = m.group(1)
    return out


def walk(source: Path):
    rows = []
    for p in sorted(source.rglob("*.pdf")):
        stem = p.stem
        if "que" not in stem.lower():
            continue
        if ANSWER_RE.search(stem):
            continue
        m = CODE_RE.search(stem)
        if not m:
            continue
        code = f"wac{m.group(2)}"
        suffix = "a" if m.group(3) else ""
        if code in EXCLUDE_CODES:
            continue
        year, ses = norm_session(p.relative_to(source), stem)
        if not year or not ses:
            print(f"  [skip] 无法解析年份/考季: {p}")
            continue
        rows.append({"pdf": p, "year": year, "session": ses,
                     "paper_code": f"{code}{suffix}".upper(), "suffix": suffix,
                     "dir": f"{year}-{ses}-{code}{suffix}"})
    # 同一年同一考季同一卷号（含 a）只留一份
    seen, out = set(), []
    for r in rows:
        if r["dir"] in seen:
            print(f"  [skip] 重复: {r['pdf']}")
            continue
        seen.add(r["dir"])
        out.append(r)
    out.sort(key=lambda r: r["dir"])
    return out


def sha256(p: Path):
    return hashlib.sha256(p.read_bytes()).hexdigest()


def write_paper_md(md_path: Path, row: dict, meta: dict):
    # pc 含 A 后缀（WAC11A）；unit 的卷号段仍是不带 A 的 WAC11，
    # A 只出现在「/01A」那一段。标题表按不带 A 的卷号索引。
    pc = row["paper_code"]                              # WAC11A
    base = pc[:-1] if row["suffix"] else pc             # WAC11
    unit = f"{base}/01" + ("A" if row["suffix"] else "")
    lines = [
        "---",
        f"id: {BOARD}-{QUAL}-{SUBJECT}-{row['dir']}",
        f"board: {BOARD}",
        f"qualification: {QUAL}",
        f"subject: {SUBJECT}",
        f"paper_code: {pc}",
        f"paper_unit: {unit}",
        f"year: {row['year']}",
        f"session: {row['session']}",
        f'title: "{UNIT_TITLES.get(base, "")}"',
    ]
    if meta.get("exam_date"):
        lines.append(f"exam_date: {meta['exam_date']}")
    if meta.get("exam_time"):
        lines.append(f'exam_time: "{meta["exam_time"]}"')
    lines += [
        # ⚠️ total_marks 记「题目分值合计」= 55+55+30×4 = 230，供 validate_cut 对账；
        #    考生实际作答满分是 200（Section B 四选三），单记 marks_awarded。
        "total_marks: 230",
        "marks_awarded: 200",
        "source_pdf: source.pdf",
    ]
    if meta.get("paper_ref"):
        lines.append(f"paper_ref: {meta['paper_ref']}")
    lines += ["cut: cut.json", "language: en", "---", ""]
    lines.append(
        f"Pearson Edexcel IAL · Accounting · {unit} · {UNIT_TITLES.get(base, '')}")
    lines.append("")
    lines.append("**卷面结构**：Section A 两题必做（各 55 分）＋ Section B 四选三（各 30 分），卷面总分 200。")
    lines.append("")
    lines.append("**三部分**：普通卷为 QP（题面＋点线作答区交错）＋ Source Booklet（资料）；"
                 "01A 中国区卷为紧凑 QP（题面）＋ Source Booklet（资料）＋ Answer Booklet（答题区）。")
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main():
    ap = argparse.ArgumentParser(description="导入 Edexcel IAL 会计卷")
    ap.add_argument("--source", default="downloadPapers/Edexcel_ALevel_Accounting")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--only", default=None, help="只处理这些目录名（逗号分隔）")
    args = ap.parse_args()

    src = (ROOT / args.source).resolve()
    if not src.is_dir():
        sys.exit(f"源目录不存在: {src}")

    rows = walk(src)
    only = set(x.strip() for x in args.only.split(",")) if args.only else None
    if only:
        rows = [r for r in rows if r["dir"] in only]
        missing = only - {r["dir"] for r in rows}
        if missing:
            sys.exit(f"未找到: {', '.join(sorted(missing))}")

    print(f"共 {len(rows)} 卷")
    for r in rows:
        print(f"  {r['dir']:<26} ← {r['pdf'].relative_to(src).as_posix()}")
    if not args.apply:
        print("\n（dry-run，未落盘。加 --apply 执行）")
        return

    DATA.mkdir(parents=True, exist_ok=True)
    for r in rows:
        dst = DATA / r["dir"]
        dst.mkdir(parents=True, exist_ok=True)
        target = dst / "source.pdf"
        sha = sha256(r["pdf"])
        if target.exists() and sha256(target) == sha:
            action = "已存在"
        else:
            shutil.copy2(r["pdf"], target)
            action = "复制"
        with pymupdf.open(target) as doc:
            meta = cover_meta(doc)
            pages = doc.page_count
        md = dst / "paper.md"
        if not md.exists():
            write_paper_md(md, r, meta)
        print(f"  {r['dir']:<26} {action} pdf（{pages} 页）")
    print(f"\n完成：{len(rows)} 卷 → {DATA}")


if __name__ == "__main__":
    main()
