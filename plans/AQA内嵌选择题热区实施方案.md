# AQA 内嵌选择题（Tick one box）热区 · 实施方案

> ### ⚠️ 2026-10-16 决策变更：**本方案不予实施**
>
> **新决策**（培琪）：整题级 MCQ（有 A/B/C/D 的）**继续保有热区不变**；化学这种内嵌形态**只要能标记出答题区域即可**，不挂热区。
>
> **核查结论：连「标记作答区域」也不需要新增任何东西。**
>
> | 曾经担心的点 | 实际情况 |
> |---|---|
> | Tick 小问会不会没被登记？ | **已登记**。`subquestions` 的小问识别按徽章 `0 N . M` 捞，**独立于题型**（见 `cut_paper_aqa.py` 小问段），structured 题里的 `.3` 照常入库 |
> | 能不能单独标这个小问？ | **能**。打标台 10-16 已为 AQA/CIE 开放「按小问打标不切子图」，小问级标注可存 |
> | 题图包含那几个打勾框吗？ | **包含**。题级 `regions` 覆盖整题，框在图上 |
> | `layout.areas` 呢？ | **AQA 从来没有，也不需要**。`areas` 的 schema 定义写死了「答题册另附卷的结构化题」，AQA 脚本头注释第 34 行明说「无答题册 → answer_regions / answer_block / 分区式拼接全不产生」 |
>
> **为什么「放弃热区」并不能省掉识别工作**（留给将来的提醒）：找出作答区与找出热区的**第一步完全相同**——都要读 `page.get_drawings()` 认那 4 条发丝线拼的框。放弃热区只省掉三样：按 y 升序编 A/B/C/D、一题两处的分组歧义、学习机侧消费选项。识别框 / 归属小问 / 改 schema / 改 layout 换算 / 跑回归，一个都不省。
>
> **本文档为何保留**：§5 的打勾框实测判据（框宽恒为 28.80pt、线宽随年份 0.24→0.48、形态 A 独立框列 / 形态 B 表格单元格）与 §7 陷阱清单是**实测数据**，将来若确需在题图上框出作答位置或恢复热区，这是唯一的现成依据。
>
> **遗留小尾巴（已知，不必处理）**：`cut_paper_aqa.py` 的 `elif opt_rows:` 分支仍会对这 ~21 题写一条 `review_notes`「封面表判为结构化题，但题段内检出选项行，请复核题型」。按本决策人工复核时**直接放行**即可——该分支只追加提示，**不影响 `status`**，题仍为 auto。
>
> 下方为 2026-10-15 原始方案，**未实施，勿照做**。

> **状态**：~~已决策，待实施~~ → **已作废**（2026-10-16）
> **原决策**：采用**方案 A** —— 选项热区挂到**小问级**（`subquestions[].option_hotzones`），而非题级。
> **范围**：`scripts/cut_paper_aqa.py`（切割）+ `scripts/render_questions.py`（渲染）+ 两个 schema；波及打标台与学习机的选项解析。
> **不在范围**：P1（两行徽章）、P3（热区上沿越界）已于 2026-10-15 修复并全量回归，本方案不涉及。

---

## 1. 问题是什么

AQA 化学卷（CH01–CH05）里有一种物理卷没有的形态：**某道结构化题的某个小问，本身就是一道选择题**。

题面长这样：

```
0 4 . 3   Which graph shows how the rate changes as ... ?
          Tick (✔) one box.                                  [1 mark]
          ┌──┐
          │  │  Graph A
          └──┘
          ┌──┐
          │  │  Graph B
          └──┘
```

三个特点让现有脚本**静默跳过**：

| # | 特征 | 后果 |
|---|---|---|
| 1 | 文本层**没有 A/B/C/D 字母**（与 Section A 的整节选择题相反） | 脚本靠"行首粗体 A–D"识别选项 → 一个都认不出 |
| 2 | 打勾框是**矢量发丝线**画的，不是文字 | 不读 `get_drawings()` 就完全看不见 |
| 3 | 题目被封面表判为 `structured` | 现流程只在 `question_type == "mcq"` 时产热区 |

