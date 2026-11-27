#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""编码规范迁移：把 md 内容与目录名收敛到统一规则。

规则（详见 docs/135-编码规范治理方案-20261120.md）：
  R1  学段目录/字段   alevel           → a-level
  R2  考季目录/字段   march            → mar
  R3  学科目录/字段   combined_science → combined-science
  R4  paper_code      md 原值          → 目录 code（小写原样）
  R5  id 中间段       带 URN           → 去掉
  R6  parent          指向旧父 id      → 指向新父 id

**不做的事**：不改文件名、不动题图 / layout / cut.json、不改 question_number、
不改 paper_unit、不改 exam_paper_id（URN 原始值，留作溯源）。

用法：
    python scripts/migrate_naming.py --dry-run [--csv _tmp/plan.csv]
    python scripts/migrate_naming.py --apply [--log _tmp/migrate.log] [--limit N]

--dry-run  只打印计划，不写任何文件（除 --csv）
--apply    真正执行：改 md → 改名目录 → 产出 var/legacy_ids.sqlite

幂等：重复跑不会改动已规范的卷。
"""
from __future__ import annotations

import argparse
import csv
import os
import re
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
VAR = ROOT / "var"
BACKUP = ROOT / "_backup"
SKIP_DIRS = {"taxonomy", "_review", "_audit"}

PAPER_DIR_RE = re.compile(r"^(\d{4})-([a-z]+)-(.+)$")
MAIN_Q_RE = re.compile(r"^q\d+\.md$")
ANY_Q_RE = re.compile(r"^q\d+.*\.md$")

QUAL_MAP = {"alevel": "a-level", "a-level": "a-level", "igcse": "igcse", "gcse": "gcse"}
SESSION_MAP = {"march": "mar"}


def norm_qual(v: str) -> str:
    k = v.strip().lower()
    return QUAL_MAP.get(k, k)


def norm_session(v: str) -> str:
    k = v.strip().lower()
    return SESSION_MAP.get(k, k)


def norm_subject(v: str) -> str:
    return v.strip().lower().replace("_", "-")


def read_fm(path: Path) -> tuple[dict[str, str], int] | tuple[None, int]:
    """读 front-matter。返回 (字段字典, front-matter 结束行号)。

    ⚠️ 必须逐行读到结束标记，不能按字节截断：`paper_unit` 在 `subquestions`
    之后出现，2KB 截断会漏读（2026-11-20 踩过，导致 unit 验证假阳性 1063 卷）。
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None, -1
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return None, -1
    end = -1
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end < 0:
        return None, -1
    fm: dict[str, str] = {}
    for line in lines[1:end]:
        if not line or line[0] in (" ", "\t", "-", "#"):
            continue
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        fm[k.strip()] = v.strip().strip('"').strip("'")
    return fm, end


def patch_fm(path: Path, updates: dict[str, str], end: int) -> list[tuple[str, str, str]]:
    """逐行替换 front-matter 中已存在的字段，正文与其他行一个字节不动。

    只改**已存在**的键：子题没有 paper_unit，不能凭空加。
    返回 [(key, old, new), ...]。
    """
    text = path.read_text(encoding="utf-8")
    lines = text.split("\n")
    changed: list[tuple[str, str, str]] = []
    for i in range(1, end):
        line = lines[i]
        if not line or line[0] in (" ", "\t", "-", "#") or ":" not in line:
            continue
        k, v = line.split(":", 1)
        key = k.strip()
        if key not in updates:
            continue
        old = v.strip().strip('"').strip("'")
        new = updates[key]
        if old == new:
            continue
        lines[i] = f"{key}: {new}"
        changed.append((key, old, new))
    if changed:
        tmp = path.with_suffix(path.suffix + ".tnmp")
        with tmp.open("w", encoding="utf-8", newline="\n") as f:
            f.write("\n".join(lines))
        os.replace(tmp, path)
    return changed


@dataclass
class Plan:
    board: str
    qual_old: str
    subject_old: str
    dir_old: str
    qual_new: str
    subject_new: str
    dir_new: str
    code_new: str
    path_old: Path
    path_new: Path
    mds: list[Path] = field(default_factory=list)

    @property
    def dir_changed(self) -> bool:
        return self.dir_old != self.dir_new or self.qual_old != self.qual_new \
            or self.subject_old != self.subject_new


