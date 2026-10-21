---
name: vision-cut
description: "源 PDF 文本层损坏（字体子集无 ToUnicode、提取出来全是 \\x01\\x02 之类）时，用纯视觉把 CIE 卷子切成题并补子问热区。触发词：视觉切割、乱码卷切割、文本层坏了切一下、这卷切不了、按图切、vision cut、把这张乱码卷切出来。适用项目：D:/Tasks/questionBank（A-Level 题库）。产出与 cut_paper_*.py 完全同构（cut.json + qNN.md + assets/*.webp + *.layout.json + subquestions），能直接进打标台。"
agent_created: true
---

# 视觉切割 · 文本层全坏时的兜底通道

**不是"另一种切割脚本"，是文本层不可用时的唯一通道。** 文本层正常就老老实实跑 `scripts/cut_paper_cie_chem.py`，
那条路 7 秒一卷；这条路 3–5 分钟一卷，只在没别的办法时用。

## 何时用 / 何时不用

| 情况 | 走哪条 |
|---|---|
| 文本层 OK（虚词命中 ≥30、控制字符 <2%） | `scripts/cut_paper_*.py`，**别用这个技能** |
| 文本层 CORRUPT（`\x01\x02…` 递增序列 / 控制字符 >15%） | 本技能 |
| 文本层 PARTIAL（选项行乱码、题图完整） | 脚本切，题目 review 标记即可，不用视觉 |
| 源能换成好版本 | **先换源**，换完就跑脚本。换源永远优先于视觉切割 |

先体检，再决定：

```bash
python scripts/vision_cut.py <卷目录>    # 体检不过（CORRUPT 才继续）
```

## 四步流程

```bash
PY="PYTHONPATH=<venv site-packages> python"   # pymupdf / rapidocr / PIL 都在 venv
S=.workbuddy/skills/vision-cut/scripts

# 1 卷级：OCR 定题界 + 像素投影定边界 + 裁图拼接   （首次 OCR 约 2.7s/页，之后走缓存）
$PY $S/vision_cut.py  data/cie/alevel/chemistry/<卷>

# 2 子问热区：OCR 行的 bbox + 序列推断标签
$PY $S/vision_subs.py data/cie/alevel/chemistry/<卷>

# 3 自查：叶子数 / 分值合计 / 顺序 / 墨密度 → 可疑项裁成对照图
$PY $S/vision_check.py data/cie/alevel/chemistry/<卷> --sheet
#   ↑ 出 .vision_work/<卷>/suspect_<卷>.png，逐块看图，判定后按索引删/插

# 4 入库：schema 合规 + 写 qNN.md + 拷 assets + 跑 validate_cut.py
$PY $S/vision_ingest.py data/cie/alevel/chemistry/<卷> --dry   # 先看
$PY $S/vision_ingest.py data/cie/alevel/chemistry/<卷>
```

中间产物全在 `<root>/.vision_work/<卷>/`（`ocr.json` 缓存 + `out/`），**不入库**。

## 硬验收线（不看这条别交付）

1. **各题 `total_marks` 之和 == `paper.md` 的 `total_marks`**。`vision_cut.py` 结尾会自动比，不等就直接退 3。这是最硬的正确性证据 —— 题界错一位，分值立刻对不上。
2. 每题 `[N]` 标记的**数值和** == 该题 `total_marks`（`vision_check.py` 的 `marksum`）。
3. `scripts/validate_cut.py <卷>` PASS（`vision_ingest.py` 会自动跑）。
4. 子问总数与卷面一致；`extra`/`few` 告警逐条定性。

## 血泪坑（每条都真踩过，改规则前先读）

### 几何
1. **版心 x 固定 55–548**。CIE 版心宽度恒 493pt，A4 595 与 Letter 612 都一样，它是按固定尺寸排的。
   按页宽算比例（W−20）会把右侧 `DO NOT WRITE` 竖栏算进内容；
   **自动探测最小 x 会撞四角定位框**（实测 x=31），那对线贯穿全页高度，一碰就把每页 y 范围撑满，
   q04 直接从 3967px 涨到 5112px。
2. **页底界 `H−46`，不是 `H−52`**。落在 740pt 的 `[Total: 19]` 会被当页脚切掉 → 整卷少一题、
   分值合计 81 vs 100。也不能更靠下：页脚 `© UCLES` / 条码在 780 附近。
3. **题首起点要跳过「无内容的页」**。两种典型：
   - 封面 p1 的 `Cambridge International AS & A Level` x0=79 < 100，会被当成题号行 → q01 把整页说明裁进来。
   - 题与题之间的**数据参考页**（如 2025-june-qp42 的 p19 只有 Table 8.2 化学位移表）整页无 `(a)` 无 `[N]`，
     属于考试提供的数据，不属于任何题。判据：该页没有子问标签行也没有 `[N]` → 跳过。
