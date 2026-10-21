# -*- coding: utf-8 -*-
"""vision_check.py — Step 3：子问热区的机器自查 + 可疑项裁图

用法：
    python vision_check.py <卷目录> [--work DIR] [--sheet]
    --sheet  额外把可疑项上下各 26pt 裁出来拼成对照图，供人眼判定

三层思路（2026-10-18 定）：
    L1 机器自洽 → L2 只把可疑项裁图 → L3 人判定后脚本执行
**不要让规则去猜，也不要让人去看整卷。** 规则粗筛，人只看 2–3 张局部图。

L1 判据（顺序有讲究，先算叶子再比数）：
    ① **一级标签 (a)(b)(c) 不是小问**，只是分组，本身没有 [N]
       → 必须先算「叶子节点」，再拿叶子数跟 [N] 标记数比。
    ② 复合行 (a) (i) 同 y，不算乱序 → 判据是 <=，不是 <。
任何一条判据写反，就会淹没在几十条误报里。
"""
import argparse
import sys
from pathlib import Path

from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).parent))
import vlib as V

PAD_PT = 26


def leaves_of(subs):
    """子问列表里哪些是叶子（后面没有更深一级挂在它下面）。"""
    idx = []
    for i, s in enumerate(subs):
        nxt = subs[i + 1] if i + 1 < len(subs) else None
        if nxt is not None and nxt["label"].startswith(s["label"] + " "):
            continue
        idx.append(i)
    return idx


def check(vol, out):
    cut = V.read_json(out / "cut.json")
    pages = V.ocr_paper(vol, out.parent)
    by_page = {pg["page"]: pg for pg in pages}
    dpi = cut["render"]["dpi"]
    issues = []

    for q in cut["questions"]:
        n = int(q["number"])
        subs = q.get("subquestions", [])
        regs = q["regions"]
        p0, p1 = regs[0]["page"], regs[-1]["page"]
        ytop, ybot = regs[0]["bbox"][1], regs[-1]["bbox"][3]

        # 本题区间内的 [N] 标记
        marks = []
        for pg in pages:
            if not (p0 <= pg["page"] <= p1):
                continue
            for r in V.rows_of(pg):
                if not V.RE_MARK.search(r["text"].strip()):
                    continue
                if pg["page"] == p0 and r["y1"] < ytop - 2:
                    continue
                if pg["page"] == p1 and r["y0"] > ybot + 2:
                    continue
                marks.append((pg["page"], r["y0"], r["y1"]))

        # 硬判据优先：本题所有 [N] 的**数值和**必须等于 total_marks。
        # 比「数个数」硬得多 —— 一个子问挂两个 [N] 是常态（如 [2] [1]），个数天然对不齐。
        import re as _re
        vals = []
        for pg in pages:
            if not (p0 <= pg["page"] <= p1):
                continue
            for r in V.rows_of(pg):
                mm = V.RE_MARK.search(r["text"].strip())
                if mm:
                    if pg["page"] == p0 and r["y1"] < ytop - 2:
                        continue
                    if pg["page"] == p1 and r["y0"] > ybot + 2:
                        continue
                    vals.append(int(mm.group(1)))
        if vals and sum(vals) != q["total_marks"]:
            issues.append(dict(q=n, kind="marksum", label="",
                               msg=f"[N] 合计 {sum(vals)} != total_marks {q['total_marks']} "
                                   f"（{vals}）", page=p0, y=ytop))

        lv = leaves_of(subs)
        if len(lv) > len(marks):
            issues.append(dict(q=n, kind="extra", label="",
                               msg=f"叶子 {len(lv)} 个 > [N] {len(marks)} 个 → 疑有假标签",
                               page=p0, y=ytop))
        elif len(lv) < len(marks):
            issues.append(dict(q=n, kind="few", label="",
                               msg=f"叶子 {len(lv)} 个 < [N] {len(marks)} 个 → 可能漏检或一子问多 [N]",
                               page=p0, y=ytop))

        # 顺序 & 墨密度
        prev = (-1, -1.0)
        for i, s in enumerate(subs):
            key = (s["page"], s["bbox"][1])
            if key < prev:
                issues.append(dict(q=n, kind="order", label=s["label"],
                                   msg=f"{s['label']} 位置倒退", page=s["page"], y=s["bbox"][1]))
            prev = key
            # 框内墨：用 OCR 词框在该区间里的覆盖率近似
            pg = by_page.get(s["page"])
            if pg:
                ink = ink_ratio(pg, s["bbox"])
                if ink < 0.02:
                    issues.append(dict(q=n, kind="blank", label=s["label"],
                                       msg=f"{s['label']} 框内几乎无墨 ({ink:.3f})",
                                       page=s["page"], y=s["bbox"][1]))
            # 叶子之后该有一段带 [N]
            if i in lv:
                nxt = subs[i + 1] if i + 1 < len(subs) else None
                hi = (nxt["bbox"][1] if (nxt and nxt["page"] == s["page"])
                      else (1e9 if s["page"] < p1 else ybot))
                got = any(mp == s["page"] and s["bbox"][1] - 2 <= my <= hi
                          for mp, my, _ in marks)
                if not got:
                    issues.append(dict(q=n, kind="nomark", label=s["label"],
                                       msg=f"{s['label']} 叶子后面没有 [N]",
                                       page=s["page"], y=s["bbox"][1]))
    return issues, cut


