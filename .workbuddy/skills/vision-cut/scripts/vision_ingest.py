# -*- coding: utf-8 -*-
"""vision_ingest.py — Step 4：把视觉切割产物落进 data/（schema 合规 + 自动校验）

用法：
    python vision_ingest.py <卷目录> [--work DIR] [--dry]

做的事：
  1. cut.json：删 marks_sum、number 转 string、status 归一、补 confidence/review_notes
  2. layout.json：segs 补 kind、pt/img 收敛成 2 元 [start, end]
  3. 写 qNN.md（front-matter 与 cut.json 同步）
  4. 拷贝 webp / layout 到 data/…/<卷>/assets/
  5. 调 scripts/validate_cut.py 复核 —— 不过就是没过

⚠️ **视觉切割的卷文本层本来就是坏的**，所以 qNN.md 不带正文，只带
`text_layer: corrupted` 的说明和 `[Total: N]`。这是设计，不是缺陷。
"""
import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import vlib as V

STATUS_OK = {"auto", "review", "verified", "manual"}


def fix_cut(cut):
    cut.pop("marks_sum", None)
    for q in cut["questions"]:
        q["number"] = str(q["number"])
        if q.get("status") not in STATUS_OK:
            q["status"] = "auto"
        q.setdefault("confidence", 1.0)
        q.setdefault("review_notes", [])
        q.setdefault("question_type", "structured")
        q.setdefault("subquestions", [])
        # 字段顺序：number, status, confidence, regions, review_notes, subquestions…
        order = ["number", "status", "confidence", "regions", "review_notes",
                 "subquestions", "question_type", "total_marks"]
        fixed = {k: q[k] for k in order if k in q}
        for k in q:
            if k not in fixed:
                fixed[k] = q[k]
        q.clear()
        q.update(fixed)
    return cut


def fix_layout(lay):
    for sg in lay["src_map"]["segs"]:
        sg.setdefault("kind", "qp")
        if len(sg["pt"]) == 4:
            sg["pt"] = [sg["pt"][1], sg["pt"][3]]
        if len(sg["img"]) == 4:
            sg["img"] = [sg["img"][1], sg["img"][3]]
    # 键顺序固定成 kind, page, pt, img
    lay["src_map"]["segs"] = [
        {"kind": sg.get("kind", "qp"), "page": sg["page"],
         "pt": sg["pt"], "img": sg["img"]}
        for sg in lay["src_map"]["segs"]]
    return lay


def make_md(cut, q, vol):
    board = vol.parents[2].name
    qual = vol.parents[1].name
    subject = vol.parents[0].name
    paper = vol.name
    yr, ses, code = paper.split("-")
    qn = f"q{int(q['number']):02d}"
    qid = f"{board}-{qual}-{subject}-{paper}-{qn}"
    return f"""---
id: {qid}
board: {board}
qualification: {qual}
subject: {subject}
paper_code: {code.upper()}
year: {yr}
session: {ses}
question_number: {int(q['number'])}
total_marks: {q['total_marks']}
question_type: {q.get('question_type','structured')}
image: assets/{qn}.webp
layout: assets/{qn}.layout.json
source: "{board.upper()}, {yr} {ses}"
cut_status: {q['status']}
language: en
text_layer: corrupted
---

{q['number']}
题面见 `assets/{qn}.webp`（源 PDF 字体缺 ToUnicode 映射，文本层不可用；视觉切割所得，正文需 OCR 或人工转录）。

[Total: {q['total_marks']}]
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("vol")
    ap.add_argument("--work", default=None)
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--no-validate", action="store_true")
    args = ap.parse_args()

    vol = Path(args.vol).resolve()
    paper = vol.name
    root = vol.parents[4] if len(vol.parents) > 4 else vol
    work = Path(args.work) if args.work else root / ".vision_work"
    out = work / paper / "out"

    cut = fix_cut(V.read_json(out / "cut.json"))
    cut["source"]["sha256"] = __import__("hashlib").sha256(
        (vol / "source.pdf").read_bytes()).hexdigest()
    pm = vol / "paper.md"
    if pm.exists():
        for line in pm.read_text(encoding="utf-8").splitlines():
            if line.startswith("paper_ref:"):
                cut["source"]["paper_ref"] = line.split(":", 1)[1].strip()

    if args.dry:
        print(f"[dry] {paper}: {len(cut['questions'])} 题")
        for q in cut["questions"]:
            print(f"  q{int(q['number']):02d} {q['total_marks']:>3}分 "
                  f"{len(q['regions'])}段 {len(q['subquestions'])}子问 status={q['status']}")
        print(f"[dry] 目标 {vol}")
        return 0

    # assets
    (vol / "assets").mkdir(parents=True, exist_ok=True)
    n_img = 0
    for f in (out / "assets").glob("*.webp"):
        shutil.copy2(f, vol / "assets" / f.name)
        n_img += 1
    for f in (out / "assets").glob("*.layout.json"):
        lay = fix_layout(V.read_json(f))
        V.write_json(vol / "assets" / f.name, lay)

    V.write_json(vol / "cut.json", cut)
    n_md = 0
    for q in cut["questions"]:
        qn = f"q{int(q['number']):02d}"
        (vol / f"{qn}.md").write_bytes(make_md(cut, q, vol).encode("utf-8"))
        n_md += 1
    print(f"{paper}: cut.json + {n_img} webp + {n_md} md + layouts → {vol}")

    if not args.no_validate:
        vp = root / "scripts" / "validate_cut.py"
        if vp.exists():
            r = subprocess.run([sys.executable, str(vp), str(vol)],
                               capture_output=True, text=True)
            print(r.stdout.strip() or r.stderr.strip())
            if r.returncode != 0:
                print("❌ validate_cut 未通过，收工前必须修")
                return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
