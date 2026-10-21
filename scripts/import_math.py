#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""import_math.py — 把 Edexcel IAL 数学真题 PDF 导入题库目录规范

用法:
    python scripts/import_math.py --source downloadPapers/Edexcel_ALevel_Math --dry-run
    python scripts/import_math.py --source downloadPapers/Edexcel_ALevel_Math \
        --apply --limit 20 --offset 0

规则:
    · 源目录 `<YYMM> Exams/<unit>-01[var]-que-<date>.pdf`；
      YYMM：2001=2020-jan、2010=2020-oct、2106=2021-june、…（10 月记 oct）
    · 目标目录 `data/edexcel/a-level/mathematics/<year>-<session>-<code>/`
      code = 单元小写 + 变体字母（`wma11` / `wma11a` / `wma11r`）
    · 源 PDF 原样复制为 `source.pdf`；卷级元数据从封面文本提取；
    · **一次最多 20 卷**（王培琪 2026-10-19 定），`--limit 20 --offset N` 翻页。
"""

import argparse
import re
import shutil
import sys
from pathlib import Path

import pymupdf

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "edexcel" / "a-level" / "mathematics"

UNITS = {"WMA11", "WMA12", "WMA13", "WMA14", "WST01", "WST02", "WST03",
         "WME01", "WME02", "WME03", "WFM01", "WFM02", "WFM03",
         "WDM11"}   # D1（2026-10-19 加入；版式特殊，切割走 cut_paper_d1.py）

TITLES = {
    "WMA11": "Pure Mathematics P1", "WMA12": "Pure Mathematics P2",
    "WMA13": "Pure Mathematics P3", "WMA14": "Pure Mathematics P4",
    "WST01": "Statistics S1", "WST02": "Statistics S2", "WST03": "Statistics S3",
    "WME01": "Mechanics M1", "WME02": "Mechanics M2", "WME03": "Mechanics M3",
    "WFM01": "Further Pure Mathematics F1", "WFM02": "Further Pure Mathematics F2",
    "WFM03": "Further Pure Mathematics F3",
    "WDM11": "Decision Mathematics D1",
}

FILE_RE = re.compile(
    r"^(?P<unit>[a-z]{3}\d{2})[-_]01(?P<var>[a-z]?)[-_]que[-_](?P<date>\d{8})",
    re.I)
DIR_RE = re.compile(r"^(?P<yy>\d{2})(?P<mm>\d{2})$")
MONTHS = {"01": "jan", "06": "june", "10": "oct"}
# 卷面月份 → 考季（2010 目录里的卷其实是 5/6 月考的，不能按目录名判）
DATE_TO_SESSION = {"01": "jan", "02": "jan", "05": "june", "06": "june",
                   "10": "oct", "11": "oct"}
MON_NAMES = {"january": "01", "february": "02", "march": "03", "april": "04",
             "may": "05", "june": "06", "july": "07", "august": "08",
             "september": "09", "october": "10", "november": "11",
             "december": "12"}
UNIT_REF_RE = re.compile(r"\b([A-Z]{3}\d{2})/(\d{2}[A-Z]?)\b")
REF_RE = re.compile(r"\b(P\d{5}[A-Z])\b")
MARKS_RE = re.compile(r"total\s+mark\s+for\s+this\s+paper\s+is\s+(\d+)", re.I)
TIME_RE = re.compile(r"(Morning|Afternoon|Evening)?\s*\(Time:\s*([^)]+)\)", re.I)
DATE_RE = re.compile(
    r"(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)?\s*"
    r"(\d{1,2})\s+([A-Z][a-z]+)\s+(\d{4})")


def collect(src, only_units=None):
    out = []
    for p in sorted(src.rglob("*que*.pdf")):
        m = FILE_RE.match(p.name)
        if not m:
            continue
        unit = m.group("unit").upper()
        if unit not in UNITS:
            continue
        if only_units and unit not in only_units:
            continue
        dm = DIR_RE.match(p.parent.name.split()[0])
        if not dm or dm.group("mm") not in MONTHS:
            print(f"  ! 跳过（考季目录看不懂）: {p.relative_to(src)}")
            continue
        year = 2000 + int(dm.group("yy"))
        session = MONTHS[dm.group("mm")]
        var = (m.group("var") or "").lower()
        code = unit.lower() + var
        out.append({"pdf": p, "unit": unit, "var": var, "code": code,
                    "year": year, "session": session, "folder": p.parent.name})
    return out


def meta_from_cover(doc, unit, var, year, session):
    """从封面提取 paper_unit / paper_ref / 考试日期 / 时长 / 总分 / 标题。

    ⚠️ D1（WDM11）有两册：2 卷（2022-jan / 2023-jan）的 PDF 是**作答册在前**，
    doc[0] 是 Answer Book 封面，不印日期与总分。所以找不全时退回全文档扫描。
    """
    text = doc[0].get_text()
    meta = {}
    m = UNIT_REF_RE.search(text)
    if m:
        meta["paper_unit"] = f"{m.group(1)}/{m.group(2)}"
    if "paper_unit" not in meta:
        meta["paper_unit"] = f"{unit}/01{var.upper()}"
    m = REF_RE.search(text)
    if m:
        meta["paper_ref"] = m.group(1)
    m = MARKS_RE.search(text)
    if m:
        meta["total_marks"] = int(m.group(1))
    m = TIME_RE.search(text)
    if m:
        ampm = (m.group(1) or "").strip()
        meta["exam_time"] = (f"{ampm} (Time: {m.group(2).strip()})" if ampm
                             else f"Time: {m.group(2).strip()}")
    m = DATE_RE.search(text)
    if m and m.group(3).lower() in MON_NAMES:
        day, mon, yr = int(m.group(2)), m.group(3).lower(), int(m.group(4))
        meta["exam_date"] = f"{yr:04d}-{MON_NAMES[mon]}-{day:02d}"
    if not meta.get("total_marks") or not meta.get("exam_date"):
        whole = "\n".join(doc[i].get_text() for i in range(doc.page_count))
        if not meta.get("total_marks"):
            m = MARKS_RE.search(whole)
            if m:
                meta["total_marks"] = int(m.group(1))
        if not meta.get("exam_date"):
            for m in DATE_RE.finditer(whole):
                if m.group(3).lower() in MON_NAMES:
                    meta["exam_date"] = (f"{int(m.group(4)):04d}-"
                                         f"{MON_NAMES[m.group(3).lower()]}-"
                                         f"{int(m.group(2)):02d}")
                    break
        if not meta.get("exam_time"):
            m = TIME_RE.search(whole)
            if m:
                ampm = (m.group(1) or "").strip()
                meta["exam_time"] = (f"{ampm} (Time: {m.group(2).strip()})" if ampm
                                     else f"Time: {m.group(2).strip()}")
    meta["title"] = TITLES[unit]
    return meta


def main():
    ap = argparse.ArgumentParser(description="导入 Edexcel IAL 数学卷")
    ap.add_argument("--source", default="downloadPapers/Edexcel_ALevel_Math")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--offset", type=int, default=0)
    ap.add_argument("--units", help="只导这些单元（逗号分隔，如 WDM11）")
    args = ap.parse_args()
    if args.limit > 20:
        sys.exit("单批最多 20 卷（王培琪 2026-10-19 定）")

    src = (ROOT / args.source).resolve()
    only = {u.strip().upper() for u in args.units.split(",")} if args.units else None
    items = collect(src, only)
    total = len(items)
    batch = items[args.offset:args.offset + args.limit]
    print(f"[批次] 第 {args.offset // max(args.limit, 1) + 1} 批："
          f"{args.offset + 1}–{args.offset + len(batch)} / 共 {total} 卷\n")

    done = skip = 0
    for it in batch:
        doc = pymupdf.open(it["pdf"])
        pages = doc.page_count
        meta = meta_from_cover(doc, it["unit"], it["var"], it["year"], it["session"])
        # 考季以**卷面考试日期**为准（2010 目录 = 2020 年 5/6 月考，不是 10 月）
        if meta.get("exam_date"):
            y, mo = meta["exam_date"].split("-")[0], meta["exam_date"].split("-")[1]
            if mo in DATE_TO_SESSION:
                it["year"] = int(y)
                it["session"] = DATE_TO_SESSION[mo]
        it["session_dir"] = f"{it['year']}-{it['session']}-{it['code']}"
        d = DATA / it["session_dir"]
        if (d / "paper.md").exists() and not args.apply:
            skip += 1
        print(f"{it['session_dir']:24s} {it['pdf'].name:34s} "
              f"{pages:3d}页 {meta.get('paper_unit','?'):12s} "
              f"{meta.get('total_marks','?')}分 {meta.get('exam_date','?')}")
        if not args.apply:
            continue
        d.mkdir(parents=True, exist_ok=True)
        shutil.copy2(it["pdf"], d / "source.pdf")
        fm = [
            ("id", f"edexcel-a-level-mathematics-{it['session_dir']}"),
            ("board", "edexcel"),
            ("qualification", "a-level"),
            ("subject", "mathematics"),
            ("paper_code", it["code"].upper()),
            ("paper_unit", meta.get("paper_unit")),
            ("year", it["year"]),
            ("session", it["session"]),
            ("title", f'"{meta["title"]}"'),
        ]
        for k in ("exam_date", "exam_time"):
            if meta.get(k):
                fm.append((k, f'"{meta[k]}"' if k == "exam_time" else meta[k]))
        if "total_marks" in meta:
            fm.append(("total_marks", meta["total_marks"]))
        fm += [("source_pdf", it["pdf"].name), ("cut", "cut.json"),
               ("language", "en")]
        body = (f"{meta['title']} · {meta['paper_unit']} · "
                f"{it['year']} {it['session']}\n"
                f"源文件 `{it['pdf'].name}`（{pages} 页）。\n")
        (d / "paper.md").write_text(
            "---\n" + "\n".join(f"{k}: {v}" for k, v in fm if v is not None)
            + "\n---\n\n" + body, encoding="utf-8")
        done += 1

    if args.apply:
        print(f"\n写入 {done} 卷 → {DATA}")
    else:
        print(f"\n[dry-run] 待导入 {len(batch)} 卷，已存在 {skip} 卷；"
              f"加 --apply 落盘")


if __name__ == "__main__":
    main()
