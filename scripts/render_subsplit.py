#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""render_subsplit.py — 按顶层小问（a/b/c…）切分子题图（一级图裁剪模式）

v2（2026-10-14）：不再从 source.pdf 重渲，而是直接在一级题图 assets/qNN.webp
上裁剪。一级图的册内整块（answer_block）本就「题号/小问标签/预留空白原样保留」，
裁剪天然保留 "(b)"、"(i)" 等标签行——修复旧版册内切片丢标签的问题。
QP 侧末组裁到 Total 行上沿（一级图含 Total 行，子图裁掉）——修复末问带
"Total for Question" 的问题。

依赖：qNN.layout.json 含 src_map（render_questions.py 2026-10-14 起输出；
旧 layout 请先重跑 render_questions.py 再切分）。

坐标约定：pt→px 用 src_map 的分段线性映射（notice/分隔带已在 img 区间体现），
与一级图逐像素一致。

产物:
    assets/qNN-<label>.webp          子题图 = 题干 + 本小问 QP 段 [+ 分隔线 + 册内切片]
    assets/qNN-<label>.layout.json   归一化作答区（label 保留 a i 等层级）

两种卷型：
    答题册另附型（A 卷，有 answer_block/answer_regions）→ 册内切片拼接（per_label）；
    留白内嵌型（非 A 卷，作答空白在 QP 流内）→ 只按 QP 切点裁剪（qp_inline），
    不拼册内、不出提示带/分隔线、无「退化」警告——作答区随本问段天然保留。

用法:
    python scripts/render_subsplit.py <session_dir> --question 4 [--labels a,b] [--json]
    也可被 import: render_split(session_dir, qnum, labels=None) -> dict

