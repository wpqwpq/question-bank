#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""import_eco.py — 把 Edexcel IAL 经济卷（WEC11–14）导入题库目录规范

用法:
    python scripts/import_eco.py --dry-run
    python scripts/import_eco.py --apply
    python scripts/import_eco.py --apply --only 2025-june-wec11,2026-jan-wec11a

规则:
    · 只收 2018 新大纲 WEC11/12/13/14；WEC01–04 旧大纲、IGCSE 4EC1、
      Specimen（实测也是旧大纲：10 题、4/48 分）一律不收；
    · 只收文件名含 `que` / `qp` 的 PDF（排除 rms / msc / pef / ms）；
    · **卷号从封面文本取**（`WEC11/01`、`WEC11/01A`），不从文件名猜——
      2022-oct 那 4 份文件名是 `U1 2022 OCT QP.pdf`，文件名里没有卷号；
    · 目录名 `<year>-<session>-<paperCode>`：session = jan|june|oct，01A（中国区）卷号后加 a；
    · 源目录里 `2019.1` / `2023.05` 这类是“年.月”命名 → 1=jan、5/6=june、10=oct；
    · PDF 复制为 session 目录下的 `source.pdf`（已存在且 sha256 相同则跳过）；
    · `paper.md` 只在缺失时写入，已存在不覆盖（人工改过的不动）。

⚠️ 与 import_acct.py 的差别：经济卷号跨 4 个单元、来源目录层级乱（`past paper/2018版/`、
   `经济2023.6/2023.6/`、`Questions paper/`），且存在同季「普通卷 + 01A 卷」并存的情形，
   故按封面取卷号 + 按父目录取考季，两条都独立于文件名。
