#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""import_papers.py — 把「真题」源目录里的试卷批量导入题库目录规范

用法:
    python scripts/import_papers.py --source <真题目录> --dry-run
    python scripts/import_papers.py --source <真题目录> --apply

规则:
    · 递归收集文件名含 que / 试卷 的 PDF，排除 rms / msc / 答案 / ms- 等答案文件；
    · 排除名单：2022.1 的 U3/U5/U6 Unused 版（王培琪 2026-10-14 定：只收 Used）；
    · 目录名 `<year>-<session>-<paperCode>`：session = jan|june|oct（源目录月份映射），
      paperCode = wph11..wph16，01A（中国区）版本后缀 a；
    · 卷级元数据（unit 标题 / 考试日期 / 时长 / 总分 / 卷号）从卷面首页文本提取；
    · --dry-run 只打印映射表，不动磁盘。
"""

import argparse
import hashlib
import os
import re
import sys
from pathlib import Path

import pymupdf

ROOT = Path(__file__).resolve().parent.parent

# 源目录月份 → 会话标记（2020.5 定 june：卷面自述 May/June 2020）
MONTH_TOK = {"1": "jan", "5": "june", "6": "june", "10": "oct"}
# 单元号 → 卷号
UNIT_CODE = {1: "wph11", 2: "wph12", 3: "wph13", 4: "wph14", 5: "wph15", 6: "wph16"}
# 排除名单（相对源目录的路径，用 / 分隔）
EXCLUDE = {
    "2022.1/U3试卷（Unsued）(1).pdf",
    "2022.1/U5试卷（Unused）(1).pdf",
    "2022.1/U6试卷_unused(1).pdf",
}
ANSWER_RE = re.compile(r"rms|msc|答案|^ms-|-ms-", re.I)
PAPER_RE = re.compile(r"que|试卷", re.I)
# 文件名带 Unused/Unsued（含源目录常见拼写变体）一律不收
UNUSED_RE = re.compile(r"unused|unsued", re.I)


def session_key(folder: str):
    """'2020.1' -> (2020, 1)；解析失败返回 None。"""
    m = re.match(r"(\d{4})\.(\d{1,2})$", folder)
    return (int(m.group(1)), int(m.group(2))) if m else None


def collect(src: Path, session_range=None):
    """递归收集试卷 PDF，返回 [(relpath, Path)]，已排序去重。

    session_range: (lo, hi) 元组（含端点），按一级目录 'YYYY.M' 过滤考季区间。
    """
    found = {}
    for root, _dirs, files in os.walk(src):
        for f in files:
            if not f.lower().endswith(".pdf"):
                continue
            if ANSWER_RE.search(f) or not PAPER_RE.search(f):
                continue
            if UNUSED_RE.search(f):
                continue
            p = Path(root) / f
            rel = p.relative_to(src).as_posix()
            if rel in EXCLUDE:
                continue
            if session_range is not None:
                lo, hi = session_range
                k = session_key(rel.split("/")[0])
                if k is None or not (lo <= k <= hi):
                    continue
            # 同一内容多次出现（嵌套目录）时按 sha 去重
            found.setdefault(rel, p)
    return sorted(found.items())


def paper_code_of(rel: str, text: str, unit: int, china: bool) -> str:
    code = UNIT_CODE[unit]
    if china or re.search(r"/0?1A\b", text) or re.search(r"-01a-", rel, re.I):
        code += "a"
    return code


MONTHS = ["January", "February", "March", "April", "May", "June", "July",
          "August", "September", "October", "November", "December"]
DATE_RE = re.compile(
    r"(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\s+"
    r"(\d{1,2})\s+(January|February|March|April|May|June|July|August|"
    r"September|October|November|December)\s+(\d{4})")


def extract_meta(path: Path, rel: str):
    """从卷面提取元数据。返回 dict。

    注意：01A（中国区）卷的总分印在**答题册封面**（PDF 后半段）而非 QP 封面，
    老卷（2021–2024 的"试卷"重命名件）封面常无考试日期——都要全篇兜底。
    """
    doc = pymupdf.open(path)
    pages = doc.page_count
    head = "\n".join(doc[i].get_text() for i in range(min(4, pages)))
    body = "\n".join(doc[i].get_text() for i in range(pages))
    doc.close()

    flat = re.sub(r"[ \t]+", " ", head)
    flat_all = re.sub(r"[ \t]+", " ", body)
    name = Path(rel).name

    unit = None
    m = re.search(r"UNIT\s*(\d)\s*[:\n]", flat, re.I)
    if m:
        unit = int(m.group(1))
    if unit is None:
        m = re.search(r"U(\d)", name)
        if m:
            unit = int(m.group(1))
    if unit is None:
        m = re.search(r"WPH1(\d)", name, re.I)
        if m:
            unit = int(m.group(1))

    title = ""
    m = re.search(r"UNIT\s*\d\s*:\s*([^\n]+)", flat, re.I)
    if m:
        title = re.sub(r"\s+", " ", m.group(1)).strip()

    unit_full = ""
    m = re.search(r"(WPH1\d\s*/\s*01A?)", flat) or re.search(r"(WPH1\d\s*/\s*01A?)", flat_all)
    if m:
        unit_full = m.group(1).replace(" ", "")
    if not unit_full and unit:
        unit_full = f"{UNIT_CODE[unit].upper()}/01" + ("A" if re.search(r"-01a-", rel, re.I) else "")

    # 考试日期：全篇找（带星期 → 不带星期），兜底用文件名里的 8 位日期
    exam_date = ""
    m = DATE_RE.search(flat_all)
    if m:
        exam_date = f"{m.group(4)}-{MONTHS.index(m.group(3)) + 1:02d}-{int(m.group(2)):02d}"
    if not exam_date:
        m = re.search(r"\b(\d{1,2})\s+(" + "|".join(MONTHS) + r")\s+(\d{4})", flat_all)
        if m:
            exam_date = f"{m.group(3)}-{MONTHS.index(m.group(2)) + 1:02d}-{int(m.group(1)):02d}"
    if not exam_date:
        m = re.search(r"[-_]que[-_](\d{4})(\d{2})(\d{2})", name, re.I)
        if m:
            exam_date = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    if not exam_date:
        # 兜底：按源目录命名推考季（如 2021.6 → 2021-06）。
        # Pearson 对 2021–2023 的部分卷做了重发，卷面与文件名都不再印考试日期，
        # 但考季目录名本身就是年份+月份；只精确到月，落 YYYY-MM，不臆造具体日。
        m = re.match(r"(\d{4})\.(\d{1,2})", Path(rel).parts[0])
        if m:
            exam_date = f"{m.group(1)}-{int(m.group(2)):02d}"

    exam_time = ""
    m = re.search(r"(Morning|Afternoon)\s*\(Time:\s*([^)]+)\)", flat_all)
    if m:
        exam_time = f"{m.group(1)} (Time: {m.group(2).strip()})"
    else:
        m = re.search(r"Time\s*[:\n]?\s*(\d+\s*hours?[^\n]*minutes?)", flat_all, re.I)
        if m:
            exam_time = re.sub(r"\s+", " ", m.group(1)).strip()

    total_marks = None
    m = re.search(r"[Tt]he total mark(?:s)? for this paper is (\d+)", flat_all)
    if m:
        total_marks = int(m.group(1))

    # 卷号（Pearson ref，如 P87440A）：全篇取出现次数最多的 6 位 base + 可选尾字母。
    # 页眉处常与页码粘连（*P65800A0128*），故不用 \b 收尾。
    refs = re.findall(r"(P\d{5})([A-Z]?)", flat_all)
    if refs:
        from collections import Counter
        base = Counter(r[0] for r in refs).most_common(1)[0][0]
        tail = Counter(r[1] for r in refs if r[0] == base).most_common(1)[0][0]
        paper_ref = base + tail

    return dict(unit=unit, title=title, unit_full=unit_full, exam_date=exam_date,
                exam_time=exam_time, total_marks=total_marks, paper_ref=paper_ref,
                pages=pages)


def session_name(rel: str, meta: dict) -> str:
    parts = Path(rel).parts
    folder = parts[0]
    m = re.match(r"(\d{4})\.(\d{1,2})", folder)
    if not m:
        raise ValueError(f"无法解析考季目录: {folder}")
    year, month = m.group(1), m.group(2)
    tok = MONTH_TOK[month]
    if meta["unit"] is None:
        raise ValueError(f"无法判定单元: {rel}")
    china = bool(re.search(r"-01a-|/01A", rel)) or "[01A]" in rel
    code = UNIT_CODE[meta["unit"]] + ("a" if china else "")
    return f"{year}-{tok}-{code}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", required=True)
    ap.add_argument("--apply", action="store_true", help="真正落盘（建目录+拷 PDF+写 paper.md）")
    ap.add_argument("--meta-only", action="store_true",
                    help="只重写 paper.md，不碰 source.pdf / cut.json（改元数据提取规则后用）")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--from", dest="from_session", default=None,
                    help="起始考季目录，如 2020.1（含）")
    ap.add_argument("--to", dest="to_session", default=None,
                    help="结束考季目录，如 2026.6（含）")
    ap.add_argument("--csv", default=None, help="导出映射表 CSV")
    args = ap.parse_args()

    src = Path(args.source).resolve()
    rng = None
    if args.from_session or args.to_session:
        lo = session_key(args.from_session) if args.from_session else (0, 0)
        hi = session_key(args.to_session) if args.to_session else (9999, 12)
        if lo is None or hi is None:
            ap.error("--from/--to 需形如 2020.1")
        rng = (lo, hi)
    files = collect(src, rng)
    print(f"源目录: {src}")
    print(f"收集到试卷 {len(files)} 份\n")

    rows, seen, problems = [], {}, []
    for rel, p in files:
        try:
            meta = extract_meta(p, rel)
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

    print(f"{'会话目录':<24}{'单元':<5}{'总分':<5}{'日期':<12}{'页':<4}源文件")
    print("-" * 110)
    for sess, rel, meta, _ in rows:
        print(f"{sess:<24}{str(meta['unit']):<5}{str(meta['total_marks']):<5}"
              f"{meta['exam_date']:<12}{meta['pages']:<4}{rel}")

    miss = [r for r in rows if r[2]["total_marks"] is None]
    print(f"\n合计 {len(rows)} 份；总分未提取到 {len(miss)} 份；"
          f"日期未提取到 {len([r for r in rows if not r[2]['exam_date']])} 份；"
          f"卷号未提取到 {len([r for r in rows if not r[2]['paper_ref']])} 份")
    for rel, why in problems:
        print(f"  ! PROBLEM {rel}: {why}")
    for sess, rel, meta, _ in miss:
        print(f"  ! 无总分 {sess}  <- {rel}")

    if args.csv:
        import csv
        with open(args.csv, "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.writer(fh)
            w.writerow(["session_dir", "src_rel", "unit", "total_marks", "exam_date",
                        "exam_time", "paper_ref", "title", "pages", "sha256"])
            for sess, rel, meta, sha in rows:
                w.writerow([sess, rel, meta["unit"], meta["total_marks"], meta["exam_date"],
                            meta["exam_time"], meta["paper_ref"], meta["title"],
                            meta["pages"], sha])
        print(f"\n映射表已写入 {args.csv}")

    if args.apply or args.meta_only:
        meta_only = args.meta_only and not args.apply
        base = ROOT / "data" / "edexcel" / "a-level" / "physics"
        n_new = n_same = n_backup = 0
        for sess, rel, meta, sha in rows:
            d = base / sess
            d.mkdir(parents=True, exist_ok=True)
            dst = d / "source.pdf"
            if meta_only:
                pass
            elif dst.exists():
                old = hashlib.sha256(dst.read_bytes()).hexdigest()
                if old == sha:
                    n_same += 1
                else:
                    # 源头变了：旧 cut 产物整体挪进 _prev/（不删，保留人工成果）
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
                f"id: edexcel-a-level-physics-{sess}",
                "board: edexcel",
                "qualification: a-level",
                "subject: physics",
                f"paper_code: {meta['unit_full'].split('/')[0] if meta['unit_full'] else UNIT_CODE[meta['unit']].upper()}",
                f"paper_unit: {meta['unit_full']}",
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
                f"Pearson Edexcel International Advanced Level · Physics · "
                f"{meta['unit_full'] or ''} · {meta['title']}",
                f"源文件 `{Path(rel).name}`（{meta['pages']} 页）。",
                "",
            ]
            (d / "paper.md").write_text("\n".join(lines), encoding="utf-8")
        if meta_only:
            print(f"已刷新 {len(rows)} 卷的 paper.md（未动 source.pdf / cut.json）")
        else:
            print(f"已落盘 {len(rows)} 卷到 {base}"
                  f"（新建 {n_new}，源文件一致 {n_same}，源头变更已备份 {n_backup}）")


if __name__ == "__main__":
    main()
