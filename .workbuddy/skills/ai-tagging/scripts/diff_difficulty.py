#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""审核后校准：把「AI 初判难度」与「人工审核后的难度」逐单元对比，给出错法与统计。

用法：
    python diff_difficulty.py decisions.json [--paper <paper_id>] [--json out.json]

- paper_id 缺省时从 decisions.json 的 paper 字段取。
- 只读云端，不写任何东西。
- 输出四块：① 分布对照 ② 差异明细（判低/判高）③ 按分值段统计 ④ 一句话结论。

用途：每卷人工审核完跑一次，把新错法补进 references/rules.md §八 的锚点表。
"""
import argparse
import json
import os
import urllib.request
from collections import Counter
from pathlib import Path

ORDER = ["very-easy", "easy", "medium", "hard", "very-hard"]
CN = {"very-easy": "very-easy", "easy": "easy", "medium": "medium", "hard": "hard", "very-hard": "very-hard"}


def api_key():
    k = os.environ.get("QBANK_API_KEY", "").strip()
    if k:
        return k
    p = os.path.expanduser("~/.qbank_key")
    return Path(p).read_text(encoding="utf-8").strip() if os.path.exists(p) else ""


OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def get(base, path, key):
    req = urllib.request.Request(base + path, headers={"Authorization": "Bearer " + key})
    with OPENER.open(req, timeout=40) as r:
        return json.loads(r.read().decode())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("decisions")
    ap.add_argument("--paper")
    ap.add_argument("--base", default=os.environ.get("QBANK_BASE", "http://140.143.229.85"))
    ap.add_argument("--json")
    a = ap.parse_args()

    dec = json.loads(Path(a.decisions).read_text(encoding="utf-8"))
    paper = a.paper or dec["paper"]
    key = api_key()

    qs = get(a.base, f"/api/papers/{paper}/questions", key)
    now, marks = {}, {}
    for q in qs:
        d = get(a.base, "/api/questions/" + q["qid"], key)
        n = str(q["question_number"])
        marks[n] = d.get("meta", {}).get("total_marks")
        if d.get("subs"):
            now[n] = {x["label"]: {"d": x.get("difficulty"), "t": x.get("tags") or []} for x in d["subs"]}
        else:
            now[n] = {"整题": {"d": d.get("difficulty"), "t": d.get("tags") or []}}

    mine = {}
    for it in dec["items"]:
        n = str(it["n"])
        marks.setdefault(n, it.get("marks"))
        if it.get("subs"):
            for s in it["subs"]:
                mine[(n, s["label"])] = s.get("difficulty")
        else:
            mine[(n, "整题")] = it.get("difficulty")

    rows = []
    for n, units in now.items():
        for lab, v in units.items():
            m = mine.get((n, lab))
            if m != v["d"]:
                direction = "—"
                if m in ORDER and v["d"] in ORDER:
                    direction = "判低" if ORDER.index(v["d"]) > ORDER.index(m) else "判高"
                rows.append({"q": int(n), "label": lab, "marks": marks.get(n),
                             "ai": m, "human": v["d"], "dir": direction, "tags": v["t"]})
    rows.sort(key=lambda r: (r["q"], str(r["label"])))

    c_ai = Counter(m for m in mine.values())
    c_man = Counter(v["d"] for units in now.values() for v in units.values())
    total = sum(c_man.values())

    print(f"卷：{paper}")
    print(f"标注单元：{total}　差异：{len(rows)}（{len(rows)/total*100:.0f}%）")
    print()
    print(f"{'档':<12}{'AI':>5}{'人工':>6}")
    for d in ORDER:
        print(f"{CN[d]:<12}{c_ai.get(d,0):>5}{c_man.get(d,0):>6}")
    print()
    low = sum(1 for r in rows if r["dir"] == "判低")
    high = sum(1 for r in rows if r["dir"] == "判高")
    print(f"方向：判低 {low} / 判高 {high}")
    print()
    if rows:
        print("差异明细：")
        for r in rows:
            print(f"  q{r['q']}({r['marks']}分) {r['label']}: AI={r['ai']} → 人工={r['human']}  [{r['dir']}]  {','.join(r['tags'])}")
        print()
        print("按分值段：")
        seg = {}
        for r in rows:
            k = "1分" if r["marks"] == 1 else ("2-3分" if r["marks"] <= 3 else ("4-6分" if r["marks"] <= 6 else "7分+"))
            seg.setdefault(k, Counter())[r["dir"]] += 1
        for k in ["1分", "2-3分", "4-6分", "7分+"]:
            if k in seg:
                print(f"  {k}: 判低 {seg[k].get('判低',0)} / 判高 {seg[k].get('判高',0)}")

    if a.json:
        Path(a.json).write_text(json.dumps({
            "paper": paper, "total": total, "diffs": rows,
            "ai": dict(c_ai), "human": dict(c_man),
        }, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        print(f"\n已写 {a.json}")


if __name__ == "__main__":
    main()