代码上落在 `cut_paper_aqa.py` 的 `main()`：

```python
if qtype == "mcq":
    ... 产 option_hotzones ...
elif opt_rows:                      # ← 就是这里
    notes.append("封面表判为结构化题，但题段内检出选项行，请复核")
    # opt_rows 被丢掉，没有任何热区产出，也没有落盘
```

后果：打标台/学习机上，这些题的选项**点不了**。

---

## 2. 当前实测数据（2026-10-15 基线）

抽样范围：`data/aqa/a-level/chemistry/` 下 **15 卷**（物理 72 卷未发现此形态）。

| 指标 | 数值 | 口径 |
|---|---|---|
| 含 Tick 型小问的卷 | **14 / 15**（`2018-june-ch05` 无） | — |
| Tick 出现处 | **约 25 处** | 宽松匹配 `tick\b`，已排除 `Turn over` / `Do not write` 等 |
| 涉及题数 | **约 21 题** | — |
| **一题两处** | **5 题** | 例：`2026-june-ch03 Q4`（小问 3 与 4 各一处）→ 这是必须挂小问级的原因 |
| 每处选项数 | **3 或 4 个**（不固定） | 例：`2022-jan-ch01` 3 个；`2026-june-ch03` 4 个 |

⚠️ **口径敏感，以上仅为参考量级。实施前必须先跑 §8 的探测脚本重取基线**，见 §7 陷阱 6/10。

---

## 3. 数据模型（方案 A）

### 3.1 `cut.json` —— 热区挂到小问

`question` 级保持现状（整节 MCQ 继续用 `option_hotzones`），**新增**小问级字段：

```jsonc
{
  "number": "4",
  "question_type": "structured",
  "option_hotzones": null,              // 题级：整节 MCQ 用，保持不变
  "subquestions": [
    { "label": "1", "page": 7, "bbox": [51.90, 317.03, 100.07, 333.35] },
    { "label": "2", "page": 8, "bbox": [81.60, 191.03, 101.03, 207.35] },
    { "label": "3", "page": 10, "bbox": [52.62, 67.01, 101.99, 83.27],
      "option_hotzones": [                         // ← 新增（可选）
        { "label": "A", "bbox": [51.52, 465.84, 217.14, 494.70] },
        { "label": "B", "bbox": [51.52, 507.72, 217.14, 536.52] },
        { "label": "C", "bbox": [51.52, 549.54, 217.14, 578.40] },
        { "label": "D", "bbox": [51.52, 591.42, 217.14, 620.28] }
      ] },
    { "label": "4", "page": 10, "bbox": [52.62, 328.67, 101.99, 344.99],
      "option_hotzones": [ /* 该处另一组 A–D */ ] }
  ],
  "regions": [ ... ]
}
```

- `bbox` 为 **QP 页面坐标（pt，左上原点）**，与题级 `option_hotzones` 同规格。
- `label` 由**框的纵向顺序**生成 `A/B/C/D`（文本层无字母，见 §7 陷阱 9）。
- 一题两处 → 天然分列在两个小问下，不冲突。

**schema 改动**（`schema/cut.schema.json`，`$defs/question/properties/subquestions/items`）：

> ⚠️ 该节点当前是 `"additionalProperties": false`，**必须同步加字段**，否则 validate 直接失败。
> 可直接内联复用题级同款结构：

```jsonc
"option_hotzones": {
  "type": "array",
  "description": "本小问内嵌选择题的选项热区（Tick one box）；仅 AQA 化学的少数小问有",
  "items": {
    "type": "object",
    "required": ["label", "bbox"],
    "additionalProperties": false,
    "properties": {
      "label": { "type": "string", "description": "A / B / C / D，按框纵向顺序" },
      "bbox":  { "type": "array", "minItems": 4, "maxItems": 4,
                 "items": { "type": "number" } }
    }
  }
}
```

