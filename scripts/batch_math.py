#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""batch_math.py — Edexcel IAL 数学卷「导入 → 切割 → 渲染 → 校验」一条龙（分批）

用法:
    python scripts/batch_math.py --offset 0 --limit 20
    python scripts/batch_math.py --offset 20 --limit 20

规矩（王培琪 2026-10-19 定）:
    · **单批最多 20 卷**，出问题就停在这一批改，改完再跑；
    · **已验收的卷子永不重切重渲染**：`<session_dir>/.done` 存在即跳过，
      要强制重跑必须 `--force`（一次只对明确要改的卷用）。

产出: `data/edexcel/a-level/mathematics/<session>/` 下 cut.json / qNN.md /
      assets/qNN.webp / layout.json；全部通过校验后写 `.done`。
"""

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

import pymupdf

sys.path.insert(0, str(Path(__file__).resolve().parent))
import import_math as IM  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable


def run(cmd, **kw):
    """子进程继承当前环境（调用方已设好 PYTHONPATH）。"""
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def batch_dirs(source, limit, offset, only=None):
    """本批对应的 session 目录（按源文件排序，与 import_math 一致）。"""
    items = IM.collect((ROOT / source).resolve(), only)
    out = []
    for it in items[offset:offset + limit]:
        doc = pymupdf.open(it["pdf"])
        meta = IM.meta_from_cover(doc, it["unit"], it["var"],
                                  it["year"], it["session"])
        year, session = it["year"], it["session"]
        if meta.get("exam_date"):
            y, mo = meta["exam_date"].split("-")[:2]
            if mo in IM.DATE_TO_SESSION:
                year, session = int(y), IM.DATE_TO_SESSION[mo]
        out.append((IM.DATA / f"{year}-{session}-{it['code']}",
                    f"{year}-{session}-{it['code']}"))
    return out


def main():
    ap = argparse.ArgumentParser(description="数学卷批量导入+切割+渲染+校验")
    ap.add_argument("--source", default="downloadPapers/Edexcel_ALevel_Math")
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--offset", type=int, default=0)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--skip-import", action="store_true")
    ap.add_argument("--units", help="只跑这些单元（逗号分隔，如 WDM11）")
    args = ap.parse_args()
    only = {u.strip().upper() for u in args.units.split(",")} if args.units else None
    if args.limit > 20:
        sys.exit("单批最多 20 卷（王培琪 2026-10-19 定）")

    if not args.skip_import:
        cmd = [PY, str(ROOT / "scripts" / "import_math.py"), "--apply",
               "--source", args.source, "--limit", str(args.limit),
               "--offset", str(args.offset)]
        if args.units:
            cmd += ["--units", args.units]
        r = run(cmd)
        if r.returncode != 0:
            sys.exit("导入失败:\n" + r.stdout + r.stderr)

    dirs = batch_dirs(args.source, args.limit, args.offset, only)
    print(f"\n[批次] {args.offset + 1}–{args.offset + len(dirs)} / 共 265 卷，"
          f"{len(dirs)} 卷\n")
    ok, skipped, failed = [], [], []
    t0 = time.time()
    for i, (d, name) in enumerate(dirs, 1):
        tag = f"[{i:2d}/{len(dirs)}] {name}"
        if (d / ".done").exists() and not args.force:
            print(f"{tag}  SKIP（已验收）")
            skipped.append(name)
            continue
        if not (d / "source.pdf").exists():
            print(f"{tag}  ✗ 缺 source.pdf")
            failed.append((name, "缺 source.pdf"))
            continue
        force = ["--force"] if args.force else []
        # D1（WDM11）版式特殊：题面 + 独立作答册，走 cut_paper_d1.py
        cutter = "cut_paper_d1.py" if d.name.endswith("wdm11") or "wdm11" in d.name \
            else "cut_paper_math.py"
        r1 = run([PY, str(ROOT / "scripts" / cutter), str(d)] + force)
        if r1.returncode != 0:
            print(f"{tag}  ✗ 切割失败\n{r1.stdout}{r1.stderr}")
            failed.append((name, "切割失败"))
            continue
        line = (r1.stdout or "").strip().splitlines()[0]
        r2 = run([PY, str(ROOT / "scripts" / "render_questions.py"), str(d)])
        if r2.returncode != 0:
            print(f"{tag}  ✗ 渲染失败\n{r2.stdout[-500:]}{r2.stderr[-500:]}")
            failed.append((name, "渲染失败"))
            continue
        r3 = run([PY, str(ROOT / "scripts" / "validate_cut.py"), str(d)])
        if r3.returncode != 0:
            print(f"{tag}  ✗ 校验失败\n{r3.stdout[-800:]}{r3.stderr[-400:]}")
            failed.append((name, "校验失败"))
            continue
        cut = json.loads((d / "cut.json").read_text(encoding="utf-8"))
        (d / ".done").write_text(
            json.dumps({"at": time.strftime("%Y-%m-%d %H:%M:%S"),
                        "questions": len(cut["questions"]),
                        "marks": sum(q.get("total_marks", 0)
                                     for q in cut["questions"])},
                       ensure_ascii=False), encoding="utf-8")
        print(f"{tag}  ✓ {line}")
        ok.append(name)

    print(f"\n完成：成功 {len(ok)} / 跳过 {len(skipped)} / 失败 {len(failed)}"
          f"　耗时 {time.time() - t0:.0f}s")
    for n, why in failed:
        print(f"  ✗ {n}: {why}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
