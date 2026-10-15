# scripts/ · 工具

数据先落盘，工具按需补齐。

## 切割流水线（已实现）

把一份真题 PDF 切成"一题一文件"：坐标是数据，图片是产物。

```
source.pdf ──cut_paper.py──▶ cut.json + qNN.md（MCQ 热区入 cut.json 的 option_hotzones）
                 │
                 └─（人工校对改 cut.json 的 regions/status）──▶ 重跑受保护
source.pdf + cut.json ──render_questions.py──▶ assets/qNN.webp + assets/qNN.layout.json
全部产物 ──validate_cut.py──▶ 校验报告
```

| 脚本 | 作用 | 依赖 |
| --- | --- | --- |
| `cut_paper.py` | 自动切割：定位题号/`(Total for Question N)` 锚点 → 每题 regions → `cut.json`；检测 QP 小问标签行写入 `subquestions`（label + 页码 + 坐标）；MCQ 选项热区写入 `cut.json` 的 `option_hotzones`；生成 `qNN.md` 脚手架。另含三项自动处理：**页尾空白裁剪**、**答题册检测**（QP 与 Answer Booklet 分离的卷型，结构化题另存 `answer_regions`（带 label 的作答区，供 layout 定位）与 `answer_block`（册内题号行 → 册内 Total 的整块连续区域）；MCQ 不提取） | pymupdf |
| `render_questions.py` | 按 `cut.json` 渲染每题 WebP（200 DPI，跨页题多 region 纵向拼接）；有 `answer_block` 时成图分区式：题干全部在上（含 QP Total 行）→ 交界细分隔线 → 册内 answer_block 整块在下，图顶加「请在题目后面的专属作答区回答」提示；MCQ 与无 answer_block 的旧数据按原有逻辑拼接；同时产出 `qNN.layout.json`（**归一化坐标 0–1**）：`areas` = 每小问作答区域（boxes 多框）、`options` = MCQ 选项热区，图片任意缩放时设备端一次乘法即可换算像素位置；只读坐标，绝不回写 |
| `validate_cut.py` | 校验：JSON Schema、sha256 一致、题号连续、区域与答题区无重叠、答题区页序、answer_regions 标签与 subquestions 对应、layout.json 归一化坐标合法且与 option_hotzones 一致、分值合计、front-matter 的 `cut_status` 同步 | pymupdf、jsonschema |
| `render_subsplit.py` | 按顶层小问（a/b/c…）切分渲染子题图：`assets/qNN-<label>.webp` + `.layout.json`（题干 + 本小问 QP 段 + 分隔线 + 本小问册内作答区切片，layout `areas` 保留 a i 等层级标签）。QP 侧用 `subquestions` 标签行坐标切（QP 无独立 "(b)" 行时回退到其首个子行）；册内侧聚合 `answer_regions`（退化/缺失时降级为纯题干并在 warnings 说明）。只读坐标不回写；物理/化学通用。可 CLI（`--json` 供打标台后端解析）或 import `render_split()`（打标台保存切分题时调用） | pymupdf、pillow |
| `import_papers.py` | 批量入库：从「真题」类源目录递归收集试卷 PDF（名含 `que`/`试卷`，排除 `rms`/`msc`/`答案`/`ms-`），按 `<year>-<session>-<paperCode>` 规则映射到 `data/` 目录，拷 `source.pdf` + 从卷面提取元数据生成 `paper.md`（单元/日期/时长/总分/卷号）。`--dry-run` 只打印映射表，`--apply` 才落盘；覆盖已有卷时把旧 cut 产物整体挪入 `_prev/`（不删）。`--csv` 导出映射表 | pymupdf |
| `batch_run.py` | 批量跑 `cut → render → validate`：`--subject physics|chemistry`（理化共用同一套脚本）、`--only` 指定卷、`--json` 出明细。输出通过率、耗时、题目总数与**需人工复核清单** | — |

运行示例见各脚本头部注释。三个脚本都吃 `data/<board>/<qual>/<subject>/<session>/` 目录（内含 `source.pdf`），**理化共用同一套**（学科差异以条件分支表达，不分叉目录）。

**依赖安装**（本机统一装进 WorkBuddy 托管隔离 venv，勿全局安装）：

