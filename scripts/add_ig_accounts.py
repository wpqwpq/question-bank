#!/usr/bin/env python3
"""批量生成 IG 专用打标账号（2026-10-27）。

七个学科组各 5 个，role=tagger，levels=[ig]：只看得到本学科 IGCSE 卷，
也只能改 IGCSE 那份词表。存量 80 个账号不写 levels，仍是 A-Level + IG 全开。

    python add_ig_accounts.py            # 干跑：只打印 35 行清单，不碰文件
    python add_ig_accounts.py --write    # 真写：先备份 users.yaml，再落盘 + 导出 CSV

账号已存在时跳过（幂等），不会重置它的密码——重跑安全。
初始密码只在这里出现一次；CSV 分发完请删除。
"""
from __future__ import annotations

import argparse
import csv
import shutil
import sys
from datetime import datetime
from pathlib import Path

# backend 目录：线上是 /opt/qbank/workbench/backend，本地仓库是 <repo>/cloud/workbench/backend
for _p in (
    Path(__file__).resolve().parents[1] / "workbench" / "backend",
    Path(__file__).resolve().parents[1] / "cloud" / "workbench" / "backend",
):
    if (_p / "auth.py").exists():
        sys.path.insert(0, str(_p))
        break

import auth  # noqa: E402
import manage_users as mu  # noqa: E402

# (学科 token, 中文, 用户名前缀, 起始序号, 结束序号)
GROUPS = [
    ("physics", "物理", "phy", 11, 15),
    ("chemistry", "化学", "che", 11, 15),
    ("biology", "生物", "bio", 11, 15),
    ("mathematics", "数学", "mat", 21, 25),
    ("economics", "经济", "eco", 11, 15),
    ("accounting", "会计", "acc", 11, 15),
    ("business", "商务", "bus", 11, 15),
]

CSV_OUT = auth.CONFIG_DIR / "users-ig-initial.csv"
BACKUP_DIR = auth.CONFIG_DIR.parent / "_backup"


def plan() -> list[dict]:
    rows: list[dict] = []
    for subject, zh, prefix, lo, hi in GROUPS:
        for i in range(lo, hi + 1):
            rows.append({
                "username": f"{prefix}{i:02d}_IG",
                "role": "tagger",
                "subject": subject,
                "name": f"{zh}IG{i:02d}",
                "levels": ["ig"],
            })
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true", help="真的写盘（默认只干跑）")
    args = ap.parse_args()

    data = mu._load_raw()
    existing = {str(u.get("username")) for u in data["users"]}
    todo = plan()
    fresh = [r for r in todo if r["username"] not in existing]
    skipped = [r for r in todo if r["username"] in existing]

    print(f"计划 {len(todo)} 个账号；已存在 {len(skipped)} 个、待新建 {len(fresh)} 个")
    if skipped:
        print("  跳过：" + " ".join(r["username"] for r in skipped))

    out_rows: list[list[str]] = []
    for r in fresh:
        entry, pw = mu._new_entry(r["username"], r["name"], r["role"],
                                  [r["subject"]], r["levels"])
        r["password"] = pw
        data["users"].append(entry)
        out_rows.append([r["username"], r["role"], r["subject"], r["name"], "ig", pw or ""])

    if not args.write:
        for r in fresh:
            print(f"  {r['username']:<12}{r['role']:<8}{r['subject']:<14}{r['name']}")
        print("\n干跑结束，未改动任何文件。加 --write 真写。")
        return 0

    if not fresh:
        print("没有需要新建的账号，什么也没做。")
        return 0

    # 备份：带时间戳，和 _backup/ 里其它备份同一命名习惯
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    bak = BACKUP_DIR / f"users.yaml.bak-{stamp}-before-ig"
    shutil.copy2(auth.USERS_PATH, bak)
    print(f"已备份 → {bak}")

    mu._save_raw(data)

    CSV_OUT.parent.mkdir(parents=True, exist_ok=True)
    with CSV_OUT.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["用户名", "角色", "学科", "姓名", "学段", "新初始密码"])
        w.writerows(out_rows)
    try:
        CSV_OUT.chmod(0o600)
    except OSError:
        pass

    print(f"已新增 {len(fresh)} 个账号 → {auth.USERS_PATH}")
    print(f"初始密码清单 → {CSV_OUT}（分发后删除）")
    print(f"账号总数：{len(data['users'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
