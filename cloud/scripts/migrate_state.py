#!/usr/bin/env python3
"""把 data/_review/{confirmed,flagged}.json 迁进 var/state.db（一次性）。

用法：
  python scripts/migrate_state.py                  # 正式迁移
  python scripts/migrate_state.py --dry-run        # 只统计，不写库、不改名
  python scripts/migrate_state.py --force          # 库里已有数据也硬灌（会先清空两张表）
  python scripts/migrate_state.py --db /tmp/x.db --data-dir /path/to/data

原则：
  · 逐项比对条数，**不一致就中止**并保留旧文件；
  · 旧文件改名成 *.migrated-YYYYMMDD 而不是删（留一周再清理）；
  · 可重复执行：库里已有数据且没给 --force 时直接退出，不会二次灌。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]          # cloud/（服务器上是 /opt/qbank）
BACKEND = REPO_ROOT / "workbench" / "backend"

ap = argparse.ArgumentParser(description="confirmed/flagged JSON → state.db")
ap.add_argument("--data-dir", default=str(REPO_ROOT / "data"), help="题库根目录（含 _review/）")
ap.add_argument("--db", default=str(REPO_ROOT / "var" / "state.db"), help="state.db 路径")
ap.add_argument("--dry-run", action="store_true", help="只统计，不写库不改名")
ap.add_argument("--force", action="store_true", help="库里已有数据也灌（先清空两张表）")
args = ap.parse_args()

DATA_DIR = Path(args.data_dir).resolve()
DB_PATH = Path(args.db).resolve()
REVIEW_DIR = DATA_DIR / "_review"

stamp = datetime.now().strftime("%Y%m%d")
print(f"题库根：{DATA_DIR}")
print(f"目标库：{DB_PATH}")

# ---- 1. 读旧文件 ----------------------------------------------------------
def read_json(path: Path) -> dict:
    if not path.exists():
        print(f"  · {path.name} 不存在 → 视为 0 条")
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        print(f"  ✗ {path.name} 读取失败：{e}")
        sys.exit(1)
    items = raw.get("items") if isinstance(raw, dict) else None
    if not isinstance(items, dict):
        print(f"  ✗ {path.name} 结构不认识（没有 items 字典）")
        sys.exit(1)
    return items


conf_items = read_json(REVIEW_DIR / "confirmed.json")
flag_items = read_json(REVIEW_DIR / "flagged.json")
print(f"读取旧文件：确认 {len(conf_items)} 条 / 标记 {len(flag_items)} 条")

if args.dry_run:
    if flag_items:
        sample = next(iter(flag_items.items()))
        print(f"  标记样例：{sample[0]} → {json.dumps(sample[1], ensure_ascii=False)[:160]}")
    print("--dry-run：不写库、不改名。")
    sys.exit(0)

# ---- 2. 打开 state.py（用环境变量指定库路径，避免动到默认位置）--------------
os.environ["QBANK_STATE_DB"] = str(DB_PATH)
sys.path.insert(0, str(BACKEND))

import sqlite3  # noqa: E402
import state  # noqa: E402

DB_PATH.parent.mkdir(parents=True, exist_ok=True)
is_new = not DB_PATH.exists()
state.init()

old_conf, old_flag = state.confirmed_count(), state.flagged_count()
if (old_conf or old_flag) and not args.force:
    print(f"✗ 目标库里已有数据（确认 {old_conf} / 标记 {old_flag}），"
          f"不重复灌。确需重灌请加 --force")
    sys.exit(2)

if args.force:
    con = sqlite3.connect(str(DB_PATH))
    con.execute("DELETE FROM confirms")
    con.execute("DELETE FROM flags")
    con.commit()
    con.close()
    state.reload()
    print("--force：已清空两张表")

# ---- 3. 灌数据 ------------------------------------------------------------
conf_rows = []
for uid, rec in conf_items.items():
    at = (rec or {}).get("at") if isinstance(rec, dict) else None
    by = (rec or {}).get("by") if isinstance(rec, dict) else None
    conf_rows.append((str(uid), at or state.now_iso(), by))

flag_rows = []
for qid, rec in flag_items.items():
    rec = rec if isinstance(rec, dict) else {}
    fp = rec.get("fp") or {}
    flag_rows.append((
        str(qid),
        rec.get("at") or state.now_iso(),
        rec.get("by"),
        rec.get("note"),
        json.dumps(fp, ensure_ascii=False, separators=(",", ":")),
        rec.get("at") or state.now_iso(),
    ))

con = sqlite3.connect(str(DB_PATH))
con.execute("PRAGMA journal_mode=WAL")
if conf_rows:
    con.executemany("INSERT OR REPLACE INTO confirms(unit_id,at,by_user) VALUES(?,?,?)", conf_rows)
if flag_rows:
    con.executemany(
        "INSERT OR REPLACE INTO flags(qid,at,by_user,note,fp,fp_ok_at) VALUES(?,?,?,?,?,?)",
        flag_rows,
    )
con.commit()

# ---- 4. 逐项比对（不靠记忆，直接查库）------------------------------------
got_conf = con.execute("SELECT COUNT(*) FROM confirms").fetchone()[0]
got_flag = con.execute("SELECT COUNT(*) FROM flags").fetchone()[0]

# 抽样核对：确认存在性 + 标记的指纹能原样解回来
miss_conf = [r[0] for r in con.execute("SELECT unit_id FROM confirms")]
missing_ids = set(map(str, conf_items)) - set(miss_conf)
bad_fp = 0
for qid, fp_text in con.execute("SELECT qid, fp FROM flags"):
    try:
        back = json.loads(fp_text)
    except json.JSONDecodeError:
        bad_fp += 1
        continue
    if back.get("sha") != (flag_items.get(qid, {}).get("fp") or {}).get("sha"):
        bad_fp += 1
con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
con.close()

print(f"写入结果：确认 {got_conf} / 标记 {got_flag}")
ok = (got_conf == len(conf_items)) and (got_flag == len(flag_items)) and not missing_ids and not bad_fp
if not ok:
    print(f"✗ 校验失败：缺确认 {len(missing_ids)} 条、指纹异常 {bad_fp} 条")
    print("  库里数据未清理，旧文件未改名。请人工检查。")
    sys.exit(3)

size = DB_PATH.stat().st_size
print(f"✓ 校验通过（{size / 1024:.0f} KB, {'新建' if is_new else '已有库' + ('，--force 重灌' if args.force else '')}）")

# ---- 5. 旧文件改名留档 ----------------------------------------------------
for name in ("confirmed.json", "flagged.json"):
    src = REVIEW_DIR / name
    if not src.exists():
        continue
    dst = REVIEW_DIR / f"{name}.migrated-{stamp}"
    src.rename(dst)
    print(f"  旧文件已改名：{dst.name}")

state.close()
print("完成。别忘了：备份脚本要把 state.db 加进去（用 VACUUM INTO，别直接 rsync WAL 库）。")
