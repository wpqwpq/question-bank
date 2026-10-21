#!/usr/bin/env python
"""从 static.gximg.cn 补 CIE 乱码卷（9709 数学 / 9231 进阶数学 / 9701 化学）。

背景：CIE 自 2024-Nov 起用 Type0 + Identity-H 无 ToUnicode 子集，文字层不可逆。
gximg 这一路是**正版印刷文件**（带 IB/UCLES 标记），文本层可读，可直接替换。

源格式（对象存储直链，**无目录 listing**，只能拼 URL 探测）：
  https://static.gximg.cn/alevel/test_paper/<dir_id>/qp-<yyyymm>-<subject>-p<nn>.pdf
  dir_id: 10003=9709 数学 / 10004=9231 进阶数学 / 10006=9701 化学
  404 返回 application/xml（≈390B），不是 PDF

年月只有 6 月与 10/11 月两场（实测 202001/202006/202010/2021~2023/202606 全 404）：
  2024-11 场 → 202410    2025-06 场 → 202505    2025-10 场 → 202510
  3 月场（印度区）没有。

用法：
  python scripts/fetch_cie_gximg.py --dry-run
  python scripts/fetch_cie_gximg.py --hours 6 --gap 5,8
状态文件 _tmp/gx_fetch_state.json，中断重跑同命令即续。
"""
import argparse
import json
import random
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fetch_papers_lib import verdict  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
PENDING = ROOT / "_tmp" / "pending.json"
STATE = ROOT / "_tmp" / "gx_fetch_state.json"
MISS = ROOT / "_tmp" / "gx_fetch_miss.json"
DEST = ROOT / "downloadPapers" / "_new"
QUAR = ROOT / ".tmp" / "quarantine_gximg"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
BASE = "https://static.gximg.cn/alevel/test_paper"

DIR_ID = {"mathematics": 10003, "further-mathematics": 10004, "chemistry": 10006}
SLUG = {"mathematics": "mathematics", "further-mathematics": "furthermathematics",
        "chemistry": "chemistry"}
SYL = {"mathematics": "9709", "further-mathematics": "9231", "chemistry": "9701"}
SUBJ_DIR = {"mathematics": "CIE_9709_Mathematics",
            "further-mathematics": "CIE_9231_FurtherMathematics",
            "chemistry": "CIE_9701_Chemistry"}

# (year, sess) -> (yyyymm, 文件名里的考季码)
SEASON = {
    (2024, "oct"): ("202410", "w24"),
    (2025, "june"): ("202505", "s25"),
    (2025, "oct"): ("202510", "w25"),
}

# 实测各学科实际有货的场次（2026-10-20 逐个 HEAD 确认）
# 进阶数学只收了 2025-10 一场：2024-06/2024-10/2025-06 全 404，slug 变体也试过
AVAIL = {
    "mathematics": {(2024, "oct"), (2025, "june"), (2025, "oct")},
    "further-mathematics": {(2025, "oct")},
    "chemistry": {(2024, "oct"), (2025, "june"), (2025, "oct")},
}

PER_HOUR = 400
GAP = (5.0, 8.0)


class Budget:
    def __init__(self, per_hour=PER_HOUR, gap=GAP):
        self.per_hour, self.gap = per_hour, gap
        self._hits, self.requests = [], 0

    def wait(self):
        now = time.monotonic()
        self._hits = [t for t in self._hits if now - t < 3600]
        if len(self._hits) >= self.per_hour:
            nap = 3600 - (now - self._hits[0]) + 1
            print(f"  [预算] 本小时已达 {self.per_hour} 次，停 {nap:.0f}s", flush=True)
            time.sleep(nap)
        if self._hits:
            gap = random.uniform(*self.gap)
            due = self._hits[-1] + gap
            if due > time.monotonic():
                time.sleep(due - time.monotonic())
        self._hits.append(time.monotonic())
        self.requests += 1


def curl(url, out_path, timeout=120):
    """返回 (http_code, size)。code '000' 表示网络层失败。"""
    cmd = ["curl", "-sS", "-L", "--noproxy", "*", "--max-time", str(timeout),
           "-A", UA, "-o", str(out_path), "-w", "%{http_code} %{size_download}", url]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 30)
    except subprocess.TimeoutExpired:
        return "000", 0
    out = (p.stdout or "").strip().split()
    if len(out) != 2:
        return "000", 0
    try:
        return out[0], int(out[1])
    except ValueError:
        return "000", 0


def load_state():
    if STATE.exists():
        try:
            st = json.loads(STATE.read_text(encoding="utf-8"))
        except Exception:
            st = {}
    else:
        st = {}
    st.setdefault("done", {})
    st.setdefault("failed", {})
    return st


def save_state(st):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")


