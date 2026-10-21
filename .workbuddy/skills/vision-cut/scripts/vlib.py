# -*- coding: utf-8 -*-
"""vision-cut 公共层：路径、OCR、像素投影、行聚类、罗马/字母标签。

设计约定（2026-10-18 定）：
- **坐标由算法算，字符由 OCR 认，标签由序列推断**。三者不要混。
- OCR 的 bbox 可信、OCR 的**字符**不可信（(i)→()、(iii)→(ili)、(ii)→(m)）。
- 一切 y 坐标单位是 PDF pt（左上原点，y 向下），与库内 schema 一致。
"""
import json
import re
from pathlib import Path

import numpy as np
import pymupdf
from PIL import Image

# ---------- 版心（按页面尺寸换算，别写死） ----------
PAD_L, PAD_R = 20.0, 20.0      # 左右各收 20pt（躲开 DO NOT WRITE 竖栏 / 定位框）
PAD_T = 56.0                   # 上：躲开页眉的密文行 / 条形码
PAD_B = 46.0                   # 下：页高 − 46pt。⚠️ 别用 −52：落在 740pt 的 [Total: 19]
                               # 会被当页脚切掉（实测 2025-june-qp42 p15），题界直接少一题。
                               # 也别更靠下：页脚 © UCLES / 条码在 780 附近。

ROMAN = ["i", "ii", "iii", "iv", "v", "vi", "vii", "viii", "ix", "x"]
LETTER = "abcdefghijklmnop"

RE_TOTAL = re.compile(r"^\[\s*Total[:\s]*(\d+)\s*\]?$", re.I)
RE_TAG1 = re.compile(r"^\(([a-z])\)")
RE_TAG2 = re.compile(r"^\(([a-z0-9|]{0,6})\)")
# ⚠️ 分值标记要用 search + 行尾锚定，不能要求整行就是 [N]：
# CIE 常写成 "pH = [4]"、"mass of iron in one tablet = .. mg [4]"，
# 按整行匹配会漏掉一半，导致「叶子数 vs [N] 数」全线误报（2026-10-18 实测）。
RE_MARK = re.compile(r"\[\s*(\d{1,2})\s*\]\s*$")
APPENDIX = re.compile(r"important values|constants and standards|^periodic table", re.I)

OCR_DPI = 300      # 短标签行在 200DPI 漏检明显，必须 300
BLOCK_DPI = 100    # 像素投影够用，快


class Geom:
    """一张卷的页面几何。

    ⚠️ **版心 x 用固定 55/548，不要按页宽算比例，也不要靠像素自动探测**（2026-10-18 实测）：
    CIE 的版心宽度恒为 493pt（55–548），A4 595 与 Letter 612 都一样 —— 它是按固定尺寸排的。
    按比例算（W−20）会把右侧 DO NOT WRITE 竖栏算进内容；
    自动探测最小 x（实测 31）会撞上**四角定位框**，那对线贯穿全页高度，一碰就把每页 y 范围撑满。
    """

    def __init__(self, doc):
        self.doc = doc
        self.W = float(doc[0].rect.width)
        self.H = float(doc[0].rect.height)
        self.pages = doc.page_count
        if self.W < 700:                       # A4 / Letter
            self.x0, self.x1 = 55.0, min(548.0, self.W - 40)
        else:                                  # A3（842）：版心同样 493 宽，居中
            self.x0 = self.W / 2 - 246.5
            self.x1 = self.W / 2 + 246.5
        self.yt = PAD_T
        self.yb = self.H - PAD_B

    def __repr__(self):
        return (f"Geom({self.pages}p {self.W:.0f}x{self.H:.0f} "
                f"版心 x{self.x0:.0f}-{self.x1:.0f} y{self.yt:.0f}-{self.yb:.0f})")


def open_paper(vol_dir):
    """卷目录 → (doc, Geom)。"""
    pdf = Path(vol_dir) / "source.pdf"
    if not pdf.exists():
        raise FileNotFoundError(f"缺 source.pdf: {pdf}")
    doc = pymupdf.open(str(pdf))
    return doc, Geom(doc)


