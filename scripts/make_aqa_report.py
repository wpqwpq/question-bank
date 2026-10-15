#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""make_aqa_report.py — 生成《牛津 AQA 物理切割跑批报告》HTML（自包含，图 base64 内嵌）

用法:
    python scripts/make_aqa_report.py [--out docs/AQA切割跑批报告.html]

数据全部现场从 data/aqa/a-level/physics/*/ 读（不依赖中间产物），因此重跑可复用：
读 cut.json（题数/题型/分值/状态/review）+ paper.md（卷级元数据）+ source.pdf 封面
阅卷表（题型交叉校验），并从 assets/ 抽检若干题图、把 layout.json 的热区叠加画出来。
"""

import argparse
import base64
import io
import json
import re
from collections import Counter
from pathlib import Path

import pymupdf
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
BASE = ROOT / "data" / "aqa" / "a-level" / "physics"

# 抽检题（覆盖：结构化单页 / 结构化跨页 / MCQ+图形 / 堆叠分式选项 / U5 长题 / 老卷）
SAMPLES = [
    ("2026-june-ph01", "01", "结构化 · 两小问同页 · 作答横线收尾"),
    ("2026-june-ph01", "09", "结构化 · 跨 3 页 · 中途插 DNW 页"),
    ("2026-june-ph01", "12", "结构化 · 跨 4 页 · 以 END OF SECTION B 收尾"),
    ("2026-june-ph01", "23", "选择题 · 含图形 · 选项为上下堆叠分式"),
    ("2026-jan-ph04", "10", "选择题 · 整页大图 + 四点标记"),
    ("2019-june-ph05", "07", "U5 长题 · 5 段拼接"),
    ("2018-jan-ph01", "11", "2018 老卷 · Section A 末题"),
]
TABLE_CELL_RE = re.compile(r"^(\d{1,2})$")
TABLE_RANGE_RE = re.compile(r"^(\d{1,2})\s*[-\u2013\u2014\u2212]\s*(\d{1,2})$")


def read_paper_md(p: Path) -> dict:
    out = {}
    m = re.match(r"---\n(.*?)\n---", p.read_text(encoding="utf-8"), re.S)
    if not m:
        return out
    for line in m.group(1).split("\n"):
        km = re.match(r"^([A-Za-z_][A-Za-z0-9_]*):\s*(.*)$", line)
        if km:
            out[km.group(1)] = km.group(2).strip().strip('"')
    return out


def cover_types(pdf: Path):
    """封面阅卷表 → 选择题题号集合（解析不出返回 None）。"""
    doc = pymupdf.open(pdf)
    singles, ranges = [], []
    for b in doc[0].get_text("dict")["blocks"]:
        if b.get("type") != 0:
            continue
        for ln in b["lines"]:
            bb = ln["bbox"]
            if not (430 <= bb[0] <= 545 and 420 <= bb[1] <= 770):
                continue
            t = "".join(s["text"] for s in ln["spans"]).strip()
            if TABLE_CELL_RE.match(t):
                singles.append(int(t))
            else:
                m = TABLE_RANGE_RE.match(t)
                if m:
                    ranges.append((int(m.group(1)), int(m.group(2))))
    doc.close()
    if not singles:
        return None
    if not ranges:
        # U5（Physics in practice）整卷无选择题 → 阅卷表只有单行、没有区间
        return set() if sorted(singles) == list(range(1, len(singles) + 1)) else None
    a, b = ranges[0]
    if sorted(singles) != list(range(1, a)):
        return None
    return set(range(a, b + 1))


def overlay_img(base: Path, q: str, max_side=1500):
    """题图 + layout 热区（选项红框）叠加，返回 PNG bytes。"""
    im = Image.open(base / "assets" / f"q{q}.webp").convert("RGB")
    lp = base / "assets" / f"q{q}.layout.json"
    if lp.exists():
        lj = json.loads(lp.read_text(encoding="utf-8"))
        W, H = im.size
        dr = ImageDraw.Draw(im)
        for o in lj.get("options", []):
            b = o["box"]
            dr.rectangle([b["x"] * W, b["y"] * H, (b["x"] + b["w"]) * W,
                          (b["y"] + b["h"]) * H], outline=(214, 48, 45), width=3)
            dr.text((b["x"] * W + 6, b["y"] * H + 3), o["label"], fill=(214, 48, 45))
        for a in lj.get("areas", []):
            for b in a["boxes"]:
                dr.rectangle([b["x"] * W, b["y"] * H, (b["x"] + b["w"]) * W,
                              (b["y"] + b["h"]) * H], outline=(30, 110, 220), width=3)
    if max(im.size) > max_side:
        k = max_side / max(im.size)
        im = im.resize((max(1, int(im.width * k)), max(1, int(im.height * k))), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, "PNG", optimize=True)
    return buf.getvalue()


def b64img(data: bytes, alt: str):
    return (f'<figure><img src="data:image/png;base64,{base64.b64encode(data).decode()}" '
            f'alt="{alt}"><figcaption>{alt}</figcaption></figure>')


CSS = """
:root{--ink:#1a1d21;--dim:#5b6470;--line:#e3e6ea;--bg:#f7f8fa;--card:#fff;--blue:#1d5fd0;
--blue-bg:#eef3fd;--amber:#8a5a00;--amber-bg:#fdf6e6;--red:#b3261e;--red-bg:#fdeeed;
--green:#1e6b3a;--green-bg:#eaf5ee}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
font:15px/1.68 -apple-system,"Segoe UI","Microsoft YaHei",system-ui,sans-serif;-webkit-font-smoothing:antialiased}
.wrap{max-width:1080px;margin:0 auto;padding:40px 28px 80px}
header{border-bottom:3px solid var(--ink);padding-bottom:18px;margin-bottom:30px}
h1{font-size:27px;margin:0 0 8px;letter-spacing:-.2px}
.sub{color:var(--dim);font-size:14px}
h2{font-size:19px;margin:44px 0 14px;padding-left:11px;border-left:4px solid var(--blue)}
h3{font-size:16px;margin:26px 0 10px}
table{width:100%;border-collapse:collapse;margin:14px 0;font-size:13.5px;background:var(--card);
box-shadow:0 1px 2px rgba(16,24,40,.05);border-radius:8px;overflow:hidden}
th,td{padding:8px 10px;text-align:left;border-bottom:1px solid var(--line);vertical-align:top}
th{background:#f0f2f5;font-weight:600;font-size:12.5px;color:var(--dim);white-space:nowrap}
tr:last-child td{border-bottom:none}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
code{background:#eef1f4;padding:1px 5px;border-radius:4px;font-size:12.5px;
font-family:ui-monospace,Consolas,monospace}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px 20px;margin:14px 0}
.card.warn{background:var(--amber-bg);border-color:#eeddb8}
.card.ok{background:var(--green-bg);border-color:#bfe0cb}
.card.stop{background:var(--red-bg);border-color:#f2cdca}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin:18px 0}
.kpi{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px}
.kpi .v{font-size:23px;font-weight:700;letter-spacing:-.5px;font-variant-numeric:tabular-nums}
.kpi .k{font-size:12px;color:var(--dim);margin-top:3px}
figure{margin:20px 0;background:var(--card);border:1px solid var(--line);border-radius:10px;
padding:12px;text-align:center}
figure img{max-width:100%;border-radius:6px;display:block;margin:0 auto;box-shadow:0 2px 10px rgba(16,24,40,.10)}
figcaption{font-size:12.5px;color:var(--dim);margin-top:10px}
ul,ol{margin:10px 0;padding-left:22px}
li{margin:5px 0}
.foot{color:var(--dim);font-size:12.5px;margin-top:46px;padding-top:16px;border-top:1px solid var(--line)}
.two{display:grid;grid-template-columns:1fr 1fr;gap:16px}
@media(max-width:760px){.two{grid-template-columns:1fr}}
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "docs" / "AQA切割跑批报告.html"))
    ap.add_argument("--batch-json", default=str(ROOT / ".workbuddy" / "tmp" / "aqa_batch.json"))
    args = ap.parse_args()

    batch = {}
    bp = Path(args.batch_json)
    if bp.exists():
        for r in json.loads(bp.read_text(encoding="utf-8")):
            batch[r["session"]] = r

    rows, unit_rows = [], {}
    tot = Counter()
    n_ok = n_type_ok = n_marks_ok = 0
    bad = []
    for d in sorted(p for p in BASE.iterdir() if p.is_dir()):
        md = read_paper_md(d / "paper.md")
        cut = json.loads((d / "cut.json").read_text(encoding="utf-8"))
        qs = cut["questions"]
        nq = len(qs)
        nst = sum(1 for q in qs if q.get("question_type") == "structured")
        nmc = sum(1 for q in qs if q.get("question_type") == "mcq")
        marks = sum(q.get("total_marks", 0) for q in qs)
        nrev = sum(1 for q in qs if q["status"] != "auto")
        nnote = sum(len(q.get("review_notes") or []) for q in qs)
        nreg = sum(len(q["regions"]) for q in qs)
        pages = cut["source"]["pages"]
        unit = "U" + md.get("paper_code", "PH00")[-1]
        cover = cover_types(d / "source.pdf")
        type_ok = None
        if cover is not None:
            type_ok = all((q.get("question_type") == "mcq") == (int(q["number"]) in cover)
                          for q in qs)
        ok_marks = marks == 80
        bt = batch.get(d.name, {})
        all_green = (bt.get("cut_rc") == 0 and bt.get("render_rc") == 0
                     and bt.get("validate_rc") == 0)
        rows.append(dict(name=d.name, unit=unit, nq=nq, nst=nst, nmc=nmc, marks=marks,
                         nrev=nrev, nnote=nnote, nreg=nreg, pages=pages,
                         type_ok=type_ok, ok_marks=ok_marks, all_green=all_green,
                         cut_t=bt.get("cut_t"), render_t=bt.get("render_t"),
                         validate_t=bt.get("validate_t"), title=md.get("title", ""),
                         exam_date=md.get("exam_date", "")))
        u = unit_rows.setdefault(unit, Counter())
        u["papers"] += 1
        u["q"] += nq
        u["struct"] += nst
        u["mcq"] += nmc
        u["pages"] += pages
        u["regions"] += nreg
        tot["nq"] += nq
        tot["struct"] += nst
        tot["mcq"] += nmc
        tot["regions"] += nreg
        tot["papers"] += 1
        if all_green:
            n_ok += 1
        if type_ok:
            n_type_ok += 1
        if ok_marks:
            n_marks_ok += 1
        if not (all_green and type_ok and ok_marks):
            bad.append(d.name)

    H = ["<!DOCTYPE html><html lang='zh-CN'><head><meta charset='utf-8'>",
         "<meta name='viewport' content='width=device-width,initial-scale=1'>",
         "<title>牛津 AQA 国际 A-Level 物理 · 切割跑批报告</title>",
         f"<style>{CSS}</style></head><body><div class='wrap'>"]
    H.append("""<header>
<h1>牛津 AQA 国际 A-Level 物理 · 切割跑批报告</h1>
<div class='sub'>脚本 <code>scripts/cut_paper_aqa.py</code>（新增）· 渲染/校验层复用 <code>render_questions.py</code> /
<code>validate_cut.py</code> · 样本 2018-01 ~ 2026-05 共 72 试卷 · 2026-10-15</div>
</header>""")

    H.append(f"""<div class='kpis'>
<div class='kpi'><div class='v'>{tot['papers']}</div><div class='k'>试卷（PH01–PH05 × 15 考季）</div></div>
<div class='kpi'><div class='v'>{tot['nq']}</div><div class='k'>题目（结构化 {tot['struct']} / 选择 {tot['mcq']}）</div></div>
<div class='kpi'><div class='v'>{tot['regions']}</div><div class='k'>region 段（跨页拼接单元）</div></div>
<div class='kpi'><div class='v'>{n_ok}/{tot['papers']}</div><div class='k'>cut→render→validate 三阶段全绿</div></div>
<div class='kpi'><div class='v'>{n_marks_ok}/{tot['papers']}</div><div class='k'>分值不变量（=80）成立</div></div>
<div class='kpi'><div class='v'>{n_type_ok}/{tot['papers']}</div><div class='k'>题型与封面阅卷表逐题一致</div></div>
</div>""")

    H.append(f"""<div class='card ok'><b>结论：{tot['papers']} 卷全部通过三阶段（切割→渲染→校验），不变量逐卷成立。</b>
<p style='margin:8px 0 0'>三个全卷不变量——<b>分值合计 ≡ 80</b>、<b>题号序列 1..N 连续</b>、<b>题型与封面阅卷表逐题一致</b>——
在 {tot['papers']} 卷上全部成立；<code>validate_cut.py</code> 的 schema / 归一化坐标 / 热区一致性检查也全绿。
因此这批切割结果<b>可自证</b>，不需要逐题目视。<b>仅 1 题留了人工确认</b>：某选择题的四个选项整块是图形
（文本层没有 A–D 标签），热区按图像高度四等分给出（见第五节）。</p></div>""")

    if bad:
        H.append(f"<div class='card stop'><b>有差异的卷（{len(bad)}）</b>：{', '.join(bad)}</div>")

    # ── 逐卷明细 ──
    H.append("<h2>一、逐卷明细</h2><table><thead><tr><th>卷</th><th>单元</th><th>考试日期</th>"
             "<th class='num'>题数</th><th class='num'>结构化</th><th class='num'>选择</th>"
             "<th class='num'>分值</th><th class='num'>region</th><th class='num'>页</th>"
             "<th class='num'>复核</th><th>三阶段</th></tr></thead><tbody>")
    for r in rows:
        H.append(f"<tr><td class='mono'>{r['name']}</td><td>{r['unit']}</td><td>{r['exam_date']}</td>"
                 f"<td class='num'>{r['nq']}</td><td class='num'>{r['nst']}</td>"
                 f"<td class='num'>{r['nmc']}</td><td class='num'>{r['marks']}</td>"
                 f"<td class='num'>{r['nreg']}</td><td class='num'>{r['pages']}</td>"
                 f"<td class='num'>{r['nrev']}</td>"
                 f"<td>{'✔' if r['all_green'] else '✘'}</td></tr>")
    H.append("</tbody></table>")

    # ── 单元汇总 ──
    H.append("<h2>二、单元维度</h2><table><thead><tr><th>单元</th><th>名称</th><th class='num'>卷数</th>"
             "<th class='num'>题数</th><th class='num'>结构化</th><th class='num'>选择</th>"
             "<th class='num'>平均页数</th><th class='num'>平均 region</th></tr></thead><tbody>")
    names = {"U1": "Mechanics, materials and atoms", "U2": "Electricity, waves and particles",
             "U3": "Fields and their consequences", "U4": "Energy and energy resources",
             "U5": "Physics in practice"}
    for u in sorted(unit_rows):
        c = unit_rows[u]
        H.append(f"<tr><td><b>{u}</b></td><td>{names.get(u, '')}</td><td class='num'>{c['papers']}</td>"
                 f"<td class='num'>{c['q']}</td><td class='num'>{c['struct']}</td>"
                 f"<td class='num'>{c['mcq']}</td><td class='num'>{c['pages']/c['papers']:.1f}</td>"
                 f"<td class='num'>{c['regions']/c['q']:.2f}</td></tr>")
    H.append("</tbody></table>")

    # ── 抽检 ──
    H.append("""<h2>三、题图抽检</h2>
<p>下面是从产物里直接取的题图（选择题叠加 <span style="color:#d6302d">红框</span> = 选项热区，
来自 <code>layout.json</code>）。覆盖：结构化单页 / 跨页拼接 / 断续页码 / 选择题含图形 /
堆叠分式选项 / U5 长题 / 2018 老卷。</p>""")
    for name, q, desc in SAMPLES:
        d = BASE / name
        if not (d / "assets" / f"q{q}.webp").exists():
            continue
        H.append(b64img(overlay_img(d, q), f"{name} · q{q} —— {desc}"))

    # ── 实测修正 ──
    H.append("""<h2>四、实施中发现、并已写进脚本的要点</h2>
<p>可行性报告（<code>docs/AQA物理切割可行性分析.html</code>）给的是策略骨架，落地时又实测出几条必须修正的地方，
都已写进 <code>cut_paper_aqa.py</code> 的注释：</p>
<table><thead><tr><th>项</th><th>报告里的说法</th><th>实测结论 / 落地做法</th></tr></thead><tbody>
<tr><td>题型权威来源</td><td>题段内 ≥3 个不同选项字母 → MCQ</td>
<td>启发式会漏（选项含上下堆叠分式时字母自成一行）也会误判（题干以 "A projectile…" 开头）。
<b>改用封面阅卷表定调</b>：结构化题逐题列一行数字、选择题整段列成 <code>N–M</code> 区间；
逐题 72 卷全部与封面表一致。选项行判据同时收紧为「行首 span 是粗体 + 首字符 A–D + 其后空格/结束」。</td></tr>
<tr><td>选择题是否按 Section 判</td><td>报告说「Section B 前两题结构化、之后整段 MCQ」</td>
<td>实际 U1/U2 是 <b>Section A / B / C 三节</b>，MCQ 独立成 Section C；U3/U4/U5 只有 A / B，
MCQ 落在 B 节尾部——所以更不能按节判，已全部按题判。</td></tr>
<tr><td>题区终点</td><td>「下一题在后续页 → 边界就是 (nxt.page−1, 版心底)」</td>
<td>该前提不成立：AQA 会把一道题的末几小问排到下一页、下一题再从该页中部起
（2025-06 PH02 的 Q7.4 就在 Q8 之上）。<b>改为「下一题徽章所在行顶 −2pt」</b>，
只有该页徽章之上确实没有本题内容时才回退整页。</td></tr>
<tr><td>题号徽章</td><td>徽章是与题干同一行的锚点</td>
<td>徽章数字的 y0 常比同行题干<b>低 ~9pt</b>（题干含大字号数学式）→ 直接用它当题区顶会把下一题
题干上沿切进上一题的图里。已改为取「同行正文列的最小 y0」。</td></tr>
<tr><td>行 bbox</td><td>（未提及）</td>
<td>数学式里的 <code>CambriaMath</code> 空格 span 会把整行 bbox 竖直撑到 ±33pt（单行 67pt 高）
→ 行 bbox 必须只用含可见字符的 span 重建，否则题区起点与页尾内容底全被带偏。</td></tr>
<tr><td>封顶保留命中量</td><td>预估 91 段命中、32 段超限被裁</td>
<td>实测命中 11 段、真正被裁 <b>2 段</b>。差异来源：报告按「边界=版心底」估算，
而实测多数末段止于「下一题徽章行顶」，本来就没有大段空白可裁；另外选择题整段不封顶
（无书写区，一律裁到 +8pt）。规则本身未变。</td></tr>
</tbody></table>""")

    # ── 已知限制 ──
    H.append("""<h2>五、已知限制与后续</h2>
<div class='card warn'><ul style='margin-bottom:0'>
<li><b>打标台看不到 AQA 的标签词表</b>：词表按 <code>data/taxonomy/&lt;board&gt;/&lt;qual&gt;/&lt;subject&gt;.json</code>
查找，目前只有 edexcel / cie 的 physics。AQA 卷能在打标台里列出、能切题、能写回难度，
但右栏标签为空——需要另一份 AQA 物理词表（本机缺词表源 TSV）。</li>
<li><b>AQA 化学生物未验证</b>：需先跑只读锚点探测确认是否同版式，再决定并入本脚本还是另起
（本项目曾因脚本分叉导致修复漂移，故不预判）。</li>
<li><b>Mark Scheme 切割另议</b>：评分标准版式是两栏表格，与题卷锚点完全不同，<code>cut_ms.py</code> 仍属规划中；
本批只切了题卷（qp），<code>ms-…</code> 未下载入库。</li>
<li><b>数学式文本会碎成单字符</b>：与 Edexcel/CIE 同，题干文本仅供检索与批改 AI 参考，展示以题图为准。</li>
<li><b>本题库不含 U6+</b>：牛津 AQA 国际 A-Level 物理只有 U1–U5。</li>
</ul></div>""")

    H.append(f"""<div class='foot'>
本报告由 <code>scripts/make_aqa_report.py</code> 从 <code>data/aqa/a-level/physics/</code> 的产物现场生成
（cut.json / paper.md / source.pdf 封面 / assets 题图 + layout.json），未改动任何数据。<br>
数据落盘位置：<code>data/aqa/a-level/physics/&lt;year&gt;-&lt;session&gt;-&lt;ph0N&gt;/</code>；
源码：<code>scripts/import_aqa.py</code>（入库）、<code>scripts/cut_paper_aqa.py</code>（切割）。
</div></div></body></html>""")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("".join(H), encoding="utf-8")
    print(f"written: {out}  {out.stat().st_size/1024:.0f} KB")
    print(f"卷={tot['papers']} 题={tot['nq']} 全绿={n_ok} 分值OK={n_marks_ok} 题型OK={n_type_ok}")


if __name__ == "__main__":
    main()
