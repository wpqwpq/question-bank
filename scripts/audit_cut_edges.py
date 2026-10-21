#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""audit_cut_edges.py — 切图边界体检：找「切到内容中间」的题段。

为什么需要：题图是给人和 AI 看的产物，一张图在题段接缝处把某行字/某条作答横线
切成两半，肉眼扫一遍 76 卷不容易发现，但学生写字时会发现。本脚本按坐标做机器
检查，比逐张看更可靠，也作为「切割脚本改完必须跑一遍」的回归项。

判据（每个 region 的上/下边界各查一次）：
  1. 非空文本行的 bbox 跨过边界（y0 < 边界 < y1）且横向与 region 有交集；
  2. 真图形（非页框、非页边家具）跨过边界。
  页框线、空白行、页边「Do not write outside the box」方框、页眉页脚一律不算。

用法:
    python scripts/audit_cut_edges.py                     # 默认 data/aqa/a-level/chemistry
    python scripts/audit_cut_edges.py data/<board>/<qual>/<subject>
    python scripts/audit_cut_edges.py <session_dir>        # 只查一卷

退出码：0 = 无风险；1 = 有风险（可用于 CI/批处理门禁）。
"""

import json
import re
import sys
from pathlib import Path

import pymupdf

# Windows 控制台默认 GBK：化学正文含 − (U+2212) 等字符，print 会抛
# UnicodeEncodeError 把整轮体检打断。强制 UTF-8 输出。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

EYE = 3.0          # 边界容差 pt
HEADER_Y, FOOTER_Y = 50.0, 768.0
L_DEF, R_DEF = 40.0, 536.0


def is_furniture(db):
    """页框线 / 角点残段 / 页边家具。"""
    w, h = db.width, db.height
    if w <= 3 and h <= 3:
        return True
    if w > 450 and h > 600:
        return True
    if h <= 3 and w > 450 and (db.y0 < 57 or db.y1 > 763):
        return True
    if w <= 3 and h > 400 and (db.x0 < 42 or db.x1 > 536):
        return True
    if db.x0 >= R_DEF - 1.0 and db.width < 120:
        return True
    return False


def visible_bbox(ln):
    """只用**含可见字符**的 span 求并集。

    ⚠️ 必须这样算：AQA 化学卷的数学行（如 `12 mass of 12C` 的分数式）里，
    只含空格的 span 带异常 ascent/descent，bbox 会竖直撑到 60pt 以上。
    直接用 ln["bbox"] 会把正常行误报成「被切成两半」。
    """
    xs0, ys0, xs1, ys1 = [], [], [], []
    for s in ln["spans"]:
        if not s["text"].strip():
            continue
        b = s["bbox"]
        xs0.append(b[0]); ys0.append(b[1]); xs1.append(b[2]); ys1.append(b[3])
    if not xs0:
        return None
    return [min(xs0), min(ys0), max(xs1), max(ys1)]


def audit_dir(sdir):
    cj = sdir / "cut.json"
    if not cj.exists():
        return None
    cut = json.loads(cj.read_text(encoding="utf-8"))
    doc = pymupdf.open(sdir / "source.pdf")
    issues = []
    for q in cut["questions"]:
        # 会计卷的三段（资料/题目/答题区）都要查，别只看 regions；
        # 点线压缩还会在同页切出多条 region，边界数量天然更多。
        all_regs = list(q.get("regions") or []) + list(q.get("material_regions") or []) \
            + list(q.get("answer_block") or [])
        for rg in all_regs:
            pi, bb = rg["page"], rg["bbox"]
            page = doc[pi]
            for b in page.get_text("dict")["blocks"]:
                if b.get("type") != 0:
                    continue
                for ln in b["lines"]:
                    txt = "".join(s["text"] for s in ln["spans"]).strip()
                    if not txt:
                        continue
                    # 剥掉控制字符后为空 → 不是内容行（CIE 化学 −0x1D 乱码卷里，
                    # 空格被压成 \x03 独立成行，bbox 跨边界会刷出几十条假阳性）
                    if not re.sub(r"[\x00-\x1f]", "", txt).strip():
                        continue
                    lb = visible_bbox(ln)
                    if lb is None:
                        continue
                    if lb[2] < bb[0] - EYE or lb[0] > bb[2] + EYE:
                        continue
                    # 横向只蹭到边不算切开：要求这行有**过半**的宽度落在题区内。
                    # 2019–2022 化学卷右侧竖排「DO NOT WRITE IN THIS AREA」宽约 21pt、
                    # 高跨整页，与版心右沿只交 3.7pt（占行宽 18%），旧判据会把它算成
                    # 每题上下边界都被切成两半，单卷刷出 26 条假阳性。
                    _ov = min(bb[2], lb[2]) - max(bb[0], lb[0])
                    if _ov < 2.0 or _ov < 0.5 * (lb[2] - lb[0]):
                        continue
                    for edge in (bb[1], bb[3]):
                        if lb[1] < edge - EYE and lb[3] > edge + EYE:
                            issues.append((q["number"], pi + 1, "text",
                                           round(edge, 1), txt[:50]))
            # 填充图案（pattern）的 tile：同一页会重复几十次的同尺寸小矩形，
            # 不是可见内容，却会刷出成片假阳性（经济卷 2025-jan-wec11 Q5 的
            # 饼图斜线填充，26×26 出现 55 次）。选项框最多 4 个，取 ≥8 次作阈值。
            tile_sizes = {}
            for _dr in page.get_drawings():
                _b = _dr["rect"]
                _k = (round(_b.width), round(_b.height))
                if _b.width <= 48 and _b.height <= 48:
                    tile_sizes[_k] = tile_sizes.get(_k, 0) + 1
            for dr in page.get_drawings():
                db = dr["rect"]
                if db.width <= 0 and db.height <= 0:
                    continue
                if is_furniture(db):
                    continue
                # 版心边缘的竖排「DO NOT WRITE IN THIS AREA」底纹框（窄而高、贴着页边）：
                # 它每页都有、和正文只在 x 上重叠（y 上错开），任何横向切割都会切到它。
                # 经济卷 652 版式里它压在版心内，渲染层已遮白，审计不该再报。
                if (db.width <= 60 and db.height >= 300
                        and (db.x0 < 50 or db.x1 > page.rect.width - 50)):
                    continue
                if (db.width <= 48 and db.height <= 48
                        and tile_sizes.get((round(db.width), round(db.height)), 0) >= 8):
                    continue
                if db.x1 < bb[0] - EYE or db.x0 > bb[2] + EYE:
                    continue
                # 与 region 的横向重叠不足 2pt 的只算「蹭到边」，不是把内容切开
                # （例：2019 卷右上角 `Do not write outside the box` 方框 x0=534.2，
                #  与版心右沿 536 只交 1.8pt，渲染出来是一条 1px 浅灰线，看不出来）
                if min(bb[2], db.x1) - max(bb[0], db.x0) < 2.0:
                    continue
                for edge in (bb[1], bb[3]):
                    if db.y0 < edge - EYE and db.y1 > edge + EYE:
                        issues.append((q["number"], pi + 1, "line", round(edge, 1),
                                       f"{round(db.width)}x{round(db.height)} @y{round(db.y0)}"))
    doc.close()
    return issues


def main():
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else \
        Path(__file__).resolve().parent.parent / "data" / "aqa" / "a-level" / "chemistry"
    if (target / "cut.json").exists():
        dirs = [target]
    else:
        dirs = sorted(d for d in target.iterdir()
                      if d.is_dir() and not d.name.startswith("_"))
    total = n_vol = 0
    for d in dirs:
        issues = audit_dir(d)
        if issues is None:
            continue
        n_vol += 1
        if issues:
            total += len(issues)
            print(f"\n{d.name}: {len(issues)} 处")
            for it in issues[:8]:
                print(f"   Q{it[0]:<3} p{it[1]:<3} {it[2]:<4} edge={it[3]:<7} {it[4]!r}")
    print(f"\n审计 {n_vol} 卷，边界风险 {total} 处")
    return 0 if total == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