### 3.2 `layout.json` —— 推荐 `option_groups`（与 `options` 并存）

`render_questions.py` 把 pt 坐标换算成题图归一化 0–1。由于 `layout.schema.json` 顶层是 `additionalProperties: false`，新增字段必须同步进 schema。

**推荐**：新增 `option_groups`，**保留 `options` 不动**（现有整节 MCQ 零回归）。

```jsonc
{
  "options": [ { "label": "A", "box": {...} } ],        // 整节 MCQ：现状不变
  "option_groups": [                                     // ← 新增：内嵌 MCQ，按小问分组
    { "label": "3", "options": [ { "label": "A", "box": {...} }, ... ] },
    { "label": "4", "options": [ ... ] }
  ]
}
```

**备选（更小改动）**：给 `options.items` 加可选 `sub` 字段，摊平成一维数组：

```jsonc
"options": [
  { "label": "A", "sub": "3", "box": {...} },
  { "label": "A", "sub": "4", "box": {...} }
]
```

| | `option_groups`（推荐） | `options[].sub`（备选） |
|---|---|---|
| 语义 | 清晰，分组显式 | 靠 `sub` 去重，一题两处时同 label 重复出现 |
| 改动面 | 顶层加 1 个属性 | `options.items` 加 1 个属性 |
| 学习机侧 | 需读新字段；不改则这 21 题仍点不了 | 不改也能点（忽略 `sub`），但分不清是哪组 |

> **无论选哪个，学习机侧都要改**才能让这 21 题真正可点。实施时先确认学习机怎么消费 `layout.options`。

---

## 4. 改动清单

| # | 文件 | 位置 | 动作 |
|---|---|---|---|
| 1 | `schema/cut.schema.json` | `$defs → question → subquestions → items` | 加 `option_hotzones`（§3.1），**items 是 `additionalProperties:false`，漏改必炸** |
| 2 | `schema/layout.schema.json` | 顶层 `properties` | 加 `option_groups`（§3.2） |
| 3 | `scripts/cut_paper_aqa.py` | 新增函数 | `extract_inline_mcq_hotzones(q, pages_content, rects_cache, B, R)`：按 Tick 行 → 找打勾框 → 归属小问 → 写 `subquestions[i].option_hotzones` |
| 4 | `scripts/cut_paper_aqa.py` | `main()` 的 `elif opt_rows:` 分支（约 L917） | 由"只打警告"改为"调用 #3 产热区"；失败时降级为 `review_notes` |
| 5 | `scripts/render_questions.py` | 约 L354–371（现有 `option_hotzones` → `options` 换算段） | 增补：遍历 `subquestions[]`，把 `option_hotzones` 用**同一套 `src_map` 映射**换算归一化，写入 `layout["option_groups"]` |
| 6 | `workbench/` 前端 + 学习机 | 选项渲染处 | 消费 `option_groups`（若选备选方案则只需忽略未知字段） |

**落点提示**

- `extract_option_hotzones()`（L648）是**题级**版本，以"选项标签行的 y 中心"分带；内嵌 MCQ **无标签行**，必须新写，不能复用。
- 归一化换算务必复用现有逻辑：`render_questions.py` L357–367 用 `src_map.segs` 把源 PDF 的 `(page, pt_y)` 映射到图内像素 —— 直接照搬，别自己算 DPI。
- ⚠️ **改完 `schema/*.json` 与 `data/taxonomy/*.json` 必须重启打标台**（进程内缓存），改 `scripts/` 不用。

---

## 5. 识别打勾框：算法与判据

实测得到两种形态，**判据不同**，这是本方案最容易翻车的地方。

### 形态 A —— 独立打勾框列（占多数）

框由 **4 条发丝线**拼成（2 竖 + 2 横），**不是单个矩形对象**。四个角另各有一个 0.48×0.48pt 的残留点。

实测尺寸（跨 2018/2022/2026 全部样本）：

