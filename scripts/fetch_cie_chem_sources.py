# -*- coding: utf-8 -*-
"""fetch_cie_chem_sources.py — 给文本层损坏的 CIE 9701 化学卷换源

⚠️ 只换 source.pdf，**不切割**。换完源自己决定什么时候重切。

来源：pastpapers.co（`/api/file/...` 直链才是 PDF，页面 URL 返回的是 HTML 壳）。
实测它给的 9701_s25_qp_42.pdf 与另外拿到的「好版本」sha256 完全一致。

用法:
    python scripts/fetch_cie_chem_sources.py              # 只体检 + 下载 + 验证，不动 data/
    python scripts/fetch_cie_chem_sources.py --apply      # 验证通过的直接替换 source.pdf（原件备份）
    python scripts/fetch_cie_chem_sources.py --only 2024-oct-qp22,2025-june-qp42

判据:
    换源成功 = 新文件文本层 verdict 为 OK（虚词 ≥30、控制字符 <2%）。
    只是「能下载」不算数 —— 站点上也可能挂着同样是乱码的版本。
"""
import argparse
import hashlib
import json
import re
import shutil
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pymupdf

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "cie" / "alevel" / "chemistry"
CACHE = ROOT / ".tmp" / "pp_cache"
NEWSRC = ROOT / ".tmp" / "newsrc"
BASE = "https://pastpapers.co"
SYL = "chemistry-9701"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
SESS = {"june": "may-june", "oct": "oct-nov", "mar": "march"}

# 两种命名都要认：旧 9701_s25_qp_42.pdf / 新 9701-chemistry-june-2026-question-paper-42.pdf
RE_OLD = re.compile(r"9701_[smw]\d\d_qp_(\d\d)\.pdf$")
RE_NEW = re.compile(r"9701-chemistry-[a-z]+-\d{4}-question-paper-(\d\d)\.pdf$")

COMMON = set("the of and to in is are for that with which can be a an on by as at from "
             "this not was were has have it its".split())


def verdict(pdf):
    """文本层质量。返回 (cls, ctrl_ratio, common_words)。"""
    try:
        doc = pymupdf.open(str(pdf))
        txt = "".join(p.get_text() for p in doc)
        doc.close()
    except Exception as e:
        return f"ERROR:{type(e).__name__}", 0, 0
    total = len(txt)
    ctrl = sum(1 for c in txt if ord(c) < 32 and c not in "\n\r\t")
    words = re.findall(r"[A-Za-z]{3,}", txt)
    common = sum(1 for w in words if w.lower() in COMMON)
    ratio = ctrl / total if total else 0
    if common >= 30 and ratio < 0.02:
        cls = "OK"
    elif ratio > 0.15 or common < 5:
        cls = "CORRUPT"
    else:
        cls = "PARTIAL"
    return cls, round(ratio, 4), common


def get(url, referer=None, timeout=60, binary=False):
    req = urllib.request.Request(url)
    req.add_header("User-Agent", UA)
    if referer:
        req.add_header("Referer", referer)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=timeout) as r:
        return r.read() if binary else r.read().decode("utf-8", "replace")


