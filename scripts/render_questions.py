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
                                       [--notice-font <中文字体路径>]

规则:
    · 只读 cut.json 的坐标/regions，绝不回写坐标（单向数据流）；
      仅当命令行覆盖 DPI/质量时同步 cut.json 的 render 记录块。
    · 多 region（跨页题）按顺序纵向拼接为一图。
    · answer_regions（答题册另附卷）存在时：成图分区式——题干全部在上
      （含 QP 末尾的 Total 行），交界处加细分隔线，下方为答题册整块
      （answer_block：册内题号行 → 册内 Total，小问标签/预留空白/横线
      原样保留），图顶加「请在题目后面的专属作答区回答」提示；
      无 answer_block 的旧数据退回按条目拼接（题干切 Total 行）。
    · 提示文字字体跨平台：按 Windows / macOS / Linux 候选表探测中文字体，
      再扫常见字体目录兜底；都找不到时不画提示文字（分隔线保留）并打
      [warn]，不再中断渲染；可用 --notice-font 显式指定。
    · 同一 source.pdf + 同参数 → 输出确定性（可作重渲一致性验收）。
    · 每题顺手产出 assets/qNN.layout.json：最终合成图内的
      ① 小问作答区（答题册另附卷的大题，areas）、② MCQ 选项框（options）、
      ③ 小问标签行位置（subquestions），均归一化坐标 0–1（x/宽、y/高），
      图片任意缩放时设备端一次乘法即可换算。
      subquestions 供下游按纵向区间判断内容/笔迹落在哪个小问下（小问纵向
      排列，y 区间即可界定）；只认 kind=qp 段且需匹配 page（小问可跨页）。

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

# ── 作答提示与交界分隔（仅分区式成图使用） ──────────────
NOTICE_TEXT = "请在题目后面的专属作答区回答"
# 会计卷：资料块拼在题面之前，提示要说清楚三段顺序
NOTICE_TEXT_MATERIAL = "本题依次为：资料（Source Booklet）→ 题面 → 作答区"
NOTICE_TEXT_MATERIAL_ONLY = "上方为资料（Source Booklet），下方为题面与作答区"
# CIE 经济（2026-10-21）：资料在题目前、且全卷无作答区 → 不能说「作答区」
NOTICE_TEXT_MATERIAL_NOANS = "上方为资料，下方为题目"
# 经济卷（material_order = after）：题面在前、资料在后，且不切作答区
NOTICE_TEXT_MATERIAL_AFTER = "本题依次为：题面 → 资料（Source Booklet）"
NOTICE_TEXT_MATERIAL_AFTER_ANS = "本题依次为：题面 → 资料（Source Booklet）→ 作答区"

# 提示文字中文字体：按平台列出常见候选，命中第一个存在的即用。
# 跨平台（Windows / macOS / Linux）——原先只列 Windows 路径，非 Windows 机器
# 会直接 sys.exit 中断渲染；现改为「探测 → 目录扫描 → 找不到就不画提示文字」，
# 渲染始终跑完（分隔线保留）。
NOTICE_FONT_CANDIDATES = [
    # Windows
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/msyh.ttf",
    "C:/Windows/Fonts/simhei.ttf",
    "C:/Windows/Fonts/simsun.ttc",
    # macOS
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/STHeiti Light.ttc",
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/Library/Fonts/Arial Unicode.ttf",
    "~/Library/Fonts/NotoSansCJKsc-Regular.otf",
    "~/Library/Fonts/SourceHanSansSC-Regular.otf",
    # Linux（Debian/Ubuntu / Fedora / Arch 常见路径）
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/truetype/arphic/uming.ttc",
    "~/.fonts/NotoSansCJK-Regular.ttc",
    "~/.local/share/fonts/NotoSansCJK-Regular.ttc",
]

# 候选表落空时的目录兜底：扫这些目录，按文件名关键字认中文字体
# （应对发行版把同一字体装在不同路径）。
NOTICE_FONT_DIRS = ["/System/Library/Fonts", "/Library/Fonts", "~/Library/Fonts",
                    "/usr/share/fonts", "~/.fonts", "~/.local/share/fonts"]
