#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""batch_biz.py — Edexcel 商务卷批量切割（每批 ≤10 卷，切→渲→校验→audit）

用法:
    python scripts/batch_biz.py --all                 # 全部未切的卷
    python scripts/batch_biz.py --match 2021-         # 只跑目录名以 2021- 开头的
    python scripts/batch_biz.py --dirs 2025-oct-wbs11,2025-oct-wbs12
    python scripts/batch_biz.py --all --skip-done     # 跳过已有 assets/q01.webp 的卷

铁律：单批不超过 10 卷；每批跑完立刻审计，有问题即停，不带病往下跑。
"""

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "edexcel" / "a-level" / "business"
BATCH_MAX = 10
PY = sys.executable


def run(cmd, tail=0):
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    out = (r.stdout or "") + (r.stderr or "")
    if r.returncode != 0:
        return r.returncode, out
    return 0, out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--match", default=None)
    ap.add_argument("--dirs", default=None)
    ap.add_argument("--skip-done", action="store_true")
    args = ap.parse_args()

    if args.dirs:
        dirs = [DATA / d.strip() for d in args.dirs.split(",") if d.strip()]
    else:
        dirs = sorted(d for d in DATA.iterdir() if d.is_dir())
        if args.match:
            dirs = [d for d in dirs if d.name.startswith(args.match)]
    if args.skip_done:
        dirs = [d for d in dirs if not (d / "assets" / "q01.webp").exists()]

    print(f"待处理 {len(dirs)} 卷，分 {-(-len(dirs) // BATCH_MAX)} 批（每批 ≤{BATCH_MAX}）")
    fails = []
    for bi in range(0, len(dirs), BATCH_MAX):
        batch = dirs[bi:bi + BATCH_MAX]
        print(f"\n===== 第 {bi // BATCH_MAX + 1} 批（{len(batch)} 卷）=====")
        for d in batch:
            if not (d / "source.pdf").exists():
                print(f"  [skip] {d.name}: 无 source.pdf")
                continue
            rc, out = run([PY, "scripts/cut_paper_biz.py", str(d)])
            if rc:
                print(f"  [FAIL cut] {d.name}\n{out[-600:]}")
                fails.append((d.name, "cut"))
                continue
            nq = out.count("已写入")
            rc, out = run([PY, "scripts/render_questions.py", str(d)])
            if rc:
                print(f"  [FAIL render] {d.name}\n{out[-600:]}")
                fails.append((d.name, "render"))
                continue
            sizes = [ln.split()[0] for ln in out.splitlines() if "-> q" in ln]
            rc, out = run([PY, "scripts/validate_cut.py", str(d)])
            ok = "PASS" in out
            if not ok:
                print(f"  [FAIL validate] {d.name}\n{out[-600:]}")
                fails.append((d.name, "validate"))
                continue
            print(f"  OK {d.name}")
        # 每批审计
        rc, out = run([PY, "scripts/audit_cut_edges.py", str(DATA)])
        risk = [ln for ln in out.splitlines() if "处" in ln]
        print("  audit:", risk[-1].strip() if risk else out.strip()[-120:])

    print(f"\n===== 完成：成功 {len(dirs) - len(fails)} / {len(dirs)} =====")
    if fails:
        print("失败：")
        for n, k in fails:
            print(f"  {n} ({k})")
        sys.exit(1)


if __name__ == "__main__":
    main()
