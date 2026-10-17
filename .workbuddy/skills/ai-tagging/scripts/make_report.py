#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""由 decisions.json 生成 AI 打标 HTML 报告（含主标签标记）。

用法：
    python make_report.py decisions.json --out <项目根>/docs/NN-AI打标报告-<board>-<卷>.html
    （--root 指定项目根，默认 D:/Tasks/questionBank；中文标签名从 data/taxonomy 现读）

主标签展示：
    · 每个单元的主标签 chip 加深底色 + 前置「主」小标；
    · 单标签单元自动视为主标签（与后端 effective_primary 同口径）；
    · 若同名 sidecar（`<decisions>.result.json`，由 apply_tags.py --apply 产出）里记录了
      --auto-primary 兜底，则该题整行标黄并在统计卡计数。

decisions.json 格式见 references/decisions-format.md。
"""
import argparse
import json
from pathlib import Path

DEFAULT_ROOT = "D:/Tasks/questionBank"


def load_zh(root: Path) -> dict:
    """全库 id -> 中文名（跨 board 合并；_prev 备份目录跳过）。"""
    zh = {}
    tax = root / "data" / "taxonomy"
    if not tax.is_dir():
        return zh
    for f in tax.rglob("*.json"):
        if "_prev" in f.parts:
            continue
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        for k, v in d.items():
            if k.startswith("$") or not isinstance(v, dict):
                continue
            for t in v.get("tags") or []:
                zh[t["id"]] = t["zh"]
    return zh


def resolve_primary(tags, given):
    """与后端 store.effective_primary 同口径：显式值 ∈ tags 才用，否则单标签自动接任。"""
    if given and given in (tags or []):
        return given
    return tags[0] if tags and len(tags) == 1 else None


def chips(tags, zh, low=False, primary=None):
    out = []
    for t in tags:
        cls = "chip"
        if low:
            cls += " low"
        if t == primary:
            cls += " pri"
        short = t.split("-", 1)[1] if "-" in t else t
        mark = '<i class="pri">主</i>' if t == primary else ""
        out.append(f'<span class="{cls}">{mark}{zh.get(t, t)}<code>{short}</code></span>')
    return "".join(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("decisions")
    ap.add_argument("--out", required=True)
    ap.add_argument("--root", default=DEFAULT_ROOT)
    a = ap.parse_args()

    dec = json.loads(Path(a.decisions).read_text(encoding="utf-8"))
    zh = load_zh(Path(a.root))

    # apply_tags.py --apply 的 sidecar：记录 --auto-primary 兜底选中的主标签（整题的 label 为 None）
    auto_map = {}
    side = Path(a.decisions).with_suffix(".result.json")
    if side.exists():
        try:
            for x in (json.loads(side.read_text(encoding="utf-8")).get("auto_primary") or []):
                lb = x.get("label")
                auto_map[(str(x.get("n")), None if lb is None else str(lb))] = x.get("picked")
        except Exception:  # noqa: BLE001
            auto_map = {}

    rows, n_ok, n_sub, n_low, n_skip, n_img, n_auto, n_pri = [], 0, 0, 0, 0, 0, 0, 0
    for it in dec["items"]:
        n, marks = it.get("n"), it.get("marks", "—")
        if it.get("skip_reason"):
            n_skip += 1
            if it.get("skip_kind") == "image":
                n_img += 1
            tag = "疑似错图" if it.get("skip_kind") == "image" else "无合适标签"
            rows.append(
                f'<tr class="skiprow"><td class="num">{n}</td><td class="mk">{marks}</td>'
                f'<td colspan="2"><b>{tag}</b>　<span class="muted">{it["skip_reason"]}</span></td>'
                f'<td class="why">{it.get("why", "")}</td></tr>')
            continue
        n_ok += 1
        row_autos = [v for (kn, _kl), v in auto_map.items() if kn == str(n)]
        auto_hit = bool(row_autos)
        if auto_hit:
            n_auto += 1
        rowcls = ' class="autorow"' if auto_hit else ""
        auto_note = ('<div class="autohint">以下主标签为兜底取值（tags[0]），建议人工复看：'
                     + "、".join(row_autos) + "</div>") if auto_hit else ""
        subs = it.get("subs")
        if subs:
            n_sub += len(subs)
            cells, whys = [], []
            for s in subs:
                if s.get("low"):
                    n_low += 1
                pri = resolve_primary(s.get("tags"),
                                      s.get("primary_tag") or auto_map.get((str(n), str(s["label"]))))
                if pri:
                    n_pri += 1
                lab = it.get("sub_prefix", "")
                inner = (chips(s["tags"], zh, s.get("low", False), pri) if s.get("tags")
                         else '<span class="blank">留空 · 无合适标签</span>')
                cells.append(f'<div class="sub"><em>{lab}{s["label"]}</em> {inner}</div>')
                whys.append(s.get("why", ""))
            rows.append(f'<tr{rowcls}><td class="num">{n}</td><td class="mk">{marks}</td>'
                        f'<td class="lg">{len(subs)} 小问</td>'
                        f'<td>{"".join(cells)}{auto_note}</td>'
                        f'<td class="why">{" / ".join(w for w in whys if w)}</td></tr>')
        else:
            n_sub += 1
            if it.get("low"):
                n_low += 1
            tags = it.get("tags") or []
            pri = resolve_primary(tags, it.get("primary_tag") or auto_map.get((str(n), None)))
            if pri:
                n_pri += 1
            rows.append(f'<tr{rowcls}><td class="num">{n}</td><td class="mk">{marks}</td>'
                        f'<td class="lg">整题</td>'
                        f'<td>{chips(tags, zh, it.get("low", False), pri)}{auto_note}</td>'
                        f'<td class="why">{it.get("why", "")}</td></tr>')

    # 补充说明块
    blocks = []
    for note in dec.get("notes", []):
        kind = note.get("kind", "info")
        cls = {"ok": "tip", "warn": "tip warn", "info": "tip gray"}.get(kind, "tip gray")
        blocks.append(f'<div class="{cls}"><b>{note["title"]}</b>{note.get("html", "")}</div>')
    gaps = dec.get("vocabulary_gaps") or []
    if gaps:
        body = "".join(f'<li><b>{g.get("point","")}</b>（{g.get("where","")}）：{g.get("detail","")}</li>'
                       for g in gaps)
        blocks.append(f'<div class="tip warn"><b>词表缺口（需你决定是否补标签）</b>'
                      f'<ul class="gaps">{body}</ul></div>')

    html = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>{dec.get("title", dec["paper"])} · AI 打标报告</title>
<style>
*{{box-sizing:border-box}}
body{{margin:0;padding:28px 32px;background:#f7f8fa;color:#1f2430;
 font:14px/1.6 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}}
h1{{margin:0 0 4px;font-size:20px}} h2{{margin:26px 0 10px;font-size:16px}}
.sub2{{color:#6b7280;font-size:13px;margin-bottom:18px}}
.cards{{display:flex;gap:12px;margin-bottom:20px;flex-wrap:wrap}}
.card{{background:#fff;border:1px solid #e5e7eb;border-radius:10px;padding:12px 16px;min-width:112px}}
.card b{{display:block;font-size:22px;line-height:1.3;color:#111827}}
.card span{{font-size:12px;color:#6b7280}}
.card.ok b{{color:#047857}} .card.warn b{{color:#b45309}}
table{{width:100%;border-collapse:collapse;background:#fff;border:1px solid #e5e7eb;
 border-radius:10px;overflow:hidden;font-size:13px;margin-bottom:8px}}
th{{background:#f3f4f6;text-align:left;padding:9px 10px;font-weight:600;color:#374151;
 border-bottom:1px solid #e5e7eb;white-space:nowrap}}
th .hint{{font-weight:400;color:#9ca3af;font-size:11.5px}}
td{{padding:8px 10px;border-bottom:1px solid #f1f2f4;vertical-align:top}}
tr:last-child td{{border-bottom:none}}
td.num{{text-align:center;font-weight:600;color:#111827;width:46px}}
td.mk{{text-align:center;color:#6b7280;width:46px}}
td.lg{{color:#9ca3af;width:64px;white-space:nowrap;font-size:12px}}
td.why{{color:#4b5563;width:32%;font-size:12.5px}}
.skiprow td{{background:#fafafa}} .skiprow .num{{color:#b45309}}
tr.autorow td{{background:#fffdf4}}
tr.autorow td.num{{box-shadow:inset 3px 0 0 #f59e0b}}
.autohint{{margin-top:4px;color:#b45309;font-size:11.5px}}
.muted{{color:#9ca3af;font-size:12px}}
.sub{{margin:2px 0}} .sub em{{font-style:normal;color:#6b7280;margin-right:3px;font-size:12px}}
.chip{{display:inline-flex;align-items:center;gap:5px;background:#eff6ff;border:1px solid #bfdbfe;
 color:#1d4ed8;border-radius:6px;padding:1px 7px;margin:1px 4px 1px 0;white-space:nowrap;font-size:12.5px}}
.chip.low{{background:#fffbeb;border-color:#fcd34d;color:#92400e}}
.chip.pri{{background:#dbeafe;border-color:#93c5fd;color:#1e40af;font-weight:500}}
.chip i.pri{{font-style:normal;border:1px solid #2563eb;color:#1d4ed8;background:#fff;
 border-radius:3px;font-size:9.5px;line-height:1.5;padding:0 3px}}
.chip code{{font:10.5px ui-monospace,Consolas,monospace;color:#64748b}}
.chip.low code{{color:#b45309}}
.chip.pri code{{color:#1d4ed8}}
.blank{{display:inline-block;background:#f3f4f6;border:1px dashed #d1d5db;color:#9ca3af;
 border-radius:6px;padding:1px 8px;font-size:12px}}
.tip{{background:#ecfdf5;border:1px solid #a7f3d0;border-radius:8px;padding:11px 14px;
 font-size:13px;color:#065f46;margin-top:10px}}
.tip b{{color:#047857}}
.tip.warn{{background:#fffbeb;border-color:#fde68a;color:#78350f}} .tip.warn b{{color:#92400e}}
.tip.gray{{background:#f9fafb;border-color:#e5e7eb;color:#4b5563}} .tip.gray b{{color:#374151}}
ul.gaps{{margin:6px 0 0;padding-left:18px}} ul.gaps li{{margin:2px 0}}
.foot{{color:#9ca3af;font-size:12px;margin-top:18px;text-align:center}}
</style></head><body>
<h1>{dec.get("title", dec["paper"])} · AI 打标报告</h1>
<div class="sub2">{dec.get("subtitle", "")}</div>

<div class="cards">
  <div class="card"><b>{len(dec["items"])}</b><span>总题数</span></div>
  <div class="card ok"><b>{n_ok}</b><span>成功打标</span></div>
  <div class="card"><b>{n_sub}</b><span>标注单元</span></div>
  <div class="card ok"><b>{n_pri}</b><span>主标签已定</span></div>
  <div class="card{' warn' if n_auto else ''}"><b>{n_auto}</b><span>主标签兜底</span></div>
  <div class="card{' warn' if n_skip else ''}"><b>{n_skip}</b><span>跳过</span></div>
  <div class="card{' warn' if n_low else ''}"><b>{n_low}</b><span>低置信</span></div>
  <div class="card{' warn' if n_img else ''}"><b>{n_img}</b><span>疑似错图</span></div>
</div>

<h2>逐题结果</h2>
<table><thead><tr><th>题</th><th>分</th><th>结构</th>
<th>知识点标签　<span class="hint">带「主」标记的是主标签（每单元恰好一个）</span></th>
<th>依据</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table>

{''.join(blocks)}

<div class="foot">由 ai-tagging 技能生成 · 已标知识点 + 主标签（难度未标，按约定留给人工）·
标签与主标签可在打标台逐条修改</div>
</body></html>"""

    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8", newline="") as f:
        f.write(html)
    print(f"✅ 报告：{out}（{len(html)} chars）")
    print(f"   成功 {n_ok} / 单元 {n_sub} / 主标签 {n_pri}（兜底 {n_auto}）"
          f" / 跳过 {n_skip} / 低置信 {n_low} / 疑似错图 {n_img}")


if __name__ == "__main__":
    main()