```
C:\Users\Administrator\.workbuddy\binaries\python\envs\default\Scripts\pip.exe install pymupdf pillow jsonschema
```

## 版式容错（2026-10-14 批量入库批次新增）

跑 2020–2026 共 130 份真题时暴露的三种卷式差异，已修：

| 缺陷 | 现象 | 修法 |
| --- | --- | --- |
| 题号带冒号 | 2025-june 整季题号写作 `1:\t A cube…`，原正则要求数字后直接空白 → 整卷 0 题 | `QSTART_RE`/`QNUM_ONLY_RE` 允许题号后可选冒号 |
| 非粗体题号行 | 老卷文本层个别题号行丢 `bold` 标记（如 2021-jan U5 第 3 题）→ 严格递增序列永久卡死 | 粗体降为**优先项**：先按文档序收集全部候选，序列推进优先取粗体；该题号在当前位置后无粗体候选时接受最早的非粗体候选补位（且须早于下一题的首个粗体候选） |
| Total 锚点含星号 | 星号题写作 `(Total for Question *15 = 6 marks)`，原正则不认 `*` → 终点锚点丢失，单题区域吞到文末（跨 21 页），渲染撞 WebP 单边上限崩溃 | `TOTAL_RE`/`TOTAL_SQUASH_RE` 允许题号前星号；`render_questions.py` 再加**尺寸预检**：预估像素高超过 `WEBP_MAX_SIDE` 时对该题单独降 DPI 渲染（layout.json 是归一化坐标，不受缩放影响） |

三项修复落在物理/化学**共用的同一套脚本**（2026-10-14 合并为单份超集，原先 `scripts/chemistry/` 副本已撤销）；化学 12 卷回归零变化。

## 已知限制

- 题干/选项文本来自 PDF 文本层：普通文字 100% 准确；**数学式（分式、上下标）会碎成单字符**（如 Q6 选项），展示以题图为准，文本仅供批改 AI 与检索参考。
- 仅实现 Edexcel 锚点策略（`(Total for Question N = X marks)`）；CIE 方括号分值、双栏版式待加 per-board 策略。
- 小问标签已支持两种 Edexcel 卷式：带题号前缀 `N (a)(i)…`（WPH12 等）与无前缀 `(a)…`（WPH11 等）；U1 式接续行（`(ii)` 省略父前缀）按文档序记录，label 不回填父级（如 `ii` 而非 `a ii`），组层级时按顺序推断。
- 扫描卷（无文本层）会写 `needs_ocr` 的 `cut.json` 后终止，需人工画框或 OCR。
- 页尾空白裁剪只在"最后内容是答题横线"时触发：以纯空白结尾的画图作答空间会**保留**（宁多勿少），是否裁由人工校对定夺。
- `layout.json` 只覆盖 QP 区域与 MCQ 热区；答题册 `answer_regions` 的归一化坐标按需从 cut.json 换算（册内多为点线，需要时再扩展）。
- 会话目录名解析用 `sdir.name.split("-", 2)`，因此**带连字符的 session 标记写不进目录名**——文档里的 `may-jun` / `oct-nov` 实际不可用；May/June 场统一记 `june`。
- `import_papers.py` 的 `exam_date` 有 35 份（2021–2023 年 Pearson 重发版「试卷」）**卷面无日期可提取**，留 `null`；文件名与正文都没有，不臆造。

## 规划中

| 脚本 | 作用 | 状态 |
| --- | --- | --- |
| `validate.py` | 校验 `data/` 下每个 `qNN.md` 的 front-matter 是否符合 `schema/question.schema.json` | 规划中 |
| `index.py` | 扫描 `data/` 生成全库索引（题目 ID → 路径 + 关键元数据） | 规划中 |
| `search.py` | 按考试局 / 学科 / 年份 / 大纲编号 / 标签检索题目 | 规划中 |
| `new_paper.py` | 按规范脚手架一套新试卷目录 | 规划中 |
| `build_paper.py` | 从题库按条件抽题、组卷、导出 | 规划中 |
| `cut_ms.py` | mark scheme 切割（复用 cut schema） | 规划中 |

## 约定

- 脚本面向本仓库 `data/` 的目录规范编写，不硬编码具体试卷。
- 依赖尽量少；引入第三方库必须在此登记。
- 运行示例统一写在各脚本头部注释里。
