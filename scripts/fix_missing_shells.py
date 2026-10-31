"""按体检结果补「缺壳」子题文件（通用版）。

输入：audit_leaf_all.py 产出的 JSON；对每条 missing_shell 调 store.save_split
只建缺的那些 label（tags 空），已有子题文件一律不碰。

用法：
  /opt/qbank/venv/bin/python srv_fix_missing_shells.py /tmp/leaf_audit.json
  /opt/qbank/venv/bin/python srv_fix_missing_shells.py /tmp/leaf_audit.json --write

补完必须 rebuild_paper（脚本会自己调）。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import os

ROOT = Path(os.environ.get("QBANK_ROOT", "/opt/qbank"))
sys.path.insert(0, str(ROOT / "workbench/backend"))
sys.path.insert(0, str(ROOT / "scripts"))

import index_db  # noqa: E402
import store  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("audit_json")
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args()

    issues = json.loads(Path(args.audit_json).read_text(encoding="utf-8"))
    todo = [x for x in issues if x.get("missing_shell")]
    print(f"待补壳 {len(todo)} 题")
    touched = set()

    for x in todo:
        pid, qnum = x["paper"], int(x["q"])
        paper = store.find_paper(pid)
        if paper is None:
            print(f"  [skip] 找不到卷 {pid}")
            continue
        qpath = paper.path / f"q{qnum:02d}.md"
        if not qpath.exists():
            print(f"  [skip] 没有 {qpath.name}")
            continue
        labels = x["missing_shell"]
        print(f"  {pid} Q{qnum}: 补 {labels}")
        if not args.write:
            continue
        res = store.save_split(
            paper, qpath,
            subs=[{"label": l, "tags": [], "difficulty": None, "primary_tag": None}
                  for l in labels],
            render_results=[{"label": l, "image": None, "layout": None} for l in labels],
        )
        print(f"      现有 {len(res.get('subs') or [])} 个小问文件")
        touched.add(pid)

    for pid in sorted(touched):
        paper = store.find_paper(pid)
        if paper:
            n = index_db.rebuild_paper(paper.path, paper.board,
                                       paper.qualification, paper.subject)
            print(f"  重建索引 {pid} → {n} 行")
    print("\n干跑结束（未写盘）" if not args.write else "\n写盘完成")


if __name__ == "__main__":
    main()
