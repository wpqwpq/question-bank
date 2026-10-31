#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""fix_math_subq.py — 只修 cut.json 的 `subquestions`（Edexcel IAL 数学）

背景（2026-10-26）：旧版 cut_paper_math.py 只认行首标签、每行只取第一个 token，
Edexcel 把下一级标签印在上一级同一行 → 大量题少 a（或 b i）。本脚本用修好后的
`analyze()` 重新算小问，把结果**只回填到 subquestions 字段**：

  region / anchors / total_marks / status / 题图 / qNN.md 一律不动 —— 小问标签
  只影响打标台的小问单元划分（数学学科不裁子图，见 store.SUB_NO_IMAGE_SUBJECTS）。

用法:
    python scripts/fix_math_subq.py <root>                 # 只体检，打印 diff
    python scripts/fix_math_subq.py <root> --apply         # 落盘（逐卷留 .bak-<stamp>）
    python scripts/fix_math_subq.py <root> --papers 2020-jan-wma11,...
    python scripts/fix_math_subq.py <root> --report _tmp/fix_report.json
"""
import argparse
import json
import shutil
import sys
import time
from pathlib import Path

import pymupdf

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cut_paper_math import analyze  # noqa: E402
from cut_paper import build_page_lines  # noqa: E402


def patch_paper(sdir, apply=False, stamp=""):
    sp = sdir / "cut.json"
    cut = json.loads(sp.read_text(encoding="utf-8"))
    doc = pymupdf.open(sdir / "source.pdf")
    try:
        pages_lines = [build_page_lines(p) for p in doc]
        new_qs, _meta = analyze(doc, pages_lines, paper_marks=None)
    finally:
        doc.close()
    by_num = {q["number"]: q for q in new_qs}
    changes = []
    for q in cut["questions"]:
        nq = by_num.get(q["number"])
        if nq is None:
            continue
        old = [s["label"] for s in q.get("subquestions") or []]
        new = [s["label"] for s in nq.get("subquestions") or []]
        if old == new:
            continue
        changes.append({"q": q["number"], "old": old, "new": new,
                        "new_subs": nq.get("subquestions") or []})
        if apply:
            q["subquestions"] = nq.get("subquestions") or []
    if apply and changes:
        shutil.copy2(sp, sp.with_suffix(f".json.bak-{stamp}"))
        sp.write_text(json.dumps(cut, ensure_ascii=False, indent=1) + "\n",
                      encoding="utf-8")
    return changes


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("root")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--papers", default="")
    ap.add_argument("--exclude", default="",
                    help="跳过名字含该子串的卷（如 wdm11 —— D1 走 cut_paper.py 另一条管线）")
    ap.add_argument("--report", default="")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--offset", type=int, default=0, help="从第几卷开始（分块跑用）")
    ap.add_argument("--stamp", default="", help="备份后缀，分块跑时传同一个值")
    args = ap.parse_args()

    root = Path(args.root)
    only = {p.strip() for p in args.papers.split(",") if p.strip()}
    excl = [x.strip().lower() for x in args.exclude.split(",") if x.strip()]
    dirs = sorted(p for p in root.iterdir() if p.is_dir()
                  and (p / "cut.json").exists() and (p / "source.pdf").exists()
                  and (not only or p.name in only)
                  and not any(x in p.name.lower() for x in excl))
    if args.limit:
        dirs = dirs[args.offset:args.offset + args.limit]
    elif args.offset:
        dirs = dirs[args.offset:]
    print(f"[块] 第 {args.offset + 1}–{args.offset + len(dirs)} 卷（共 {len(dirs)}）",
          flush=True)
    stamp = args.stamp or time.strftime("%Y%m%d-%H%M%S")
    report, n_changed, n_q = [], 0, 0
    for i, d in enumerate(dirs, 1):
        try:
            ch = patch_paper(d, apply=args.apply, stamp=stamp)
        except Exception as e:  # noqa: BLE001
            print(f"FAIL {d.name}: {e}", flush=True)
            continue
        if ch:
            n_changed += 1
            n_q += len(ch)
            report.append({"paper": d.name, "changes": [
                {"q": c["q"], "old": c["old"], "new": c["new"]} for c in ch]})
            print(f"{'FIX' if args.apply else 'DIFF'} {d.name}: "
                  + ", ".join(f"Q{c['q']} {c['old']}→{c['new']}" for c in ch),
                  flush=True)
        if i % 25 == 0:
            print(f"  ...{i}/{len(dirs)}", flush=True)
    print(f"\n{'已写入' if args.apply else '待改'} {n_changed} 卷 / {n_q} 题"
          f"（共扫 {len(dirs)} 卷）")
    if args.report:
        Path(args.report).write_text(
            json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"报告 -> {args.report}")


if __name__ == "__main__":
    main()
