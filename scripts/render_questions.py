#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""render_questions.py — 按 cut.json 把每题渲染成 WebP 题图

ℹ️ 适用范围：渲染本身与学科/考试局解耦——只读 cut.json 的 regions/坐标，
    理论上任何按仓库 schema 产出的数据都能渲染。但其中的「答题册分区
    拼接」与「MCQ 选项热区」逻辑是按 Edexcel 理化卷式（answer_regions
    带 label、选项 A–D）设计的；其他考试局/学科若数据结构不同，
    需相应扩展。切割层（cut_paper.py）为 Edexcel IAL 物理/化学共用一套。

用法:
    python scripts/render_questions.py <session_dir> [--dpi 200] [--quality 85]

规则:
    · 只读 cut.json 的坐标/regions，绝不回写坐标（单向数据流）；
      仅当命令行覆盖 DPI/质量时同步 cut.json 的 render 记录块。
    · 多 region（跨页题）按顺序纵向拼接为一图。
    · answer_regions（答题册另附卷）存在时：成图分区式——题干全部在上
      （含 QP 末尾的 Total 行），交界处加细分隔线，下方为答题册整块
      （answer_block：册内题号行 → 册内 Total，小问标签/预留空白/横线
      原样保留），图顶加「请在题目后面的专属作答区回答」提示；
      无 answer_block 的旧数据退回按条目拼接（题干切 Total 行）。
    · 同一 source.pdf + 同参数 → 输出确定性（可作重渲一致性验收）。
    · 每题顺手产出 assets/qNN.layout.json：最终合成图内的小问作答区
      （大题）或 MCQ 选项框（选择题）位置，归一化坐标 0–1（x/宽、y/高），
      图片任意缩放时设备端一次乘法即可换算。