def detect_content_x(doc, g, sample=6):
    """实测整卷内容 x 边界（取若干页并集），比写死 55/548 稳。

    ⚠️ 右侧要 clamp 到 W*0.92：不然 Letter 卷的 DO NOT WRITE 竖栏（x>566）会被算进内容。
    """
    xs0, xs1 = [], []
    step = max(1, doc.page_count // sample)
    for pno in range(0, doc.page_count, step):
        for b in page_blocks(doc, pno, g):
            xs0.append(b["x0"])
            xs1.append(b["x1"])
    if not xs0:
        return g.x0, g.x1
    left = min(xs0)
    right = min(max(xs1), g.W * 0.92)
    return max(left - 2, 4.0), min(right + 2, g.W - 4.0)


# ---------- 文本层体检（决定走脚本还是走视觉） ----------
COMMON = set("the of and to in is are for that with which can be a an on by as at from "
             "this not are was were has have it its".split())


def textlayer_verdict(pdf):
    """返回 dict(cls=OK|PARTIAL|CORRUPT, ...)。CORRUPT 才需要视觉切割。"""
    doc = pymupdf.open(str(pdf))
    txt = "".join(p.get_text() for p in doc)
    pages = doc.page_count
    w, h = round(doc[0].rect.width), round(doc[0].rect.height)
    doc.close()
    total = len(txt)
    ctrl = sum(1 for c in txt if ord(c) < 32 and c not in "\n\r\t")
    words = re.findall(r"[A-Za-z]{3,}", txt)
    common = sum(1 for wd in words if wd.lower() in COMMON)
    ratio = ctrl / total if total else 0
    if common >= 30 and ratio < 0.02:
        cls = "OK"
    elif ratio > 0.15 or common < 5:
        cls = "CORRUPT"
    else:
        cls = "PARTIAL"
    return dict(cls=cls, pages=pages, w=w, h=h, chars=total,
                ctrl_ratio=round(ratio, 4), common_words=common)


# ---------- 像素投影 ----------
def page_blocks(doc, pno, g, dpi=BLOCK_DPI):
    """一页切成墨迹块（按行方向）。返回 pt 坐标块列表，已限制在版心内。

    ⚠️ 阈值与合并间距：行距 gap > 3pt 视为新块。点线（作答横线）会单独成块、
    高度只有 0–1pt —— 这是后面筛子问的关键特征，别在这里滤掉。
    """
    pp = dpi / 72.0
    pix = doc[pno].get_pixmap(dpi=dpi)
    a = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)[:, :, :3]
    gimg = a.mean(axis=2)[:, int(g.x0 * pp):int(g.x1 * pp)]
    dark = gimg < 160
    ys = np.where(dark.any(axis=1))[0]
    if not len(ys):
        return []
    grp, s, prev = [], ys[0], ys[0]
    for y in ys[1:]:
        if y - prev > 3 * pp:
            grp.append((s, prev))
            s = y
        prev = y
    grp.append((s, prev))
    out = []
    for a0, a1 in grp:
        band = dark[a0:a1 + 1]
        cols = np.where(band.any(axis=0))[0]
        out.append(dict(y0=a0 / pp, y1=a1 / pp,
                        x0=g.x0 + cols.min() / pp, x1=g.x0 + cols.max() / pp,
                        w=(cols.max() - cols.min()) / pp, h=(a1 - a0) / pp))
    return [b for b in out if b["y0"] >= g.yt and b["y1"] <= g.yb]


def page_span(doc, pno, g):
    """一页正文的首末 y。含附录截断判定。

    附录截断规则（2026-10-18 实测，别改判据）：
    找到「右对齐窄行」（可能是 [Total: N]）→ 其下空白 >20pt → 再往下出现满宽块(w>380)
    或居中标题（左右留白差<30）→ 判卷末附录，截断。
    ⚠️ 右界要用**本页最大 x1** 做基准，不能写死（正文右界各卷从 483 到 548 不等）。
    """
    bs = page_blocks(doc, pno, g)
    if not bs:
        return 0.0, 0.0
    maxx = max(z["x1"] for z in bs)
    content_x0 = min(z["x0"] for z in bs)
    for i, b in enumerate(bs):
        if not (b["x1"] > maxx - 15 and 25 < b["w"] < 130 and b["h"] < 22):
            continue
        below = bs[i + 1:]
        if not below:
            break
        if below[0]["y0"] - b["y1"] < 20:
            continue
        looks_appendix = False
        for z in below:
            if z["w"] > 380:
                looks_appendix = True
                break
            left = z["x0"] - content_x0
            right = maxx - z["x1"]
            if 120 < z["w"] < 360 and abs(left - right) < 30:
                looks_appendix = True
                break
        if looks_appendix:
            bs = bs[:i + 1]
            break
    return bs[0]["y0"], bs[-1]["y1"]


