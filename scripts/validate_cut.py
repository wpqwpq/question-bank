#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""validate_cut.py — 校验切割产物

ℹ️ 适用范围：大部分检查项与学科/考试局无关（schema、sha256、题号连续、
    区域重叠、分值合计），但检查项 8/9（MCQ 选项热区 A–D）基于 Edexcel
    理化选择题（四选项）的假设；其他学科选项数不同时需调整该项。
    切割层（cut_paper.py）为 Edexcel IAL 物理/化学共用一套。

用法:
    python scripts/validate_cut.py <session_dir>

检查项:
    1. cut.json 通过 JSON Schema 校验
    2. source.pdf 的 sha256 与 cut.json 记录一致
    3. 题号连续（1..N）
    4. 相邻题区域无重叠（同页时），region 均在页面范围内
    5. 分值合计 == paper.md 的 total_marks
    6. qNN.md 存在且 front-matter 的 cut_status 与 cut.json 同步
    7. front-matter 的 image / layout 路径指向存在的文件；layout.json 通过 schema 校验且归一化坐标合法
    8. MCQ 题有 A–D 四个选项热区（读 cut.json option_hotzones，缺时警告不判失败）
    9. layout.json 与 cut.json 的 MCQ 热区一致（数量与标签）
退出码: 全部通过 0，任一失败 1。
"""

import hashlib
import json
import re
import sys
from pathlib import Path

import jsonschema

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent


def load_front_matter(path):
    text = path.read_text(encoding="utf-8")
    m = re.match(r"^---\n(.*?)\n---", text, re.S)
    if not m:
        return None
    fm = {}
    cur = None
    for line in m.group(1).split("\n"):
        km = re.match(r"^([A-Za-z_][A-Za-z0-9_]*):\s*(.*)$", line)
        if km and not line.startswith((" ", "-")):
            cur = km.group(1)
            fm[cur] = km.group(2).strip().strip('"')
        elif cur and line.strip():
            fm.setdefault("_extra", {}).setdefault(cur, []).append(line.strip())
    return fm


def main():
    ap = sys.argv
    if len(ap) != 2:
        sys.exit("用法: python scripts/validate_cut.py <session_dir>")
    sdir = Path(ap[1]).resolve()
    cut_path = sdir / "cut.json"
    if not cut_path.exists():
        sys.exit(f"缺少 cut.json: {cut_path}")
    cut = json.loads(cut_path.read_text(encoding="utf-8"))

    failures, warnings = [], []

    def check(cond, msg):
        if not cond:
            failures.append(msg)

    # 1. Schema 校验
    schema = json.loads((ROOT / "schema" / "cut.schema.json").read_text(encoding="utf-8"))
    for err in jsonschema.Draft202012Validator(schema).iter_errors(cut):
        failures.append(f"cut.json schema: {'/'.join(map(str, err.absolute_path))}: {err.message}")

    # 2. sha256
    src = sdir / "source.pdf"
    sha = hashlib.sha256(src.read_bytes()).hexdigest()
    check(cut.get("source", {}).get("sha256") == sha, "source.pdf sha256 与 cut.json 不符")

    if cut.get("needs_ocr"):
        print("该卷标记 needs_ocr（扫描卷），仅做 schema/sha 校验。")
        print(f"{'FAIL' if failures else 'PASS'}  {'; '.join(failures) if failures else '无失败项'}"
              + (f"  警告: {'; '.join(warnings)}" if warnings else ""))
        sys.exit(1 if failures else 0)

    questions = cut.get("questions", [])
    check(questions, "cut.json 无题目数据")

    # 3. 题号连续
    nums = [int(q["number"]) for q in questions]
    check(nums == list(range(1, len(nums) + 1)), f"题号不连续: {nums}")

    # paper.md 总分
    total = None
    paper_md = sdir / "paper.md"
    if paper_md.exists():
        m = re.search(r"^total_marks:\s*(\d+)", paper_md.read_text(encoding="utf-8"), re.M)
        if m:
            total = int(m.group(1))

    for q in questions:
        n = q["number"]
        # 4. region 边界与相邻题重叠
        for rg in q.get("regions", []):
            b = rg["bbox"]
            check(len(b) == 4 and b[0] < b[2] and b[1] < b[3],
                  f"Q{n}: 非法 bbox {b}")
            check(rg["page"] < cut.get("source", {}).get("pages", 0),
                  f"Q{n}: 页码越界 {rg['page']}")
        idx = nums.index(int(n))
        if idx + 1 < len(questions):
            nxt = questions[idx + 1]
            if nxt["regions"] and q["regions"]:
                cur_last, next_first = q["regions"][-1], nxt["regions"][0]
                # 共用题干块（MCQ "Questions N and M refer" 式）会复制进
                # 两题的 regions，重叠属预期，跳过检查
                if cur_last["page"] == next_first["page"] \
                        and not (next_first.get("shared") or cur_last.get("shared")):
                    overlap = cur_last["bbox"][3] - next_first["bbox"][1]
                    check(overlap <= 6, f"Q{n} 与 Q{nxt['number']} 同页区域重叠 {overlap:.1f}pt")

        # 4b. answer_regions（答题册另附卷，{label, regions} 结构）
        flat_ans = [r for a in (q.get("answer_regions") or []) for r in a.get("regions", [])]
        for a in q.get("answer_regions") or []:
            if a.get("label") is not None:
                check(any(s.get("label") == a["label"] for s in q.get("subquestions") or []),
                      f"Q{n}: answer_regions 标签 {a['label']!r} 在 subquestions 中不存在")
        if q.get("question_type") == "mcq" and q.get("answer_regions"):
            warnings.append(f"Q{n}: MCQ 题不应有 answer_regions（选择题不拼接答题册）")
        for rg in flat_ans:
            b = rg["bbox"]
            check(len(b) == 4 and b[0] < b[2] and b[1] < b[3],
                  f"Q{n}: answer_regions 非法 bbox {b}")
            check(rg["page"] < cut.get("source", {}).get("pages", 0),
                  f"Q{n}: answer_regions 页码越界 {rg['page']}")
        if flat_ans and q.get("regions"):
            check(flat_ans[0]["page"] > q["regions"][-1]["page"],
                  f"Q{n}: answer_regions 起页不晚于 QP 末页")
        if idx + 1 < len(questions):
            nxt = questions[idx + 1]
            nxt_ans = [r for a in (nxt.get("answer_regions") or []) for r in a.get("regions", [])]
            if nxt_ans and flat_ans:
                cur_last, next_first = flat_ans[-1], nxt_ans[0]
                if cur_last["page"] == next_first["page"]:
                    overlap = cur_last["bbox"][3] - next_first["bbox"][1]
                    check(overlap <= 6, f"Q{n} 与 Q{nxt['number']} 答题区同页重叠 {overlap:.1f}pt")

        # 4c. subquestions（QP 小问标签锚点）
        for s in q.get("subquestions") or []:
            check(bool(s.get("label")), f"Q{n}: subquestions 缺 label")
            check(s.get("page", -1) < cut.get("source", {}).get("pages", 0),
                  f"Q{n}: subquestions 页码越界 {s.get('page')}")
            b = s.get("bbox", [])
            check(len(b) == 4 and b[0] < b[2] and b[1] < b[3],
                  f"Q{n}: subquestions 非法 bbox {b}")

        # 5. 分值
        if total is not None:
            pass
        # 7. 路径存在
        assets = sdir / "assets"
        wp = assets / f"q{int(n):02d}.webp"
        lp = assets / f"q{int(n):02d}.layout.json"
        if not wp.exists():
            warnings.append(f"Q{n}: 缺渲染图 {wp.name}（未跑 render_questions.py？）")
        if not lp.exists():
            failures.append(f"Q{n}: 缺 {lp.name}")

        # layout.json schema + 归一化坐标范围 + MCQ 热区一致性
        if lp.exists():
            lj = json.loads(lp.read_text(encoding="utf-8"))
            lschema = json.loads((ROOT / "schema" / "layout.schema.json").read_text(encoding="utf-8"))
            for err in jsonschema.Draft202012Validator(lschema).iter_errors(lj):
                failures.append(f"Q{n} layout.json schema: {'/'.join(map(str, err.absolute_path))}: {err.message}")
            boxes = [b for a in lj.get("areas", []) for b in a.get("boxes", [])]
            boxes += [o["box"] for o in lj.get("options", []) if "box" in o]
            for b in boxes:
                if not (0 <= b["x"] <= 1 and 0 <= b["y"] <= 1
                        and b["x"] + b["w"] <= 1.001 and b["y"] + b["h"] <= 1.001
                        and b["w"] > 0 and b["h"] > 0):
                    failures.append(f"Q{n}: layout.json 非法归一化坐标 {b}")
                    break
            # 9. 与 cut.json option_hotzones 一致
            hz = q.get("option_hotzones") or []
            if q.get("question_type") == "mcq":
                hz_labels = {o.get("label") for o in hz}
                if hz_labels != {"A", "B", "C", "D"}:
                    warnings.append(f"Q{n}: MCQ 选项热区为 {sorted(hz_labels)}")
                if sorted(o.get("label") for o in lj.get("options", [])) != sorted(o.get("label") for o in hz):
                    failures.append(f"Q{n}: layout.json options 与 cut.json option_hotzones 标签不一致")

        # 6. qNN.md front-matter 同步
        md = sdir / f"q{int(n):02d}.md"
        if not md.exists():
            failures.append(f"Q{n}: 缺 q{int(n):02d}.md")
            continue
        fm = load_front_matter(md)
        if fm is None:
            failures.append(f"Q{n}: qNN.md 缺 front-matter")
            continue
        if fm.get("cut_status") != q.get("status"):
            failures.append(f"Q{n}: cut_status 不同步 (md={fm.get('cut_status')} vs cut.json={q.get('status')})")
        if fm.get("question_number") != str(n):
            failures.append(f"Q{n}: question_number 不符")
        if fm.get("image") != f"assets/q{int(n):02d}.webp" or fm.get("layout") != f"assets/q{int(n):02d}.layout.json":
            failures.append(f"Q{n}: image/layout 路径字段不符")
        if total is not None and fm.get("total_marks") is not None:
            if int(fm["total_marks"]) != int(q.get("total_marks", -1)):
                failures.append(f"Q{n}: total_marks md/cut.json 不一致")

    # 5. 分值合计
    if total is not None:
        marks_sum = sum(int(q.get("total_marks", 0)) for q in questions)
        check(marks_sum == total, f"分值合计 {marks_sum} ≠ paper.md total_marks {total}")

    # 9. 答题册卷型：结构化题都应有 answer_regions（MCQ 不拼接，属正常）
    if cut.get("answer_booklet"):
        missing = [q["number"] for q in questions
                   if not q.get("answer_regions") and q.get("question_type") != "mcq"]
        if missing:
            warnings.append(f"答题册卷型但结构化题缺 answer_regions: Q{', '.join(missing)}")

    for w in warnings:
        print("  [warn]", w)
    status = "FAIL" if failures else "PASS"
    print(f"{status}  {len(questions)} 题"
          + (f"  失败项: {'; '.join(failures)}" if failures else "  全部检查通过"))
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