像素换算: 像素 = pt × DPI ÷ 72。
"""

import argparse
import hashlib
import json
import sys
from pathlib import Path

import pymupdf
from PIL import Image, ImageDraw, ImageFont

# WebP 单边像素上限（libwebp 硬限制 16383）。题图是纵向拼接，只可能超高度。
# 超限时对该题单独降 DPI 渲染（layout.json 是归一化坐标，不受缩放影响）。
WEBP_MAX_SIDE = 16320

# ── 作答提示与交界分隔（仅 answer_regions 分区式成图使用） ──────────────
NOTICE_TEXT = "请在题目后面的专属作答区回答"
NOTICE_FONTS = ["C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/msyh.ttf",
                "C:/Windows/Fonts/simhei.ttf", "C:/Windows/Fonts/simsun.ttc"]


def _notice_font(dpi):
    """提示文字字体（中文，随 DPI 缩放）；返回 (font, 字高px)。"""
    fs = max(int(round(9 * dpi / 72.0)), 13)
    for p in NOTICE_FONTS:
        if Path(p).exists():
            return ImageFont.truetype(p, fs), fs
    sys.exit(f"找不到中文字体（ tried: {', '.join(NOTICE_FONTS)}），无法绘制作答提示。")


def main():
    ap = argparse.ArgumentParser(description="按 cut.json 渲染每题 WebP 题图")
    ap.add_argument("session_dir")
    ap.add_argument("--dpi", type=int, default=None)
    ap.add_argument("--quality", type=int, default=None)
    args = ap.parse_args()

    sdir = Path(args.session_dir).resolve()
    cut_path = sdir / "cut.json"
    src = sdir / "source.pdf"
    if not cut_path.exists() or not src.exists():
        sys.exit(f"缺少 cut.json 或 source.pdf: {sdir}")

    cut = json.loads(cut_path.read_text(encoding="utf-8"))
    sha = hashlib.sha256(src.read_bytes()).hexdigest()
    if cut.get("source", {}).get("sha256") != sha:
        sys.exit("source.pdf 与 cut.json 的 sha256 不符，拒绝渲染。")

    render = dict(cut.get("render", {}))
    dpi = args.dpi or render.get("dpi", 200)
    quality = args.quality or render.get("quality", 85)

    assets = sdir / "assets"
    assets.mkdir(exist_ok=True)
    doc = pymupdf.open(src)
    zoom = dpi / 72.0
    mat = pymupdf.Matrix(zoom, zoom)

    total_bytes = 0
    n_layout = 0
    for q in cut.get("questions", []):
        regions = [dict(r) for r in q.get("regions", [])]
        ans = q.get("answer_regions") or []
        n_qp = len(regions)
        # pieces: (kind, label, region)；kind="qp" 题干段，"answer" 作答区
        # （记录进 layout），"answer_block" 册内整块（记录进 layout 换算用）
        pieces = []
        block = q.get("answer_block") or []
        if ans or block:
            if block:
                # 分区式：题干全部在上（含 QP 末尾的 Total 行，不再切掉），
                # 答题册整块连续在下（册内题号行 → 册内 Total，小问标签/
                # 预留空白/横线/分值原样保留）
                for r in regions:
                    pieces.append(("qp", None, r))
                for r in block:
                    pieces.append(("answer_block", None, r))
                tag = f"QP {n_qp} + 册整块 {len(block)} region（题号→Total）"
            else:
                # 兜底（人工题等无 answer_block 的旧结构）：题干切 Total 行 +
                # 册内作答区按条目拼接
                en = (q.get("anchors") or {}).get("end")
                if en and regions and regions[-1]["page"] == en.get("page"):
                    y_top = en["bbox"][1] - 6
                    if y_top > regions[-1]["bbox"][1] + 10:
                        regions[-1] = {**regions[-1], "bbox": regions[-1]["bbox"][:3] + [y_top]}
                for r in regions:
                    pieces.append(("qp", None, r))
                n_ans = 0
                for a in ans:
                    for r in a.get("regions", []):
                        pieces.append(("answer", a.get("label"), r))
                        n_ans += 1
                tag = f"QP {n_qp} + 册 {n_ans} region 分区"
        else:
            for r in regions:
                pieces.append(("qp", None, r))
            tag = f"{n_qp} region"
        if not pieces:
            continue
        has_ans = any(k in ("answer", "answer_block") for k, _, _ in pieces)

        # ── 尺寸预检：整卷题图纵向拼接，高度可能超过 WebP 单边上限 ──
        # 超限时按比例下调本题 DPI（layout.json 用归一化坐标，缩放无关），
        # 避免整卷因一题过长而渲染中断。
        est_pt = sum(pymupdf.Rect(r["bbox"]).height for _k, _l, r in pieces)
        q_dpi = dpi
        for _ in range(5):
            _z = q_dpi / 72.0
            if has_ans:
                _fs = max(int(round(9 * q_dpi / 72.0)), 13)
                _extra = _fs + 2 * max(int(round(5 * q_dpi / 72.0)), 4) \
                    + max(int(round(18 * q_dpi / 72.0)), 10)
            else:
                _extra = 0
            if est_pt * _z + _extra <= WEBP_MAX_SIDE - 16:
                break
            q_dpi = max(int(q_dpi * (WEBP_MAX_SIDE - 16 - _extra) / (est_pt * _z)), 24)
        if q_dpi != dpi:
            print(f"  ! Q{q['number']}: 题图过高（约 {est_pt * zoom:.0f}px > WebP 上限 "
                  f"{WEBP_MAX_SIDE}），本题降 DPI 渲染 {dpi} → {q_dpi}")
        q_zoom = q_dpi / 72.0
        q_mat = pymupdf.Matrix(q_zoom, q_zoom)

        notice_h = sep_h = 0
        font = None
        if has_ans:
            font, fs_px = _notice_font(q_dpi)
            pad = max(int(round(5 * q_dpi / 72.0)), 4)
            notice_h = fs_px + 2 * pad
            sep_h = max(int(round(18 * q_dpi / 72.0)), 10)
        imgs, areas, block_pieces, offsets = [], [], [], []
        for kind, label, rg in pieces:
            if rg["page"] >= doc.page_count:
                sys.exit(f"Q{q['number']}: 页码 {rg['page']} 越界")
            clip = pymupdf.Rect(rg["bbox"])
            if clip.is_empty or clip.is_infinite:
                sys.exit(f"Q{q['number']}: 非法 bbox {rg['bbox']}")
            pix = doc[rg["page"]].get_pixmap(matrix=q_mat, clip=clip, alpha=False)
            im = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            offsets.append(sum(i.height for i in imgs) + notice_h
                           + (sep_h if kind in ("answer", "answer_block") else 0))
            if kind == "answer":
                areas.append({"label": label, "region": rg, "y_off": offsets[-1], "im": im})
            elif kind == "answer_block":
                block_pieces.append((rg, offsets[-1], im))
            imgs.append(im)
        if has_ans:
            # 分区式合成：顶部提示带 → 题干 → 交界分隔线 → 册内作答区
            W = max(i.width for i in imgs)
            H = sum(i.height for i in imgs) + notice_h + sep_h
            canvas = Image.new("RGB", (W, H), "white")
            draw = ImageDraw.Draw(canvas)
            tb = draw.textbbox((0, 0), NOTICE_TEXT, font=font)
            draw.text(((W - (tb[2] - tb[0])) // 2 - tb[0],
                       (notice_h - (tb[3] - tb[1])) // 2 - tb[1]),
                      NOTICE_TEXT, font=font, fill=(110, 110, 110))
            y = notice_h
            lw = max(int(round(1.2 * q_dpi / 72.0)), 2)
            sep_drawn = False
            for (kind, _lbl, _rg), im in zip(pieces, imgs):
                if kind in ("answer", "answer_block") and not sep_drawn:
                    # 分隔线只画在题干/答题册交界一处
                    ly = y + (sep_h - lw) // 2
                    draw.rectangle([0, ly, W, ly + lw - 1], fill=(205, 205, 205))
                    y += sep_h
                    sep_drawn = True
                canvas.paste(im, (0, y))
                y += im.height
            out = canvas
        else:
            out = imgs[0]
            for im in imgs[1:]:
                canvas = Image.new("RGB", (max(out.width, im.width), out.height + im.height), "white")
                canvas.paste(out, (0, 0))
                canvas.paste(im, (0, out.height))
                out = canvas
        path = assets / f"q{int(q['number']):02d}.webp"
        out.save(path, "WEBP", quality=quality, method=4)
        total_bytes += path.stat().st_size

        # ── layout.json：题图内作答区 / MCQ 选项框，归一化坐标（0–1） ──
        W, H = out.width, out.height

        def nbox(x0, y0, x1, y1):
            return {"x": round(x0 / W, 4), "y": round(y0 / H, 4),
                    "w": round((x1 - x0) / W, 4), "h": round((y1 - y0) / H, 4)}

        area_entries = []
        if block_pieces:
            # 整块模式：answer_regions 各条目（已剔除册内标签行）在册内
            # 整块图内的位置 → 归一化作答区框
            for a in ans:
                lbl = a.get("label")
                for rg in a.get("regions", []):
                    bb = rg["bbox"]
                    box = None
                    for brg, yoff, bim in block_pieces:
                        rb = brg["bbox"]
                        if brg["page"] == rg["page"] and rb[1] - 1 <= bb[1] and bb[3] <= rb[3] + 1:
                            y0 = yoff + (bb[1] - rb[1]) * q_zoom
                            box = nbox(0, y0, bim.width, y0 + (bb[3] - bb[1]) * q_zoom)
                            break
                    if box is None:
                        sys.exit(f"Q{q['number']}: 作答区 {lbl} 不在 answer_block 内")
                    if area_entries and area_entries[-1]["label"] == lbl:
                        area_entries[-1]["boxes"].append(box)
                    else:
                        area_entries.append({"label": lbl, "boxes": [box]})
        else:
            for a in areas:
                bb = a["region"]["bbox"]
                box = nbox(0, a["y_off"], a["im"].width, a["y_off"] + a["im"].height)
                lbl = a["label"]
                if area_entries and area_entries[-1]["label"] == lbl:
                    area_entries[-1]["boxes"].append(box)
                else:
                    area_entries.append({"label": lbl, "boxes": [box]})
        opt_entries = []
        if q.get("question_type") == "mcq" and q.get("option_hotzones"):
            # 选项热区可落在任一题干段（共用题干块拼接后 MCQ 有多段），
            # 找到包含它的段，按段内相对坐标 + 段在图内的像素偏移换算
            for hz in q["option_hotzones"]:
                b = hz["bbox"]
                box = None
                for (kind, _lbl, rg), yoff in zip(pieces, offsets):
                    rb = rg["bbox"]
                    if kind == "qp" and rb[1] - 1 <= b[1] and b[3] <= rb[3] + 1:
                        box = nbox((b[0] - rb[0]) * q_zoom, yoff + (b[1] - rb[1]) * q_zoom,
                                   (b[2] - rb[0]) * q_zoom, yoff + (b[3] - rb[1]) * q_zoom)
                        break
                if box is None:  # 兜底：按首段换算
                    bb0 = pieces[0][2]["bbox"]
                    box = nbox((b[0] - bb0[0]) * q_zoom, (b[1] - bb0[1]) * q_zoom,
                               (b[2] - bb0[0]) * q_zoom, (b[3] - bb0[1]) * q_zoom)
                opt_entries.append({"label": hz["label"], "box": box})
        layout = {
            "schema_version": 1,
            "image": path.name,
            "coords": "normalized 0-1: x/w, y/h relative to this image",
            "size": {"w": W, "h": H},
        }
        # src_map：图内像素 y ↔ 源 PDF (page, pt_y) 的纵向映射，供
        # render_subsplit 在一级图上直接裁剪子图（册内标签行天然保留）。
        # segs 按拼接顺序记录每条原始条带；notice/separate 带本身不含内容，
        # 只记偏移量，由 offsets 已包含在 img 区间中体现。
        src_segs = []
        for (kind, _lbl, rg), yoff, im in zip(pieces, offsets, imgs):
            src_segs.append({
                "kind": kind,
                "page": rg["page"],
                "pt": [rg["bbox"][1], rg["bbox"][3]],
                "img": [yoff, yoff + im.height],
            })
        layout["src_map"] = {
            "dpi": q_dpi,
            "notice_h": notice_h,
            "sep_h": sep_h,
            "segs": src_segs,
        }
        if area_entries:
            layout["areas"] = area_entries
        if opt_entries:
            layout["options"] = opt_entries
        (assets / f"q{int(q['number']):02d}.layout.json").write_text(
            json.dumps(layout, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        n_layout += 1
        n_desc = f", areas={len(area_entries)}" if area_entries else ""
        if opt_entries:
            n_desc += f", options={len(opt_entries)}"
        print(f"  Q{q['number']:>2s} -> {path.name}  {W}x{H}px  "
              f"{path.stat().st_size / 1024:.0f} KB  ({tag}{n_desc})")

    if render.get("dpi") != dpi or render.get("quality") != quality:
        cut["render"] = {"dpi": dpi, "format": "webp", "quality": quality}
        cut_path.write_text(json.dumps(cut, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        print(f"  （render 记录块已同步为 dpi={dpi} quality={quality}；坐标未动）")

    print(f"OK  渲染 {len(cut.get('questions', []))} 题（layout.json × {n_layout}），"
          f"共 {total_bytes / 1024 / 1024:.1f} MB（dpi={dpi}, q={quality}）")


if __name__ == "__main__":
    main()