def list_page(year, session):
    """抓一个 session 的列表页，返回 {paper_code: (filename, url)}。按年缓存。"""
    CACHE.mkdir(parents=True, exist_ok=True)
    slug = f"{year}-{SESS[session]}"
    cp = CACHE / f"{slug}.html"
    if cp.exists():
        html = cp.read_text(encoding="utf-8")
    else:
        try:
            html = get(f"{BASE}/caie/a-level/{SYL}/{slug}")
            cp.write_text(html, encoding="utf-8")
            time.sleep(0.6)
        except urllib.error.HTTPError as e:
            return {}, f"HTTP {e.code}"
        except Exception as e:
            return {}, f"{type(e).__name__}"
    out = {}
    for m in re.finditer(r'href="(/caie/a-level/chemistry-9701/[^"]+\.pdf)"', html):
        path = m.group(1)
        fn = path.rsplit("/", 1)[-1]
        mm = RE_OLD.search(fn) or RE_NEW.search(fn)
        if mm:
            out[mm.group(1)] = (fn, BASE + "/api/file" + path)
    return out, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="验证通过就替换 source.pdf")
    ap.add_argument("--only", default="", help="逗号分隔的卷名")
    ap.add_argument("--include-partial", action="store_true",
                    help="PARTIAL（选项行乱码）也换")
    args = ap.parse_args()

    only = {s.strip() for s in args.only.split(",") if s.strip()}
    vols = sorted([d for d in DATA.iterdir() if d.is_dir() and (d / "source.pdf").exists()])
    if only:
        vols = [v for v in vols if v.name in only]

    # 1 体检
    todo = []
    for v in vols:
        cls, ratio, common = verdict(v / "source.pdf")
        if cls == "OK":
            continue
        if cls == "PARTIAL" and not args.include_partial:
            continue
        todo.append(dict(paper=v.name, dir=str(v), cls=cls,
                         ratio=ratio, common=common))
    print(f"待换源 {len(todo)} 卷 / 共扫 {len(vols)} 卷\n")

    # 2 按 (year, session) 分组抓列表
    groups = {}
    for t in todo:
        yr, ses, code = t["paper"].split("-")
        groups.setdefault((yr, ses), []).append((t, code.replace("qp", "")))

    NEWSRC.mkdir(parents=True, exist_ok=True)
    results = []
    for (yr, ses), items in sorted(groups.items()):
        files, err = list_page(yr, ses)
        if err:
            print(f"[{yr}-{ses}] 列表页失败: {err} → 这批 {len(items)} 卷跳过")
            for t, pp in items:
                results.append(dict(t, new="NO_LIST", note=err))
            continue
        print(f"[{yr}-{ses}] 列表页 {len(files)} 个 qp 文件")
        for t, pp in items:
            if pp not in files:
                print(f"  {t['paper']:<18} 站上没有 qp{pp}")
                results.append(dict(t, new="NOT_ON_SITE", note=""))
                continue
            fn, url = files[pp]
            dst = NEWSRC / f"{t['paper']}.pdf"
            if not dst.exists() or dst.stat().st_size < 10000:
                try:
                    raw = get(url, referer=f"{BASE}/caie/a-level/{SYL}/{yr}-{SESS[ses]}",
                              binary=True)
                    if not raw.startswith(b"%PDF"):
                        raise ValueError(f"不是 PDF（{len(raw)}B，疑似 HTML 壳）")
                    dst.write_bytes(raw)
                    time.sleep(0.6)
                except Exception as e:
                    print(f"  {t['paper']:<18} 下载失败: {type(e).__name__}: {e}")
                    results.append(dict(t, new="DL_FAIL", note=str(e)[:80]))
                    continue
            cls, ratio, common = verdict(dst)
            ok = cls == "OK"
            print(f"  {t['paper']:<18} {fn:<52} → {cls} 控制{ratio*100:.1f}% 虚词{common} "
                  f"{'✅' if ok else '❌'}")
            results.append(dict(t, new=cls, note=fn, new_ratio=ratio,
                                new_common=common, size=dst.stat().st_size))

    # 3 替换
    if args.apply:
        stamp = time.strftime("%Y%m%d")
        n = 0
        for r in results:
            if r["new"] != "OK":
                continue
            vol = Path(r["dir"])
            src = NEWSRC / f"{r['paper']}.pdf"
            bak = vol / f"source.pdf.bak-{stamp}"
            if not bak.exists():
                shutil.copy2(vol / "source.pdf", bak)
            shutil.copy2(src, vol / "source.pdf")
            n += 1
        print(f"\n已替换 {n} 卷（原件备份为 source.pdf.bak-{stamp}）")

    # 4 报告
    rep = ROOT / ".tmp" / "fetch_report.json"
    rep.write_bytes(json.dumps(results, ensure_ascii=False, indent=1).encode("utf-8"))
    good = sum(1 for r in results if r["new"] == "OK")
    bad = [r for r in results if r["new"] != "OK"]
    print(f"\n=== 汇总：{good} 卷拿到好源 / {len(bad)} 卷没解决 ===")
    for r in bad:
        print(f"  {r['paper']:<18} {r['new']:<12} {r.get('note','')}")
    print(f"报告: {rep}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
