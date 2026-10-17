#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""拉取一张试卷的打标上下文：逐题结构信息 + 本卷候选标签。

用法：
    python fetch_paper.py <卷目录名> [--json out.json] [--base URL]

卷目录名 = data/<board>/<qual>/<subject>/<session> 的最后一段，如 2019-jan-ph01

输出 JSON：
    {paper, board, qual, subject, unit, count, items:[
        {n, qid, type, marks, split_eligible, top_labels, image_url,
         tags_now, primary_now, status,
         available_tags:[{id,zh,en,group}], available_groups:[{id,zh,unit}]}
    ]}

`primary_now` 是现状主标签（读侧已按 effective_primary 回退解析）；打新标签时**不要沿用**它，
必须按 references/rules.md「主标签怎么选」重新判一次。

`available_tags` 每项带 `en`（英文术语名，语义拿不准时以它为准）与 `group`（所属组 id）；
候选**不止本单元**——AQA U5 会并进 U1–U4、CIE P1P2 会并进 P4 的电场组，
归属靠 id 的单元前缀辨认，详见 references/rules.md §一。
"""
import argparse
import json
import sys
import urllib.error
import urllib.request

DEFAULT_BASE = "http://127.0.0.1:8642"


def get(url):
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("paper", help="卷目录名，如 2019-jan-ph01")
    ap.add_argument("--json", help="输出文件；缺省打到 stdout")
    ap.add_argument("--base", default=DEFAULT_BASE)
    a = ap.parse_args()

    try:
        papers = get(a.base + "/api/papers")
    except Exception as e:  # noqa: BLE001
        print(f"打标台不可达（{a.base}）：{e}\n先确认后端在跑，见 references/pitfalls.md", file=sys.stderr)
        sys.exit(2)

    p = next((x for x in papers if x["id"] == a.paper), None)
    if p is None:
        near = [x["id"] for x in papers if a.paper.split("-")[-1] in x["id"]][:8]
        print(f"找不到卷 {a.paper}。" + (f" 同卷号候选：{near}" if near else ""), file=sys.stderr)
        sys.exit(2)

    qs = get(f"{a.base}/api/papers/{p['id']}/questions")
    out = {"paper": p["id"], "board": p["board"], "qual": p["qualification"],
           "subject": p["subject"], "unit": p["unit"], "count": len(qs), "items": []}

    n_tags = 0
    for i, q in enumerate(qs, 1):
        d = get(f"{a.base}/api/questions/{q['qid']}")
        m = d["meta"]
        n_tags = len(d["available_tags"])
        out["items"].append({
            "n": m.get("question_number"),
            "qid": q["qid"],
            "type": m.get("question_type"),
            "marks": m.get("total_marks"),
            "split_eligible": d["split_eligible"],
            "top_labels": d["top_labels"],
            "image_url": d["image_url"],
            "tags_now": d["tags"],
            "primary_now": d.get("primary_tag"),
            "status": q.get("status"),
            "available_tags": [{"id": t["id"], "zh": t["zh"], "en": t.get("en") or "",
                                "group": t.get("group") or ""} for t in d["available_tags"]],
            "available_groups": [{"id": g["id"], "zh": g["zh"], "unit": g.get("unit")}
                                 for g in d["available_groups"]],
        })
        print(f"  [{i}/{len(qs)}] q{m.get('question_number')}", file=sys.stderr)

    text = json.dumps(out, ensure_ascii=False, indent=1)
    if a.json:
        with open(a.json, "w", encoding="utf-8", newline="") as f:
            f.write(text + "\n")
        n_split = sum(1 for x in out["items"] if x["split_eligible"])
        print(f"✅ {out['paper']}：{out['count']} 题（可拆分 {n_split}）"
              f"· 单元 {out['unit']} · 候选标签 {n_tags} 个 → {a.json}")
    else:
        print(text)


if __name__ == "__main__":
    main()
