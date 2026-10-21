# -*- coding: utf-8 -*-
"""fetch_cie_papers.py — CIE (CAIE) A-Level 真题抓取：数学 / 生物 / 经济 / 会计 / 商务

源：pastpapers.co（与 scripts/fetch_cie_chem_sources.py 同一条通道，2026-10-18 已验证）
    学科页  https://pastpapers.co/caie/a-level/<subject-slug>            → 列出所有场次
    场次页  https://pastpapers.co/caie/a-level/<subject-slug>/<session>  → 列出文件
    真 PDF  https://pastpapers.co/api/file/caie/a-level/<subject-slug>/<session>/<file>.pdf

只下 **question paper**：含 `question-paper`（2026 起命名）或 `_qp_`（≤2025）；
`_ms_` / `_er_` / `_gt_` / `_in_` / `_ci_` 等附件一律排除。
只下 **2016 年及以后**。

用法：
    python scripts/fetch_cie_papers.py --scan              # 扫学科页 + 场次页，生成 manifest
    python scripts/fetch_cie_papers.py                     # 按 manifest 下载 + 文本层体检
    python scripts/fetch_cie_papers.py --only biology      # 只跑一科
    python scripts/fetch_cie_papers.py --probe-only        # 只探不写盘

交付判据：**文本层 verdict == OK**；PARTIAL / CORRUPT 落 `.tmp/quarantine/` 等换源。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fetch_papers_lib import RateLimiter, fetch, verdict   # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "downloadPapers"
CACHE = ROOT / ".tmp" / "pp_cache"
MANIFEST = ROOT / ".tmp" / "cie_manifest.json"

BASE = "https://pastpapers.co"
AREA = "caie/a-level"

# 学科 → pastpapers.co 目录 slug
SUBJECTS = {
    "mathematics": "mathematics-9709",
    "biology": "biology-9700",
    "economics": "economics-9708",
    "accounting": "accounting-9706",
    "business": "business-9609",
}
NICE = {
    "mathematics": ("9709", "Mathematics"),
    "biology": ("9700", "Biology"),
    "economics": ("9708", "Economics"),
    "accounting": ("9706", "Accounting"),
    "business": ("9609", "Business"),
}
YEAR_FLOOR = 2016

RE_SESSION = re.compile(r"/(\d{4})(?:-(march|feb-march|may-june|oct-nov))?(?:/(\d{4})-(mar|jun|nov|march|may-june|oct-nov))?$")
RE_OLD = re.compile(r"^(\d{4})_([smw])(\d{2})_qp_(\d{2})\.pdf$", re.I)
RE_NEW = re.compile(r"^(\d{4})-[a-z-]+-(january|march|june|october|november)-"
                    r"(\d{4})-question-paper-(\d+)\.pdf$", re.I)
EXCLUDE = re.compile(r"_(ms|er|gt|in|ci|ir|sf|sm|tn|lt|msc|sc)_|mark-scheme|examiner-report|"
                     r"grade-threshold|insert|confidential|specimen|syllabus|notes", re.I)

SESS_TOKEN = {"may-june": "june", "june": "june", "oct-nov": "oct", "october": "oct",
              "november": "oct", "march": "mar", "feb-march": "mar", "january": "jan"}
LETTER = {"s": "june", "w": "oct", "m": "mar"}


def cache_path(slug):
    CACHE.mkdir(parents=True, exist_ok=True)
    return CACHE / (re.sub(r"[^\w.\-]", "_", slug) + ".html")


def get_page(url, limiter, log, retries=3):
    cp = cache_path(url.split("pastpapers.co/", 1)[-1])
    if cp.exists():
        if cp.stat().st_size < 200:
            return None
        return cp.read_text(encoding="utf-8", errors="replace")
    try:
        raw = fetch(url, limiter=limiter, retries=retries, log=log)
    except FileNotFoundError:
        cp.write_bytes(b"404")
        return None
    except Exception as e:
        log(f"    页面失败 {type(e).__name__}: {url}")
        return None
    cp.write_bytes(raw)
    return raw.decode("utf-8", "replace")


def sessions_of(subject, limiter, log):
    """学科页 → [(year, session_token, rel_path)]，只留 >=2016。

    pastpapers.co 的层级不齐：2018 起是 `/2019-may-june` 一层，
    2016/2017 是 `/2016/2016-jun` 两层。所以这里只做「路径是不是年份开头」的筛，
    真正的下钻交给 scan() 的 BFS。
    """
    slug = SUBJECTS[subject]
    html = get_page(f"{BASE}/{AREA}/{slug}", limiter, log)
    if not html:
        return []
    out, seen = [], set()
    pat = re.compile(r'href="(/' + AREA + r'/' + re.escape(slug) + r'/([^"#?]+))"')
    for m in pat.finditer(html):
        rel, tail = m.group(1), m.group(2)
        head = tail.split("/")[0]
        if not head[:4].isdigit() or int(head[:4]) < YEAR_FLOOR:
            continue
        if any(k in tail.lower() for k in ("notes", "solved", "topical", "syllabus", "specimen")):
            continue
        if rel in seen:
            continue
        seen.add(rel)
        out.append((int(head[:4]), None, rel))
    return sorted(out)


def sub_sessions(html, slug):
    """年份合并页 → 下一层场次路径。"""
    out, seen = [], set()
    pat = re.compile(r'href="(/' + AREA + r'/' + re.escape(slug) + r'/(\d{4})/([^"#?]+))"')
    for m in pat.finditer(html):
        rel = m.group(1)
        if rel in seen:
            continue
        seen.add(rel)
        out.append(rel)
    return sorted(out)


def parse_listing(html, sess_path, subject):
    """返回 [(code, filename, url, year, sess_token)]，只留 question paper。

    页面里每个文件两条链接：`href="/caie/.../x.pdf"`（View，无查询串）
    与 `href="/api/file/.../x.pdf?download=1"`（下载）。真 PDF 走后者，
    所以这里按 View 链接抓路径，再拼成 `/api/file` + path。
    """
    out, seen = [], set()
    slug = SUBJECTS[subject]
    pat = re.compile(r'href="(/caie/a-level/' + re.escape(slug) + r'/[^"]+?\.pdf)"')
    for m in pat.finditer(html):
        path = m.group(1)
        fn = path.rsplit("/", 1)[-1]
        if fn in seen or EXCLUDE.search(fn):
            continue
        mo, mn = RE_OLD.match(fn), RE_NEW.match(fn)
        if mo:
            # 9709_s18_qp_11.pdf → 大纲 9709 / 场次 s / 年份 18 / 卷号 11
            year, letter, code = 2000 + int(mo.group(3)), mo.group(2).lower(), mo.group(4)
            sess = LETTER[letter]
        elif mn:
            year, code = int(mn.group(3)), mn.group(4)
            sess = SESS_TOKEN.get(mn.group(2).lower(), "mixed")
        else:
            continue
        if year < YEAR_FLOOR:
            continue
        seen.add(fn)
        out.append((code, fn, BASE + "/api/file" + path, year, sess))
    return out


def scan(only, limiter, log):
    manifest = []
    for subject in (only or SUBJECTS):
        slug = SUBJECTS[subject]
        roots = sessions_of(subject, limiter, log)
        log(f"[{subject}] 年份页 {len(roots)} 个")
        queue = [(rel, 1) for _, _, rel in roots]
        seen_paths = set(r for r, _ in queue)
        got = 0
        while queue:
            rel, depth = queue.pop(0)
            html = get_page(BASE + rel, limiter, log)
            if not html:
                log(f"  {rel} 页面取不到")
                continue
            items = parse_listing(html, rel, subject)
            if items:
                for code, fn, url, y, s in items:
                    manifest.append(dict(subject=subject, year=y, session=s,
                                         code=code, file=fn, url=url, listing=rel))
                got += len(items)
                log(f"  {rel:<46} {len(items):>2} 份")
                continue
            if depth >= 2:
                continue
            subs = sub_sessions(html, slug)
            if subs:
                log(f"  {rel:<46} → 下钻 {len(subs)} 个场次")
            for s2 in subs:
                if s2 not in seen_paths:
                    seen_paths.add(s2)
                    queue.append((s2, depth + 1))
        log(f"[{subject}] 合计 {got} 份")
    # 去重（同一文件可能在合并页里出现两次）
    uniq, seen = [], set()
    for m in manifest:
        if (m["subject"], m["file"]) in seen:
            continue
        seen.add((m["subject"], m["file"]))
        uniq.append(m)
    MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST.write_bytes(json.dumps(uniq, ensure_ascii=False, indent=1).encode("utf-8"))
    by_sub = {}
    for m in uniq:
        by_sub[m["subject"]] = by_sub.get(m["subject"], 0) + 1
    log(f"\nmanifest: {len(uniq)} 份 → {MANIFEST}")
    for k, v in sorted(by_sub.items()):
        log(f"  {k:<12} {v}")
    return uniq


def dest_dir(subject):
    code, nice = NICE[subject]
    return OUT / f"CIE_{code}_{nice}" / "qp"


def download(manifest, only, limiter, log, probe_only=False):
    todo = [m for m in manifest if not only or m["subject"] in only]
    stat = dict(ok=0, skip=0, bad=0, fail=0)
    bad = []
    for i, m in enumerate(todo, 1):
        d = dest_dir(m["subject"])
        d.mkdir(parents=True, exist_ok=True)
        path = d / m["file"]
        if path.exists() and path.stat().st_size > 20000:
            stat["skip"] += 1
            continue
        if probe_only:
            stat["ok"] += 1
            continue
        try:
            raw = fetch(m["url"], referer=BASE + m["listing"], limiter=limiter, log=log)
        except FileNotFoundError:
            stat["fail"] += 1
            bad.append(dict(**m, why="404（列表里有、文件接口没有）"))
            continue
        except Exception as e:
            stat["fail"] += 1
            bad.append(dict(**m, why=f"DL {type(e).__name__}: {e}"))
            log(f"  下载失败 {m['file']}: {type(e).__name__}")
            continue
        if not raw.startswith(b"%PDF"):
            stat["fail"] += 1
            bad.append(dict(**m, why=f"非 PDF（{len(raw)}B）"))
            continue
        v = verdict(raw)
        if v["cls"] != "OK":
            q = ROOT / ".tmp" / "quarantine" / m["file"]
            q.parent.mkdir(parents=True, exist_ok=True)
            q.write_bytes(raw)
            stat["bad"] += 1
            bad.append(dict(**m, why=f"{v['cls']} {v['reason']}"))
            log(f"  文本层 {v['cls']:<8} {m['file']}  {v['reason']}")
            continue
        path.write_bytes(raw)
        stat["ok"] += 1
        if i % 20 == 0:
            log(f"  …{i}/{len(todo)}  {stat}")

    log(f"\n=== 汇总 {stat} ===")
    if bad:
        log(f"未交付 {len(bad)} 份：")
        for b in bad[:60]:
            log(f"  {b['subject']:<12} {b['file']:<46} {b['why'][:70]}")
    rep = ROOT / ".tmp" / "cie_fetch_report.json"
    rep.write_bytes(json.dumps(dict(stat=stat, bad=bad), ensure_ascii=False,
                               indent=1).encode("utf-8"))
    log(f"报告 {rep}")
    return 0 if not bad else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scan", action="store_true", help="只扫页生成 manifest")
    ap.add_argument("--only", default="", help="逗号分隔学科: " + ",".join(SUBJECTS))
    ap.add_argument("--probe-only", action="store_true")
    ap.add_argument("--interval", type=float, default=0.0, help="同域最小间隔秒")
    ap.add_argument("--concurrency", type=int, default=0)
    args = ap.parse_args()
    only = [s.strip() for s in args.only.split(",") if s.strip()]

    def log(msg):
        print(msg, flush=True)

    interval = args.interval or (1.1 if args.scan else 0.4)
    conc = args.concurrency or (1 if args.scan else 2)
    limiter = RateLimiter(min_interval=interval, max_concurrency=conc, cooldown=45)

    if args.scan:
        scan(only, limiter, log)
        return 0
    if not MANIFEST.exists():
        log("manifest 不存在，先跑 --scan")
        return 1
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    log(f"manifest {len(manifest)} 份")
    return download(manifest, only, limiter, log, args.probe_only)


if __name__ == "__main__":
    sys.exit(main())
