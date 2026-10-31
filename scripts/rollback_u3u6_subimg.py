#!/usr/bin/env python3
"""U3/U6 回退：从「按小问切子图」退回「按小问打标但不产子图」（AQA/CIE 语义）。

2026-10-27 王培琪定：Edexcel IAL 物理 U3（WPH13）/ U6（WPH16）不再切子图。

做什么（只碰 3 件事，其余一字不动）：
  1. 子题 md 的 front-matter 里删掉 `image:` / `layout:` 两行 —— 前端 image_url
     变 null，预览自动回退整题图（QuestionViewer: imageOverride ?? detail.image_url）。
  2. 删磁盘上对应的 qNN-<label>.webp 与 qNN-<label>.layout.json。
  3. rebuild_paper 重灌索引（索引里缓存着 image 列）。

不做什么：
  · 不删子题 md —— 小问标签、主标签、难度全在原处（45 个小问标签是老师打的，一个不动）。
  · 不动 state.db —— 「核对完成」在 confirms 表按 qid 存，且不校验文件指纹，改 md 不掉。
  · 不碰 subject / paper 目录结构、不碰 cut.json、不碰主题图 qNN.webp。

⚠️ 「此题有误」标记（flags 表）校验 md 的 (mtime,size,sha256) 指纹 —— 改 md 会让标记
   自动消失。运行前先查一遍；本批 3 卷均无标记（2026-10-27 12:55 实测），脚本仍会拦。

用法：
  venv/bin/python scripts/rollback_u3u6_subimg.py            # 干跑，只打印
  venv/bin/python scripts/rollback_u3u6_subimg.py --write    # 真改
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

ROOT = Path("/opt/qbank/data/edexcel/a-level/physics")
BACKUP = Path("/opt/qbank/_backup")
STATE_DB = Path("/opt/qbank/var/state.db")
BACKEND = Path("/opt/qbank/workbench/backend")

SUB_MD_RE = re.compile(r"^q\d+(-[a-z0-9]+)+\.md$")
DROP_RE = re.compile(r"^(image|layout):")
KEEP_FM_KEYS = ("id", "parent", "sub_label", "tags", "primary_tag", "difficulty", "question_number")
UNIT_RE = re.compile(r"wph(13|16)")


def strip_image_fields(text: str) -> tuple[str, int, list[str]]:
    """只删 front-matter（前两个 --- 之间）里的 image/layout 行，其余字节原样保留。"""
    lines = text.splitlines(keepends=True)
    bounds = [i for i, ln in enumerate(lines) if ln.rstrip("\r\n").strip() == "---"]
    if len(bounds) < 2:
        return text, 0, []
    a, b = bounds[0], bounds[1]
    out: list[str] = []
    dropped: list[str] = []
    for i, ln in enumerate(lines):
        if a < i < b and DROP_RE.match(ln):
            dropped.append(ln.rstrip("\r\n"))
            continue
        out.append(ln)
    return "".join(out), len(dropped), dropped


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
    ap.add_argument("--write", action="store_true", help="真的改盘（默认干跑）")
    args = ap.parse_args()

    papers = sorted(d for d in ROOT.iterdir() if d.is_dir() and UNIT_RE.search(d.name))
    if not papers:
        print("找不到 U3/U6 卷", file=sys.stderr)
        return 1

    # ---- 前置闸 1：flags 表不能有本批的题（改 md 会让标记失效）----
    flagged: set[str] = set()
    if STATE_DB.exists():
        con = sqlite3.connect(f"file:{STATE_DB}?mode=ro", uri=True)
        try:
            rows = con.execute(
                "select qid from flags where qid like '%wph13%' or qid like '%wph16%'"
            ).fetchall()
            flagged = {r[0] for r in rows}
        finally:
            con.close()
    if flagged:
        print("❌ 这些题带「有误」标记，改 md 会让它消失，先人工处理：")
        for q in sorted(flagged):
            print("   ", q)
        return 2

    # ---- 收集目标 ----
    plan: list[tuple[Path, str, list[str]]] = []   # (子题md, 新文本, 被删的字段行)
    img_files: list[Path] = []
    n_sub = 0
    for d in papers:
        subs = [p for p in sorted(d.glob("q*.md")) if SUB_MD_RE.match(p.name)]
        if not subs:
            continue
        n_sub += len(subs)
        assets = d / "assets"
        for p in subs:
            old = p.read_text(encoding="utf-8")
            new, n, dropped = strip_image_fields(old)
            if n:
                plan.append((p, new, dropped))
            for ext in ("webp", "layout.json"):
                f = assets / f"{p.stem}.{ext}"
                if f.exists():
                    img_files.append(f)
        print(f"{d.name:24} 子题 {len(subs):3d}  待删图 {len([x for x in img_files if x.parent == assets]):3d}")

    print(f"\n合计：子题 md {n_sub} 个，需改 {len(plan)} 个，需删图 {len(img_files)} 个")
    if not plan and not img_files:
        print("已经是目标状态，无需改动。")
        return 0

    if not args.write:
        print("\n[干跑] 未改动任何文件。加 --write 执行。")
        return 0

    # ---- 备份（打 tar.gz 后再动盘）----
    BACKUP.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    tar_path = BACKUP / f"u3u6-nosubimg-{stamp}.tar.gz"
    with tarfile.open(tar_path, "w:gz") as tf:
        for d in papers:
            if d.is_dir():
                tf.add(d, arcname=str(d.relative_to(ROOT)))
    print(f"\n已备份 → {tar_path}  ({tar_path.stat().st_size / 1024:.0f} KB)")

    # ---- 改 md ----
    changed = 0
    bad: list[str] = []
    for p, new, _dropped in plan:
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
    for f in img_files:
        try:
            f.unlink()
            removed += 1
        except OSError as e:
            print(f"   ⚠ 删不掉 {f}: {e}")
    print(f"已删子图文件：{removed} 个（含 webp / layout.json）")

    # ---- 重灌索引（3 卷）----
    sys.path.insert(0, str(BACKEND))
    import index_db  # noqa: E402
    rebuilt = []
    for d in papers:
        has_sub = any(SUB_MD_RE.match(p.name) for p in d.glob("q*.md"))
        if not has_sub:
            continue
        try:
            index_db.rebuild_paper(d, "edexcel", "a-level", "physics")
            rebuilt.append(d.name)
        except Exception as e:  # noqa: BLE001
            print(f"   ⚠ 重建索引失败 {d.name}: {e}")
    print(f"已重建索引：{len(rebuilt)} 卷 → {', '.join(rebuilt)}")

    report = {
        "stamp": stamp,
        "backup": str(tar_path),
        "papers": [d.name for d in papers],
        "sub_md_changed": changed,
        "image_files_removed": removed,
        "index_rebuilt": rebuilt,
    }
    out = BACKUP / f"u3u6-nosubimg-{stamp}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n报告 → {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
