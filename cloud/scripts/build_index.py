#!/usr/bin/env python3
"""index.db 的构建 / 对账 / 压测入口（独立于服务进程，可随时跑）。

用法：
  python scripts/build_index.py                  # 全量重建
  python scripts/build_index.py --check          # 只对账，不改库（列出 md 与索引不一致的地方）
  python scripts/build_index.py --paper 2019-jan-ph01   # 只重灌一卷
  python scripts/build_index.py --bench          # 冷启动/查询耗时对比（不改库）
  python scripts/build_index.py --db /tmp/x.db --data-dir /path/to/data

⚠️ index.db 是抄件：删了重跑即可，不要备份它。
   确认记录/有误标记在 var/state.db（那是事实，备份脚本已覆盖）。
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND = REPO_ROOT / "workbench" / "backend"

ap = argparse.ArgumentParser(description="构建 / 对账题库索引")
ap.add_argument("--data-dir", default=str(REPO_ROOT / "data"))
ap.add_argument("--db", default=str(REPO_ROOT / "var" / "index.db"))
ap.add_argument("--check", action="store_true", help="只对账（逐文件核对状态，慢但彻底）")
ap.add_argument("--verify", action="store_true", help="快速对账（只比目录 stat，快）")
ap.add_argument("--paper", default=None, help="只重灌这一卷（目录名）")
ap.add_argument("--bench", action="store_true", help="耗时对比")
ap.add_argument("--quiet", action="store_true")
args = ap.parse_args()

import os  # noqa: E402

os.environ["QBANK_INDEX_DB"] = str(Path(args.db).resolve())
sys.path.insert(0, str(BACKEND))

import index_db  # noqa: E402


def human(n: float) -> str:
    return f"{n * 1000:.1f} ms" if n < 1 else f"{n:.2f} s"


# ---------------------------------------------------------------------------
# --bench：现状 vs 索引
# ---------------------------------------------------------------------------
def bench() -> None:
    import store  # 现状实现（全量扫目录 + 解析 YAML）

    print(f"题库：{index_db.DATA_DIR}")
    print(f"索引：{index_db.DB_PATH}\n")

    # A. 现状：冷启动 + 一次列表请求
    t = time.perf_counter()
    store.warm()
    t_warm = time.perf_counter() - t
    t = time.perf_counter()
    n_q = 0
    for p in store.iter_papers():
        p.summary()
        n_q += len(p.questions())
    t_list = time.perf_counter() - t
    n_papers = len(store.iter_papers())
    print(f"A 现状（内存缓存）")
    print(f"   冷启动 warm()        {human(t_warm)}")
    print(f"   列表全量计算          {human(t_list)}  ({n_papers} 卷 / {n_q} 题)")
    print(f"   合计冷启动到能出列表  {human(t_warm + t_list)}")

    # B. 建索引（必须清空元数据缓存，否则是白嫖 A 阶段的热缓存，测不出真实成本）
    store._meta_cache.clear()
    t = time.perf_counter()
    res = index_db.rebuild()
    t_build = time.perf_counter() - t
    print(f"\nB 建索引（冷缓存：解析全部 md + 写库）")
    print(f"   rebuild()            {human(t_build)}  ({res['papers']} 卷 / {res['rows']} 行)")

    # C. 索引查询
    t = time.perf_counter()
    rows = index_db.papers()
    t_papers = time.perf_counter() - t
    t = time.perf_counter()
    n2 = 0
    for r in rows:
        n2 += len(index_db.questions(r["paper_id"]))
    t_qs = time.perf_counter() - t
    st = index_db.stats()
    print(f"\nC 索引查询")
    print(f"   卷列表（{len(rows)} 行）      {human(t_papers)}")
    print(f"   全部卷的题（{n2} 行）   {human(t_qs)}")
    print(f"   db 体积 {st['bytes'] / 1024 / 1024:.1f} MB（{st['bytes'] / max(st['questions'],1):.0f} 字节/行）")

    print(f"\n对比")
    print(f"   冷启动 {human(t_warm + t_list)} → 建索引一次性 {human(t_build)}，之后每次列表 {human(t_papers + t_qs)}")
    print(f"   索引重建 / 现状冷启动 = {t_build / (t_warm + t_list):.2f}（越小越省）")


# ---------------------------------------------------------------------------
# --verify：快速对账（只比目录 stat，不解析 md）
# ---------------------------------------------------------------------------
def verify() -> int:
    if not index_db.exists():
        print("✗ index.db 不存在，先跑一次全量重建")
        return 2
    import store  # paper_id_for：索引主键（board-qual-subject-目录名），不能再用裸目录名对账

    rows = index_db.papers()
    db = {r["paper_id"]: r for r in rows}
    live: dict[str, int | None] = {}
    for d, b, q, s in index_db._paper_dirs():
        pid = store.paper_id_for(d, b, q, s)
        try:
            live[pid] = d.stat().st_mtime_ns
        except OSError:
            live[pid] = None

    missing = sorted(set(live) - set(db))          # 新卷还没进索引（读到时会自动建）
    extra = sorted(set(db) - set(live))            # 索引里的幽灵卷（磁盘已删）
    changed = [pid for pid in live
               if pid in db and db[pid].get("dir_mtime") != live[pid]]
    stale = [pid for pid in db if db[pid].get("stale")]

    print(f"卷：磁盘 {len(live)} / 索引 {len(db)}")
    if missing:
        print(f"  待入索引（读到时自动建）{len(missing)}：{missing[:6]}")
    if extra:
        print(f"  幽灵卷（磁盘已无）{len(extra)}：{extra[:6]}")
    if changed:
        print(f"  目录被改过（下次扫描会标 stale）{len(changed)}：{changed[:6]}")
    if stale:
        print(f"  已标 stale（下次读会重灌）{len(stale)}：{stale[:6]}")
    ok = not (extra or changed)
    print("✓ 一致" if ok else "✗ 有差异（下一次目录扫描会自动修正；急用可 --paper / 全量重建）")
    return 0 if ok else 1


# ---------------------------------------------------------------------------
# --check：对账
# ---------------------------------------------------------------------------
def check() -> int:
    import store

    if not index_db.exists():
        print("✗ index.db 不存在，先跑一次全量重建")
        return 2
    rows = index_db.papers()
    db_papers = {r["paper_id"]: r for r in rows}
    disk = {}
    for d, b, q, s in index_db._paper_dirs():
        disk[store.paper_id_for(d, b, q, s)] = (d, b, q, s)

    missing = sorted(set(disk) - set(db_papers))
    extra = sorted(set(db_papers) - set(disk))
    print(f"卷：磁盘 {len(disk)} / 索引 {len(db_papers)}")
    if missing:
        print(f"  索引里缺 {len(missing)} 卷：{missing[:8]}")
    if extra:
        print(f"  索引里多 {len(extra)} 卷：{extra[:8]}")

    bad = 0
    checked = 0
    for pid, (d, b, q, s) in disk.items():
        r = db_papers.get(pid)
        if r is None:
            continue
        checked += 1
        # 用 md 现场算一遍主题级状态与标注数，跟索引比
        mains = sorted((p for p in d.glob("q*.md") if store.MAIN_Q_RE.match(p.name)),
                       key=lambda p: p.name)
        statuses = []
        for p in mains:
            meta = store.read_meta(p)
            statuses.append(store.compute_status(meta, [store.read_meta(x) for x in
                              sorted((y for y in d.glob("q*-*.md")
                                      if store.SUB_Q_RE.match(y.name) and
                                      y.stem.split("-", 1)[0] == p.stem), key=lambda y: y.name)]))
        annotated = statuses.count("done")
        if annotated != r["annotated_count"] or len(mains) != r["question_count"]:
            bad += 1
            if bad <= 10:
                print(f"  ✗ {pid}: 索引 {r['annotated_count']}/{r['question_count']} "
                      f"vs md {annotated}/{len(mains)}")

    print(f"内容对账：检查 {checked} 卷，不一致 {bad} 卷")
    print("✓ 一致" if not (missing or extra or bad) else "✗ 有差异，跑一次全量重建即可（索引可丢弃）")
    return 0 if not (missing or extra or bad) else 1


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
if args.bench:
    bench()
    sys.exit(0)
if args.verify:
    sys.exit(verify())
if args.check:
    sys.exit(check())

if args.paper:
    pid = args.paper
    hit = [x for x in index_db._paper_dirs() if x[0].name == pid]
    if not hit:
        print(f"✗ 找不到卷 {pid}")
        sys.exit(2)
    d, b, q, s = hit[0]
    t = time.perf_counter()
    n = index_db.rebuild_paper(d, b, q, s)
    print(f"✓ {pid} 重灌 {n} 行，{human(time.perf_counter() - t)}")
    sys.exit(0)

def _progress(i: int, n: int) -> None:
    if not args.quiet:
        print(f"\r  重建中 {i}/{n} 卷…", end="", flush=True)

t = time.perf_counter()
res = index_db.rebuild(progress=None if args.quiet else _progress)
dt = time.perf_counter() - t
if not args.quiet:
    print()
st = index_db.stats()
print(f"✓ 索引重建完成：{res['papers']} 卷 / {res['rows']} 行，{dt:.2f}s")
print(f"  {st['db']}  {st['bytes'] / 1024 / 1024:.1f} MB")
