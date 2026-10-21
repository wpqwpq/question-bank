#!/usr/bin/env python
"""剥离 PapaCambridge 打在 CIE 卷子上的水印。

两类水印，都从 content stream 里去掉，不碰正文字形：
1. 斜向大字：`/FormXob.pcm Do`（重复平铺的矢量路径，已转曲线）→ 替换为 `n`（空操作，保留 q/Q 配对）
2. 页脚条：从 `0.78 0.82 0.86 RG`（分隔线）到 `ET\\nQ`（Trace ID 结尾）整段 → 压成 `Q`

正文层是独立 XObject（`/R Do`），不受影响。

用法：
  python scripts/strip_watermark.py --dry-run data/.../source.pdf
  python scripts/strip_watermark.py --write data/.../source.pdf
"""
import argparse
import re
import sys
from pathlib import Path

import pymupdf

FOOT = re.compile(rb"0\.78 0\.82 0\.86 RG.*?ET\nQ\n", re.S)

# 斜向字母水印（真正的那个）：30° 旋转矩阵 + 紧跟一条内联路径 + f 填充。
# 注意 `/FormXob.pcm Do` 只是前置指令，路径写在它后面，所以必须整块删；
# 只把 Do 换成 n 是不会阻止绘制的（踩过这个坑）。
WM_PATH = re.compile(
    rb"q\n0?\.866025 0?\.5 -0?\.5 0?\.866025 [-\d. ]+cm\n"
    rb"(?:n\n|/FormXob\.pcm Do\n)?"
    rb"(?:[-\d. ]+[mlc]\n|h\n)*"
    rb"f\*?\nQ\n",
    re.S,
)

# 已知的两类第三方水印
# 1) PapaCambridge：斜向大字 = FormXob.pcm 平铺；页脚条 = 浅灰分隔线 + 4/6.3pt 小字（含 Trace ID）
# 2) edutvonline：页眉右上角一行蓝色网址，画在 APDE_X1 这个 XObject 里
PC_XOB = b"/FormXob.pcm Do"
EDUTV_XOB = re.compile(rb"/APDE_X\d+ Do")

MARKERS = (PC_XOB, b"0.78 0.82 0.86 RG", b"edutvonline")


def has_watermark(doc):
    for page in doc:
        for xref in page.get_contents():
            b = doc.xref_stream(xref)
            if any(m in b for m in MARKERS):
                return True
    return False


def strip(doc):
    seen, touched = set(), 0
    for page in doc:
        for xref in page.get_contents():
            if xref in seen:
                continue
            seen.add(xref)
            b = doc.xref_stream(xref)
            o = b
            if PC_XOB in b or WM_PATH.search(b):
                b = WM_PATH.sub(b"", b)
                b = b.replace(PC_XOB, b"n")
                b = FOOT.sub(b"Q\n", b)
            if b"edutvonline" in b:
                b = EDUTV_XOB.sub(b"n", b)
            if b != o:
                doc.update_stream(xref, b)
                touched += 1
    return touched


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("paths", nargs="+")
    ap.add_argument("--write", action="store_true", help="原地写回（默认只报告）")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    targets = []
    for p in args.paths:
        pp = Path(p)
        if pp.is_dir():
            targets += sorted(pp.rglob("source.pdf"))
        else:
            targets.append(pp)

    hit = 0
    for f in targets:
        doc = pymupdf.open(str(f))
        if not has_watermark(doc):
            doc.close()
            continue
        hit += 1
        if args.dry_run or not args.write:
            print(f"  [水印] {f}")
            doc.close()
            continue
        n = strip(doc)
        tmp = f.with_suffix(".pdf.tmp")
        doc.save(str(tmp), garbage=4, deflate=True)
        doc.close()
        tmp.replace(f)
        print(f"  [已剥] {f}  ({n} 个内容流)")

    print(f"\n{'命中' if (args.dry_run or not args.write) else '处理'} {hit} / {len(targets)} 个文件")


if __name__ == "__main__":
    main()