def build_todo():
    """从 pending.json 取 CIE 乱码卷，只留 gximg 有年月的场次。"""
    raw = json.loads(PENDING.read_text(encoding="utf-8"))
    todo, skip = [], []
    for r in raw:
        if r["board"] != "cie" or r["read"] != "garbled":
            continue
        key = (r["year"], r["sess"])
        if key not in SEASON:
            skip.append(dict(subject=r["subject"], year=r["year"], sess=r["sess"],
                             code=r["code"], why="gximg 无此场次"))
            continue
        if key not in AVAIL.get(r["subject"], set()):
            skip.append(dict(subject=r["subject"], year=r["year"], sess=r["sess"],
                             code=r["code"], why="该学科此场次无货"))
            continue
        m = re.fullmatch(r"QP(\d{2})", r["code"] or "")
        if not m:
            skip.append(dict(subject=r["subject"], year=r["year"], sess=r["sess"],
                             code=r["code"], why="卷号无法解析"))
            continue
        todo.append(dict(subject=r["subject"], year=r["year"], sess=r["sess"],
                         code=r["code"], paper=m.group(1)))
    todo.sort(key=lambda w: (w["subject"], w["year"], w["sess"], w["paper"]))
    return todo, skip


def target(w):
    ym, sc = SEASON[(w["year"], w["sess"])]
    return DEST / SUBJ_DIR[w["subject"]] / f"{w['year']}-{w['sess']}" / \
        f"{SYL[w['subject']]}_{sc}_qp_{w['paper']}.pdf"


def url_of(w):
    ym, _ = SEASON[(w["year"], w["sess"])]
    return f"{BASE}/{DIR_ID[w['subject']]}/qp-{ym}-{SLUG[w['subject']]}-p{w['paper']}.pdf"


def key_of(w):
    return f"{w['subject']}|{w['year']}|{w['sess']}|{w['code']}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--hours", type=float, default=6.0)
    ap.add_argument("--per-hour", type=int, default=PER_HOUR)
    ap.add_argument("--gap", default="5,8", help="间隔秒数区间，如 5,8")
    args = ap.parse_args()

    lo, hi = (float(x) for x in args.gap.split(","))
    budget = Budget(per_hour=args.per_hour, gap=(lo, hi))

    todo, skip = build_todo()
    print(f"清单 {len(todo)} 卷 | 跳过 {len(skip)} 卷（无场次/无法解析）"
          f" | 间隔 {lo:g}-{hi:g}s | 每小时 {args.per_hour} 次 | 时长 {args.hours}h", flush=True)

    if args.dry_run:
        from collections import Counter
        print("  [dry-run] 不发任何请求", flush=True)
        for k, v in sorted(Counter((w["subject"], w["year"], w["sess"])
                                   for w in todo).items()):
            print(f"    {k[0]:<20}{k[1]}-{k[2]:<6}{v:>3} 卷", flush=True)
        print(f"  跳过原因：{dict(Counter(s['why'] for s in skip))}", flush=True)
        for w in todo[:3]:
            print(f"    样例 {url_of(w)}", flush=True)
        return

    st = load_state()
    deadline = time.monotonic() + args.hours * 3600
    done = bad = skipped = 0
    n = 0

    for w in todo:
        if args.limit and n >= args.limit:
            break
        if time.monotonic() > deadline:
            print("  到时收工", flush=True)
            break
        n += 1
        k = key_of(w)
        p = target(w)
        if k in st["done"] or (p.exists() and p.stat().st_size > 20000):
            skipped += 1
            continue

        budget.wait()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".part")
        code, size = curl(url_of(w), tmp)
        if code != "200" or size < 20000:
            tmp.unlink(missing_ok=True)
            st["failed"][k] = dict(why=f"http {code} size {size}", url=url_of(w))
            save_state(st)
            bad += 1
            print(f"  [{code}] {w['subject']:<20}{w['year']}-{w['sess']:<5}{w['code']}"
                  f"  ({size}B)", flush=True)
            continue

        v = verdict(str(tmp))
        if v["cls"] != "OK":
            QUAR.mkdir(parents=True, exist_ok=True)
            tmp.replace(QUAR / p.name)
            st["failed"][k] = dict(why=f"{v['cls']}: {v['reason']}", url=url_of(w))
            save_state(st)
            bad += 1
            print(f"  [×] {p.name}  文本层 {v['cls']}：{v['reason']}", flush=True)
            continue

        tmp.replace(p)
        st["done"][k] = str(p.relative_to(ROOT))
        save_state(st)
        done += 1
        print(f"  [✓] {w['subject']:<20}{w['year']}-{w['sess']:<5}{w['code']:<6}"
              f"{size/1024:>7.0f}KB  ({done} 新 / {skipped} 跳过 / {bad} 失败)", flush=True)

    save_state(st)
    MISS.write_text(json.dumps(dict(failed=st["failed"], skipped=skip),
                               ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n=== 新增 {done} / 跳过 {skipped} / 未得 {bad} ===", flush=True)
    print(f"  请求 {budget.requests} 次", flush=True)
    print(f"  状态 {STATE}", flush=True)
    print(f"  未得 {MISS}", flush=True)


if __name__ == "__main__":
    main()