NOTICE_FONT_HINTS = ("pingfang", "heiti", "hiragino", "notosanscjk", "notoserifcjk",
                     "sourcehansans", "sourcehanserif", "wqy", "microhei", "zenhei",
                     "droidsansfallback", "uming", "ukai", "yuanti",
                     "msyh", "simhei", "simsun")
NOTICE_FONT_EXTS = (".ttf", ".ttc", ".otf", ".otc")

_FONT_UNSET = object()
_notice_font_path = _FONT_UNSET


def _scan_font_candidates():
    for p in NOTICE_FONT_CANDIDATES:
        f = Path(p).expanduser()
        if f.is_file():
            return f
    return None


def _scan_font_dirs():
    for d in NOTICE_FONT_DIRS:
        root = Path(d).expanduser()
        if not root.is_dir():
            continue
        try:
            entries = root.rglob("*")
            for f in entries:
                if not f.is_file() or f.suffix.lower() not in NOTICE_FONT_EXTS:
                    continue
                stem = f.name.lower().replace(" ", "").replace("-", "").replace("_", "")
                if any(h in stem for h in NOTICE_FONT_HINTS):
                    return f
        except OSError:
            continue
    return None


def _resolve_notice_font_path(override=None):
    """解析中文字体路径（进程内只解析一次，避免逐题重复扫描目录）。
    顺序：--notice-font 指定 → 平台候选表 → 常见字体目录扫描 → None（无可用字体）。
    None 也会被缓存——调用方据此跳过提示文字绘制。"""
    global _notice_font_path
    if _notice_font_path is not _FONT_UNSET:
        return _notice_font_path
    p = None
    if override:
        c = Path(override).expanduser()
        if c.is_file():
            p = c
        else:
            print(f"    [warn] --notice-font 指定的字体不存在：{c}，改用自动探测")
    if p is None:
        p = _scan_font_candidates()
    if p is None:
        p = _scan_font_dirs()
    if p is None:
        print("    [warn] 未找到中文字体：题图将不绘制「作答提示」文字（分隔线保留），"
              "渲染继续。可用 --notice-font <字体路径> 指定。")
    _notice_font_path = p
    return p


def _notice_font(dpi, path):
    """提示文字字体（中文，随 DPI 缩放）；返回 (font, 字高px)。
    path 为 _resolve_notice_font_path() 的结果；为空或加载失败时返回
    (None, 0)，调用方据此只画分隔线、跳过提示文字——**不再中断渲染**。"""
    if not path:
        return None, 0
    fs = max(int(round(9 * dpi / 72.0)), 13)
    try:
        return ImageFont.truetype(str(path), fs), fs
    except Exception as e:  # 字体文件损坏 / 格式不受支持
        print(f"    [warn] 字体加载失败 {path}: {e}，跳过作答提示文字")
        return None, 0


def _mask_sidebars(doc, rg, im, zoom):
    """把落在题段内、被上下边界切断的「Do not write in this area」竖排底纹涂白。

    经济卷 652×899 那 3 卷的左侧边栏框（x≈41.6–50、h≈645）压在版心内，
    而版心左沿取 39——矩形切割分不开它：正文题号行与它在 x 上重叠、只在 y 上错开。
    不处理的话图左会露出半截竖排字（2026-10-19）。

    只涂「窄且高」的竖条，且必须与题段在 y 上有实质交叠；物理/化学的边栏在
    版心外（x<36 或 x>558），根本进不了题段，这里什么都不会发生。
    """
    try:
        page = doc[rg["page"]]
        pw = page.rect.width
        if pw < 620:
            return                       # A4 卷的边栏在版心外，不必遮（也避免误伤表格边框）
        drawn = page.get_drawings()
    except Exception:
        return
    bb = rg["bbox"]
    for dr in drawn:
        db = dr["rect"]
        # 边栏是「灰底纹框 + 两条竖线」三层叠在一起，宽度 8–36pt 都要收
        if db.width > 60 or db.height < 300:
            continue
        if not (db.x1 <= 60 or db.x0 >= pw - 60):
            continue
        ov = min(bb[3], db.y1) - max(bb[1], db.y0)
        if ov < 60:                      # 只蹭到一点边，不值得涂
            continue
        x0 = max(0, int((db.x0 - bb[0]) * zoom) - 1)
        x1 = min(im.width, int((db.x1 - bb[0]) * zoom) + 1)
        if x1 - x0 < 2 or x0 >= im.width:
            continue
        ImageDraw.Draw(im).rectangle([x0, 0, x1 - 1, im.height], fill="white")


