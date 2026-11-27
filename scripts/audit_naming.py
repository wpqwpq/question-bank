# -*- coding: utf-8 -*-
"""命名规范扫描器（只读）。

检查 data/ 下全部卷的编码规范，输出汇总 + 逐条 CSV。
**不修改任何文件**。

用法：
    python scripts/audit_naming.py                    # 汇总
    python scripts/audit_naming.py --csv out.csv      # 另存逐条明细
    python scripts/audit_naming.py --limit 20         # 每类只看前 N 条
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

DATA = Path(__file__).resolve().parents[1] / "data"
SKIP = {"taxonomy", "_review", "_audit"}

# ---- 目标规范（这一步只做"检查"，不落地）----
QUAL_OK = {"a-level", "igcse"}
SESSION_OK = {"jan", "feb", "mar", "may", "june", "oct", "nov"}
QUAL_ALIAS = {"a-level": "a-level", "alevel": "a-level", "al": "a-level", "as": "a-level",
              "igcse": "igcse", "ig": "igcse", "gcse": "gcse"}
SESSION_ALIAS = {"march": "mar"}
SUBJECT_RE = re.compile(r"^[a-z]+(-[a-z]+)*$")
PAPER_DIR_RE = re.compile(r"^(\d{4})-([a-z]+)-(.+)$")
QID_TAIL_RE = re.compile(r"-q\d+(-\S+)?$")


def head_fm(p: Path) -> dict[str, str]:
    try:
        with p.open("r", encoding="utf-8", errors="replace") as f:
            txt = f.read(2048)
    except OSError:
        return {}
    if not txt.startswith("---"):
        return {}
    end = txt.find("\n---", 3)
    if end < 0:
        return {}
    out: dict[str, str] = {}
    for line in txt[3:end].splitlines():
        if ":" in line and not line.startswith((" ", "\t", "-")):
            k, v = line.split(":", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def norm_code(c: str) -> str:
    return re.sub(r"[^a-z0-9]", "", c.lower())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default="", help="逐条明细输出路径")
    ap.add_argument("--limit", type=int, default=5, help="每类打印样例数")
    args = ap.parse_args()

    issues: list[tuple[str, str, str, str, str, str]] = []  # paper_id, file, check, current, suggested, note
    counters: Counter[str] = Counter()
    scope_hit: dict[str, Counter[str]] = defaultdict(Counter)
    papers = 0

    for board_dir in sorted(p for p in DATA.iterdir() if p.is_dir() and p.name not in SKIP):
        for qual_dir in sorted(p for p in board_dir.iterdir() if p.is_dir()):
            for subj_dir in sorted(p for p in qual_dir.iterdir() if p.is_dir()):
                for pd in sorted(p for p in subj_dir.iterdir() if p.is_dir()):
                    papers += 1
                    paper_id = f"{board_dir.name}-{qual_dir.name}-{subj_dir.name}-{pd.name}"
                    where = f"{board_dir.name}/{qual_dir.name}/{subj_dir.name}"

                    # --- 目录层 ---
                    if qual_dir.name not in QUAL_OK:
                        counters["QUAL_DIR"] += 1
                        scope_hit["QUAL_DIR"][where] += 1
                        issues.append((paper_id, "", "QUAL_DIR", qual_dir.name,
                                       QUAL_ALIAS.get(qual_dir.name, "a-level"), "目录名不在白名单"))
                    if not SUBJECT_RE.match(subj_dir.name):
                        counters["SUBJECT_DIR"] += 1
                        scope_hit["SUBJECT_DIR"][where] += 1
                        issues.append((paper_id, "", "SUBJECT_DIR", subj_dir.name,
                                       subj_dir.name.replace("_", "-"), "学科名不符小写连字符"))

                    m = PAPER_DIR_RE.match(pd.name)
                    if not m:
                        counters["PAPER_DIR"] += 1
                        scope_hit["PAPER_DIR"][where] += 1
                        issues.append((paper_id, "", "PAPER_DIR", pd.name, "",
                                       "卷目录名不是 <year>-<session>-<code>"))
                        dir_year = dir_sess = dir_code = ""
                    else:
                        dir_year, dir_sess, dir_code = m.groups()
                        if dir_sess not in SESSION_OK:
                            counters["SESSION_DIR"] += 1
                            scope_hit["SESSION_DIR"][where] += 1
                            issues.append((paper_id, "", "SESSION_DIR", dir_sess,
                                           SESSION_ALIAS.get(dir_sess, dir_sess), "session 别名"))

                    # --- 内容层：每卷抽第一个 md（规则卷级一致）---
                    mds = sorted(pd.glob("q*.md"))
                    if not mds:
                        counters["NO_MD"] += 1
                        continue
                    probe = mds[0]
                    fm = head_fm(probe)
                    qid = fm.get("id", "")

                    if not qid:
                        counters["MISSING_ID"] += 1
                        issues.append((paper_id, probe.name, "MISSING_ID", "", "", "front-matter 无 id"))
                        continue

                    # URN：qid 去掉尾部 -qNN 后，应恰好等于 paper_id
                    tail = QID_TAIL_RE.search(qid)
                    head = qid[: tail.start()] if tail else qid
                    if head != paper_id:
                        extra = head[len(paper_id) + 1:] if head.startswith(paper_id + "-") else "<无法对齐>"
                        counters["QID_URN"] += 1
                        scope_hit["QID_URN"][where] += 1
                        new_qid = f"{paper_id}{qid[tail.start():] if tail else ''}"
                        issues.append((paper_id, probe.name, "QID_URN", qid, new_qid,
                                       f"qid 中间多一段：{extra[:24]}"))

                    # qualification 字段
                    fq = fm.get("qualification", "")
                    if fq not in QUAL_OK:
                        counters["QUAL_FM"] += 1
                        scope_hit["QUAL_FM"][where] += 1
                        issues.append((paper_id, probe.name, "QUAL_FM", fq,
                                       QUAL_ALIAS.get(fq, "a-level"), "front-matter qualification 非规范值"))

                    # session 字段
                    fs = fm.get("session", "")
                    if fs and fs not in SESSION_OK:
                        counters["SESSION_FM"] += 1
                        scope_hit["SESSION_FM"][where] += 1
                        issues.append((paper_id, probe.name, "SESSION_FM", fs,
                                       SESSION_ALIAS.get(fs, fs), "front-matter session 别名"))

                    # subject 字段
                    fsub = fm.get("subject", "")
                    if fsub and not SUBJECT_RE.match(fsub):
                        counters["SUBJECT_FM"] += 1
                        scope_hit["SUBJECT_FM"][where] += 1
                        issues.append((paper_id, probe.name, "SUBJECT_FM", fsub,
                                       fsub.replace("_", "-"), "front-matter subject 不符规范"))

                    # paper_code 与目录 code 一致性
                    fpc = fm.get("paper_code", "")
                    if dir_code and fpc and norm_code(fpc) != norm_code(dir_code):
                        counters["CODE_MISMATCH"] += 1
                        scope_hit["CODE_MISMATCH"][where] += 1
                        issues.append((paper_id, probe.name, "CODE_MISMATCH",
                                       f"{fpc} vs 目录 {dir_code}", dir_code,
                                       "同一卷两种卷号写法"))

                    # year 一致性
                    fy = fm.get("year", "")
                    if dir_year and fy and fy != dir_year:
                        counters["YEAR_MISMATCH"] += 1
                        scope_hit["YEAR_MISMATCH"][where] += 1
                        issues.append((paper_id, probe.name, "YEAR_MISMATCH",
                                       f"{fy} vs 目录 {dir_year}", dir_year, "年份与目录不符"))

    # ---------- 输出 ----------
    LABEL = {
        "QID_URN": "qid 中间多一段（URN）",
        "QUAL_DIR": "学段目录名非规范（a-level/gcse）",
        "QUAL_FM": "front-matter qualification 非规范",
        "SESSION_DIR": "卷目录 session 别名",
        "SESSION_FM": "front-matter session 别名",
        "SUBJECT_DIR": "学科目录名不符小写连字符",
        "SUBJECT_FM": "front-matter subject 不符规范",
        "CODE_MISMATCH": "paper_code 与目录卷号不一致",
        "PAPER_DIR": "卷目录名结构异常",
        "YEAR_MISMATCH": "年份与目录不符",
        "MISSING_ID": "缺 id 字段",
        "NO_MD": "卷内没有 md",
    }
    order = ["QID_URN", "QUAL_DIR", "QUAL_FM", "CODE_MISMATCH", "SESSION_DIR",
             "SESSION_FM", "SUBJECT_DIR", "SUBJECT_FM", "PAPER_DIR", "YEAR_MISMATCH",
             "MISSING_ID", "NO_MD"]

    print("=" * 74)
    print(f"命名规范扫描 · 共 {papers} 卷")
    print("=" * 74)
    total_bad = 0
    for k in order:
        if not counters.get(k):
            continue
        total_bad += counters[k]
        print(f"\n[{k}] {LABEL[k]}  —— {counters[k]} 卷")
        for where, n in scope_hit[k].most_common(4):
            print(f"      {where:42s} {n}")
        shown = 0
        for pid, fn, ck, cur, sug, note in issues:
            if ck != k:
                continue
            shown += 1
            if shown > args.limit:
                break
            print(f"      · {cur}")
            if sug and sug != cur:
                print(f"        → {sug}")

    print("\n" + "=" * 74)
    print(f"违规条目合计：{sum(counters.values())}（同一卷可命中多项）")
    print("=" * 74)

    if args.csv:
        out = Path(args.csv)
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(["paper_id", "file", "check", "current", "suggested", "note"])
            w.writerows(issues)
        print(f"逐条明细已写入 {out}（{len(issues)} 行）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
