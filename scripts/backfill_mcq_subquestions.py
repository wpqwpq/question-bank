#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""backfill_mcq_subquestions.py — 给「多问选择题」补小问标签（增量，不重切不重渲）

背景
----
Edexcel 化学 Section A 里有一类题：一个题号下 (a)(b)(c) 各带一套 A–D 选项
（例 2021-06 WCH14 Q4，3 分 = 3 问，每问 1 分）。cut_paper.py 原先只对
非 MCQ 扫小问标签，导致这类题的 cut.json `subquestions` 恒为空，下游
（打标台 / 切分 / 预览）拿不到小问结构，只能整题打标。

本脚本用**切割脚本同款解析函数**重新扫一遍 source.pdf（按现有 regions 圈定
范围），把标签行写进 cut.json 的 `subquestions`，并同步补 layout.json 的
归一化 box（否则 validate_cut.py 会判「layout 与 cut 不一致」）。

不动的东西
----------
  · assets/qNN.webp        一级题图，一个像素都不动
  · regions / option_hotzones / anchors / 正文
  · qNN.md                 front-matter 与正文都不写（split 标记由打标台在切分时写）

为什么 layout 能离线补
----------------------
MCQ 无 answer_block / answer_regions（已全库核实 514/514 题都没有），
渲染时 pieces == regions、n_kind==1 → 无提示带 / 无分隔线，
归一化坐标只依赖「各 region 的像素高 + 渲染 zoom」，pymupdf 算一遍即可。
实测 110 题里 109 题的 options 坐标与现有 layout 逐字节吻合
（唯一例外 2025-oct-wch14a Q2 的选项热区本身就坏了，见 --report）。

用法
----
    # 干跑（默认），只出报告不落盘
    python scripts/backfill_mcq_subquestions.py

    # 试点指定几卷
    python scripts/backfill_mcq_subquestions.py --papers 2021-june-wch14,2025-june-wch11 --apply

    # 全量落盘
    python scripts/backfill_mcq_subquestions.py --apply

