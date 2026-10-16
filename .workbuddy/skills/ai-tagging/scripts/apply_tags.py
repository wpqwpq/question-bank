#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""按 decisions.json 经打标台 API 写知识点标签（默认 dry-run）。

用法：
    python apply_tags.py decisions.json [--apply] [--base URL]

写库路径（与人工在打标台点击完全等价）：
    PUT /api/questions/{qid}/annotation
      · 整题：{"tags": [...], "difficulty": null}
      · 拆分：{"subs": [{"label","tags","difficulty": null}...], "difficulty": null}

⚠️ 绝不直接改 qNN.md：拆分题保存时后端要跑 render_subsplit 产子图，直写文件没有这个副作用。
"""
import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_BASE = "http://127.0.0.1:8642"


def put(base, qid, body):
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(f"{base}/api/questions/{qid}/annotation", data=data,
                                 method="PUT", headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("decisions")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--only", help="只跑这些题号，逗号分隔（调试用）")
    a = ap.parse_args()

    dec = json.loads(Path(a.decisions).read_text(encoding="utf-8"))
    items = dec["items"]
    if a.only:
        want = {x.strip() for x in a.only.split(",")}
        items = [x for x in items if str(x.get("n")) in want]

    ok, skip, fail = [], [], []
    t0 = time.time()
    for it in items:
        n = it.get("n")
        qid = it.get("qid")
        reason = it.get("skip_reason")
        if reason:
            skip.append({"n": n, "reason": reason})
            print(f"[skip] q{n}：{reason}")
            continue
        if not qid:
            fail.append({"n": n, "error": "decisions.json 缺 qid（先用 fetch_paper.py 拉上下文再合并）"})
            print(f"✗ q{n}：缺 qid", file=sys.stderr)
            continue

        subs = it.get("subs")
        if subs:
            body = {"subs": [{"label": s["label"], "tags": s["tags"], "difficulty": None}
                             for s in subs], "difficulty": None}
            desc = "subs[" + ", ".join(s["label"] for s in subs) + "]"
        else:
            body = {"tags": it.get("tags") or [], "difficulty": None}
            desc = "tags=" + ",".join(body["tags"])

        if not a.apply:
            print(f"q{n} {desc}")
            continue
        try:
            r = put(a.base, qid, body)
            ok.append({"n": n, "status": r.get("status"), "split": r.get("split"),
                       "sub_labels": r.get("sub_labels")})
            print(f"✓ q{n} status={r.get('status')} {desc}")
        except urllib.error.HTTPError as e:
            msg = e.read().decode("utf-8")[:300]
            fail.append({"n": n, "code": e.code, "error": msg})
            print(f"✗ q{n} HTTP {e.code}: {msg}", file=sys.stderr)
        except Exception as e:  # noqa: BLE001
            fail.append({"n": n, "error": str(e)})
            print(f"✗ q{n} {e}", file=sys.stderr)

    if not a.apply:
        print(f"\n[dry-run] {len(items)} 题，未写库。加 --apply 执行。")
        return

    print(f"\n✅ 成功 {len(ok)} / 跳过 {len(skip)} / 失败 {len(fail)}，耗时 {time.time() - t0:.1f}s")
    side = Path(a.decisions).with_suffix(".result.json")
    with open(side, "w", encoding="utf-8", newline="") as f:
        f.write(json.dumps({"ok": ok, "skip": skip, "fail": fail}, ensure_ascii=False, indent=1) + "\n")
    print(f"   结果写到 {side}")
    if fail:
        sys.exit(1)


if __name__ == "__main__":
    main()
