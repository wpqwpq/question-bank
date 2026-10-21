#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""import_edexcel_chem.py — 把 Edexcel IAL 化学（WCH11–WCH16）真题导入题库目录规范

⚠️ 与 scripts/import_papers.py（物理）**不共用代码**：本脚本只处理化学文件名式
   `QP(WCH11)-June2025.pdf` / `QP(WCH11)-January2026A.pdf`（A = WCH1x/01A 中国区版），
   物理那份按 `<year>.<month>/U<n>试卷.pdf` 的旧源目录结构走，两边命名与元数据
   位置都不同。改化学规则只改本文件，别去动物理那份（2026-10-17 约定）。

用法:
    python scripts/import_edexcel_chem.py --source downloadPapers/Edexcel_ALevel_Chemistry --dry-run
    python scripts/import_edexcel_chem.py --source downloadPapers/Edexcel_ALevel_Chemistry --apply

规则:
    · 目录名 `<year>-<session>-<paperCode>`：session = jan|june|oct（由文件名月份映射），
      paperCode = wch11..wch16，01A（中国区）版本后缀 a；
    · 卷级元数据（单元标题 / 考试日期 / 时长 / 总分 / 卷号）从卷面首页文本提取；
    · 01A 卷若含答题册（同一 PDF 两套页眉卷号），试卷卷号取**封面**那个，
      答题册卷号只记进 paper.md 的 answer_book_ref 备查；
    · 已存在同名目录：source.pdf 的 sha256 一致则跳过，不一致**不覆盖**、只报冲突；
    · --dry-run 只打印映射表，不动磁盘。