def _mask_corner_label(doc, rg, im, zoom):
    """涂白压在题段顶部的旧版「Leave blank」角标（2020–2022 数学卷）。

    老版式在每页右上角印两行小字 "Leave / blank"（x≈530、y≈38–61）。切割层
    已把它剔出内容行，但数学题号行本身就在 y≈58，题段上沿只能压到 y≈55，
    角标下半截仍会漏进题图右上角（看起来像水印残字）。这里按词框精确涂白，
    只在 x>500 且 y<70 且整词为 Leave/blank 时触发——其他学科卷没有这两个词
    长在这个位置，等于空操作。
    """
    bb = rg["bbox"]
    if bb[3] < 40 or bb[1] > 75:
        return
    try:
        words = doc[rg["page"]].get_text("words")
    except Exception:
        return
    for w in words:
        if w[4].strip() not in ("Leave", "blank"):
            continue
        if w[0] < 500 or w[3] > 70:
            continue
        x0 = max(0, int((w[0] - bb[0]) * zoom) - 1)
        y0 = max(0, int((w[1] - bb[1]) * zoom) - 1)
        x1 = min(im.width, int((w[2] - bb[0]) * zoom) + 1)
        y1 = min(im.height, int((w[3] - bb[1]) * zoom) + 1)
        if x1 > x0 and y1 > y0:
            ImageDraw.Draw(im).rectangle([x0, y0, x1 - 1, y1 - 1], fill="white")