退出码: 成功 0（含降级），致命错误 2。
"""

import argparse
import hashlib
import json
import sys
from pathlib import Path

from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from render_questions import (  # noqa: E402
    NOTICE_TEXT,
    _notice_font,
    _resolve_notice_font_path,
)

ROMAN_TOKENS = {"i", "ii", "iii", "iv", "v", "vi", "vii", "viii", "ix", "x", "xi", "xii"}
GAP_PX = 2  # 册内切片向上扩的边距（覆盖到上一片底部即可，标签行在间隙里）


def top_labels(q) -> list[str]:
    """顶层小问标签（各标签行首 token，非罗马），按 subquestions 文档序去重。
    QP 可无独立 "(b)" 行（直接 "(b)(i)"），故按首 token 归组而非只认单 token 行。

    单 token 罗马字母（i/v/x）默认当子项排除，但**顶层小问本身就叫 (i)**
    （会计 a–l、CIE 化学 a–j 都排到 i）。补回判据是断档：h 与 j 都在却缺 i，
    且 cut.json 里确实有独立的 "i" 行 —— 那它一定是顶层题被误排，插回 j 之前。

    全罗马（2026-10-21 补）：Edexcel 数学另有一类 parent 丢失的裸 "i"——
    **整题的顶层小问就是 (i)(ii)**，没有外层字母，序列也不断档。这种题过滤后
    out 为空，以前直接返回 []，前端 split_eligible=False、后端 422「顶层小问不足 2 个」，
    等于永远拆不了。现在：过滤后若一个都不剩、且首层全是罗马字母，则按文档序整体返回。"""
    subs = q.get("subquestions") or []
    out: list[str] = []
    for s in subs:
        top = s["label"].split()[0]
        if top not in ROMAN_TOKENS and top not in out:
            out.append(top)
    if not out:
        # 首层全是罗马 → 它们就是本题的顶层小问，不是子项
        roman_only: list[str] = []
        for s in subs:
            top = s["label"].split()[0]
            if top and top not in roman_only:
                roman_only.append(top)
        if roman_only and all(t in ROMAN_TOKENS for t in roman_only):
            return roman_only
    letters = [x for x in out if len(x) == 1]
    for before, mid, after in (("h", "i", "j"), ("u", "v", "w"), ("w", "x", "y")):
        if before in letters and after in letters and mid not in letters:
            if any(s["label"] == mid for s in subs):
                out.insert(out.index(after), mid)
    return out


def leaf_labels(q) -> list[str]:
    """打标叶子标签：把「字母 + 罗马」结构展开成实际答题单元。

    · 字母有罗马子项的（a / a i / a ii）→ 字母本身不单独成单元，只留 "a i"、"a ii"；
    · 字母没有子项的（b 后面直接跟 c）→ 字母自己算一个单元。

    例：['a','a i','a ii','b','c','c i','c ii','d'] → ['a i','a ii','b','c i','c ii','d']

    会计（IAL Accounting）按 (a)(i)(ii) 打分，一个 (a) 下面往往 3–5 分项，
    按顶层字母打标会粗到没法区分知识点，故该学科走这条。
    纯函数，不碰磁盘与依赖，云端/本地两份脚本内容一致。
    """
    subs = q.get("subquestions") or []
    parents = {s["label"].split()[0] for s in subs if len(s["label"].split()) > 1}
    out: list[str] = []
    for s in subs:
        lab = s["label"]
        if len(lab.split()) > 1 or lab not in parents:
            if lab not in out:
                out.append(lab)
    return out


class SrcMap:
    """一级图 layout.json 的 src_map：源 PDF (page, pt_y) → 图内像素 y。"""

    def __init__(self, layout: dict):
        m = layout.get("src_map")
        if not m:
            raise ValueError(
                f"{layout.get('image')}: layout.json 缺 src_map，请先重跑 render_questions.py"
            )
        self.segs = m["segs"]

    def y(self, page: int, pt_y: float) -> float:
        for seg in self.segs:
            if seg["page"] != page:
                continue
            pt0, pt1 = seg["pt"]
            if pt0 - 0.5 <= pt_y <= pt1 + 0.5:
                img0, img1 = seg["img"]
                if pt1 <= pt0:
                    return float(img0)
                return img0 + (pt_y - pt0) / (pt1 - pt0) * (img1 - img0)
        raise ValueError(f"(page {page}, pt_y {pt_y}) 不在一级图 src_map 覆盖范围内")


def _total_trimmed_end(q, regions) -> float:
    """QP 侧末组下边界（pt）：裁掉 Total 行——上沿留 2pt 余量。
    Total 锚点不在末 region 页内或结构异常时不裁。"""
    end_pt = regions[-1]["bbox"][3]
    en = (q.get("anchors") or {}).get("end")
    if en and regions[-1]["page"] == en.get("page"):
        t = en["bbox"][1] - 2.0
        if regions[-1]["bbox"][1] + 10 < t <= end_pt + 0.5:
            end_pt = t
    return end_pt


def render_split(session_dir: str | Path, qnum: str | int,
                 labels: list[str] | None = None,
                 dpi: int | None = None, quality: int | None = None) -> dict:
    """对指定题按顶层小问在一级图上裁剪子图。返回结果 dict（含 results/warnings）。"""
    sdir = Path(session_dir).resolve()
    cut_path = sdir / "cut.json"
    src = sdir / "source.pdf"
    if not cut_path.exists() or not src.exists():
        raise FileNotFoundError(f"缺少 cut.json 或 source.pdf: {sdir}")
    cut = json.loads(cut_path.read_text(encoding="utf-8"))
    if cut.get("source", {}).get("sha256") != hashlib.sha256(src.read_bytes()).hexdigest():
        raise ValueError("source.pdf 与 cut.json 的 sha256 不符，拒绝渲染。")

    q = next((x for x in cut["questions"] if str(x["number"]) == str(qnum)), None)
    if q is None:
        raise KeyError(f"cut.json 中无第 {qnum} 题")
    # MCQ 也可有小问（化学 Section A「(a)(b)(c) 各带一套 A–D」，2026-10-19 起
    # cut.json.subquestions 已补）。MCQ 无答题册 → booklet=False → 自动走 qp_inline，
    # 只按 QP 切点裁剪；真正的闸是下面的「顶层标签 ≥ 2」，单问 MCQ 照样切不了。
    labels = labels or top_labels(q)
    if len(labels) < 2:
        raise ValueError(f"顶层小问不足 2 个（{labels}），无需切分")

    qbase = f"q{int(q['number']):02d}"
    main_img_path = sdir / "assets" / f"{qbase}.webp"
    layout_path = sdir / "assets" / f"{qbase}.layout.json"
    if not main_img_path.exists() or not layout_path.exists():
        raise FileNotFoundError(f"缺少一级图 {main_img_path.name} / layout，请先跑 render_questions.py")
    smap = SrcMap(json.loads(layout_path.read_text(encoding="utf-8")))
    main = Image.open(main_img_path)
    W = main.width
    render = dict(cut.get("render", {}))
    pt2px = render.get("dpi", 200) / 72.0

    # ── QP 侧切点（pt → px） ──
    regions = q.get("regions") or []
    if not regions:
        raise ValueError("该题无 QP regions")
    qp_top_pt = (regions[0]["page"], regions[0]["bbox"][1])
    qp_top_px = smap.y(*qp_top_pt)
    qp_end_pt = _total_trimmed_end(q, regions)
    qp_end_px = smap.y(regions[-1]["page"], qp_end_pt)

    cuts: dict[str, float] = {}
    wanted = list(labels)
    for s in q.get("subquestions") or []:
        top = s["label"].split()[0]
        # 按「调用方请求的顶层标签」取切点：顶层标签行（"a"）或其首个子行
        # （"b i"——QP 可无独立 "(b)" 行）都算。旧版用 `top not in ROMAN_TOKENS`
        # 排除罗马字母，会把**本身就叫 (i) 的顶层小问**一并排掉（生物/化学/会计
        # 小问字母排到 i），导致「顶层标签缺 QP 切点」。
        if top in wanted and top not in cuts:
            cuts[top] = smap.y(s["page"], s["bbox"][1])
    missing = [l for l in labels if l not in cuts]
    if missing:
        raise ValueError(f"顶层标签缺 QP 切点: {missing}")
    ordered = sorted(labels, key=lambda l: cuts[l])
    # 每问子图 QP 段 = 题干段 [QP顶, 首组切点) + 本问段 [本组切点, 下一切点/Total)。
    # 题号与 "(a)" 同行（无题干段）时首组切点会被压到 QP 顶，题干段为空。
    stem_end_px = max(cuts[ordered[0]], qp_top_px)
    ends = [max(cuts[l], qp_top_px) for l in ordered] + [qp_end_px]

    # ── 册内侧：answer_regions 按顶层组聚合（pt），再经 src_map 转 px ──
    # 内嵌型卷（非 A 卷：作答空白就在 QP 流内）无 answer_block/answer_regions，
    # 直接按 QP 切点裁剪即可，子图天然含作答区，不走册内拼接、不报警告。
    ans = q.get("answer_regions") or []
    block = q.get("answer_block") or []
    booklet = bool(ans or block)
    warns: list[str] = []
    bk_groups: dict[str, list[tuple[float, float, dict]]] = {l: [] for l in ordered}
    degraded = False
    if booklet:
        degraded = not ans or any(a.get("label") is None for a in ans)
        if degraded:
            warns.append("answer_regions 缺失或退化为整块，子图不含册内作答区")
        else:
            for a in ans:  # answer_regions 本身按文档序
                top = a["label"].split()[0]
                if top in bk_groups:
                    for rg in a["regions"]:
                        y0 = smap.y(rg["page"], rg["bbox"][1])
                        y1 = smap.y(rg["page"], rg["bbox"][3])
                        bk_groups[top].append((y0, y1, {**rg, "_area_label": a["label"]}))
    has_bk = booklet and any(bk_groups[l] for l in ordered)

    # 各组册内裁剪区间：顶边向上扩到上一非空组底部（间隙里是 "(b)"/"(i)" 标签行，
    # 一级图原样保留）；首组扩到册内整块顶（题号行，含标题）
    bk_crops: dict[str, tuple[float, float, list]] = {}
    prev_bottom = None
    bk_section_top = smap.y(block[0]["page"], block[0]["bbox"][1]) if block else None
    # 册内 Total 行上沿（px）：末组切片若越过它则收回（册内 region 底边
    # 在切割层可能包含 Total 行，子图不要）
    ae = (q.get("anchors") or {}).get("answer_end")
    ae_px = None
    if ae:
        try:
            ae_px = smap.y(ae["page"], ae["bbox"][1])
        except ValueError:
            ae_px = None
    for l in ordered:
        pieces = bk_groups[l]
        if not pieces:
            bk_crops[l] = None
            continue
        top = min(p[0] for p in pieces)
        bottom = max(p[1] for p in pieces)
        if ae_px is not None and top - 1 <= ae_px <= bottom + 1:
            bottom = min(bottom, ae_px - 2 * pt2px)
        if prev_bottom is not None:
            ext = min(prev_bottom + GAP_PX, top)
        elif bk_section_top is not None:
            ext = min(bk_section_top, top)
        else:
            ext = top
        bk_crops[l] = (ext, bottom, pieces)
        prev_bottom = bottom
    if booklet and not degraded:
        empty = [l for l in ordered if bk_crops[l] is None]
        if empty:
            warns.append("以下小问无册内作答区切片（原卷即为空白预留或被剔除）: " + ", ".join(empty))

    # ── 提示带/分隔线尺寸（与一级图同参数） ──
    render = dict(cut.get("render", {}))
    quality = quality or render.get("quality", 85)
    notice_h = sep_h = 0
    font = None
    if has_bk:
        font, fs_px = _notice_font(render.get("dpi", 200), _resolve_notice_font_path())
        pad = max(int(round(5 * render.get("dpi", 200) / 72.0)), 4)
        # 与 render_questions 一致：拿不到中文字体就不留提示带，只保留分隔线
        notice_h = fs_px + 2 * pad if font is not None else 0
        sep_h = max(int(round(18 * render.get("dpi", 200) / 72.0)), 10)

    assets = sdir / "assets"
    assets.mkdir(exist_ok=True)
    results = []
    for i, lbl in enumerate(ordered):
        grp_y0, grp_y1 = ends[i], ends[i + 1]
        bk = bk_crops[lbl]
        if grp_y1 - grp_y0 < 1 and bk is None and stem_end_px - qp_top_px < 1:
            raise ValueError(f"Q{qnum} 小问 {lbl}: QP 段与册内切片均为空")
        imgs = []
        if stem_end_px - qp_top_px >= 1:  # 题干段（每问都带）
            imgs.append(main.crop((0, int(round(qp_top_px)), W, int(round(stem_end_px)))))
        if grp_y1 - grp_y0 >= 1:  # 本问段
            imgs.append(main.crop((0, int(round(grp_y0)), W, int(round(grp_y1)))))
        if bk is not None:
            imgs.append(main.crop((0, int(round(bk[0])), W, int(round(bk[1])))))
        H = notice_h + sum(im.height for im in imgs) + (sep_h if bk is not None else 0)
        canvas = Image.new("RGB", (W, H), "white")
        draw = ImageDraw.Draw(canvas)
        if has_bk and font is not None and notice_h > 0:
            tb = draw.textbbox((0, 0), NOTICE_TEXT, font=font)
            draw.text(((W - (tb[2] - tb[0])) // 2 - tb[0],
                       (notice_h - (tb[3] - tb[1])) // 2 - tb[1]),
                      NOTICE_TEXT, font=font, fill=(110, 110, 110))
        n_qp = len(imgs) - (1 if bk is not None else 0)
        y = notice_h
        for j, im in enumerate(imgs):
            if bk is not None and j == n_qp:  # 交界分隔线只画一处（QP/册内之间）
                sep_y = y + (sep_h - 2) // 2
                lw = max(int(round(1.2 * render.get("dpi", 200) / 72.0)), 2)
                draw.rectangle([0, sep_y, W, sep_y + lw - 1], fill=(205, 205, 205))
                y += sep_h
            canvas.paste(im, (0, y))
            y += im.height

        out_img = assets / f"{qbase}-{lbl}.webp"
        canvas.save(out_img, "WEBP", quality=quality, method=4)

        # layout：册内作答区（answer_regions 条目）在子图内的归一化框
        def nbox(y0, y1):
            return {"x": 0.0, "y": round(y0 / H, 4), "w": 1.0, "h": round((y1 - y0) / H, 4)}

        area_entries = []
        if bk is not None:
            qp_h = sum(im.height for im in imgs[:n_qp])
            bk_y0 = notice_h + qp_h + sep_h - bk[0]  # px→子图坐标的平移量
            for _, _, rg in bk[2]:
                y0 = smap.y(rg["page"], rg["bbox"][1]) + bk_y0
                y1 = smap.y(rg["page"], rg["bbox"][3]) + bk_y0
                lbl2 = rg["_area_label"]
                if area_entries and area_entries[-1]["label"] == lbl2:
                    area_entries[-1]["boxes"].append(nbox(y0, y1))
                else:
                    area_entries.append({"label": lbl2, "boxes": [nbox(y0, y1)]})
        layout = {
            "schema_version": 1,
            "image": out_img.name,
            "coords": "normalized 0-1: x/w, y/h relative to this image",
            "size": {"w": W, "h": H},
        }
        if area_entries:
            layout["areas"] = area_entries
        out_layout = assets / f"{qbase}-{lbl}.layout.json"
        out_layout.write_text(json.dumps(layout, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")

        results.append({
            "label": lbl,
            "image": f"assets/{out_img.name}",
            "layout": f"assets/{out_layout.name}",
            "w": W, "h": H,
            "areas": len(area_entries),
            "has_booklet": bk is not None,
        })

    return {
        "session_dir": sdir.name,
        "question": str(q["number"]),
        "labels": ordered,
        "booklet_mode": "per_label" if has_bk else ("none" if booklet else "qp_inline"),
        "dpi": render.get("dpi", 200), "quality": quality,
        "results": results,
        "warnings": warns,
    }


def main():
    ap = argparse.ArgumentParser(description="按顶层小问切分渲染子题图（一级图裁剪）")
    ap.add_argument("session_dir")
    ap.add_argument("--question", required=True)
    ap.add_argument("--labels", default=None, help="逗号分隔，默认全部顶层标签")
    ap.add_argument("--json", action="store_true", help="stdout 输出 JSON")
    args = ap.parse_args()
    labels = args.labels.split(",") if args.labels else None
    try:
        res = render_split(args.session_dir, args.question, labels)
    except Exception as e:  # noqa: BLE001
        if args.json:
            print(json.dumps({"error": str(e)}))
        else:
            print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(2)
    if args.json:
        print(json.dumps(res, ensure_ascii=False))
    else:
        for w in res["warnings"]:
            print(f"WARN: {w}", file=sys.stderr)
        for r in res["results"]:
            print(f"  Q{res['question']} {r['label']} -> {r['image']}  "
                  f"{r['w']}x{r['h']}px  areas={r['areas']}", file=sys.stderr)


if __name__ == "__main__":
    main()
