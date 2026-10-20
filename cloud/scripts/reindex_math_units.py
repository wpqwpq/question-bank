# -*- coding: utf-8 -*-
"""服务器侧：把数学科 282 卷的 unit 列按新规则重灌进 index.db。

背景：unit_from_paper_code 2026-10-19 新增数学分支（WMA1x→P1…），
索引里的 unit 是落盘的旧值（U1–U4 / unknown），必须重灌才与打标台一致。
只重灌 subject=mathematics 的卷，其余卷一行不动。

用法（服务器 /opt/qbank）：
    PYTHONPATH=/opt/qbank/workbench/backend python3 scripts/reindex_math_units.py
"""
import sys
import time

sys.path.insert(0, "/opt/qbank/workbench/backend")
import index_db  # noqa: E402
import store  # noqa: E402


def main() -> int:
    targets = [x for x in index_db._paper_dirs() if x[3] == "mathematics"]
    print(f"数学卷 {len(targets)} 卷，开始重灌…")
    t = time.perf_counter()
    rows = 0
    changed = []
    con = index_db.connect()
    before = {(r[0], r[1]) for r in con.execute(
        "SELECT paper_id, unit FROM papers WHERE subject='mathematics'")}
    for i, (d, b, q, s) in enumerate(targets, 1):
        rows += index_db.rebuild_paper(d, b, q, s)
        if i % 50 == 0:
            print(f"  {i}/{len(targets)}", flush=True)
    after = {(r[0], r[1]) for r in con.execute(
        "SELECT paper_id, unit FROM papers WHERE subject='mathematics'")}
    old = dict(before)
    for pid, unit in sorted(after):
        if old.get(pid) != unit:
            changed.append((pid, old.get(pid), unit))
    print(f"完成：{len(targets)} 卷 / {rows} 题行，{time.perf_counter() - t:.1f}s")
    print(f"unit 变化 {len(changed)} 卷：")
    for pid, a, b2 in changed:
        print(f"  {pid}: {a} → {b2}")
    # 汇总
    from collections import Counter
    print("单元分布：", dict(sorted(Counter(u for _, u in after).items())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