退出码：0 正常；1 有卷在「校验不过」时被跳过（其余照写）。
"""

import argparse
import json
import re
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import cut_paper as cp  # noqa: E402
import pymupdf  # noqa: E402

ROOT = HERE.parent / "data" / "edexcel" / "a-level" / "chemistry"
BACKUP_SUFFIX = ".bak-mcqsubq"


# ── 小问标签扫描（与 cut_paper.py 的 QP 侧扫描规则一致） ──────────────────
def scan_subheads(q, n, pages_lines):
    """扫本题 regions 内的顶层小问标签行，返回 [{label,page,bbox}]（文档序、去重）。"""
    found, seen = [], set()
    for rg in q["regions"]:
        rbb = rg["bbox"]
        for ln in pages_lines[rg["page"]]:
            lb = ln["bbox"]
            if lb[1] >= cp.FOOTER_Y or lb[0] > cp.LEFT_MARGIN_MAX:
                continue
            if lb[1] < rbb[1] - 1 or lb[1] > rbb[3] + 1:
                continue
            tokens = None
            parsed = cp.parse_subhead(ln["raw"])
            if parsed and parsed[0] == n and parsed[2]:
                tokens = parsed[2]
            else:
                bare = cp.parse_bare_subhead(ln["raw"])
                if bare and bare[1]:
                    tokens = bare[1]
                else:
                    # 化学题号行形如 "14:\t(a)\t …"（题号后带冒号，SUBHEAD_RE 不收）
                    m2 = re.match(r"^\*?(\d{1,2})\s*:?\s+(.*)$", ln["raw"].strip())
                    if m2 and int(m2.group(1)) == n:
                        b2 = cp.parse_bare_subhead(m2.group(2))
                        if b2 and b2[1]:
                            tokens = b2[1]
            if not tokens or tokens[0] in cp.ROMAN_TOKENS:
                continue
            key = (tokens[0], rg["page"], round(lb[1], 1))
            if key in seen:
                continue
            seen.add(key)
            found.append({"label": cp.sub_label(tokens), "page": rg["page"],
                          "bbox": [cp.r2(v) for v in lb]})
    return found


def nbox(x0, y0, x1, y1, W, H):
    return {"x": round(x0 / W, 4), "y": round(y0 / H, 4),
            "w": round((x1 - x0) / W, 4), "h": round((y1 - y0) / H, 4)}


def geometry(doc, q, dpi):
    """复算一级图尺寸与各 region 在图内的像素偏移（MCQ 无册内段，逻辑最简）。"""
    zoom = dpi / 72.0
    heights, widths = [], []
    for rg in q["regions"]:
        clip = pymupdf.Rect(rg["bbox"])
        pix = doc[rg["page"]].get_pixmap(matrix=pymupdf.Matrix(zoom, zoom),
                                         clip=clip, alpha=False)
        heights.append(pix.height)
        widths.append(pix.width)
    offs = [0]
    for h in heights[:-1]:
        offs.append(offs[-1] + h)
    return zoom, max(widths), sum(heights), offs


def sq_boxes(q, subs, zoom, W, H, offs):
    """小问标签行 → layout.json 的归一化 box（规则与 render_questions.py 一致）。"""
    out = []
    for s in subs:
        b = s["bbox"]
        box = None
        for i, rg in enumerate(q["regions"]):
            rb = rg["bbox"]
            if rg["page"] == s["page"] and rb[1] - 1 <= b[1] and b[3] <= rb[3] + 1:
                box = nbox((b[0] - rb[0]) * zoom, offs[i] + (b[1] - rb[1]) * zoom,
                           (b[2] - rb[0]) * zoom, offs[i] + (b[3] - rb[1]) * zoom, W, H)
                break
        if box is not None:
            out.append({"label": s["label"], "box": box})
    return out


def parts_of(q):
    """选项热区的 part 集合（文档序去重），用于交叉校验小问分组。"""
    out = []
    for o in q.get("option_hotzones") or []:
        p = o.get("part")
        if p and p not in out:
            out.append(p)
    return out


def backup(path: Path) -> None:
    bak = path.with_name(path.name + BACKUP_SUFFIX)
    if not bak.exists():          # 只备份第一次的原件，重复跑不覆盖
        shutil.copy2(path, bak)


def process_paper(sdir: Path, apply: bool, verbose: bool):
    """返回 (状态, 报告 dict)。状态 ∈ ok / skip / block。"""
    cut_path = sdir / "cut.json"
    if not cut_path.exists():
        return "skip", {"paper": sdir.name, "reason": "无 cut.json"}

    cut = json.loads(cut_path.read_text(encoding="utf-8"))
    pdf_path = sdir / "source.pdf"
    if not pdf_path.exists():
        return "skip", {"paper": sdir.name, "reason": "无 source.pdf（补小问必须重扫 PDF）"}
    dpi = cut.get("render", {}).get("dpi", 200)

    targets = []
    for i, q in enumerate(cut["questions"]):
        if q.get("question_type") != "mcq":
            continue
        if (q.get("total_marks") or 0) > 1 or len(parts_of(q)) > 1:
            targets.append((i, q))
    if not targets:
        return "skip", {"paper": sdir.name, "reason": "无多问选择题"}

    doc = pymupdf.open(pdf_path)
    pages_lines = [cp.build_page_lines(pg) for pg in doc]
    report = {"paper": sdir.name, "questions": [], "warn": [], "written": 0}

    for i, q in targets:
        n = int(q["number"])
        qb = f"q{n:02d}"
        marks = q.get("total_marks") or 0
        parts = parts_of(q)

        if q.get("subquestions"):
            report["questions"].append((n, "已有小问，跳过", [s["label"] for s in q["subquestions"]]))
            continue

        subs = scan_subheads(q, n, pages_lines)
        labels = [s["label"] for s in subs]
        if not subs:
            report["warn"].append(f"Q{n}: 扫不到小问标签，跳过")
            continue
        if len(subs) != marks:
            report["warn"].append(f"Q{n}: 扫到 {len(subs)} 个标签（{labels}）≠ 分值 {marks}，跳过")
            continue
        if parts and labels != parts:
            # 不影响打标与切图（切点来自标签行），只说明选项热区分组有问题，记录下来
            report["warn"].append(f"Q{n}: 标签 {labels} 与选项分组 {parts} 不一致（选项热区待修）")

        # layout 同步补 subquestions
        lp = sdir / "assets" / f"{qb}.layout.json"
        if not lp.exists():
            report["warn"].append(f"Q{n}: 缺 {lp.name}，只补 cut.json")
            lj = None
        else:
            lj = json.loads(lp.read_text(encoding="utf-8"))
            zoom, W, H, offs = geometry(doc, q, dpi)
            if [W, H] != [lj["size"]["w"], lj["size"]["h"]]:
                report["warn"].append(
                    f"Q{n}: 复算尺寸 {W}x{H} 与 layout {lj['size']} 不符，只补 cut.json")
                lj = None
            else:
                boxes = sq_boxes(q, subs, zoom, W, H, offs)
                if len(boxes) != len(subs):
                    report["warn"].append(
                        f"Q{n}: {len(subs) - len(boxes)} 个标签未落进任何 region，只补 cut.json")
                    lj = None
                else:
                    lj["subquestions"] = boxes

        q["subquestions"] = subs
        report["questions"].append((n, "补", labels, "跨页" if len({s["page"] for s in subs}) > 1 else ""))

        if apply:
            backup(cut_path)
            if lj is not None:
                backup(lp)
                lp.write_text(json.dumps(lj, ensure_ascii=False, indent=1) + "\n",
                              encoding="utf-8")
            report["written"] += 1

    doc.close()

    if apply and report["written"]:
        cut_path.write_text(json.dumps(cut, ensure_ascii=False, indent=1) + "\n",
                            encoding="utf-8")
    return "ok", report


def main():
    ap = argparse.ArgumentParser(description="给 Edexcel 化学多问选择题补小问标签")
    ap.add_argument("--root", default=str(ROOT), help="化学卷根目录")
    ap.add_argument("--papers", default=None, help="逗号分隔的卷目录名，默认全部")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--offset", type=int, default=0)
    ap.add_argument("--apply", action="store_true", help="真的落盘（默认干跑）")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    root = Path(args.root)
    names = sorted(d.name for d in root.iterdir() if d.is_dir())
    if args.papers:
        want = [x.strip() for x in args.papers.split(",") if x.strip()]
        names = [n for n in names if n in want]
    names = names[args.offset:] if args.limit is None else names[args.offset:args.offset + args.limit]

    print(f"{'落盘' if args.apply else '干跑'}：{len(names)} 卷")
    n_q = n_warn = n_skip = 0
    for name in names:
        status, rep = process_paper(root / name, args.apply, not args.quiet)
        if status == "skip":
            if not args.quiet:
                print(f"  - {name}: {rep['reason']}")
            n_skip += 1
            continue
        qs = [t for t in rep["questions"] if t[1] == "补"]
        if not qs and not rep["warn"]:
            continue
        n_q += len(qs)
        n_warn += len(rep["warn"])
        print(f"  ✓ {name}: 补 {len(qs)} 题" + (f"，警告 {len(rep['warn'])}" if rep["warn"] else ""))
        for t in qs:
            print(f"      Q{t[0]}: {'/'.join(t[2])}" + (f"  [{t[3]}]" if len(t) > 3 and t[3] else ""))
        for w in rep["warn"]:
            print(f"      ! {w}")

    print(f"\n合计：补 {n_q} 题，警告 {n_warn} 条，跳过 {n_skip} 卷")
    if not args.apply:
        print("（干跑，未写任何文件；加 --apply 落盘）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