| 量 | 实测值 | 说明 |
|---|---|---|
| 框宽 | **28.44 – 29.28 pt**（几乎恒为 28.80） | 最稳的特征 |
| 框高 | **25.44 – 28.90 pt** | |
| 线宽 | 2018 卷 **0.24pt** / 2022+ 卷 **0.48pt** | ⚠️ 随年份变 |
| 纵向 pitch | **36.8 – 52.1 pt** | ⚠️ 随卷变 |
| 框列 x 位置 | 2018/2022: **x≈390–470**；2026: **x≈174–345** | ⚠️ 随年份**大幅**变动 |

**推荐判据**（不依赖 x、不依赖线宽）：

1. 遍历 `page.get_drawings()` 的 `dr["items"]`，收集所有 `it[0] == "re"` 的子矩形；
2. 取"**上横线**"：宽 ∈ [25, 32] 且 高 ≤ 1.2；
3. 配对下横线：同 x 范围（`|Δx0| < 2`）、`Δy ∈ [20, 35]` → 得到一个框的 `[x0, y_top, x1, y_bot]`；
4. 只用 **Tick 行下方 340pt 内**的框，且按 y 排序。

> 备选判据："左竖线锚"（宽 ≤1.2、高 ∈ [22,34]，配对右竖线 `Δx0 ∈ [24,34]`）。两种都能覆盖形态 A，但**都覆盖不了形态 B**。

### 形态 B —— 表格内打勾（少数，但确实存在）

题面写 **"Tick (✔) one box in Table 2 to identify …"**，打勾位置是**表格里的单元格**，不是独立方框：

```
案例 2022-jan-ch02 p9：表格列分隔 x = 221.58 / 306.90 / 370.74 / 427.44
                        行高 ≈ 25.32 / 22.74 pt，单元格宽 63.36 – 84.84 pt
案例 2026-june-ch05 p8：列分隔 x = 114.78 / 256.68 / 398.70 / 466.32
                        行高 25.5 / 34.02 pt
```

形态 B 的"框"尺寸 63–141pt，**用形态 A 的 25–32pt 判据必然漏检**，且需要先做表格行列切分。

> **建议**：形态 A 先落地；形态 B 单独评估（可先只在 `review_notes` 标注"表格型内嵌 MCQ，热区待办"），不要混在一个判据里硬凑。

### 归属小问

每处 Tick 归给**其上方最近的 `subquestions[].bbox`**（同页、`y` 最大且 `< tick_y` 者）；若该 Tick 落在某小问的 region 区间内则以区间为准。

---

## 6. 验证与回归

单卷：

```bash
export PYTHONPATH="C:/Users/Administrator/.workbuddy/binaries/python/envs/default/Lib/site-packages"
PY="C:/Users/Administrator/.workbuddy/binaries/python/versions/3.13.12/python.exe"
"$PY" scripts/cut_paper_aqa.py     data/aqa/a-level/chemistry/<卷名>
"$PY" scripts/render_questions.py  data/aqa/a-level/chemistry/<卷名>
"$PY" scripts/validate_cut.py      data/aqa/a-level/chemistry/<卷名>
```

验收标准：

| # | 检查项 | 期望 |
|---|---|---|
| 1 | 全库切割 | 87 卷（物理 72 + 化学 15）cut 失败 **0** |
| 2 | 全库校验 | `validate_cut.py` 非 PASS **0** |
| 3 | **物理零回归** | 72 卷 `cut.json` 逐题指纹**完全不变**；`layout.json` 不变 |
| 4 | 化学新增 | 形态 A 的每一处都产出小问级 `option_hotzones`，label 为 A/B/C/D 且纵向递增 |
| 5 | 负坐标 | `layout.json` 中所有 box 的 x/y/w/h 均 ∈ [0,1]（P3 已修，勿回退） |
| 6 | 打标台 | 重启后，命中题的预览能点选选项；一题两处的两题分组正确 |

