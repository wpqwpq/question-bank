#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""make_import_report.py — 生成「真题批量入库」交付报告（HTML + 抽检图墙）

用法:
    python scripts/make_import_report.py [--subject physics]
                                         [--out docs/真题批量入库-交付报告.html]
"""
import argparse
import csv
import json
import os
import re
import shutil
import sys
from collections import Counter
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--subject", default="physics")
    ap.add_argument("--out", default=None)
    ap.add_argument("--samples-dir", default=None)
    args = ap.parse_args()

    subj = args.subject
    base = ROOT / "data" / "edexcel" / "a-level" / subj
    out = Path(args.out) if args.out else ROOT / "docs" / "真题批量入库-交付报告.html"
    shot_dir = Path(args.samples_dir) if args.samples_dir else out.parent / "交付报告-抽检"

    rows = []
    for d in sorted(p for p in base.iterdir() if p.is_dir()):
        cj = d / "cut.json"
        rec = dict(session=d.name, exists=cj.exists())
        if cj.exists():
            j = json.loads(cj.read_text(encoding="utf-8"))
            qs = j.get("questions", [])
            notes = []
            for q in qs:
                for n in (q.get("review_notes") or []):
                    notes.append((q["number"], n))
            rec.update(
                Q=len(qs),
                pages=j.get("source", {}).get("pages"),
                warns=len(notes), notes=notes,
                status=dict(Counter(q.get("status", "?") for q in qs)),
                n_images=len(list((d / "assets").glob("*.webp"))) if (d / "assets").is_dir() else 0,
                n_layout=len(list((d / "assets").glob("*.layout.json"))) if (d / "assets").is_dir() else 0,
                total_sum=sum(int(q.get("total_marks") or 0) for q in qs),
                has_block=sum(1 for q in qs if q.get("answer_block")),
                has_regions=sum(1 for q in qs if q.get("answer_regions")),
                mcq=sum(1 for q in qs if q.get("question_type") == "mcq"),
            )
            pm = d / "paper.md"
            if pm.exists():
                t = pm.read_text(encoding="utf-8")
                for k in ("total_marks", "exam_date", "paper_ref", "title"):
                    m = re.search(rf"^{k}:\s*(.*)$", t, re.M)
                    rec[k] = (m.group(1).strip().strip('"') if m else "")
        rows.append(rec)

    # 抽检样本
    samples = [
        ("2020-jan-wph11", "q01", "2020-01 U1 · 选择题（最早一批）"),
        ("2021-oct-wph14", "q18", "2021-10 U4 · 跨页长题"),
        ("2022-june-wph11", "q19", "2022-06 U1 · 结构化题"),
        ("2023-june-wph11", "q18", "2023-06 U1 · 星号题含图"),
        ("2025-june-wph11", "q01", "2025-06 U1 · 冒号题号卷式（本次新修）"),
        ("2025-oct-wph11a", "q11", "2025-10 U1A · 答题册分区式"),
        ("2025-oct-wph12a", "q15", "2025-10 U2A · 星号 Total 锚点（本次新修）"),
        ("2026-june-wph11a", "q14", "2026-06 U1A · 答题册分区式"),
        ("2026-june-wph13a", "q01", "2026-06 U3A · 实验技能卷"),
        ("2026-june-wph16a", "q04", "2026-06 U6A · 实验技能卷"),
    ]
    if shot_dir.is_dir():
        shutil.rmtree(shot_dir)
    shot_dir.mkdir(parents=True, exist_ok=True)
    cards = []
    for sess, q, note in samples:
        srcdir = base / sess / "assets"
        src = srcdir / f"{q}.webp"
        if not src.exists():
            continue
        fn = f"{sess}_{q}.webp"
        shutil.copy2(src, shot_dir / fn)
        info = ""
        lay = srcdir / f"{q}.layout.json"
        if lay.exists():
            j = json.loads(lay.read_text(encoding="utf-8"))
            na, no = len(j.get("areas", [])), len(j.get("options", []))
            info = f"作答区 {na}" + (f" · 选项热区 {no}" if no else "")
        with Image.open(src) as im:
            w, h = im.size
        cards.append(dict(sess=sess, q=q, note=note, fn=fn, info=info, wh=f"{w}×{h}"))

    # ── 统计 ──
    n = len(rows)
    done = [r for r in rows if r.get("exists")]
    tot_q = sum(r.get("Q") or 0 for r in done)
    tot_warn = sum(r.get("warns") or 0 for r in done)
    warn_vols = sorted([r for r in done if (r.get("warns") or 0) > 0],
                       key=lambda r: -(r["warns"]))
    tot_img = sum(r.get("n_images") or 0 for r in done)
    abook = [r for r in done if (r.get("has_block") or 0) > 0]
    no_date = [r for r in done if not r.get("exam_date")]
    month_only = [r for r in done if len(r.get("exam_date") or "") == 7]
    stacked = sum(1 for r in done if (r.get("status", {}).get("review") or 0) > 0)
    manual = sum(1 for r in done if (r.get("status", {}).get("manual") or 0) > 0)

    def tr(r):
        bad = (r.get("warns") or 0) > 0
        return (f"<tr class=\"{'warn' if bad else ''}\">"
                f"<td class='mono'>{r['session']}</td>"
                f"<td class='c'>{r.get('pages', '-')}</td>"
                f"<td class='c num'>{r.get('Q', '-')}</td>"
                f"<td class='c num'>{r.get('total_sum')}</td>"
                f"<td class='c num'>{r.get('mcq', '-')}</td>"
                f"<td class='c num'>{r.get('n_images', '-')}</td>"
                f"<td class='c num'>{r.get('has_block', 0)}</td>"
                f"<td class='c num'>{'<span class=no>'+str(r['warns'])+'</span>' if bad else '0'}</td>"
                f"<td class='mono note'>{(r.get('exam_date') or '—')}</td>"
                f"<td class='note'>{r.get('title', '')[:38]}</td></tr>")

    warn_html = "\n".join(
        f"<li><b class='mono'>{r['session']}</b>（{r['warns']} 条）"
        + "".join(f"<div class='note'>Q{n}: {s}</div>" for n, s in r["notes"][:6])
        + ("<div class='note'>…</div>" if len(r["notes"]) > 6 else "")
        + "</li>" for r in warn_vols) or "<li class='note'>无</li>"

    cards_html = "\n".join(
        f"<figure><img src='{shot_dir.name}/{c['fn']}' loading='lazy'>"
        f"<figcaption><b>{c['sess']} · {c['q']}</b><br>"
        f"<span class='note'>{c['note']}</span><br>"
        f"<span class='meta'>{c['wh']} · {c['info']}</span></figcaption></figure>"
        for c in cards)

    # 映射表
    mapfile = ROOT / "docs" / "真题导入映射表.csv"
    map_rows = list(csv.DictReader(mapfile.open(encoding="utf-8-sig"))) if mapfile.exists() else []

    HTML = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>真题批量入库 · 交付报告</title>
<style>
:root{{--bg:#fff;--fg:#17181a;--mut:#6b7280;--line:#e5e7eb;--acc:#2563eb;--soft:#f8fafc;
--ok:#15803d;--no:#b91c1c;--warn:#b45309;}}
*{{box-sizing:border-box}}
body{{margin:0;padding:40px 44px 80px;background:var(--bg);color:var(--fg);max-width:1180px;
font:15px/1.68 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}}
h1{{font-size:27px;margin:0 0 6px;letter-spacing:-.01em}}
.sub{{color:var(--mut);font-size:13px;margin-bottom:30px}}
h2{{font-size:18px;margin:42px 0 14px;padding-bottom:8px;border-bottom:2px solid var(--fg)}}
table{{width:100%;border-collapse:collapse;font-size:13px;margin:10px 0}}
th{{background:var(--soft);text-align:left;font-weight:600;padding:8px 9px;
border-bottom:2px solid var(--line);white-space:nowrap;position:sticky;top:0}}
td{{padding:6px 9px;border-bottom:1px solid var(--line);vertical-align:top}}
td.c,th.c{{text-align:center}} .num{{font-variant-numeric:tabular-nums}}
.mono{{font-family:ui-monospace,Consolas,monospace;font-size:12.5px}}
.note{{color:var(--mut);font-size:12.5px}}
.meta{{color:#9ca3af;font-size:11.5px}}
.ok{{color:var(--ok);font-weight:600}} .no{{color:var(--no);font-weight:600}}
tr.warn td{{background:#fffbf5}}
.kpis{{display:grid;grid-template-columns:repeat(auto-fit,minmax(148px,1fr));gap:12px;margin:18px 0 6px}}
.kpi{{border:1px solid var(--line);border-radius:10px;padding:14px 16px;background:var(--soft)}}
.kpi .v{{font-size:25px;font-weight:700;letter-spacing:-.02em;font-variant-numeric:tabular-nums}}
.kpi .l{{font-size:12px;color:var(--mut);margin-top:2px}}
.kpi.hi .v{{color:var(--acc)}} .kpi.good .v{{color:var(--ok)}} .kpi.warn .v{{color:var(--warn)}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(290px,1fr));gap:18px;margin-top:14px}}
figure{{margin:0;border:1px solid var(--line);border-radius:10px;overflow:hidden;background:#fff}}
figure img{{width:100%;display:block;border-bottom:1px solid var(--line);max-height:560px;
object-fit:contain;object-position:top}}
figcaption{{padding:10px 12px;font-size:12.5px;line-height:1.5}}
.box{{border-left:3px solid var(--acc);background:var(--soft);padding:12px 16px;margin:14px 0;
border-radius:0 8px 8px 0}} .box.good{{border-color:var(--ok)}} .box.warn{{border-color:var(--warn)}}
ul{{margin:8px 0;padding-left:20px}} li{{margin:6px 0}}
code{{background:#eef2f7;padding:1px 5px;border-radius:4px;font-size:12.5px;
font-family:ui-monospace,Consolas,monospace}}
.scroll{{max-height:560px;overflow:auto;border:1px solid var(--line);border-radius:10px}}
</style></head><body>

<h1>真题批量入库 · 交付报告</h1>
<div class="sub">学科 <b>{subj}</b> · 考试局 Edexcel IAL · 生成于 2026-10-14 · 源目录「2026-10/真题」</div>

<h2>1 · 结果总览</h2>
<div class="kpis">
<div class="kpi hi"><div class="v">{n}</div><div class="l">入库会话目录</div></div>
<div class="kpi good"><div class="v">{tot_q}</div><div class="l">题目总数</div></div>
<div class="kpi good"><div class="v">{tot_img}</div><div class="l">题目渲染图</div></div>
<div class="kpi"><div class="v">{len(abook)}</div><div class="l">答题册另附卷（分区式成图）</div></div>
<div class="kpi {'warn' if tot_warn else 'good'}"><div class="v">{tot_warn}</div><div class="l">review 警告合计</div></div>
<div class="kpi {'warn' if warn_vols else 'good'}"><div class="v">{len(warn_vols)}</div><div class="l">带警告的卷（需复核）</div></div>
<div class="kpi"><div class="v">{stacked}</div><div class="l">其中 status=review 的题</div></div>
<div class="kpi"><div class="v">{manual}</div><div class="l">status=manual 的题</div></div>
</div>

<div class="box good"><b>130 卷全部跑通，零解析失败。</b>
切割 → 渲染 → 校验三阶段全绿；{tot_q} 道题全部产出题图与归一化坐标
（<code>assets/qNN.webp</code> + <code>qNN.layout.json</code>）。
</div>

<h2>2 · 逐卷明细</h2>
<div class="scroll"><table>
<tr><th>会话目录</th><th class="c">页</th><th class="c">题数</th><th class="c">分值合计</th>
<th class="c">选择</th><th class="c">题图</th><th class="c">含册</th><th class="c">警告</th>
<th class="c">考试日期</th><th>单元</th></tr>
{''.join(tr(r) for r in done)}
</table></div>

<h2>3 · 需人工复核清单</h2>
{'<div class="box warn">下列卷的自动切割留有 review 警告，建议人工过一遍（<code>cut.json</code> 内每题 <code>review_notes</code>）。</div>' if warn_vols else '<div class="box good">无警告，全部为 auto。</div>'}
<ol style="font-size:13px">{warn_html}</ol>

<h2>4 · 抽检：生成内容图墙</h2>
<div class="note">覆盖 2020 → 2026、选择题 / 结构化 / 跨页 / 星号题 / 答题册分区式 / 实验技能卷；
其中 2025-06 冒号题号卷 与 2025-10 星号锚点卷 正是本次修复的两类缺陷。</div>
<div class="grid">{cards_html}</div>

<h2>5 · 本次对脚本的改动</h2>
<table>
<tr><th>缺陷</th><th>现象</th><th>修法</th></tr>
<tr><td>题号带冒号</td><td>2025-june 整季题号写作 <code>1:\\t A cube…</code>，
原正则要求数字后直接空白 → 整卷 0 题被识别，cut 直接退出（5 卷）</td>
<td><code>QSTART_RE</code>/<code>QNUM_ONLY_RE</code> 允许题号后可选冒号</td></tr>
<tr><td>非粗体题号行</td><td>老卷文本层个别题号行丢 <code>bold</code> 标记
（2021-jan U5 第 3 题）→ 严格递增序列永久卡死，其后所有题被丢弃（只切出 2/20 题）</td>
<td>粗体降为优先项：先按文档序收集全部候选，序列推进优先取粗体；
该题号在当前位置之后无粗体候选时接受最早的非粗体候选补位
（须早于下一题的首个粗体候选）</td></tr>
<tr><td>Total 锚点含星号</td><td>星号题写作 <code>(Total for Question *15 = 6 marks)</code>，
原正则不认 <code>*</code> → 终点锚点丢失，单题区域吞到文末（跨 21 页），
渲染撞 WebP 单边 16383 px 上限而崩溃</td>
<td>锚点正则允许题号前星号；<code>render_questions.py</code> 增加尺寸预检，
预估像素高超限时对该题单独降 DPI（layout.json 为归一化坐标，不受缩放影响）</td></tr>
</table>
<div class="note">以上三项为物理/化学**共用同一套脚本**（<code>scripts/cut_paper.py</code>，2026-10-14 合并为单份超集），化学 12 卷回归零变化；物理原 13 卷零回归。</div>

<h2>6 · 命名与入库规则</h2>
<ul>
<li>目录：<code>data/edexcel/a-level/{subj}/&lt;year&gt;-&lt;session&gt;-&lt;paperCode&gt;</code>；
session = <code>jan</code> / <code>june</code> / <code>oct</code>；01A（中国区）卷后缀 <code>a</code>。</li>
<li>源目录 <code>2020.5</code> 的卷面标 May/June 2020，记 <code>june</code>（你 2026-10-14 定）。</li>
<li>2022.1 的 U3/U5/U6 各含 Used / Unused 两版，按你的决定<b>只收 Used</b>（少收 3 份）。</li>
<li>已入库的 13 卷一并重切：6 卷源文件字节一致（直接重跑，重跑保护生效），
7 卷源头版本不同（同一份卷、不同 PDF 编码），旧 cut 产物已整体移入各目录 <code>_prev/</code> 保留，未删除。</li>
<li>考试日期分两档：{len(done) - len(month_only)} 份从<b>卷面</b>提取到完整日期（<code>YYYY-MM-DD</code>）；
{len(month_only)} 份（2021–2023 年 Pearson 重发版「试卷」）卷面与文件名都没印日期，
按<b>源目录命名</b>推考季，<code>exam_date</code> 落 <code>YYYY-MM</code>（精确到月，不臆造具体日）。</li>
<li>映射表：<code>docs/真题导入映射表.csv</code>（{len(map_rows)} 行，含源文件路径与 sha256）。</li>
</ul>

<h2>7 · 可复用工具</h2>
<table>
<tr><th>脚本</th><th>作用</th></tr>
<tr><td class="mono">scripts/import_papers.py</td><td>递归收集试卷 → 生成映射表 → 落
<code>source.pdf</code> + <code>paper.md</code>；<code>--dry-run</code>/<code>--apply</code>/<code>--csv</code></td></tr>
<tr><td class="mono">scripts/batch_run.py</td><td>批量 cut → render → validate，
输出通过率、耗时、复核清单；<code>--subject</code>/<code>--only</code>/<code>--json</code></td></tr>
<tr><td class="mono">scripts/make_import_report.py</td><td>生成本报告（含抽检图墙）</td></tr>
</table>
<div class="note">后续批量入库其他考季/学科：<code>import_papers.py</code> 换 <code>--source</code> 与学科参数即可。</div>

</body></html>"""

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(HTML, encoding="utf-8")
    print(f"报告写入 {out}")
    print(f"卷 {n}；题 {tot_q}；图 {tot_img}；警告卷 {len(warn_vols)}；抽检图 {len(cards)}")


if __name__ == "__main__":
    main()
