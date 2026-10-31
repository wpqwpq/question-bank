#!/usr/bin/env python3
"""通用回退：从「按小问切子图」退回「按小问打标但不产子图」（AQA/CIE 语义）。

2026-10-29 首用于 Edexcel IAL 商务（王培琪定：edexcel 商务 alevel 不再切子图）。
逻辑与 scripts/rollback_u3u6_subimg.py 同一套，只是把学科/考试局/根目录/卷号过滤做成参数。

做什么（只碰 3 件事，其余一字不动）：
  1. 子题 md 的 front-matter 里删掉 `image:` / `layout:` 两行 —— 前端 image_url
     变 null，预览自动回退整题图（QuestionViewer: imageOverride ?? detail.image_url）。
  2. 删磁盘上对应的 qNN-<label>.webp 与 qNN-<label>.layout.json。
  3. rebuild_paper 重灌索引（索引里缓存着 image 列）。

不做什么：
  · 不删子题 md —— 小问标签、主标签、难度全在原处，一个不动。
  · 不动 state.db ——「核对完成」按 qid 存；「此题有误」的指纹只校验**主题 md**
    （qNN.md）+ 题图，子题 md 不在指纹里，所以标记不会因此失效。
  · 不碰 subject / paper 目录结构、不碰 cut.json、不碰主题图 qNN.webp。

用法：
  venv/bin/python scripts/rollback_subimg.py --board edexcel --qual a-level \
      --subject business                     # 干跑，只打印
  venv/bin/python scripts/rollback_subimg.py --board edexcel --qual a-level \
      --subject business --write             # 真改
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import tarfile
import time
from pathlib import Path

QBANK = Path("/opt/qbank")
BACKUP = QBANK / "_backup"
STATE_DB = QBANK / "var" / "state.db"
BACKEND = QBANK / "workbench" / "backend"

SUB_MD_RE = re.compile(r"^q\d+(-[a-z0-9]+)+\.md$")
DROP_RE = re.compile(r"^(image|layout):")
KEEP_FM_KEYS = ("id", "parent", "sub_label", "tags", "primary_tag", "difficulty", "question_number")


def strip_image_fields(text: str) -> tuple[str, list[str]]:
    """只删 front-matter（前两个 --- 之间）里的 image/layout 行，其余字节原样保留。"""
    lines = text.splitlines(keepends=True)
    bounds = [i for i, ln in enumerate(lines) if ln.rstrip("\r\n").strip() == "---"]
    if len(bounds) < 2:
        return text, []
    a, b = bounds[0], bounds[1]
    out: list[str] = []
    dropped: list[str] = []
    for i, ln in enumerate(lines):
        if a < i < b and DROP_RE.match(ln):
            dropped.append(ln.rstrip("\r\n"))
            continue
        out.append(ln)
    return "".join(out), dropped


def fm_snapshot(text: str) -> dict:
    """取出必须原样保住的字段（用 yaml，跟后端同一套解析）。"""
    import yaml
    m = re.match(r"\A---[ \t]*\r?\n(.*?)\r?\n---", text, re.DOTALL)
    if not m:
        return {}
    d = yaml.safe_load(m.group(1)) or {}
    return {k: d.get(k) for k in KEEP_FM_KEYS}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--board", required=True)
    ap.add_argument("--qual", required=True, help="a-level / igcse")
    ap.add_argument("--subject", required=True)
    ap.add_argument("--root", default="", help="默认 <qbank>/data/<board>/<qual>/<subject>")
    ap.add_argument("--only", default="", help="只处理卷名含该子串的卷（逗号分隔）")
    ap.add_argument("--write", action="store_true", help="真的改盘（默认干跑）")
    args = ap.parse_args()

    root = Path(args.root) if args.root else QBANK / "data" / args.board / args.qual / args.subject
    if not root.is_dir():
        print(f"目录不存在：{root}", file=sys.stderr)
        return 1

    papers = sorted(d for d in root.iterdir() if d.is_dir())
    if args.only:
        keys = [k.strip() for k in args.only.split(",") if k.strip()]
        papers = [d for d in papers if any(k in d.name for k in keys)]
    if not papers:
        print("没有匹配的卷目录", file=sys.stderr)
        return 1

    # ---- 标记现状（只报，不拦：指纹只认主题 md + 题图，子题 md 不在里面）----
    flagged: list[str] = []
    if STATE_DB.exists():
        con = sqlite3.connect(f"file:{STATE_DB}?mode=ro", uri=True)
        try:
            flagged = [r[0] for r in con.execute("select qid from flags").fetchall()]
        finally:
            con.close()
    stem_prefix = f"{args.board}-{args.qual}-{args.subject}-"
    flagged = [q for q in flagged if q.startswith(stem_prefix)]

    # ---- 收集目标 ----
    plan: list[tuple[Path, str]] = []
    files_to_delete: list[Path] = []
    per_paper: list[tuple[Path, int, int]] = []
    for d in papers:
        subs = [p for p in sorted(d.glob("q*.md")) if SUB_MD_RE.match(p.name)]
        if not subs:
            continue
        assets = d / "assets"
        n_img = 0
        for p in subs:
            old = p.read_text(encoding="utf-8")
            new, dropped = strip_image_fields(old)
            if dropped:
                plan.append((p, new))
            for ext in ("webp", "layout.json"):
                f = assets / f"{p.stem}.{ext}"
                if f.exists():
                    files_to_delete.append(f)
                    if ext == "webp":
                        n_img += 1
        per_paper.append((d, len(subs), n_img))
        print(f"  {d.name:26} 子题 {len(subs):3d}  子图 {n_img:3d}")

    print(f"\n卷 {len(per_paper)} 个；子题 md {sum(x[1] for x in per_paper)} 个，"
          f"需改 {len(plan)} 个；需删子图 {len([f for f in files_to_delete if f.suffix == '.webp'])} 张"
          f"（含 layout.json 共 {len(files_to_delete)} 个文件）")
    if flagged:
        print(f"⚠ 本学科现有「此题有误」标记 {len(flagged)} 条（不受影响，改完仍在）：")
        for q in flagged:
            print("   ", q)
    if not plan and not files_to_delete:
        print("已经是目标状态，无需改动。")
        return 0

    if not args.write:
        print("\n[干跑] 未改动任何文件。加 --write 执行。")
        return 0

    # ---- 备份（打 tar.gz 后再动盘）----
    BACKUP.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    tag = f"{args.board}-{args.qual}-{args.subject}-nosubimg"
    tar_path = BACKUP / f"{tag}-{stamp}.tar.gz"
    with tarfile.open(tar_path, "w:gz") as tf:
        for d, _n, _i in per_paper:
            tf.add(d, arcname=d.name)
    print(f"\n已备份 → {tar_path}  ({tar_path.stat().st_size / 1024:.0f} KB)")

    # ---- 改 md ----
    changed = 0
    bad: list[str] = []
    for p, new in plan:
        old = p.read_text(encoding="utf-8")
        before = fm_snapshot(old)
        after = fm_snapshot(new)
        if before != after:
            bad.append(f"{p.name}: {before} → {after}")
            continue
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(new, encoding="utf-8", newline="")   # 保留原行尾
        tmp.replace(p)
        changed += 1
    if bad:
        print("❌ 有文件在改写中丢了标注字段，已中止（前几个已改，需回滚备份）：")
        for b in bad[:10]:
            print("   ", b)
        return 3
    print(f"已改 md：{changed} 个（标签/难度/主标签逐项校验一致）")

    # ---- 删子图 ----
    removed = 0
    for f in files_to_delete:
        try:
            f.unlink()
            removed += 1
        except OSError as e:
            print(f"   ⚠ 删不掉 {f}: {e}")
    print(f"已删子图文件：{removed} 个（含 webp / layout.json）")

    # ---- 重灌索引 ----
    sys.path.insert(0, str(BACKEND))
    import index_db  # noqa: E402
    rebuilt: list[str] = []
    fails: list[str] = []
    for d, _n, _i in per_paper:
        try:
            index_db.rebuild_paper(d, args.board, args.qual, args.subject)
            rebuilt.append(d.name)
        except Exception as e:  # noqa: BLE001
            fails.append(f"{d.name}: {e}")
    print(f"已重建索引：{len(rebuilt)} 卷" + (f"，失败 {len(fails)}" if fails else ""))
    for f in fails:
        print("   ⚠", f)

    report = {
        "stamp": stamp,
        "target": {"board": args.board, "qual": args.qual, "subject": args.subject, "root": str(root)},
        "backup": str(tar_path),
        "papers": [d.name for d, _n, _i in per_paper],
        "sub_md_changed": changed,
        "image_files_removed": removed,
        "index_rebuilt": len(rebuilt),
        "index_failed": fails,
        "flagged_untouched": flagged,
    }
    out = BACKUP / f"{tag}-{stamp}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n报告 → {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
