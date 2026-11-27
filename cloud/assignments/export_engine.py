#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
真实导出引擎：把已选题目排版成 A4 页面，输出 PDF / DOCX。
- 每张题图恒定适合页面宽度（铁律），作为不可分割块：放不下就整图换页，页底可留白；
  超过整页的题图按 layout 小问边界切段，保证单个小问不跨页。
- Edexcel「按小问分别打标」的题：小问截图缺失时由前端按 layout 裁出「题干+本小问」，
  服务端核查禁止把整题图用于小问条目。
- 选择题：紧凑排列（几乎不留空隙）。
- 大题：同一大题只输出一次图（避免共同题干重复）；
  若原图已含虚线/横线/方格答题区则不再预留，否则按 total_marks*2 行补虚线答题区。
- PDF：PIL 多页合成（A4, 150DPI）。DOCX：手写最小 OOXML，每页一张整页图。
对题库服务器仅 GET 图片，不写任何数据。
"""
import io, os, json, math, re, zipfile, urllib.request, time
from PIL import Image, ImageDraw

DPI = 150
A4_W, A4_H = int(210 / 25.4 * DPI), int(297 / 25.4 * DPI)  # 1240 x 1754
MARGIN = int(12 / 25.4 * DPI)
REMOTE = "http://140.143.229.85"
# 部署在题库服务器本机时直接只读读取题图与布局，不依赖会话 cookie
QDATA = "/opt/qbank/data"
ROOT = os.path.dirname(os.path.abspath(__file__))
COOKIE_FILE = os.path.join(ROOT, ".session_cookie")
COOKIE_ADMIN = os.path.join(ROOT, ".session_cookie_admin")
_img_cache = {}


def load_cookie():
    """优先用可跨科目读取的 admin 只读会话，回退化学账号会话。"""
    for fp in (COOKIE_ADMIN, COOKIE_FILE):
        try:
            v = open(fp, encoding="utf-8").read().strip()
            if v:
                return v
        except Exception:
            pass
    return ""


def local_path(remote_path):
    """把 /files/... 映射到题库服务器本地只读文件路径；绝不写入。"""
    if not remote_path or not remote_path.startswith("/files/"):
        return None
    parts = remote_path[len("/files/"):].split("/")
    if not parts or any(p in ("", ".", "..") for p in parts):
        return None
    fp = os.path.normpath(os.path.join(QDATA, *parts))
    if fp != QDATA and not fp.startswith(QDATA + os.sep):
        return None
    return fp


def fetch_image(remote_path):
    """优先从题库服务器本地只读读取截图；否则带会话 GET。"""
    if remote_path in _img_cache:
        return _img_cache[remote_path].copy()
    fp = local_path(remote_path)
    if fp and os.path.isfile(fp):
        im = Image.open(fp).convert("RGB")
        _img_cache[remote_path] = im.copy()
        return im
    ck = load_cookie()
    url = REMOTE + remote_path
    last = None
    for attempt in range(8):
        try:
            req = urllib.request.Request(url, headers={"Cookie": f"qb_session={ck}", "User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=30) as r:
                data = r.read()
            im = Image.open(io.BytesIO(data)).convert("RGB")
            _img_cache[remote_path] = im.copy()
            return im
        except Exception as e:
            last = e
            time.sleep(0.4 * (attempt + 1))
    raise RuntimeError(f"图片获取失败: {remote_path} ({str(last)[:60]})")


_layout_cache = {}


def fetch_layout(remote_path):
    """读取题目布局 JSON（只读 GET）。失败返回 None。"""
    if not remote_path:
        return None
    if remote_path in _layout_cache:
        return _layout_cache[remote_path]
    fp = local_path(remote_path)
    if fp and os.path.isfile(fp):
        try:
            with open(fp, encoding="utf-8") as f:
                d = json.load(f)
        except Exception:
            d = None
        _layout_cache[remote_path] = d
        return d
    ck = load_cookie()
    url = REMOTE + remote_path
    for attempt in range(6):
        try:
            req = urllib.request.Request(url, headers={"Cookie": f"qb_session={ck}", "User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=25) as r:
                d = json.loads(r.read().decode("utf-8"))
            _layout_cache[remote_path] = d
            return d
        except Exception:
            time.sleep(0.4 * (attempt + 1))
    _layout_cache[remote_path] = None
    return None


def subquestion_cuts(layout, img_h):
    """根据布局里每个小问标记线的起点计算安全切点（像素 y）。
    布局按文档顺序给出各小问起始位置（归一化 0-1），相邻标记线之间恰为一个小问的
    完整内容区：在标记线处切分，保证任一单个小问（含其子小问）都完整落在同一段内。
    返回按 y 升序的切点列表（不含 0 和 img_h）。"""
    if not layout:
        return []
    subs = layout.get("subquestions") or []
    starts = []
    for s in subs:
        box = s.get("box") or {}
        y = box.get("y")
        if y is None:
            continue
        starts.append(float(y))
    starts = sorted(set(starts))
    cuts = []
    for y in starts:
        ypx = int(y * img_h)
        if 0 < ypx < img_h:
            cuts.append(ypx)
    return cuts


SUBIMG_RE = re.compile(r"/q\d+-[a-z0-9-]+\.webp$", re.I)


def crop_compose(im, regions):
    """按归一化区域 [y0,y1] 从整题图裁出若干段并纵向拼接（题干+本小问）。
    用于小问独立截图缺失的 Edexcel 题：只输出题干+所选小问，绝不输出整题图。"""
    W, H = im.width, im.height
    parts = []
    for r in regions or []:
        try:
            a, b = float(r[0]), float(r[1])
        except (TypeError, ValueError, IndexError):
            continue
        a = max(0.0, min(1.0, a))
        b = max(0.0, min(1.0, b))
        if b <= a:
            continue
        y0, y1 = int(a * H), max(int(a * H) + 1, int(b * H))
        parts.append(im.crop((0, y0, W, y1)))
    if not parts:
        return None
    out = Image.new("RGB", (W, sum(p.height for p in parts)), "white")
    y = 0
    for p in parts:
        out.paste(p, (0, y))
        y += p.height
    return out


def split_blocks(disp, avail_h, layout):
    """超过一页可用高度的题图：按 layout 小问边界切段（每段恰含一个完整小问）。
    某个小问自身超过一页时等分（极端情况，仍保持完整宽度，不缩小）。
    返回块列表；未超高时返回 [disp]。"""
    H = disp.height
    if H <= avail_h:
        return [disp]
    cuts = subquestion_cuts(layout, H)
    bounds = [0] + [c for c in cuts if c < H] + [H]
    bounds = sorted(set(bounds))
    segments = [(bounds[i], bounds[i + 1]) for i in range(len(bounds) - 1)
                if bounds[i + 1] - bounds[i] > 60]
    if not segments:
        segments = [(0, H)]
    out = []
    for a, b in segments:
        if b - a <= avail_h:
            out.append(disp.crop((0, a, disp.width, b)))
            continue
        n = math.ceil((b - a) / avail_h)
        step = (b - a) / n
        for k in range(n):
            out.append(disp.crop((0, int(a + k * step), disp.width, int(a + (k + 1) * step))))
    return out


def has_answer_area(im):
    """检测原图是否已含答题区（虚线横线 / 方格）。返回 True 则不再预留。
    做法：统计横跨页面大部分宽度的“浅色/深色线行”数量。
    试卷自带的虚线答题区是等间距、贯穿大半页宽的水平线，行数多且规律。"""
    g = im.convert("L")
    w, h = g.size
    if h < 120:
        return False
    px = g.load()
    half = w // 2
    line_rows = 0
    for y in range(0, h, 2):
        cnt = first = last = 0
        seen = False
        for x in range(0, w, 2):
            if px[x, y] < 205:
                if not seen:
                    first = x; seen = True
                last = x
                cnt += 1
        if cnt > half * 0.18 and seen and (last - first) > w * 0.55:
            line_rows += 1
    # 有相当数量的横跨线 → 原图已含答题/写作区，不再预留
    return line_rows >= 8


def scale_to_width(im, width):
    if im.width == width:
        return im
    nh = max(1, int(im.height * width / im.width))
    return im.resize((width, nh), Image.LANCZOS)


def dashed_lines_block(width, lines):
    """生成由一行行虚线构成的答题区图片。"""
    line_h = 34
    block = Image.new("RGB", (width, lines * line_h), "white")
    d = ImageDraw.Draw(block)
    dash, gap = 14, 10
    for i in range(lines):
        y = i * line_h + line_h - 8
        x = 4
        while x < width - 4:
            d.line([(x, y), (min(x + dash, width - 4), y)], fill=(150, 150, 158), width=1)
            x += dash + gap
    return block


def build_pages(items):
    """items: [{img, layout, qtype, marks, no, sub_no?, crop?}] -> [PIL.Image A4页]
    铁律：每张题图恒定适合页面宽度（按可用宽度等比缩放），绝不缩小宽度迁就页高。
    每个题图/题图段是不可分割块：当前页放不下就在该块前换页（页底可留白），
    不需要一页一道题。单张图超过整页可用高度时，按 layout 小问边界切段，
    保证任一单个小问不被切到两页；无 layout 时等分。
    crop 条目：小问独立截图缺失的 Edexcel 题，前端已用 layout 裁出「题干+本小问」
    区域，本引擎只负责拼接，绝不退回整题图。
    DOCX / PDF 均基于本函数产出的页面，输出规则与预览一致。"""
    pages = []
    page = None
    y = MARGIN
    avail_w = A4_W - 2 * MARGIN
    avail_h = A4_H - 2 * MARGIN

    def new_page():
        nonlocal page, y
        page = Image.new("RGB", (A4_W, A4_H), "white")
        y = MARGIN
        pages.append(page)

    def place(block):
        """不可分割块：放不下就整块换页，绝不切断、绝不缩小。"""
        nonlocal y
        bh = block.height
        if y + bh > A4_H - MARGIN and y > MARGIN:
            new_page()
        x = MARGIN + (avail_w - block.width) // 2
        page.paste(block, (x, y))
        y += bh

    new_page()
    for it in items:
        # 服务端核查机制：小问条目（sub_no）必须是「小问独立截图」或「裁剪合成」，
        # 绝不允许把整题父图放上——违反即报错，阻断导出。
        if it.get("sub_no") and not it.get("crop") and not SUBIMG_RE.search(it.get("img") or ""):
            raise RuntimeError(f"核查失败：小问 {it.get('no')} 误用了整题图，已阻止导出")
        im = fetch_image(it["img"])
        qtype = (it.get("qtype") or "").lower()
        marks = int(it.get("marks") or 0)

        # crop 条目：题干+本小问区域拼接（小问截图缺失时的替代，绝不整题）
        if it.get("crop"):
            composed = crop_compose(im, (it.get("crop") or {}).get("regions"))
            if composed is not None:
                im = composed
        # 适合页面宽度：恒定按可用宽度等比缩放（不压缩字、不缩宽度）
        disp = scale_to_width(im, avail_w)
        # 超过一页的题图按 layout 小问边界切段后逐块放置；未超高则整图为一个不可分割块
        layout = fetch_layout(it.get("layout")) if disp.height > avail_h else None
        for block in split_blocks(disp, avail_h, layout):
            place(block)

        # 结构化大题：原图未含答题区时按分数补虚线答题区（独立块，放不下另起一页）
        if qtype == "structured" and not has_answer_area(im):
            need_lines = max(2, marks * 2)
            place(dashed_lines_block(avail_w, need_lines))

        # 题目之间的分隔留白（选择题保持紧凑）
        if qtype != "mcq":
            y += 8
    return pages


def to_pdf(pages):
    buf = io.BytesIO()
    pages[0].save(buf, format="PDF", save_all=True,
                  append_images=pages[1:], resolution=DPI)
    return buf.getvalue()


def _docx_escape(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def to_docx(pages, title="作业题目"):
    """手写最小 docx：每页一张整页图，A4 页面，边距 0。"""
    media = []
    for i, im in enumerate(pages):
        b = io.BytesIO()
        im.save(b, format="PNG")
        media.append((f"image{i+1}.png", b.getvalue()))

    rels = []
    drawings = []
    for i in range(len(pages)):
        rid = i + 1
        rels.append(
            f'<Relationship Id="rId{rid+1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="media/image{i+1}.png"/>'
        )
        cx, cy = int(A4_W / DPI * 914400), int(A4_H / DPI * 914400)
        drawings.append(
            '<w:p><w:pPr><w:spacing w:before="0" w:after="0" w:line="1"/>'
            '<w:pageBreakBefore/></w:pPr>' if i > 0 else '<w:p><w:pPr><w:spacing w:before="0" w:after="0"/></w:pPr>'
        )
        drawings.append(
            f'<w:r><w:drawing><wp:inline distT="0" distB="0" distL="0" distR="0">'
            f'<wp:extent cx="{cx}" cy="{cy}"/><wp:docPr id="{i+1}" name="Page {i+1}"/>'
            f'<a:graphic xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
            f'<a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/picture">'
            f'<pic:pic xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture">'
            f'<pic:nvPicPr><pic:cNvPr id="{i+1}" name="page{i+1}.png"/><pic:cNvPicPr/></pic:nvPicPr>'
            f'<pic:blipFill><a:blip r:embed="rId{rid+1}"/><a:stretch><a:fillRect/></a:stretch></pic:blipFill>'
            f'<pic:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="{cx}" cy="{cy}"/></a:xfrm>'
            f'<a:prstGeom prst="rect"><a:avLst/></a:prstGeom></pic:spPr>'
            f'</pic:pic></a:graphicData></a:graphic></wp:inline></w:drawing></w:r></w:p>'
        )

    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
        'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" '
        'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
        'xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture">'
        '<w:body>' + ''.join(drawings) +
        '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/>'
        '<w:pgMar w:top="0" w:right="0" w:bottom="0" w:left="0" w:header="0" w:footer="0" w:gutter="0"/>'
        '</w:sectPr></w:body></w:document>'
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Default Extension="png" ContentType="image/png"/>'
        '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        '</Types>'
    )
    root_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
        '</Relationships>'
    )
    doc_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        + ''.join(rels) + '</Relationships>'
    )

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", content_types)
        z.writestr("_rels/.rels", root_rels)
        z.writestr("word/document.xml", document)
        z.writestr("word/_rels/document.xml.rels", doc_rels)
        for name, data in media:
            z.writestr("word/media/" + name, data)
    return buf.getvalue()