def main():
    ap = argparse.ArgumentParser(description="按 cut.json 渲染每题 WebP 题图")
    ap.add_argument("session_dir")
    ap.add_argument("--dpi", type=int, default=None)
    ap.add_argument("--quality", type=int, default=None)
    ap.add_argument("--no-notice", action="store_true",
                    help="不画「上方为资料」的提示行（会计 P3/P4 的 INSERT 用）")
    ap.add_argument("--notice-font", default=None,
                    help="作答提示文字所用的中文字体文件路径。缺省按平台自动探测"
                         "（Windows / macOS / Linux 候选表 + 字体目录扫描）；"
                         "探测不到则不绘制提示文字、只保留分隔线，渲染不中断。")
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
        mat = [dict(r) for r in (q.get("material_regions") or [])]
        ans = q.get("answer_regions") or []
        n_qp = len(regions)
        block = q.get("answer_block") or []
        # pieces: (kind, label, region)。kind：
        #   "material"     资料块（会计卷 Source Booklet，拼在最前；经济卷拼在题面之后）
        #   "qp"           题目块（理化=题干；会计=题面+点线作答区）
        #   "answer_block" 答题册整块（01A 会计 / 理化 A 卷）
        #   "answer"       答题册按小问分条（记录进 layout.areas）
        # material_order: "before"（默认，会计/理化）= 资料在前；
        #                 "after"（经济）= 题面在前、资料在后（2026-10-19 经济卷定）。
        mat_after = (cut.get("material_order") or "before") == "after"
        pieces = []
        if ans or block:
            if not block:
                # 兜底（无 answer_block 的旧结构）：题干切 Total 行 + 册内按条拼接
                en = (q.get("anchors") or {}).get("end")
                if en and regions and regions[-1]["page"] == en.get("page"):
                    y_top = en["bbox"][1] - 6
                    if y_top > regions[-1]["bbox"][1] + 10:
                        regions[-1] = {**regions[-1], "bbox": regions[-1]["bbox"][:3] + [y_top]}
        if not mat_after:
            for r in mat:
                pieces.append(("material", None, r))
        for r in regions:
            pieces.append(("qp", None, r))
        if mat_after:
            for r in mat:
                pieces.append(("material", None, r))
        if block:
            for r in block:
                pieces.append(("answer_block", None, r))
        elif ans:
            for a in ans:
                for r in a.get("regions", []):
                    pieces.append(("answer", a.get("label"), r))
        if not pieces:
            continue
        # 段间分隔线：只在不同 kind 交界处画（资料|题目、题目|答题册各一条）
        boundaries = [i for i in range(1, len(pieces))
                      if pieces[i][0] != pieces[i - 1][0]]
        n_kind = len({k for k, _, _ in pieces})
        has_ans = any(k in ("answer", "answer_block") for k, _, _ in pieces)
        tag = f"资料 {len(mat)} + QP {n_qp}" + (
            f" + 册 {len(block) or sum(len(a.get('regions', [])) for a in ans)}"
            if has_ans else "")

        # ── 尺寸预检：整卷题图纵向拼接，高度可能超过 WebP 单边上限 ──
        # 超限时按比例下调本题 DPI（layout.json 用归一化坐标，缩放无关），
        # 避免整卷因一题过长而渲染中断。
        def _h_after(r):
            """该段渲染进画布后的高度（旋转 90° 的段，高度变成原宽度）。"""
            b = pymupdf.Rect(r["bbox"])
            return b.width if r.get("rotate") in (90, -90) else b.height

        est_pt = sum(_h_after(r) for _k, _l, r in pieces)
        q_dpi = dpi
        for _ in range(6):
            _z = q_dpi / 72.0
            if n_kind > 1:
                _fs = max(int(round(9 * q_dpi / 72.0)), 13)
                _extra = _fs + 2 * max(int(round(5 * q_dpi / 72.0)), 4) \
                    + len(boundaries) * max(int(round(18 * q_dpi / 72.0)), 10)
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
        if n_kind > 1:
            font, fs_px = _notice_font(q_dpi, _resolve_notice_font_path(args.notice_font))
            pad = max(int(round(5 * q_dpi / 72.0)), 4)
            notice_h = fs_px + 2 * pad if (font is not None and not args.no_notice) else 0
            sep_h = max(int(round(18 * q_dpi / 72.0)), 10)
        # 点线压缩接缝：同页两段之间被删掉了一截，交界处画一条浅灰虚线提示
        seam_idx = set()
        for i in range(1, len(pieces)):
            pa, pb = pieces[i - 1][2], pieces[i][2]
            if pa["page"] == pb["page"] and pb["bbox"][1] - pa["bbox"][3] > 2:
                seam_idx.add(i)

        imgs, areas, block_pieces, offsets = [], [], [], []
        y_cursor = notice_h
        for i, (kind, label, rg) in enumerate(pieces):
            if i in boundaries:
                y_cursor += sep_h
            if rg["page"] >= doc.page_count:
                sys.exit(f"Q{q['number']}: 页码 {rg['page']} 越界")
            clip = pymupdf.Rect(rg["bbox"])
            if clip.is_empty or clip.is_infinite:
                sys.exit(f"Q{q['number']}: 非法 bbox {rg['bbox']}")
            pix = doc[rg["page"]].get_pixmap(matrix=q_mat, clip=clip, alpha=False)
            im = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            if rg.get("rotate"):
                # 整页横排印刷的作答页（D1 网络图/网格）：先转正再进画布。
                # 涂白边栏/角标的换算是按未旋转坐标写的，旋转后必然错位，故跳过。
                im = im.rotate(int(rg["rotate"]), expand=True, fillcolor="white")
            else:
                _mask_sidebars(doc, rg, im, q_zoom)
                _mask_corner_label(doc, rg, im, q_zoom)
            offsets.append(y_cursor)
            if kind == "answer":
                areas.append({"label": label, "region": rg, "y_off": y_cursor, "im": im})
            elif kind == "answer_block":
                block_pieces.append((rg, y_cursor, im))
            imgs.append(im)
            y_cursor += im.height
        if n_kind > 1:
            # 分区式合成：提示带 → 资料 → 分隔 → 题目 → 分隔 → 答题区
            W = max(i.width for i in imgs)
            H = sum(i.height for i in imgs) + notice_h + len(boundaries) * sep_h
            canvas = Image.new("RGB", (W, H), "white")
            draw = ImageDraw.Draw(canvas)
            has_mat = any(k == "material" for k, _, _ in pieces)
            if mat_after:
                notice = (NOTICE_TEXT_MATERIAL_AFTER_ANS if has_ans
                          else NOTICE_TEXT_MATERIAL_AFTER) if has_mat else NOTICE_TEXT
            elif has_mat:
                notice = NOTICE_TEXT_MATERIAL if has_ans else NOTICE_TEXT_MATERIAL_NOANS
            else:
                notice = NOTICE_TEXT
            if font is not None and notice_h > 0:
                tb = draw.textbbox((0, 0), notice, font=font)
                draw.text(((W - (tb[2] - tb[0])) // 2 - tb[0],
                           (notice_h - (tb[3] - tb[1])) // 2 - tb[1]),
                          notice, font=font, fill=(110, 110, 110))
            lw = max(int(round(1.2 * q_dpi / 72.0)), 2)
            for i, (kind, _lbl, _rg) in enumerate(pieces):
                y = offsets[i]
                if i in boundaries:
                    ly = y - sep_h + (sep_h - lw) // 2
                    draw.rectangle([0, ly, W, ly + lw - 1], fill=(205, 205, 205))
                if i in seam_idx:
                    # 点线压缩接缝：画一条**实线**（虚线会跟作答点线混淆，看着像多了一行）
                    h = max(int(round(1.0 * q_dpi / 72.0)), 2)
                    draw.rectangle([0, y, W, y + h - 1], fill=(226, 226, 226))
                # align="right"：窄条（CIE 印在点线尾巴上的 [n] 分值条）贴右沿，
                # 跟真卷位置一致；默认左对齐，不影响任何现有卷
                _x = (W - imgs[i].width) if pieces[i][2].get("align") == "right" else 0
                canvas.paste(imgs[i], (_x, y))
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
            # 夹紧到图内：小问/选项 bbox 可能比题段左沿还靠左几个 pt（D1 的
            # "(a)" 标签 x≈35 < 版心 L=39），直接归一化会算出负数被 schema 拒。
            x0 = min(max(x0, 0.0), W)
            x1 = min(max(x1, 0.0), W)
            y0 = min(max(y0, 0.0), H)
            y1 = min(max(y1, 0.0), H)
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
                ent = {"label": hz["label"], "box": box}
                if hz.get("part"):
                    ent["part"] = hz["part"]
                opt_entries.append(ent)
        # 小问标签行在图内的位置（归一化）。下游据此按纵向区间判断内容/笔迹
        # 落在哪个小问下——小问纵向排列，故 y 区间即可界定归属。
        # ⚠️ 小问可跨页，必须同时匹配 page（options 不必，选项总在同一段内）；
        #    只认 kind=qp：小问标签本就在题卷上，落到册内段说明匹配有误。
        sq_entries = []
        for s in q.get("subquestions") or []:
            b = s["bbox"]
            box = None
            for (kind, _lbl, rg), yoff in zip(pieces, offsets):
                rb = rg["bbox"]
                if (kind == "qp" and rg["page"] == s["page"]
                        and rb[1] - 1 <= b[1] and b[3] <= rb[3] + 1):
                    box = nbox((b[0] - rb[0]) * q_zoom, yoff + (b[1] - rb[1]) * q_zoom,
                               (b[2] - rb[0]) * q_zoom, yoff + (b[3] - rb[1]) * q_zoom)
                    break
            if box is None:
                print(f"    [warn] Q{q['number']}: 小问 {s['label']}（page {s['page']}）"
                      f" 未落在任何题卷段内，已跳过")
                continue
            sq_entries.append({"label": s["label"], "box": box})
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
        if sq_entries:
            layout["subquestions"] = sq_entries
        (assets / f"q{int(q['number']):02d}.layout.json").write_text(
            json.dumps(layout, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        n_layout += 1
        n_desc = f", areas={len(area_entries)}" if area_entries else ""
        if opt_entries:
            n_desc += f", options={len(opt_entries)}"
        if sq_entries:
            n_desc += f", subq={len(sq_entries)}"
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
