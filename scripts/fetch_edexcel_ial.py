# -*- coding: utf-8 -*-
"""fetch_edexcel_ial.py — 从 edupapers.store 补 Edexcel IAL 缺失卷（2020–2025）

源（2026-10-20 实测可用）：
    目录页  https://edupapers.store/past-papers?ee=1&eeFolder=EDEXCEL%2FIAL%2F<Subj>%2F<Year>%2F<Month>%2FQuestion-Papers&eeListID=1
    真 PDF  https://edupapers.store/wp-content/uploads/simple-file-list/EDEXCEL/IAL/<Subj>/<Year>/<Month>/Question-Papers/<file>.pdf

两条实测结论，直接决定了这个脚本的写法：
  1. **Python urllib 被判 403**，同 URL 用 curl 就 200 → 一律走 curl 子进程。
  2. **文件不存在时站点返回 403 而不是 404**（WPH15/16 的 2020-jan 就是）。
     所以不能靠猜 URL 硬撞，必须先读场次目录页拿真实文件名，再照着下。
     这同时省掉了「403 到底是被封还是没文件」的误判。
  3. 只带 UA，别加 Accept / Referer——加了自定义 Accept 会让本来 200 的 PDF 变 403。

节奏（用户要求「一晚上慢慢下，别被黑名单」）：
    单并发、每请求 45–85 秒随机、每小时 ≤40 次、连续 4 次失败冷却 8 分钟。
    122 卷 ÷ 40 次/小时 ≈ 3.5 小时，一晚跑得完还有余量。

用法：
    python scripts/fetch_edexcel_ial.py --dry-run
    python scripts/fetch_edexcel_ial.py --limit 2      # 试跑
    python scripts/fetch_edexcel_ial.py --hours 8      # 全量
状态写 _tmp/edexcel_fetch_state.json，中断后重跑自动续。
"""
from __future__ import annotations

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
WANT = ROOT / "_tmp" / "wantlist.json"
STATE = ROOT / "_tmp" / "edexcel_fetch_state.json"
MISS = ROOT / "_tmp" / "edexcel_fetch_miss.json"
DEST = ROOT / "downloadPapers" / "_new"
QUAR = ROOT / ".tmp" / "quarantine_edexcel"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
BASE = "https://edupapers.store"
UP = BASE + "/wp-content/uploads/simple-file-list/EDEXCEL/IAL"
SUBJECT_DIR = {"chemistry": "Chemistry", "physics": "Physics", "biology": "Biology",
               "mathematics": "Mathematics", "economics": "Economics", "accounting": "Accounting"}
MONTH = {"jan": "January", "june": "June", "oct": "October"}

# 王培琪手上有源、不要从网上抓（2026-10-20）：Edexcel 化学 2020–2024 自行放入
SKIP_SUBJECTS = {"chemistry"}

PER_HOUR = 40
GAP = (45, 85)
BLOCK_COOLDOWN = 480
MAX_CONSEC_FAIL = 4


class Budget:
    def __init__(self, per_hour=PER_HOUR, gap=GAP):
        self.per_hour, self.gap = per_hour, gap
        self._hits, self._pause_until = [], 0.0
        self.requests = self.blocks = 0

    def pause(self, seconds=BLOCK_COOLDOWN):
        self._pause_until = max(self._pause_until, time.monotonic() + seconds)
        self.blocks += 1

    def wait(self):
        while True:
            left = self._pause_until - time.monotonic()
            if left <= 0:
                break
            time.sleep(min(left, 10))
        now = time.monotonic()
        self._hits = [t for t in self._hits if now - t < 3600]
        if len(self._hits) >= self.per_hour:
            nap = 3600 - (now - self._hits[0]) + random.uniform(10, 30)
            print(f"    小时配额用完，停 {nap/60:.0f} 分钟", flush=True)
            time.sleep(nap)
            self._hits = []
        if self.requests:
            time.sleep(random.uniform(*self.gap))
        self._hits.append(time.monotonic())
        self.requests += 1


