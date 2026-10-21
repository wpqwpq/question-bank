#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""preview_image.py — 把题图压成「能塞进对话/邮件」的尺寸，供人眼与视觉模型检查。

题图是 200DPI WebP（单题常 1400×5000px、单边超 5000 会被多模态接口拒收），
直接看既慢又容易失败。本脚本等比缩到指定宽度并转 JPEG，可按段切分。

用法:
    python scripts/preview_image.py assets/q01.webp                 # 宽 780，整张
    python scripts/preview_image.py assets/q01.webp --w 700 --split 3
    python scripts/preview_image.py "data/aqa/a-level/chemistry/2025-june-ch01/assets/*.webp"

输出与源文件同目录：`_pv_<原名>_<段号>.jpg`，看完记得删（别混进 data/）。
"""

import argparse
import sys
from pathlib import Path

from PIL import Image


def preview(src: Path, width: int = 780, split: int = 1, quality: int = 82):
    im = Image.open(src).convert("RGB")
    scale = width / im.width
    nh = int(im.height * scale)
    im = im.resize((width, nh), Image.LANCZOS)
    seg = nh // max(1, split)
    outs = []
    for i in range(max(1, split)):
        y0 = i * seg
        y1 = nh if i == max(1, split) - 1 else (i + 1) * seg
        out = src.with_name(f"_pv_{src.stem}_{i + 1}.jpg")
        im.crop((0, y0, width, y1)).save(out, quality=quality, optimize=True)
        outs.append((out, width, y1 - y0))
    return outs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("paths", nargs="+")
    ap.add_argument("--w", type=int, default=780, help="目标宽度（像素）")
    ap.add_argument("--split", type=int, default=1, help="纵向均分几段")
    args = ap.parse_args()
    for p in args.paths:
        for out, w, h in preview(Path(p), args.w, args.split):
            print(f"{out}  {w}x{h}")


if __name__ == "__main__":
    sys.exit(main())
