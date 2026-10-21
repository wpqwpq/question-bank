#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""batch_run.py — 对某个考试局/学科下的全部会话目录批量跑 切割→渲染→校验

用法:
    python scripts/batch_run.py [--subject physics] [--board edexcel] [--qual a-level]
                               [--only 2026-june-wph11,...] [--skip-render] [--json out.json]

考试局 → 切割脚本的对应（默认即可，一般不用显式传 --cut）:
    edexcel → cut_paper.py      cie → cut_paper_cie.py      aqa → cut_paper_aqa.py

产出：控制台汇总表 + （可选）JSON 明细，供人工复核清单使用。
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable
ENV = dict(os.environ)
VENV = Path(r"C:\Users\Administrator\.workbuddy\binaries\python\envs\default\Lib\site-packages")
if VENV.exists():
    ENV["PYTHONPATH"] = str(VENV)

CUT_SCRIPT = {"edexcel": "cut_paper.py", "cie": "cut_paper_cie.py", "aqa": "cut_paper_aqa.py"}
# 切割脚本按「考试局 × 学科」各一份（2026-10-17 定规）：AQA 化学走自己的脚本，
# 物理那份改不动化学、化学这份也改不动物理。
SUBJECT_CUT = {("aqa", "chemistry"): "cut_paper_aqa_chem.py"}


def run(script, d):
    # 理化共用同一套脚本（2026-10-14 合并，原先按学科选 scripts/chemistry/ 的分支已撤销）；
    # AQA/CIE 各自独立脚本（版式锚点体系不同，见各脚本头注释）。
    sp = ROOT / "scripts" / script
    t0 = time.time()
    p = subprocess.run([PY, str(sp), str(d)], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", env=ENV, cwd=str(ROOT))
    return p.returncode, round(time.time() - t0, 1), ((p.stdout or "") + (p.stderr or "")).strip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--subject", default="physics")
    ap.add_argument("--board", default="edexcel")
    ap.add_argument("--qual", default="a-level")
    ap.add_argument("--cut", default=None, help="切割脚本名（默认按 --board 自动选）")
    ap.add_argument("--only", default=None)
    ap.add_argument("--skip-render", action="store_true")
    ap.add_argument("--json", default=None)
    args = ap.parse_args()

    cut_script = (args.cut or SUBJECT_CUT.get((args.board, args.subject))
                  or CUT_SCRIPT.get(args.board))
    if not cut_script:
        sys.exit(f"未知 board={args.board}，请用 --cut 显式指定切割脚本")
    base = ROOT / "data" / args.board / args.qual / args.subject
    if not base.is_dir():
        sys.exit(f"目录不存在: {base}")
    dirs = sorted(p for p in base.iterdir() if p.is_dir() and not p.name.startswith("_"))
    if args.only:
        want = set(args.only.split(","))
        dirs = [p for p in dirs if p.name in want]

    print(f"批量跑 {len(dirs)} 卷（{base}，切割={cut_script}）\n")
    rows, t_all = [], time.time()
    for i, d in enumerate(dirs, 1):
        cut_rc, cut_t, cut_out = run(cut_script, d)
        info = {}
        cj = d / "cut.json"
        if cj.exists():
            j = json.loads(cj.read_text(encoding="utf-8"))
            qs = j.get("questions", [])
            info = dict(
                Q=len(qs),
                status={},
                warns=sum(len(q.get("review_notes") or []) for q in qs),
                max_regions=max((len(q.get("regions", [])) for q in qs), default=0),
                total_marks_ok=(j.get("marks_sum") if isinstance(j.get("marks_sum"), int) else None),
            )
            for q in qs:
                s = q.get("status", "?")
                info["status"][s] = info["status"].get(s, 0) + 1
        r_rc = v_rc = None
        r_t = v_t = 0
        r_out = v_out = ""
        if not args.skip_render and cut_rc == 0:
            r_rc, r_t, r_out = run("render_questions.py", d)
            v_rc, v_t, v_out = run("validate_cut.py", d)
        row = dict(session=d.name, cut_rc=cut_rc, cut_t=cut_t, render_rc=r_rc,
                   render_t=r_t, validate_rc=v_rc, validate_t=v_t,
                   cut_tail="\n".join(cut_out.splitlines()[-2:]),
                   validate_tail="\n".join(v_out.splitlines()[-2:]), **info)
        rows.append(row)
        ok = (cut_rc == 0 and r_rc == 0 and v_rc == 0)
        print(f"[{i:>3}/{len(dirs)}] {'✔' if ok else '✘'} {d.name:<22} "
              f"Q={info.get('Q', '-'):<4} warn={info.get('warns', '-'):<3} "
              f"{cut_t}+{r_t}+{v_t}s  {info.get('status', '')}")
        if not ok:
            print(f"        cut: {row['cut_tail'].replace(chr(10), ' ⏎ ')[:180]}")
            if r_rc is not None:
                print(f"        val: {row['validate_tail'].replace(chr(10), ' ⏎ ')[:180]}")

    n = len(rows)
    green = [r for r in rows if r["cut_rc"] == 0 and r["render_rc"] == 0 and r["validate_rc"] == 0]
    warn_rows = [r for r in rows if (r.get("warns") or 0) > 0]
    print(f"\n{'='*72}")
    print(f"总 {n} 卷；三阶段全绿 {len(green)}（{len(green)/n:.0%}）；"
          f"带警告 {len(warn_rows)} 卷")
    print(f"总耗时 {time.time()-t_all:.0f}s")
    print(f"题目总数 {sum(r.get('Q') or 0 for r in rows)}")
    red = [r for r in rows if r not in green]
    if red:
        print("\n--- 未全绿 ---")
        for r in red:
            print(f"  {r['session']:<22} cut={r['cut_rc']} render={r['render_rc']} "
                  f"val={r['validate_rc']}")
    if warn_rows:
        print("\n--- 需人工复核（有 review 警告） ---")
        for r in sorted(warn_rows, key=lambda x: -(x.get("warns") or 0)):
            print(f"  {r['session']:<22} 警告 {r['warns']:<3} Q={r.get('Q')}")

    if args.json:
        Path(args.json).write_text(json.dumps(rows, ensure_ascii=False, indent=1),
                                   encoding="utf-8")
        print(f"\n明细写入 {args.json}")


if __name__ == "__main__":
    main()