"""

import argparse
import hashlib
import re
import sys
from pathlib import Path

import pymupdf

ROOT = Path(__file__).resolve().parent.parent

BOARD = "edexcel"
QUAL = "a-level"
SUBJECT = "chemistry"
QUAL_TITLE = "Pearson Edexcel International Advanced Level"

# 文件名月份 → 会话标记
MONTH_TOK = {"January": "jan", "June": "june", "October": "oct"}
MONTHS = ["January", "February", "March", "April", "May", "June", "July",
          "August", "September", "October", "November", "December"]

# QP(WCH11)-June2025.pdf / QP(WCH11)-January2026A.pdf / QP(WCH13)-June2024R.pdf
# A = WCH1x/01A 中国区版；R = WCH1x/01R 替换卷（卷面自己印 /01R）
NAME_RE = re.compile(r"^QP\((WCH1[1-6])\)-(January|June|October)(\d{4})([AR]?)\.pdf$", re.I)

DATE_RE = re.compile(
    r"(?:(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\s+)?"
    r"(\d{1,2})(?:st|nd|rd|th)?\s+"
    r"(January|February|March|April|May|June|July|August|September|October|November|December)"
    r"\s+(\d{4})")
# 月精度日期（老封面只印 "June 2021"，无具体日）→ 落 YYYY-MM，不臆造日
MONTH_YEAR_RE = re.compile(
    r"^(January|February|March|April|May|June|July|August|September|October|November|December)"
    r"\s+(\d{4})$")
STOP_RE = re.compile(r"^(You must have|Instructions|Information|Advice|Question Paper|"
                     r"Question paper|Total Marks)\b", re.I)


def collect(src: Path):
    """收集符合命名式的化学 QP，返回 [(name, Path)]，按 (单元, 年, 考季) 排序。"""
    out = []
    for p in sorted(src.iterdir()):
        if not p.is_file() or p.suffix.lower() != ".pdf":
            continue
        if NAME_RE.match(p.name):
            out.append((p.name, p))
    return out


def extract_meta(path: Path):
    """从卷面提取卷级元数据。返回 dict。"""
    doc = pymupdf.open(path)
    pages = doc.page_count
    head = "\n".join(doc[i].get_text() for i in range(min(3, pages)))
    body = "\n".join(doc[i].get_text() for i in range(pages))
    # 每页页眉卷号（用于识别答题册分界；两套 → 该 PDF 含答题册）
    refs_by_page = []
    for p in doc:
        m = re.search(r"(P\d{5}[A-Z]?)", p.get_text()[:160])
        refs_by_page.append(m.group(1) if m else None)
    doc.close()

    flat = re.sub(r"[ \t]+", " ", head)
    allf = re.sub(r"[ \t]+", " ", body)

    # 单元标题：封面 "UNIT n: <标题>"，标题可能换行（WCH11/12/14/15 两行）
    title = ""
    unit = None
    m = re.search(r"UNIT\s*(\d)\s*:\s*(.+)", flat)
    if m:
        unit = int(m.group(1))
        seg = flat[flat.find(m.group(0)):].split("\n")
        parts = [m.group(2).strip()]
        for ln in seg[1:]:
            ln = ln.strip()
            if not ln or STOP_RE.match(ln) or re.match(r"^\d+\s*hours?", ln, re.I):
                break
            parts.append(ln)
            joined = " ".join(parts)
            # 已经收完整（下一行不再是标题续行）时停止
            if len(joined) > 20:
                break
        title = " ".join(parts).strip()

    # 卷号（含 /01A 中国区、/01R 替换卷判定）
    unit_ref = ""
    m = re.search(r"(WCH1\d\s*/\s*01[AR]?)", flat)
    if m:
        unit_ref = m.group(1).replace(" ", "")

    # 考试日期：逐行匹配封面（老封面 2021 及更早只印 "June 2021" → 月精度；
    # 完全不印日期的留空，不臆造）
    exam_date = ""
    for ln in flat.split("\n"):
        ln = ln.strip()
        m = DATE_RE.fullmatch(ln)
        if m:
            exam_date = f"{m.group(4)}-{MONTHS.index(m.group(3)) + 1:02d}-{int(m.group(2)):02d}"
            break
        m = MONTH_YEAR_RE.fullmatch(ln)
        if m:
            exam_date = f"{m.group(2)}-{MONTHS.index(m.group(1)) + 1:02d}"

    exam_time = ""
    m = re.search(r"(Morning|Afternoon)\s*\(Time:\s*([^)]+)\)", allf)
    if m:
        exam_time = f"{m.group(1)} (Time: {m.group(2).strip()})"
    else:
        m = re.search(r"(\d+\s*hours?(?:\s*\d+\s*minutes?)?)", flat, re.I)
        if m:
            exam_time = re.sub(r"\s+", " ", m.group(1)).strip()

    total_marks = None
    m = re.search(r"[Tt]he total mark(?:s)? for this (?:paper|qualification) is (\d+)", allf)
    if m:
        total_marks = int(m.group(1))

    # 试卷卷号取封面（page 0）那个；答题册卷号（若同一 PDF 含册）另记
    # ⚠️ 2019–2022 的老卷封面页往往印不出卷号（page 0 → None），此时不能拿
    #    「第一个非空卷号」当答题册——那其实是全卷每页都印的同一个卷号
    #    （实测 2019-jan-wch11：23/24 页都是 P60789A）。不加这条兜底，
    #    整卷会被误判成「含答题册」，cut_paper 的答题册分支会把题区扩成整页。
    paper_ref = refs_by_page[0] or ""
    if not paper_ref:
        for r in refs_by_page:
            if r:
                paper_ref = r
                break
    answer_ref = ""
    for r in refs_by_page:
        if r and r != paper_ref:
            answer_ref = r
            break
    # 真答题册应成段出现（题卷在前、答题本在后）；零星一页的异号视为噪声
    book_start = None
    if answer_ref:
        for i, r in enumerate(refs_by_page):
            if r == answer_ref:
                book_start = i
                break
        if refs_by_page.count(answer_ref) < 3:
            answer_ref, book_start = "", None

    return dict(unit=unit, title=title, unit_ref=unit_ref, exam_date=exam_date,
                exam_time=exam_time, total_marks=total_marks, paper_ref=paper_ref,
                answer_ref=answer_ref, book_start=book_start, pages=pages)


def session_name(name: str, meta: dict):
    """QP(WCH11)-January2026A.pdf → (2026-jan-wch11a, 'a')；
       QP(WCH13)-June2024R.pdf → (2024-june-wch13r, 'r')。第二项是版本标记。"""
    m = NAME_RE.match(name)
    if not m:
        raise ValueError("文件名不符合 QP(WCH1x)-<Month><Year>[A|R].pdf")
    code, month, year, tag = m.group(1).lower(), m.group(2), m.group(3), m.group(4).upper()
    if tag:
        # 文件名标了 A / R，卷面必须对应印着 /01A 或 /01R，否则宁可报错也不臆造
        if not meta["unit_ref"].endswith(tag):
            raise ValueError(f"文件名标 {tag} 但卷面是 {meta['unit_ref'] or '未知'}，请人工确认")
        return f"{year}-{MONTH_TOK[month]}-{code}{tag.lower()}", tag.lower()
    return f"{year}-{MONTH_TOK[month]}-{code}", ""


def write_paper_md(d: Path, sess: str, name: str, meta: dict, tag: str):
    """写 paper.md（字段与物理卷完全一致，多一个 answer_book_ref 备查）。"""
    code = sess.rsplit("-", 1)[-1]           # wch11 / wch11a / wch13r
    lines = [
        "---",
        f"id: {BOARD}-{QUAL}-{SUBJECT}-{sess}",
        f"board: {BOARD}",
        f"qualification: {QUAL}",
        f"subject: {SUBJECT}",
        f"paper_code: {code.upper()}",
        f"paper_unit: {meta['unit_ref'] or code.upper() + '/01' + tag.upper()}",
        f"year: {sess.split('-')[0]}",
        f"session: {sess.split('-')[1]}",
        f'title: "{meta["title"]}"',
        (f"exam_date: {meta['exam_date']}" if meta["exam_date"] else "exam_date: null"),
        (f'exam_time: "{meta["exam_time"]}"' if meta["exam_time"] else "exam_time: null"),
        (f"total_marks: {meta['total_marks']}" if meta["total_marks"] is not None
         else "total_marks: null"),
        f"source_pdf: {name}",
        f"paper_ref: {meta['paper_ref'] or 'null'}",
    ]
    if meta.get("answer_ref"):
        lines.append(f"answer_book_ref: {meta['answer_ref']}")
    lines += [
        "cut: cut.json",
        "language: en",
        "---",
        "",
        f"{QUAL_TITLE} · Chemistry · {meta['unit_ref'] or ''} · {meta['title']}",
        f"源文件 `{name}`（{meta['pages']} 页）"
        + (f"，含答题册（p{meta['book_start'] + 1} 起，卷号 {meta['answer_ref']}）。"
           if meta.get("answer_ref") else "。"),
        "",
    ]
    (d / "paper.md").write_text("\n".join(lines), encoding="utf-8")


def main():
    ap = argparse.ArgumentParser(description="导入 Edexcel IAL 化学真题到题库目录")
    ap.add_argument("--source", default="downloadPapers/Edexcel_ALevel_Chemistry")
    ap.add_argument("--apply", action="store_true", help="真正落盘（建目录+拷 PDF+写 paper.md）")
    ap.add_argument("--meta-only", action="store_true", help="只重写 paper.md")
    ap.add_argument("--new-only", action="store_true",
                    help="只处理库里还没有的会话目录，已存在的一律不碰（不重写 paper.md）")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    src = (ROOT / args.source).resolve() if not Path(args.source).is_absolute() \
        else Path(args.source).resolve()
    if not src.is_dir():
        sys.exit(f"源目录不存在: {src}")
    files = collect(src)
    print(f"源目录: {src}")
    print(f"收集到化学试卷 {len(files)} 份\n")

    rows, problems = [], []
    for name, p in files:
        try:
            meta = extract_meta(p)
            sess, tag = session_name(name, meta)
        except Exception as e:
            problems.append((name, str(e)))
            continue
        rows.append((sess, name, p, meta, tag))

    print(f"{'会话目录':<22}{'卷号':<9}{'总分':<5}{'日期':<12}{'页':<5}{'答题册':<8}标题")
    print("-" * 118)
    for sess, name, _p, meta, _c in rows:
        book = f"p{meta['book_start'] + 1}" if meta.get("book_start") else "-"
        print(f"{sess:<22}{meta['unit_ref']:<9}{str(meta['total_marks']):<5}"
              f"{meta['exam_date'] or '-':<12}{meta['pages']:<5}{book:<8}{meta['title']}")

    miss_tm = [r for r in rows if r[3]["total_marks"] is None]
    miss_dt = [r for r in rows if not r[3]["exam_date"]]
    print(f"\n合计 {len(rows)} 份；总分缺失 {len(miss_tm)}；日期缺失 {len(miss_dt)}；"
          f"含答题册 {sum(1 for r in rows if r[3].get('answer_ref'))}")
    for n, why in problems:
        print(f"  ! PROBLEM {n}: {why}")
    for sess, n, _p, _m, _c in miss_tm:
        print(f"  ! 无总分 {sess}  <- {n}")
    for sess, n, _p, _m, _c in miss_dt:
        print(f"  ! 无日期 {sess}  <- {n}（留 null，不臆造）")

    if not (args.apply or args.meta_only):
        print("\n（--dry-run：未动磁盘）")
        return

    base = ROOT / "data" / BOARD / QUAL / SUBJECT
    n_new = n_same = n_skip = 0
    conflicts = []
    for sess, name, p, meta, tag in rows:
        d = base / sess
        dst = d / "source.pdf"
        # --new-only：目录已存在就直接放过，绝不重写 paper.md（保护已过检的卷）
        if args.new_only and d.exists():
            n_skip += 1
            continue
        sha = hashlib.sha256(p.read_bytes()).hexdigest()
        if dst.exists():
            old = hashlib.sha256(dst.read_bytes()).hexdigest()
            if old != sha:
                conflicts.append((sess, name))
                continue
            n_same += 1
        else:
            n_new += 1
            d.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(p.read_bytes())
        write_paper_md(d, sess, name, meta, tag)

    print(f"\n落盘到 {base}：新建 {n_new}，已存在且一致 {n_same}，"
          f"按 --new-only 跳过 {n_skip}，paper.md 写入 {len(rows) - len(conflicts) - n_skip}")
    for sess, name in conflicts:
        print(f"  ! 冲突未覆盖 {sess}（现有 source.pdf 与 {name} 不同，需人工处理）")


if __name__ == "__main__":
    main()