# ---------- OCR ----------
def ocr_paper(vol_dir, work, dpi=OCR_DPI, force=False):
    """整卷 OCR，结果缓存到 work/<paper>/ocr.json。返回 pages 列表。"""
    paper = Path(vol_dir).name
    cache = Path(work) / paper / "ocr.json"
    if cache.exists() and not force:
        return json.loads(cache.read_text(encoding="utf-8"))

    from rapidocr_onnxruntime import RapidOCR
    engine = RapidOCR()
    doc = pymupdf.open(str(Path(vol_dir) / "source.pdf"))
    pages = []
    for pno in range(doc.page_count):
        pix = doc[pno].get_pixmap(dpi=dpi)
        img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)[:, :, :3]
        res, _ = engine(img)
        words = []
        for box, txt, score in (res or []):
            xs = [p[0] for p in box]
            ys = [p[1] for p in box]
            words.append({
                "text": txt, "score": round(float(score), 3),
                "pt": [round(min(xs) * 72 / dpi, 2), round(min(ys) * 72 / dpi, 2),
                       round(max(xs) * 72 / dpi, 2), round(max(ys) * 72 / dpi, 2)],
            })
        pages.append({"page": pno, "w": doc[pno].rect.width,
                      "h": doc[pno].rect.height, "words": words})
    doc.close()
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(pages, ensure_ascii=False), encoding="utf-8")
    return pages


def rows_of(page, g=None):
    """词 → 行。按 y 中心聚类，容差 max(4pt, 0.6×行高)。

    ⚠️ 必须滤掉 x<42 / x>566 的词：右侧 DO NOT WRITE IN THIS MARGIN 是竖排，
    OCR 会当横排读，把整行 y 中心带偏。
    """
    ws = [w for w in page["words"]
          if w["score"] > 0.3 and w["text"].strip()
          and w["pt"][0] >= 42 and w["pt"][2] <= 566]
    ws.sort(key=lambda w: ((w["pt"][1] + w["pt"][3]) / 2, w["pt"][0]))
    out = []
    for w in ws:
        yc = (w["pt"][1] + w["pt"][3]) / 2
        h = w["pt"][3] - w["pt"][1]
        for r in out:
            if abs(r["_yc"] - yc) <= max(4.0, h * 0.6):
                r["words"].append(w)
                r["_yc"] = (r["_yc"] * len(r["words"]) + yc) / (len(r["words"]) + 1)
                break
        else:
            out.append({"_yc": yc, "words": [w]})
    for r in out:
        r["words"].sort(key=lambda w: w["pt"][0])
        r["y0"] = min(w["pt"][1] for w in r["words"])
        r["y1"] = max(w["pt"][3] for w in r["words"])
        r["x0"] = min(w["pt"][0] for w in r["words"])
        r["x1"] = max(w["pt"][2] for w in r["words"])
        r["text"] = " ".join(w["text"] for w in r["words"])
        r.pop("_yc", None)
    return sorted(out, key=lambda r: r["y0"])


# ---------- 标签推断 ----------
def roman_num(raw):
    """OCR 罗马数字 → int。OCR 常把 i/l/1/| 混，先归一再查表。"""
    t = raw.lower().replace(" ", "").replace("l", "i").replace("1", "i").replace("|", "i")
    t = {"m": "ii", "rn": "ii", "in": "ii", "ili": "iii", "lii": "iii", "lll": "iii",
         "il": "iii", "ivi": "iv", "1v": "iv", "v1": "vi", "vi1": "viii"}.get(t, t)
    return ROMAN.index(t) + 1 if t in ROMAN else None


def letter_num(raw):
    t = raw.lower()
    return LETTER.index(t) + 1 if t in LETTER else None


# ---------- layout / cut 读写 ----------
def write_json(path, obj):
    """统一走 write_bytes，避免 Windows 上 write_text 改写换行。"""
    Path(path).write_bytes(json.dumps(obj, ensure_ascii=False, indent=1).encode("utf-8"))


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))
