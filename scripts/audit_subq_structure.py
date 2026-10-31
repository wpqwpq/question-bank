"""全库体检：已有标签的题，小问结构是否与 cut.json 对得上。

查三类问题（只报「有标签」的题）：
  A. missing_shell  —— cut.json 里有这个叶子小问，但磁盘上没有对应子题文件（状态会假绿）
  B. orphan_file    —— 有子题文件，但 cut.json 里没有这个标签（僵尸单元，标签挂空）
  C. letter_with_children —— 字母单元还留着文件，可 cut.json 里它已经有罗马子项了
                              （leaf 学科下该字母不该再是单元，等同我们刚修的那批）

用法：
  /opt/qbank/venv/bin/python audit_leaf_all.py [--json /tmp/out.json]
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

import os

ROOT = Path(os.environ.get("QBANK_ROOT", "/opt/qbank"))
sys.path.insert(0, str(ROOT / "workbench/backend"))
sys.path.insert(0, str(ROOT / "scripts"))

import store  # noqa: E402
import render_subsplit  # noqa: E402

DB = str(ROOT / "var/index.db")
DATA = ROOT / "data"


def load_tags(raw):
    try:
        v = json.loads(raw) if raw else []
    except Exception:  # noqa: BLE001
        return []
    return v or []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default="/tmp/leaf_audit.json")
    ap.add_argument("--all", action="store_true", help="不过滤有无标签，全部已拆分题都查")
    args = ap.parse_args()

    con = sqlite3.connect(DB)
    papers = {}
    for row in con.execute("SELECT paper_id,board,subject,dir_rel FROM papers"):
        papers[row[0]] = {"board": row[1], "subject": row[2], "dir": row[3]}

    rows = con.execute(
        "SELECT qid,paper_id,parent_qid,qnum,sub_label,tags,split FROM questions"
    ).fetchall()

    by_paper: dict[str, list] = {}
    for r in rows:
        by_paper.setdefault(r[1], []).append(r)

    issues = []
    n_papers = 0
    n_tagged_q = 0
    cut_cache: dict[str, dict] = {}

    for pid, prows in by_paper.items():
        info = papers.get(pid)
        if not info:
            continue
        pdir = DATA / info["dir"] if info["dir"] else None
        if pdir is None or not (pdir / "cut.json").exists():
            continue
        n_papers += 1
        # 按题号归组
        mains = {}
        subs_by_parent: dict[str, list] = {}
        for r in prows:
            qid, _pid, parent, qnum, sub_label, tags, split = r
            if parent:
                subs_by_parent.setdefault(parent, []).append(r)
            else:
                mains[qid] = r
        for qid, (mqid, _p, _par, qnum, _sl, mtags, msplit) in mains.items():
            subs = subs_by_parent.get(qid, [])
            tagged = bool(load_tags(mtags)) or any(load_tags(s[5]) for s in subs)
            if not tagged and not args.all:
                continue
            n_tagged_q += 1
            # 未拆分的题：整题一个单元，本来就没有子题文件，不算缺壳
            if not int(msplit or 0):
                continue
            if pid not in cut_cache:
                try:
                    cut_cache[pid] = json.loads((pdir / "cut.json").read_text(encoding="utf-8"))
                except Exception:  # noqa: BLE001
                    cut_cache[pid] = {}
            cutq = None
            for q in cut_cache[pid].get("questions", []):
                if str(q.get("number")) == str(qnum):
                    cutq = q
                    break
            if cutq is None:
                continue
            mode = store.sub_label_mode(info["board"], info["subject"])
            expected = (render_subsplit.leaf_labels(cutq) if mode == "leaf"
                        else render_subsplit.top_labels(cutq))
            actual = [str(s[4]) for s in subs if s[4] is not None]
            missing = [l for l in expected if l not in actual]
            orphan = [l for l in actual if l not in expected]
            parents = {l.split()[0] for l in expected if len(l.split()) > 1}
            letter_left = [l for l in actual if len(l.split()) == 1 and l in parents]
            if not (missing or orphan or letter_left):
                continue
            issues.append({
                "paper": pid, "q": str(qnum), "mode": mode,
                "expected": expected, "actual": actual,
                "missing_shell": missing, "orphan_file": orphan,
                "letter_with_children": letter_left,
                "tagged_units": [str(s[4]) for s in subs if load_tags(s[5])],
            })

    kinds = {"A 缺壳": 0, "B 僵尸单元": 0, "C 字母仍占位": 0}
    for x in issues:
        if x["missing_shell"]:
            kinds["A 缺壳"] += 1
        if x["orphan_file"]:
            kinds["B 僵尸单元"] += 1
        if x["letter_with_children"]:
            kinds["C 字母仍占位"] += 1
    print(f"扫描 {n_papers} 卷 / 有标签的题 {n_tagged_q} 道")
    print(f"有问题的题 {len(issues)} 道：{kinds}")
    Path(args.json).write_text(json.dumps(issues, ensure_ascii=False, indent=1),
                               encoding="utf-8")
    print("明细 →", args.json)
    for x in issues[:30]:
        print(f"  {x['paper']} Q{x['q']} [{x['mode']}]")
        if x["missing_shell"]:
            print(f"      缺壳: {x['missing_shell']}")
        if x["orphan_file"]:
            print(f"      僵尸: {x['orphan_file']}")
        if x["letter_with_children"]:
            print(f"      字母仍占位: {x['letter_with_children']} 已打标={x['tagged_units']}")


if __name__ == "__main__":
    main()