def curl(url, out_path=None, timeout=180):
    # 目录页会 301 跳到带尾斜杠的地址，必须 -L，否则拿到 0 字节
    cmd = ["curl", "-sS", "-L", "--noproxy", "*", "--max-time", str(timeout), "-A", UA]
    if out_path:
        cmd += ["-o", str(out_path), "-w", "%{http_code} %{size_download}"]
    cmd.append(url)
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout + 30)
    except subprocess.TimeoutExpired:
        return "000", b""
    if out_path:
        parts = (p.stdout or b"").decode("ascii", "ignore").split()
        code = parts[0] if parts else "000"
        return code, b""
    return ("200" if p.returncode == 0 else str(p.returncode)), p.stdout


def listing_url(subject, year, sess, folder="Question-Papers"):
    """folder 因学科而异：Physics/Chemistry 是 `Question-Papers`，
    Biology 是 `Question-paper`（单数小写），其余可能直接放月份目录。"""
    path = f"EDEXCEL/IAL/{SUBJECT_DIR[subject]}/{year}/{MONTH[sess]}"
    if folder:
        path += f"/{folder}"
    enc = path.replace("/", "%2F")
    return f"{BASE}/past-papers?ee=1&eeFolder={enc}&eeListID=1"


# 学科目录命名的候选，按命中概率排序（实测 Biology 用单数 Question-paper）
FOLDER_CANDS = ["Question-Papers", "Question-paper", ""]
KNOWN_FOLDER = {"biology": "Question-paper", "physics": "Question-Papers",
                "chemistry": "Question-Papers", "mathematics": "Question-Papers"}


def parse_listing(html):
    """返回 {文件名: 完整URL}，只要 Question-Papers 目录下的 pdf。"""
    out = {}
    for m in re.finditer(r'href="(' + re.escape(UP) + r'/[^"]+?\.pdf)"', html):
        u = m.group(1)
        fn = u.rsplit("/", 1)[-1]
        out[fn] = u
    return out


def pick(files, code):
    """在目录页文件里找该 code 的题卷（排除 ms / pef / rms）。"""
    hits = []
    for fn, u in files.items():
        fl = fn.lower()
        if code.lower() not in fl:
            continue
        if any(k in fl for k in ("ms", "rms", "pef", "msc", "er", "report", "scheme")):
            if not re.search(r"que|qp|question", fl):
                continue
        hits.append((fn, u))
    return hits


def load_state():
    if STATE.exists():
        return json.loads(STATE.read_text(encoding="utf-8"))
    return {"done": {}, "failed": {}, "listings": {}, "folders": {}}


def save_state(st):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")


def build_todo():
    want = json.loads(WANT.read_text(encoding="utf-8"))
    out, seen = [], set()
    skipped = 0
    for w in want:
        if w["board"] != "edexcel":
            continue
        if w["subject"] in SKIP_SUBJECTS:
            skipped += 1
            continue
        if w["subject"] == "mathematics" and w["sess"] == "oct":
            continue  # 2020-oct 数学 14 卷本地已有源
        k = (w["subject"], w["year"], w["sess"], w["code"])
        if k in seen:
            continue
        seen.add(k)
        out.append(w)
    if skipped:
        print(f"  [跳过] {skipped} 卷属于 {sorted(SKIP_SUBJECTS)}（自有源，不抓）", flush=True)
    # 按场次分组，减少目录页请求
    out.sort(key=lambda w: (w["subject"], w["year"], MONTH[w["sess"]], w["code"]))
    return out