4. **一页多题必须按题界 y 切**，不能整页 `span()`。早期版本只用「本页首末墨迹」，一页两题时会串题。
5. 上下各留 `−6 / +8`pt 余量。下缘不留 8pt 会切掉 `[N]` 的 descent。

### OCR
6. **300 DPI**。200 DPI 对孤立的 `(iii)` 这类短标签行漏检明显（实测 Q2 少 3 个）。
7. **右侧 `DO NOT WRITE IN THIS MARGIN` 是竖排**，OCR 会当横排读、把整行 y 中心带偏 → 必须滤掉 `x>566` 的词。
8. **OCR 的 bbox 可信，OCR 的字符不可信**。`(i)→()`、`(iii)→(ili)`、`(ii)→(m)`、`(f)→(e)` 是常态。
   所以标签**不采用 OCR 认出的字符**，只用「序列位置 + 严格递增约束」推。
9. **`lv1/lv2` 必须跨页保持**。分成每页调一次函数会让计数器每页归零，`(b)(iii)` 退化成 `(a)(iii)`。
10. `x0 ≥ 140` 的行要跳过 —— 那是表格里以 `(` 开头的文字（`(2,4-DNPH reagent)`），不是标签。
11. 分值标记用 `search` + 行尾锚定，不能要求整行只有 `[N]`：CIE 常写 `pH = [4]`、`mass of iron = .. mg [4]`。
    按整行匹配会漏一半，导致「叶子数 vs [N] 数」全线误报（17 条告警里 13 条是这么来的）。

### 校验
12. **一级标签 `(a)(b)(c)` 不是小问**，只是分组，本身没有 `[N]` → 必须先算叶子再比数，否则全是误报。
13. 复合行 `(a) (i)` 同 y，顺序判据用 `<=` 不是 `<`。
14. **删子问按索引，不按 label**。一级 `b` 和二级 `b` 可能同名，按 label 批量删会误伤（把 Q4 的 a 后直接跳到 c）。
15. 一个子问挂两个 `[N]` 是常态（`[2] [1]`），所以「叶子数 == [N] 数」只是弱判据，
    硬判据是「`[N]` 数值和 == total_marks」。

### 版式特例
16. 卷末**附录表**（`Important values, constants and standards`）常与末题同页，坐标连续只能靠规则截断：
    右对齐窄行 → 其下空白 >20pt → 再出现满宽块（w>380）或居中标题（左右留白差<30）→ 截断。
    **右界用「本页最大 x1」做基准，不能写死**（正文右界各卷从 483 到 548 不等）。
17. 新格式（2023 起）末题的 `[Total: 19]` 与 `[Turn over]` **同一行**，会合成一个满宽块；
    排页脚时容易连 Total 一起排掉。分值可从卷面总分反推，不必纠缠。

### Schema（`validate_cut.py` 会拦）
18. `cut.json` **不允许 `marks_sum`**（`additionalProperties: false`）。
19. `question.status` 枚举只有 `auto / review / verified / manual`（`"vision"` 非法）；`number` 必须是 **string**。
20. `layout.src_map.segs` 每条必须有 `kind`（`"qp"`），且 `pt` / `img` 都是 **2 元**
    （`[top, bottom]` / `[y0, y1]`），不是 4 元 bbox。
21. md 的 `cut_status` 必须与 `cut.json` 的 `status` 同步。

## 已知局限（别假装没有）

- **md 没有正文**。文本层坏就提不出字，`qNN.md` 只有 front-matter 和 `[Total: N]`。
  想补正文只能 OCR 转录或人工，那是另一件事，本技能不做。
- **子问热区准确率约 98%**，不是 100%。`vision_check.py` 会把可疑项挑出来，人看图定 2–3 处。
  不做这一步就等于把 98% 当 100% 用。
- **OCR 完全没检测到的标签**（不是读错，是没框）只能定点插补 —— 从正文行坐标反推位置。
- 全库 340 卷 ≈ 26 小时（单卷 4.6 分钟，瓶颈在转录与核对不在算），**不适用于批量**。
  批量只有一条路：换源。本技能是为「换不到源的那几十卷」准备的。

## 相关

- 实验报告：`docs/42-乱码卷视觉切割试跑.html`（含两种卷的实测数据、放弃过的路线：字形模板反解 / 肉眼认字形 / OCR 无引擎）
- 脚本切割：`scripts/cut_paper_cie_chem.py`（文本层 OK 时用这个）
- 校验：`scripts/validate_cut.py`