**回归比对脚本**：全量重跑前先把 87 卷的 `cut.json` 指纹快照存盘，重跑后逐卷 diff，只允许"命中的化学卷"有变化。

---

## 7. 已知陷阱（开工前逐条确认）

| # | 陷阱 | 依据 |
|---|---|---|
| 1 | **不能硬编码框的 x 坐标** | 2018/2022 在 x≈390–470，2026 掉到 x≈174–345（版面改版） |
| 2 | **不能硬编码线宽** | 2018 卷 0.24pt，2022+ 卷 0.48pt |
| 3 | 框**不是**单个矩形对象 | 由 4 条独立 `re` item 拼成，读 `dr["rect"]` 会被外层 path 并集吞掉 |
| 4 | 每处选项数 **3 或 4**，不固定 | `2022-jan-ch01`=3，`2026-june-ch03`=4；validate 的 MCQ 检查目前**假设 A–D 四选项**，需一并放宽 |
| 5 | **一题两处** → 必须挂小问级 | 5 个题如此，题级 `option_hotzones` 装不下（同 label 冲突） |
| 6 | 存在**表格型**（形态 B） | `"Tick … in Table 2"`，宽 63–141pt 的单元格，方框判据必漏 |
| 7 | Tick 行**可能被拆成两行** | 例：`2022-jan-ch02 p9` 文本层是 `"Tick ( )"` 与表头分离，正则要容错 |
| 8 | 正文里也出现 "tick" | 需排除 `Turn over for the next question` / `There are no questions printed on this page` / `DO NOT WRITE ON THIS PAGE` |
| 9 | **label 只能按顺序生成** | 文本层无 A/B/C/D 字母，按框 y 升序赋 `ABCD` |
| 10 | 判据口径敏感 | 同一批卷，严格正则 23 处 / 宽松 25 处；**以探测脚本输出为准** |
| 11 | schema 改动有缓存 | `schema/*.json`、`data/taxonomy/*.json` 改后**必须重启**打标台 |
| 12 | 别用内置 md 阅读器改 `data/` 下的文件 | 回写会破坏 front-matter；要改就改 `cut.json` 再重跑脚本 |

---

## 8. 附：探测脚本（实施前先跑这个）

把下面内容存为 `scripts/probe_inline_mcq.py`（或任意临时路径）后运行，用于**重取基线**与**分类形态**：

```python
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AQA 内嵌选择题（Tick one box）探测器：列出每处 Tick、形态与打勾框坐标。

用法： PYTHONPATH=<site-packages> python scripts/probe_inline_mcq.py [学科目录]
默认扫描 data/aqa/a-level/chemistry/ 下全部卷。
"""
import re
import sys
from pathlib import Path

import pymupdf

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from cut_paper_aqa import build_page_lines, content_lines  # noqa: E402

TICK = re.compile(r"tick\b", re.I)
NONOPT = re.compile(r"turn over|there are no questions|do not write", re.I)
IN_TABLE = re.compile(r"in\s+(the\s+)?table", re.I)


def rect_items(page):
    """页面全部矢量 re 子矩形（打勾框由 4 条发丝线拼成，必须逐 item 取）。"""
    out = []
    for dr in page.get_drawings():
        for it in dr["items"]:
            if it[0] == "re":
                out.append(it[1])
    return out


def boxes_under(rects, ty, reach=340.0):
    """Tick 行下方 reach 内，按「上横线锚 + 下横线配对」还原打勾框。"""
    hb = [r for r in rects
          if 25.0 <= (r.x1 - r.x0) <= 32.0 and (r.y1 - r.y0) <= 1.2
          and ty - 4 <= r.y0 <= ty + reach]
    hb.sort(key=lambda r: r.y0)
    boxes, i = [], 0
    while i < len(hb):
        if (i + 1 < len(hb)
                and 20.0 <= hb[i + 1].y0 - hb[i].y0 <= 35.0
                and abs(hb[i].x0 - hb[i + 1].x0) < 2.0):
            boxes.append((round(hb[i].x0, 2), round(hb[i].y0, 2),
                          round(hb[i].x1, 2), round(hb[i + 1].y1, 2)))
            i += 2
        else:
            i += 1
    return boxes


def main():
    base = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "data/aqa/a-level/chemistry"
    stat = {"A": 0, "B": 0, "?": 0}
    for d in sorted(base.iterdir()):
        if not (d / "source.pdf").exists():
            continue
        doc = pymupdf.open(d / "source.pdf")
        for pi, page in enumerate(doc):
            lines = content_lines(build_page_lines(page))
            ticks = [ln for ln in lines
                     if TICK.search(ln["raw"]) and not NONOPT.search(ln["raw"])]
            if not ticks:
                continue
            rects = rect_items(page)
            for t in ticks:
                ty = t["bbox"][1]
                txt = t["raw"].strip()
                boxes = boxes_under(rects, ty)
                if IN_TABLE.search(txt):
                    kind = "B"
                elif boxes:
                    kind = "A"
                else:
                    kind = "?"
                stat[kind] += 1
                print(f"{d.name:16s} p{pi:2d} y={ty:7.1f} 形态{kind} "
                      f"框数={len(boxes)} | {txt[:56]!r}")
                for b in boxes:
                    print(f"      框 x={b[0]:7.2f}..{b[2]:7.2f} y={b[1]:7.2f}..{b[3]:7.2f}")
        doc.close()
    print(f"\n分类统计: {stat}  合计 {sum(stat.values())}")
    print("形态 A=独立打勾框列 / B=表格内打勾 / ?=需人工看")


if __name__ == "__main__":
    main()
```