def dest_path(w):
    d = DEST / f"Edexcel_ALevel_{w['subject'].title()}" / f"{w['year']}-{w['sess']}"
    return d / f"{w['code']}-{MONTH[w['sess']]}{w['year']}.pdf"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--hours", type=float, default=8.0)
    ap.add_argument("--per-hour", type=int, default=PER_HOUR)
    ap.add_argument("--gap", default=f"{GAP[0]},{GAP[1]}")
    args = ap.parse_args()

    gap = tuple(float(x) for x in args.gap.split(","))
    todo = build_todo()
    st = load_state()
    st.setdefault("listings", {})
    st.setdefault("folders", {})
    budget = Budget(args.per_hour, gap)
    deadline = time.monotonic() + args.hours * 3600

    print(f"清单 {len(todo)} 卷 | 每小时 {args.per_hour} 次 | 间隔 {gap[0]:.0f}-{gap[1]:.0f}s "
          f"| 时长 {args.hours}h", flush=True)
    if args.dry_run:
        for w in todo:
            print(f"  {w['subject']:<12}{w['year']}-{w['sess']:<5}{w['code']}")
        return 0

    done = bad = skip = 0
    fails_in_row = 0
    cur_key = None
    files = {}

    for i, w in enumerate(todo, 1):
        key = f"{w['subject']}|{w['year']}|{w['sess']}|{w['code']}"
        if key in st["done"]:
            skip += 1
            continue
        p = dest_path(w)
        if p.exists() and p.stat().st_size > 20000:
            st["done"][key] = str(p.relative_to(ROOT))
            skip += 1
            continue
        if time.monotonic() > deadline:
            print(f"⏰ 到时长上限，处理到 {i-1}/{len(todo)}，下次续", flush=True)
            break
        if args.limit and (done + bad) >= args.limit:
            print(f"到 --limit {args.limit}，停", flush=True)
            break

        lk = f"{w['subject']}|{w['year']}|{w['sess']}"
        if lk != cur_key:
            cur_key = lk
            if lk in st["listings"]:
                files = st["listings"][lk]
            else:
                files = {}
                known = st.get("folders", {}).get(w["subject"]) or KNOWN_FOLDER.get(w["subject"])
                cands_folder = [known] if known is not None else FOLDER_CANDS
                for folder in cands_folder:
                    budget.wait()
                    code, html = curl(listing_url(w["subject"], w["year"], w["sess"], folder))
                    if code != "200":
                        print(f"  [页 {code}] {lk} folder='{folder}'", flush=True)
                        fails_in_row += 1
                        if fails_in_row >= MAX_CONSEC_FAIL:
                            print(f"    连续 {fails_in_row} 次失败，冷却 {BLOCK_COOLDOWN}s", flush=True)
                            budget.pause()
                            fails_in_row = 0
                        continue
                    got = parse_listing(html.decode("utf-8", "replace"))
                    print(f"  [目录] {lk:<32} folder='{folder}' → {len(got)} 份", flush=True)
                    if got:
                        files = got
                        st.setdefault("folders", {})[w["subject"]] = folder
                        break
                    fails_in_row = 0
                st["listings"][lk] = files
                save_state(st)

        cands = pick(files, w["code"])
        if not cands:
            st["failed"][key] = dict(why="目录页无此卷", url=None)
            save_state(st)
            bad += 1
            print(f"  [缺] {w['subject']} {w['year']}-{w['sess']} {w['code']}（源里没有）", flush=True)
            continue

        fn, url = cands[0]
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".part")
        budget.wait()
        code, _ = curl(url, tmp)
        if code != "200":
            print(f"  [!] {w['code']} {w['year']}-{w['sess']} 下载 {code}", flush=True)
            st["failed"][key] = dict(why=f"下载 {code}", url=url)
            save_state(st)
            bad += 1
            tmp.unlink(missing_ok=True)
            fails_in_row += 1
            if fails_in_row >= MAX_CONSEC_FAIL:
                budget.pause()
                fails_in_row = 0
            continue

        raw = tmp.read_bytes() if tmp.exists() else b""
        if not raw.startswith(b"%PDF"):
            print(f"  [!] 非 PDF {len(raw)}B", flush=True)
            st["failed"][key] = dict(why="非 PDF", url=url)
            save_state(st)
            bad += 1
            tmp.unlink(missing_ok=True)
            continue

        v = verdict(raw)
        if v["cls"] != "OK":
            QUAR.mkdir(parents=True, exist_ok=True)
            (QUAR / p.name).write_bytes(raw)
            st["failed"][key] = dict(why=f"{v['cls']} {v['reason']}", url=url)
            save_state(st)
            print(f"  [×] {p.name}  文本层 {v['cls']}：{v['reason']}", flush=True)
            bad += 1
            tmp.unlink(missing_ok=True)
            continue

        tmp.replace(p)
        st["done"][key] = str(p.relative_to(ROOT))
        save_state(st)
        done += 1
        fails_in_row = 0
        print(f"  [✓] {w['subject']:<12}{w['year']}-{w['sess']:<5}{w['code']:<7}"
              f"{len(raw)/1024:>6.0f}KB  (新{done} 跳{skip} 败{bad})", flush=True)

    save_state(st)
    MISS.write_text(json.dumps(st["failed"], ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n=== 新增 {done} / 跳过 {skip} / 未得 {bad} ===", flush=True)
    print(f"  请求 {budget.requests} 次，冷却 {budget.blocks} 回", flush=True)
    print(f"  状态 {STATE}\n  未得 {MISS}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
