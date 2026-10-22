#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""batch_aqa_fmath.py — AQA 进阶数学（9665）「切割 → 渲染 → 校验 → 边界体检」一条龙（分批）

用法:
    python scripts/batch_aqa_math.py --offset 0 --limit 10
    python scripts/batch_aqa_math.py --offset 10 --limit 10
    python scripts/batch_aqa_math.py --sessions 2020-jan-ma01,2020-jan-ma02

规矩（王培琪 2026-10-21 定）:
    · **单批最多 10 卷**；
    · **已验收的卷永不重切重渲染**：`<session_dir>/.done` 存在即跳过，
      要强制重跑必须 `--force`（一次只对明确要改的卷用）；
    · 每卷做完立刻跑 audit_cut_edges，有风险就地标出来，不攒到最后。
"""

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASE = ROOT / "data" / "aqa" / "a-level" / "further-mathematics"
PY = sys.executable


def run(cmd):
    return subprocess.run([PY] + cmd, capture_output=True, text=True,
                          cwd=str(ROOT), encoding="utf-8", errors="replace")


def sessions(args):
    dirs = sorted(d for d in BASE.iterdir() if d.is_dir()) if BASE.exists() else []
    if args.sessions:
        want = {s.strip() for s in args.sessions.split(",")}
        return [d for d in dirs if d.name in want]
    return dirs[args.offset:args.offset + args.limit]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--offset", type=int, default=0)
    ap.add_argument("--sessions", default=None)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--skip-audit", action="store_true")
    args = ap.parse_args()
    if args.limit > 10 and not args.sessions:
        sys.exit("单批最多 10 卷（王培琪 2026-10-21 定）")

    targets = sessions(args)
    if not targets:
        sys.exit("没有匹配的 session 目录")

    ok, skipped, failed = [], [], []
    t0 = time.time()
    for i, d in enumerate(targets, 1):
        tag = f"[{i:2d}/{len(targets)}] {d.name}"
        if (d / ".done").exists() and not args.force:
            print(f"{tag}  SKIP（已验收）", flush=True)
            skipped.append(d.name)
            continue
        if not (d / "source.pdf").exists():
            print(f"{tag}  ✗ 缺 source.pdf", flush=True)
            failed.append((d.name, "缺 source.pdf"))
            continue

        r = run(["scripts/cut_paper_aqa_fmath.py", str(d)] + (["--force"] if args.force else []))
        if r.returncode != 0:
            print(f"{tag}  ✗ 切割失败\n{r.stdout}{r.stderr}", flush=True)
            failed.append((d.name, "切割失败"))
            continue
        line = (r.stdout or "").strip().splitlines()[0]
        warn = "\n".join(l for l in (r.stdout or "").splitlines() if l.strip().startswith("!"))

        r = run(["scripts/render_questions.py", str(d)])
        if r.returncode != 0:
            print(f"{tag}  ✗ 渲染失败\n{r.stdout[-600:]}{r.stderr[-600:]}", flush=True)
            failed.append((d.name, "渲染失败"))
            continue

        r = run(["scripts/validate_cut.py", str(d)])
        vline = (r.stdout or "").strip().splitlines()[-1] if r.stdout.strip() else ""
        if r.returncode != 0:
            print(f"{tag}  ✗ 校验失败：{vline}", flush=True)
            failed.append((d.name, f"校验失败：{vline}"))
            continue

        a_risk = 0
        if not args.skip_audit:
            r = run(["scripts/audit_cut_edges.py", str(d)])
            aout = (r.stdout or "") + (r.stderr or "")
            for ln in aout.splitlines():
                if "处" in ln and d.name in ln:
                    try:
                        a_risk += int(ln.split(":")[1].strip().split()[0])
                    except Exception:
                        pass

        cut = json.loads((d / "cut.json").read_text(encoding="utf-8"))
        (d / ".done").write_text(json.dumps({
            "at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "questions": len(cut["questions"]),
            "marks": sum(q.get("total_marks", 0) for q in cut["questions"]),
            "segments": sum(len(q["regions"]) for q in cut["questions"]),
            "audit_risk": a_risk,
        }, ensure_ascii=False) + "\n", encoding="utf-8")
        ok.append((d.name, line, a_risk))
        print(f"{tag}  {line}" + (f"  | audit {a_risk}" if a_risk else "")
              + ("\n" + warn if warn else ""), flush=True)

    dt = time.time() - t0
    print(f"\n本批完成：成功 {len(ok)}、跳过 {len(skipped)}、失败 {len(failed)}，用时 {dt/60:.1f} 分")
    risk = [x for x in ok if x[2]]
    if risk:
        print("⚠ 有边界风险：")
        for n, _l, a in risk:
            print(f"    {n}: {a} 处")
    if failed:
        print("✗ 失败清单：")
        for n, why in failed:
            print(f"    {n}: {why}")
        sys.exit(1)


if __name__ == "__main__":
    main()