**基线期望**（2026-10-15）：`{'A': 13, 'B': 1, '?': 11}`，合计 25。
其中 **11 处 "?" 必须先人工看图定性**，再决定判据是否放宽 —— 这是正式动手前的**第一件事**。

渲染单页看图（改 `PI` 与 `CLIP`）：

```python
import pymupdf
doc = pymupdf.open("data/aqa/a-level/chemistry/<卷名>/source.pdf")
doc[PI].get_pixmap(dpi=170, clip=pymupdf.Rect(X0, Y0, X1, Y1)).save("page.png")
```

---

## 9. 实施顺序建议

1. **跑 §8 探测脚本**，把 25 处的形态定死（尤其 11 处 "?"）→ 产出完整清单。
2. 改两个 schema（1、2 号改动）。
3. 在 `cut_paper_aqa.py` 写 `extract_inline_mcq_hotzones()`，先在**单卷**（如 `2026-june-ch03`，含一题两处）验证。
4. 接进 `main()` 的 `elif opt_rows:`（4 号改动），跑化学 15 卷 + `validate`。
5. 改 `render_questions.py`（5 号改动），重渲化学 15 卷，检查 `layout.json` 的 `option_groups`。
6. **全量 87 卷回归**（§6），确认物理零变化。
7. 视形态 B 的占比决定是否单独排期。
8. 打标台/学习机侧接 `option_groups`。

---

## 10. 数据依赖（换机器必读）

⚠️ **`data/` 不入库**。另一台电脑 clone 后**没有** AQA 试卷数据，需先准备：

| 需要 | 路径 | 数量 | 说明 |
|---|---|---|---|
| AQA 化学卷 | `data/aqa/a-level/chemistry/<year>-<session>-ch0N/` | **15 卷** | 目录名已用化学卷号 `ch01..ch05`（2026-10-15 从物理卷号 `ph0N` 校正） |
| AQA 物理卷 | `data/aqa/a-level/physics/<year>-<session>-ph0N/` | 72 卷 | 回归对比用 |
| 单卷内容 | 每卷含 `source.pdf` + `paper.md` + `cut.json` + `assets/` | — | `cut.json`/`assets` 可由脚本重建，`source.pdf` 必须拷 |

若目标机没有 `data/`，从本机打包 `data/aqa/` 带走即可（体积约数百 MB）。