def build_plans() -> tuple[list[Plan], list[str]]:
    plans: list[Plan] = []
    warns: list[str] = []
    for board_dir in sorted(p for p in DATA.iterdir() if p.is_dir() and p.name not in SKIP_DIRS):
        for qual_dir in sorted(p for p in board_dir.iterdir() if p.is_dir()):
            for subj_dir in sorted(p for p in qual_dir.iterdir() if p.is_dir()):
                for pd in sorted(p for p in subj_dir.iterdir() if p.is_dir()):
                    m = PAPER_DIR_RE.match(pd.name)
                    if not m:
                        warns.append(f"卷目录名不符 <year>-<session>-<code>：{pd}")
                        continue
                    year, sess, code = m.groups()
                    new_qual = norm_qual(qual_dir.name)
                    new_subj = norm_subject(subj_dir.name)
                    new_sess = norm_session(sess)
                    new_dir = f"{year}-{new_sess}-{code}"

                    if new_qual == qual_dir.name and new_subj == subj_dir.name and new_dir == pd.name:
                        # 目录本身无需改名，但 md 里仍可能有 URN / paper_code 待修
                        pass

                    path_new = board_dir / new_qual / new_subj / new_dir
                    plans.append(Plan(
                        board=board_dir.name,
                        qual_old=qual_dir.name, subject_old=subj_dir.name, dir_old=pd.name,
                        qual_new=new_qual, subject_new=new_subj, dir_new=new_dir,
                        code_new=code, path_old=pd, path_new=path_new,
                        mds=sorted(p for p in pd.glob("q*.md") if ANY_Q_RE.match(p.name))
                            + ([pd / "paper.md"] if (pd / "paper.md").exists() else []),
                    ))
    return plans, warns


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--csv", default="")
    ap.add_argument("--log", default="")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()
    if not args.dry_run and not args.apply:
        print("必须指定 --dry-run 或 --apply", file=sys.stderr)
        return 2

    t0 = time.time()
    plans, warns = build_plans()
    if args.limit:
        plans = plans[: args.limit]
    print(f"扫描 {len(plans)} 卷" + (f"（--limit {args.limit}）" if args.limit else ""))
    for w in warns[:10]:
        print(f"  ⚠️ {w}")

    stats = {"md_changed": 0, "dir_moved": 0, "qid_map": 0, "paper_map": 0,
             "skip_dirs": 0, "no_fm": 0, "orphan_parent": 0}
    rows: list[tuple[str, str, str, str, str, str]] = []
    legacy_q: list[tuple[str, str]] = []
    legacy_p: list[tuple[str, str]] = []
    log_lines: list[str] = []

    if args.apply:
        VAR.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(VAR / "legacy_ids.sqlite")
        db.executescript(
            "CREATE TABLE IF NOT EXISTS qid_map (old TEXT PRIMARY KEY, new TEXT NOT NULL);"
            "CREATE TABLE IF NOT EXISTS paper_map (old TEXT PRIMARY KEY, new TEXT NOT NULL);"
        )
        db.commit()

    for idx, plan in enumerate(plans, 1):
        old_prefix = f"{plan.board}-{plan.qual_old}-{plan.subject_old}-{plan.dir_old}"
        new_prefix = f"{plan.board}-{plan.qual_new}-{plan.subject_new}-{plan.dir_new}"
        base_updates = {
            "qualification": plan.qual_new,
            "subject": plan.subject_new,
            "session": norm_session(PAPER_DIR_RE.match(plan.dir_old).group(2)),  # type: ignore[union-attr]
            "paper_code": plan.code_new,
        }

        # ── 第一遍：读旧 id，建立 旧→新 映射 ──
        id_map: dict[str, str] = {}
        fm_cache: dict[Path, tuple[dict[str, str], int]] = {}
        for md in plan.mds:
            fm, end = read_fm(md)
            if fm is None:
                stats["no_fm"] += 1
                if args.log:
                    log_lines.append(f"[NO_FM] {md}")
                continue
            fm_cache[md] = (fm, end)
            old_id = fm.get("id", "")
            if not old_id:
                continue
            if md.name == "paper.md":
                new_id = new_prefix
                legacy_p.append((old_id, new_id))
            else:
                new_id = f"{new_prefix}-{md.stem}"
                legacy_q.append((old_id, new_id))
            id_map[old_id] = new_id

        # ── 第二遍：改写 ──
        for md, (fm, end) in fm_cache.items():
            updates = dict(base_updates)
            old_id = fm.get("id", "")
            if old_id and old_id in id_map:
                updates["id"] = id_map[old_id]
            parent = fm.get("parent", "")
            if parent:
                if parent in id_map:
                    updates["parent"] = id_map[parent]
                else:
                    # 兜底：按后缀拼新前缀（父 id 不在本卷映射里 = 异常）
                    suffix = parent[len(old_prefix):] if parent.startswith(old_prefix) else ""
                    tail = suffix.split("-")[-1] if suffix else ""
                    if tail:
                        updates["parent"] = f"{new_prefix}-{tail}"
                        stats["orphan_parent"] += 1
                        if args.log:
                            log_lines.append(f"[ORPHAN_PARENT] {md} {parent}")
            # 只保留 md 里真实存在的键
            updates = {k: v for k, v in updates.items() if k in fm}
            if old_id and old_id in id_map:
                updates["id"] = id_map[old_id]

            if args.dry_run:
                for k, v in updates.items():
                    if fm.get(k, "") != v:
                        stats["md_changed"] += 1
                        rows.append((str(md.relative_to(ROOT)), k, fm.get(k, ""), v,
                                     "id" if k == "parent" else "", ""))
                continue

            changed = patch_fm(md, updates, end)
            if changed:
                stats["md_changed"] += 1
                if args.log:
                    for k, o, n in changed:
                        log_lines.append(f"[MD] {md.relative_to(ROOT)} {k}: {o} -> {n}")

        # ── 目录改名 ──
        if plan.dir_changed:
            if plan.path_new.exists():
                stats["skip_dirs"] += 1
                if args.log:
                    log_lines.append(f"[SKIP_DIR_EXISTS] {plan.path_new}")
            elif args.dry_run:
                stats["dir_moved"] += 1
                rows.append((str(plan.path_old.relative_to(ROOT)), "DIR", plan.dir_old,
                             plan.dir_new, f"{plan.qual_old}->{plan.qual_new};"
                             f"{plan.subject_old}->{plan.subject_new}", "mv"))
            else:
                plan.path_new.parent.mkdir(parents=True, exist_ok=True)
                plan.path_old.rename(plan.path_new)
                stats["dir_moved"] += 1
                if args.log:
                    log_lines.append(f"[MV] {plan.path_old.relative_to(ROOT)} -> "
                                     f"{plan.path_new.relative_to(ROOT)}")

        if idx % 500 == 0:
            print(f"  … {idx}/{len(plans)}  {time.time() - t0:.0f}s  "
                  f"md={stats['md_changed']} dir={stats['dir_moved']}", flush=True)

    # ── 别名表 ──
    if args.apply:
        uniq_q = [(o, n) for o, n in dict.fromkeys(legacy_q) if o != n]
        uniq_p = [(o, n) for o, n in dict.fromkeys(legacy_p) if o != n]
        db.executemany("INSERT OR REPLACE INTO qid_map(old,new) VALUES (?,?)", uniq_q)
        db.executemany("INSERT OR REPLACE INTO paper_map(old,new) VALUES (?,?)", uniq_p)
        db.commit()
        stats["qid_map"] = len(uniq_q)
        stats["paper_map"] = len(uniq_p)
        db.close()

    # ── 输出 ──
    print("\n" + "=" * 60)
    print(f"{'DRY-RUN' if args.dry_run else 'APPLY'} 完成  {time.time() - t0:.0f}s")
    print(f"  改动 md          {stats['md_changed']}")
    print(f"  目录 mv          {stats['dir_moved']}")
    print(f"  跳过(目标已存在) {stats['skip_dirs']}")
    print(f"  无 front-matter  {stats['no_fm']}")
    print(f"  父 id 兜底       {stats['orphan_parent']}")
    if args.apply:
        print(f"  别名 qid         {stats['qid_map']}")
        print(f"  别名 paper       {stats['paper_map']}")
        print(f"  别名库           {VAR / 'legacy_ids.sqlite'}")
    print("=" * 60)

    if args.csv:
        out = Path(args.csv)
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(["file", "field", "old", "new", "note", "kind"])
            w.writerows(rows)
        print(f"计划明细已写入 {out}（{len(rows)} 行）")

    if args.log and log_lines:
        out = Path(args.log)
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w", encoding="utf-8") as f:
            f.write("\n".join(log_lines))
        print(f"日志已写入 {out}（{len(log_lines)} 行）")

    return 0


if __name__ == "__main__":
    sys.exit(main())