def ink_ratio(page, bbox):
    """OCR 词框落在 bbox 内的面积占比（近似墨密度）。"""
    x0, y0, x1, y1 = bbox
    area = max((y1 - y0) * (x1 - x0), 1e-6)
    ink = 0.0
    for w in page["words"]:
        ox = max(0.0, min(w["pt"][2], x1) - max(w["pt"][0], x0))
        oy = max(0.0, min(w["pt"][3], y1) - max(w["pt"][1], y0))
        ink += ox * oy
    return ink / area


def crop_sheet(vol, out, cut, issues, path):
    """把可疑项裁成一张对照表（每项上下 PAD_PT）。"""
    from PIL import ImageFont
    tiles = []
    for it in issues:
        if it["kind"] in ("count", "extra", "few", "marksum"):
            continue
        qn = f"q{it['q']:02d}"
        lp = out / "assets" / f"{qn}.layout.json"
        if not lp.exists():
            continue
        lay = V.read_json(lp)
        img = Image.open(out / "assets" / f"{qn}.webp").convert("RGB")
        dpi = lay["src_map"]["dpi"]
        CH = lay["size"]["h"]
        ypx = None
        for sg in lay["src_map"]["segs"]:
            if sg["page"] == it["page"] and sg["pt"][0] - 2 <= it["y"] <= sg["pt"][1] + 2:
                ypx = sg["img"][0] + (it["y"] - sg["pt"][0]) * dpi / 72.0
                break
        if ypx is None:
            continue
        pad = PAD_PT * dpi / 72.0
        top = max(0, int(ypx - pad))
        bot = min(img.height, int(ypx + pad * 2.2))
        tile = img.crop((0, top, img.width, bot))
        W = 760
        tile = tile.resize((W, max(1, int(tile.height * W / tile.width))), Image.LANCZOS)
        canvas = Image.new("RGB", (W + 190, tile.height), "white")
        canvas.paste(tile, (190, 0))
        d = ImageDraw.Draw(canvas)
        d.text((8, 6), f"Q{it['q']}  [{it['kind']}]", fill=(30, 30, 30))
        d.text((8, 26), f"label={it['label'] or '-'}", fill=(180, 40, 40))
        d.text((8, 44), f"p{it['page']+1} y={it['y']:.0f}", fill=(90, 90, 90))
        d.line([(188, 0), (188, canvas.height)], fill=(200, 200, 200))
        tiles.append(canvas)
    if not tiles:
        return None
    W = max(t.width for t in tiles)
    H = sum(t.height for t in tiles)
    sheet = Image.new("RGB", (W, H), "white")
    off = 0
    for t in tiles:
        sheet.paste(t, (0, off))
        off += t.height
    sheet.save(path)
    return sheet.size


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("vol")
    ap.add_argument("--work", default=None)
    ap.add_argument("--sheet", action="store_true")
    args = ap.parse_args()

    vol = Path(args.vol).resolve()
    paper = vol.name
    root = vol.parents[4] if len(vol.parents) > 4 else vol
    work = Path(args.work) if args.work else root / ".vision_work"
    out = work / paper / "out"

    issues, cut = check(vol, out)
    nsub = sum(len(q["subquestions"]) for q in cut["questions"])
    print(f"{paper}: {len(cut['questions'])} 题 / {nsub} 子问 / {len(issues)} 条告警")
    for it in issues:
        print(f"  Q{it['q']} [{it['kind']}] {it['msg']}  (p{it['page']+1} y={it['y']:.0f})")

    if args.sheet:
        p = work / paper / f"suspect_{paper}.png"
        sz = crop_sheet(vol, out, cut, issues, p)
        if sz:
            print(f"\n[对照图] {p}  {sz[0]}x{sz[1]}  —— 逐块看，判定后按索引删/插")
        else:
            print("\n[对照图] 没有可裁的可疑项")
    return 0 if not issues else 1


if __name__ == "__main__":
    sys.exit(main())