"""

import argparse
import hashlib
import re
import shutil
import sys
from pathlib import Path

import pymupdf

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "edexcel" / "a-level" / "economics"

BOARD = "edexcel"
QUAL = "a-level"
SUBJECT = "economics"

MONTH_NUM = {"jan": 1, "june": 6, "oct": 10}
# 卷面满分（考生实际作答）：WEC11–14 全部 80 分
ECONOMICS_AWARDED = 80

# 封面卷号：WEC11/01 或 WEC11/01A
COVER_CODE_RE = re.compile(r"\bWEC\s*(1[1-4])\s*/\s*01\s*(A)?", re.I)
# 目录名「年.月」
DIR_SESSION_RE = re.compile(r"^((?:19|20)\d{2})\.(\d{1,2})$")
ANSWER_RE = re.compile(r"rms|msc|-ms-|_ms|\bms\b|pef|answer|答案", re.I)
TOTAL_RE = re.compile(r"\(?\s*Total\s+(?:for\s+)?Question\s*\*?\s*(\d+)\s*=\s*(\d+)\s*marks?\s*\)")

UNIT_TITLES = {
    "WEC11": "Unit 1: Markets in action",
    "WEC12": "Unit 2: Macroeconomic performance and policy",
    "WEC13": "Unit 3: Business behaviour",
    "WEC14": "Unit 4: Developments in the global economy",
}

# 目录名「年.月」→ 考季
DIR_MONTH_TO_SESSION = {1: "jan", 5: "june", 6: "june", 10: "oct"}


def norm_session(rel: Path):
    """从相对路径里的 `YYYY.N` 段推 (year, session)。"""
    for seg in rel.parts:
        m = DIR_SESSION_RE.match(seg)
        if not m:
            continue
        y, mo = int(m.group(1)), int(m.group(2))
        ses = DIR_MONTH_TO_SESSION.get(mo)
        if ses:
            return y, ses
    return None, None


def cover_meta(doc):
    """封面：卷号（含 A 后缀）+ 考试日期 + 时长 + 卷号 P 号。"""
    t = doc[0].get_text()
    flat = " | ".join(x.strip() for x in t.split("\n") if x.strip())
    out = {}
    m = COVER_CODE_RE.search(flat)
    if m:
        out["code"] = f"WEC{m.group(1)}"
        out["suffix"] = "a" if m.group(2) else ""
    m = re.search(r"(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\s+(\d{1,2})\s+"
                  r"(January|February|March|April|May|June|July|August|September|October|"
                  r"November|December)\s+((?:19|20)\d{2})", flat)
    if m:
        mo = {"january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
              "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
              "december": 12}[m.group(3).lower()]
        out["exam_date"] = f"{m.group(4)}-{mo:02d}-{int(m.group(2)):02d}"
        out["exam_day"] = m.group(1)
    m = re.search(r"(Morning|Afternoon|Evening)\s*\(Time:\s*([^)]+)\)", flat)
    if m:
        out["exam_time"] = f"{m.group(1)} (Time: {m.group(2).strip()})"
    m = re.search(r"\b([A-Z]\d{5}[A-Z])\b", flat)
    if m:
        out["paper_ref"] = m.group(1)
    m = re.search(r"UNIT\s*(\d)\s*:\s*([^|]+)", flat, re.I)
    if m:
        out["unit_no"] = int(m.group(1))
        out["unit_title"] = m.group(2).strip()
    return out


def marks_profile(doc):
    """(题数, 题目分值合计, 卷面满分)。分值合计按每题的首次 Total 求和（含二选一/三选一备选题）。

    ⚠️ 01A 中国区卷的 QP 封面**不印** "The total mark for this paper is …"
    （那句话在答题册封面上），所以卷面满分不能只靠正则兜底——WEC11–14 一律 80，写死并交叉校验。
    """
    first = {}
    for pg in doc:
        for m in TOTAL_RE.finditer(pg.get_text()):
            n, mk = int(m.group(1)), int(m.group(2))
            first.setdefault(n, mk)
    total = sum(first.values())
    t0 = doc[0].get_text()
    m = re.search(r"total mark for this paper is\s*(\d+)", t0, re.I)
    awarded = int(m.group(1)) if m else ECONOMICS_AWARDED
    if awarded != ECONOMICS_AWARDED:
        print(f"  [warn] 封面写的卷面满分是 {awarded}，与约定的 {ECONOMICS_AWARDED} 不符")
    return len(first), total, awarded


def walk(source: Path):
    rows = []
    for p in sorted(source.rglob("*.pdf")):
        rel = p.relative_to(source)
        flat = rel.as_posix()
        stem = p.stem
        low = stem.lower()
        if "igcse" in flat.lower() or "specimen" in flat.lower():
            continue
        if not re.search(r"que|_qp|qp\b", low):
            continue
        if ANSWER_RE.search(low):
            continue
        year, ses = norm_session(rel)
        if not year:
            print(f"  [skip] 无法解析年/考季: {flat}")
            continue
        try:
            with pymupdf.open(p) as doc:
                meta = cover_meta(doc)
        except Exception as e:  # noqa: BLE001
            print(f"  [skip] 打不开 {flat}: {e}")
            continue
        code = meta.get("code")
        if not code:
            print(f"  [skip] 封面无 WEC11–14 卷号（旧大纲？）: {flat}")
            continue
        suffix = meta.get("suffix", "")
        rows.append({"pdf": p, "rel": flat, "year": year, "session": ses,
                     "paper_code": f"{code}{suffix}".upper(), "suffix": suffix,
                     "dir": f"{year}-{ses}-{code.lower()}{suffix}",
                     "meta": meta})
    seen, out = set(), []
    for r in rows:
        if r["dir"] in seen:
            print(f"  [skip] 重复: {r['rel']}（同季同卷号已有）")
            continue
        seen.add(r["dir"])
        out.append(r)
    out.sort(key=lambda r: r["dir"])
    return out


def sha256(p: Path):
    return hashlib.sha256(p.read_bytes()).hexdigest()


def write_paper_md(md_path: Path, row: dict, n_q: int, total: int, awarded: int):
    pc = row["paper_code"]                       # WEC11A
    base = pc[:-1] if row["suffix"] else pc      # WEC11
    unit = f"{base}/01" + ("A" if row["suffix"] else "")
    meta = row["meta"]
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
        f'title: "{UNIT_TITLES.get(base, meta.get("unit_title", ""))}"',
    ]
    if meta.get("exam_date"):
        lines.append(f"exam_date: {meta['exam_date']}")
    if meta.get("exam_time"):
        lines.append(f'exam_time: "{meta["exam_time"]}"')
    lines += [
        # total_marks = 题目分值合计（含二选一 / 三选二的备选题）；
        # marks_awarded = 考生实际作答满分。两者不一致是设计如此。
        f"total_marks: {total}",
        f"marks_awarded: {awarded}",
        "source_pdf: source.pdf",
    ]
    if meta.get("paper_ref"):
        lines.append(f"paper_ref: {meta['paper_ref']}")
    lines += ["cut: cut.json", "language: en", "---", ""]
    lines.append(f"Pearson Edexcel IAL · Economics · {unit} · {UNIT_TITLES.get(base, '')}")
    lines.append("")
    if base in ("WEC11", "WEC12"):
        lines.append(f"**卷面结构**：Section A 六题必做（各 1 分）＋ Section B 五题必做（各 4 分）"
                     f"＋ Section C 资料分析（34 分，a–e）＋ Section D 二选一（各 20 分）。"
                     f"卷面 {awarded} 分，题目分值合计 {total}。")
    else:
        lines.append(f"**卷面结构**：Section A 六题必做（各 1 分）＋ Section B 资料分析（34 分，a–e）"
                     f"＋ Section C 三选二（各 20 分）。卷面 {awarded} 分，题目分值合计 {total}。")
    lines.append("")
    lines.append("**切割口径**：题图只含题面（＋该题资料册），不含点线作答区；"
                 "本节首题连带 SECTION 头与 “Study … before answering Question N” 指引。")
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main():
    ap = argparse.ArgumentParser(description="导入 Edexcel IAL 经济卷")
    ap.add_argument("--source", default="downloadPapers/Edexcel_ALevel_Eco")
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
    by_unit = {}
    for r in rows:
        by_unit[r["paper_code"]] = by_unit.get(r["paper_code"], 0) + 1
        print(f"  {r['dir']:<24} ← {r['rel']}")
    print("\n按卷号：" + "  ".join(f"{k} {v}" for k, v in sorted(by_unit.items())))

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
            n_q, total, awarded = marks_profile(doc)
            pages = doc.page_count
        md = dst / "paper.md"
        if not md.exists():
            write_paper_md(md, r, n_q, total, awarded)
        print(f"  {r['dir']:<24} {action} pdf（{pages} 页，{n_q} 题，合计 {total} / 满分 {awarded}）")
    print(f"\n完成：{len(rows)} 卷 → {DATA}")


if __name__ == "__main__":
    main()
