# -*- coding: utf-8 -*-
"""vision_subs.py — Step 2：为已切好的题补子问热区（纯视觉 + OCR）

用法：
    python vision_subs.py <卷目录> [--work DIR] [--dry]

前置：先跑过 vision_cut.py（要 out/cut.json + out/assets/qNN.layout.json）。

产出（就地改 <work>/<卷名>/out/ 下的文件）：
    assets/qNN.layout.json  → 填 subquestions（normalized box）
    cut.json                → 填 subquestions（pt bbox，跟 regions 同坐标系）

**核心认知：OCR 的 bbox 可信，OCR 的字符不可信。**
OCR 把 (i) 读成 ()、(iii) 读成 (ili)、(ii) 读成 (m)、(f) 读成 (e) 是常态；
所以标签**不采用 OCR 认出来的字符**，只用「序列位置」推：
遇到一个子问标签行，就按「严格递增」推到下一个字母/罗马数字。
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import vlib as V

X_L1_MAX = 105          # 一级标签 (a)(b)(c) 的右界（左顶格区）
X_L2_MAX = 140          # 二级标签 (i)(ii) 的左界上界；>=140 是表格里以 ( 开头的文字，跳过


def find_subs(seq):
    """在本题的行序列里找子问标签，返回 [{label, page, row}]。

    ⚠️ seq 必须**跨页拼成一整条**再传进来。分成每页调用会让 lv1/lv2 每页归零，
    q01 的 (b)(iii) 会退化成 (a)(iii)（2026-10-18 实测踩过）。
    """
    subs, lv1, lv2 = [], 0, 0
    for page, r in seq:
        t = r["text"].strip()
        if not t.startswith("("):
            continue
        if V.APPENDIX.search(t):
            continue
        if r["x0"] >= X_L2_MAX:
            continue                      # 表格内文字，不是标签
        if r["x0"] < X_L1_MAX:
            # 一级 (a)
            m1 = V.RE_TAG1.match(t)
            n1 = V.letter_num(m1.group(1)) if m1 else None
            lv1 = n1 if (n1 and n1 > lv1) else lv1 + 1
            lv2 = 0
            lab = V.LETTER[lv1 - 1] if lv1 <= len(V.LETTER) else f"?{lv1}"
            subs.append({"label": lab, "page": page, "row": r})
            # 复合行 "(a) (i) ..." → 同一行再出一条二级
            m2 = V.RE_TAG1.match(t)
            rest = t[m2.end():].strip() if m2 else ""
            m3 = V.RE_TAG2.match(rest)
            if m3:
                n2 = V.roman_num(m3.group(1))
                lv2 = n2 if (n2 and n2 > lv2) else lv2 + 1
                subs.append({"label": f"{lab} {V.ROMAN[lv2-1]}", "page": page, "row": r})
        else:
            # 二级 (i)
            if lv1 == 0:
                lv1 = 1
            m2 = V.RE_TAG2.match(t)
            n2 = V.roman_num(m2.group(1)) if m2 else None
            lv2 = n2 if (n2 and n2 > lv2) else lv2 + 1
            subs.append({"label": f"{V.LETTER[lv1-1]} {V.ROMAN[lv2-1]}",
                         "page": page, "row": r})
    return subs


def seg_of(segs, page, y):
    """(page, y) → seg 索引；不在任何 seg 里返回 None。"""
    for i, sg in enumerate(segs):
        if sg["page"] != page:
            continue
        if sg["pt"][0] - 2 <= y <= sg["pt"][1] + 2:
            return i
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("vol")
    ap.add_argument("--work", default=None)
    ap.add_argument("--dry", action="store_true", help="只打印，不写文件")
    args = ap.parse_args()

    vol = Path(args.vol).resolve()
    paper = vol.name
    root = vol.parents[4] if len(vol.parents) > 4 else vol
    work = Path(args.work) if args.work else root / ".vision_work"
    out = work / paper / "out"

    cut = V.read_json(out / "cut.json")
    pages = V.ocr_paper(vol, work)           # 走缓存
    dpi = cut["render"]["dpi"]

    total = 0
    for q in cut["questions"]:
        qn = f"q{int(q['number']):02d}"
        lp = out / "assets" / f"{qn}.layout.json"
        lay = V.read_json(lp)
        segs = lay["src_map"]["segs"]
        CH, CW = lay["size"]["h"], lay["size"]["w"]
        regs = q["regions"]

        # 本题覆盖的页区间
        p0, p1 = regs[0]["page"], regs[-1]["page"]

        seq = []
        for pg in pages:
            if not (p0 <= pg["page"] <= p1):
                continue
            for r in V.rows_of(pg):
                # 结果行要在本题区间内（首页从 region 顶起，末页到 region 底止）
                if pg["page"] == p0 and r["y1"] < regs[0]["bbox"][1] - 2:
                    continue
                if pg["page"] == p1 and r["y0"] > regs[-1]["bbox"][3] + 2:
                    continue
                seq.append((pg["page"], r))
        subs = find_subs(seq)

        boxes, pt_subs, missed = [], [], 0
        for s in subs:
            r = s["row"]
            si = seg_of(segs, s["page"], r["y0"])
            if si is None:
                missed += 1
                continue
            sg = segs[si]
            iy = sg["img"][0] + (r["y0"] - sg["pt"][0]) * dpi / 72.0
            ih = max((r["y1"] - r["y0"]) * dpi / 72.0, 8)
            boxes.append({"label": s["label"], "box": {
                "x": round((r["x0"] - (55.0)) / 493.0, 4),
                "y": round(iy / CH, 4),
                "w": round((r["x1"] - r["x0"]) / 493.0, 4),
                "h": round(ih / CH, 4)}})
            pt_subs.append({"label": s["label"], "page": s["page"],
                            "bbox": [round(r["x0"], 2), round(r["y0"], 2),
                                     round(r["x1"], 2), round(r["y1"], 2)]})

        lay["subquestions"] = boxes
        q["subquestions"] = pt_subs
        total += len(boxes)
        flag = f"  ⚠️ {missed} 条落在段外被丢" if missed else ""
        print(f"  {qn}: {len(boxes):>2} 子问 {[b['label'] for b in boxes]}{flag}")
        if not args.dry:
            V.write_json(lp, lay)

    if not args.dry:
        V.write_json(out / "cut.json", cut)
    print(f"\n[完成] 合计 {total} 个子问 → {'（dry，未写）' if args.dry else out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
