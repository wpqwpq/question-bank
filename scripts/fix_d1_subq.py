#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""fix_d1_subq.py — 只修 WDM11（D1）cut.json 的 `subquestions`

背景（2026-10-26）：D1 的 `collect_subs` 老实现 `SUB_RE=^\\(([a-z])\\)` 有三个毛病：
  ① `(i)` 里的 `i` 被当成**顶层字母**（因为 [a-z] 匹配 i）；
  ② `(ii)`/`(iii)` 整条丢（两位字符，正则不匹配）；
  ③ 首标签印在题号行（`4.\t (a)\t …`）时，行首不是 `(`，整条漏掉。
症状：`['a','i','b','c']`、`['b','c','d','e','f']` 这种。

现在 collect_subs 改用与 math 管线共用的 `_prefix_labels` + `labels_from_tokens`。
本脚本**只回填 subquestions**，regions / answer_block / anchors / total_marks
一律不动（D1 也不裁子图：SUB_NO_IMAGE_SUBJECTS 含 mathematics）。

用法:
    python scripts/fix_d1_subq.py <root>              # 只体检
    python scripts/fix_d1_subq.py <root> --apply      # 落盘（逐卷 .bak-<stamp>）
    python scripts/fix_d1_subq.py <root> --papers a,b --report _tmp/d1.json
"""
import argparse
import json
import shutil
import sys
import time
from pathlib import Path

import pymupdf

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from cut_paper import build_page_lines  # noqa: E402
from cut_paper_d1 import collect_subs  # noqa: E402


def patch_paper(sdir, apply=False, stamp=""):
    sp = sdir / "cut.json"
    cut = json.loads(sp.read_text(encoding="utf-8"))
    doc = pymupdf.open(sdir / "source.pdf")
    try:
        pages_lines = [build_page_lines(p) for p in doc]
    finally:
        doc.close()
    changes = []
    for q in cut["questions"]:
        regs = q.get("regions") or []
        if not regs:
            continue
        new = collect_subs(regs, pages_lines)
        old = [s["label"] for s in q.get("subquestions") or []]
        newl = [s["label"] for s in new]
        if old == newl:
            continue
        changes.append({"q": q["number"], "old": old, "new": newl})
        if apply:
            q["subquestions"] = new
    if apply and changes:
        shutil.copy2(sp, sp.with_name(f"cut.json.bak-{stamp}"))
        sp.write_text(json.dumps(cut, ensure_ascii=False, indent=1) + "\n",
                      encoding="utf-8")
    return changes


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("root")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--papers", default="")
    ap.add_argument("--report", default="")
    ap.add_argument("--stamp", default="")
    args = ap.parse_args()

    root = Path(args.root)
    only = {p.strip() for p in args.papers.split(",") if p.strip()}
    # 🔴 只认 D1（WDM11）：本脚本的 collect_subs 是 D1 版式（QP 册 + 作答册），
    #    套到 inline 版式的 265 卷 WMA/WFM/WST/WME 上会算错标签。
    dirs = sorted(p for p in root.iterdir() if p.is_dir()
                  and (p / "cut.json").exists() and (p / "source.pdf").exists()
                  and "wdm11" in p.name.lower()
                  and (not only or p.name in only))
    print(f"[卷] {len(dirs)} 卷")
    stamp = args.stamp or time.strftime("%Y%m%d-%H%M%S")
    report, n_paper, n_q = [], 0, 0
    for d in dirs:
        try:
            ch = patch_paper(d, apply=args.apply, stamp=stamp)
        except Exception as e:  # noqa: BLE001
            print(f"FAIL {d.name}: {e}", flush=True)
            continue
        if ch:
            n_paper += 1
            n_q += len(ch)
            report.append({"paper": d.name, "changes": ch})
            print(f"{'FIX' if args.apply else 'DIFF'} {d.name}: "
                  + ", ".join(f"Q{c['q']} {c['old']}→{c['new']}" for c in ch),
                  flush=True)
    print(f"\n{'已写入' if args.apply else '待改'} {n_paper} 卷 / {n_q} 题")
    if args.report:
        Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=1),
                                     encoding="utf-8")
        print(f"报告 -> {args.report}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
