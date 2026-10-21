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
| `cut_paper.py` | 自动切割：定位题号/`(Total for Question N)` 锚点 → 每题 regions → `cut.json`；检测 QP 小问标签行写入 `subquestions`（label + 页码 + 坐标；**MCQ 也扫**——化学 Section A 有一类「(a)(b)(c) 各带一套 A–D」的多问选择题，`total_marks>1` 才扫，单问 MCQ 仍为空）；MCQ 选项热区写入 `cut.json` 的 `option_hotzones`；生成 `qNN.md` 脚手架。另含三项自动处理：**页尾空白裁剪**、**答题册检测**（QP 与 Answer Booklet 分离的卷型，结构化题另存 `answer_regions`（带 label 的作答区，供 layout 定位）与 `answer_block`（册内题号行 → 册内 Total 的整块连续区域）；MCQ 不提取） | pymupdf |
| `cut_paper_cie.py` | **CIE 9702**（P1/P2/P4）自动切割：锚点为题号列众数 + 行内 `[n]` + 每题一条右对齐 `[Total: N]`；另含 A 型乱码（+0x1D 位移）span 级还原。产出与 `cut_paper.py` 完全同构 | pymupdf |
| `cut_paper_aqa.py` | **牛津 AQA 国际 A-Level 物理**（PH01–PH05）自动切割：题号是**两位数徽章** `0 1`（同一题随小问重复出现，须按序列 1..N 取首次）、分值是**行内 `[N mark(s)]`**（无逐题 Total 锚点，靠「下一题徽章 + `END OF SECTION`/`END OF QUESTIONS` 硬终点」收尾）、题型按**封面阅卷表**定调（选择题整段列成 `N–M` 区间，逐题列出的即结构化）；页框是 4 条 0.48pt 细线（判据 `w>490 或 h>600 或 (w≤2 且 h≤2)`）；插入页（`DO NOT WRITE` / `no questions printed` / `Additional page`）整页跳过；页尾裁剪用**封顶保留** `min(B, 末项+260pt)`（仅末段 + 末项为正文散文 + 无导航提示三条同时成立；选择题一律 +8pt）。产出同构 | pymupdf |
| `cut_paper_aqa_chem.py` | **牛津 AQA 国际 A-Level 化学（9620，CH01–CH05）专用的独立切割脚本**（2026-10-17 起，76 卷实测；**与物理那份不共享代码**，改动互不牵连）。骨架同源（徽章 `0 N` + 行内分值 + END OF QUESTIONS，76/76 总分吻合），化学侧的差异都在这里处理：① 页框按「细线 / 整页矩形」严格判据，**不吃宽表格**（化学表格多，整宽表 486×190 逼近 490）；② 版心取 **40/536**（正文最右 534.6，仍留 1.4pt），切掉右上角 `Do not write outside the box` 方框与题区的交叠（2018–2019 卷该框 x0=534.2–538.3，取 539 时每张续页图右上角都会留一道竖细线）；页脚位置不齐（`Turn over ►` 有 y0=770.1 与 762.8 两种，2018 卷还有印在版心内的 `Copyright ©`，y0≈751），故 **region 下界与「该页页脚顶沿 − 2」取小**，图底不再吊半行页脚字；③ **导航行不计内容**（`continues on the next page` / `Turn over for the next question|section`），题图尾部不再吊着提示语；④ 封顶 `CAP=120pt`（≈4 行，化学作答横线行距 30–32pt），末项是横线/表格/选项一律硬裁 +10pt；⑤ 封面表 76/76 可解析 → 不再对「结构化题里检出 A–D 行」刷假 review（化学的表格列标题、答案标签就是粗体 A/B/C） | pymupdf |
| `cut_paper_acct.py` | **Edexcel IAL 会计（WAC11/WAC12）**自动切割。卷式与理化完全不同：**三部分**——普通卷 = QP（题面与点线作答区逐小问交错）+ Source Booklet（资料）；01A 中国区卷 = 紧凑 QP（题面）+ Source Booklet（资料）+ Answer Booklet（答题区）。锚点极硬：每题以 `(Total for Question N = X marks)` 收尾，同一题在卷内出现 **2 次**（普通卷）或 **3 次**（01A），第 k 次出现即第 k 块终点，三处分值互相校验。产出 `regions`（题目）+ 新增 **`material_regions`**（资料，渲染时拼在最前）+ `answer_block` / `answer_regions`（答题区）。**点线压缩**：作答点线是文本层 `.` 串、等距 28.2pt，按 band 只保留前 N 行、其余整段删除（无缝拼接），N **自适应**——取仍能塞进 WebP 单边上限的最大值；另有保险「删除区间内一旦扫到非点线文本就收窄」，题面/表格绝不被吞。三段各带一行重复的 Total，只保留最后一块 | pymupdf |
| `import_acct.py` | **Edexcel IAL 会计**批量入库：`downloadPapers/Edexcel_ALevel_Accounting` → `data/edexcel/a-level/accounting/<year>-<session>-<wac11\|wac12>[a]/`。考季取自**目录名**（文件名里的日期是发布日，2020/Jan 卷名带 20200305，拿它会错判成 3 月季）；不收旧大纲 `2017-jan-wac02`。`paper.md` 的 `total_marks` 记 **230**（题目分值合计，供 validate 对账），考生满分 200 记 `marks_awarded` | pymupdf |
| `render_questions.py` | 按 `cut.json` 渲染每题 WebP（200 DPI，跨页题多 region 纵向拼接）；**有 `material_regions` 时按「资料 → 题目 → 答题区」三段拼接**（段间画浅灰分隔线，图顶提示带写「本题依次为：资料（Source Booklet）→ 题面 → 作答区」；点线压缩造成的接缝画一条**实心**浅灰线——虚线会跟作答点线混淆）；有 `answer_block` 时成图分区式：题干全部在上（含 QP Total 行）→ 交界细分隔线 → 册内 answer_block 整块在下，图顶加「请在题目后面的专属作答区回答」提示（该提示的中文字体按 Windows / macOS / Linux 候选表探测，再扫常见字体目录兜底；都找不到时**不画提示、只留分隔线，渲染不中断**，可用 `--notice-font <路径>` 显式指定）；MCQ 与无 answer_block 的旧数据按原有逻辑拼接；同时产出 `qNN.layout.json`（**归一化坐标 0–1**）：`areas` = 每小问作答区域（boxes 多框，仅答题册另附卷）、`options` = MCQ 选项热区、`subquestions` = 每小问标签行在题图内的位置（**供下游按纵向区间判断内容/笔迹落在哪个小问下**；小问纵向排列故 y 区间即可界定；换算只认题卷段且须匹配页码，小问可跨页）。图片任意缩放时设备端一次乘法即可换算像素位置；只读坐标，绝不回写 |
| `validate_cut.py` | 校验：JSON Schema、sha256 一致、题号连续、区域与答题区无重叠、答题区页序、answer_regions 标签与 subquestions 对应、layout.json 归一化坐标合法、与 option_hotzones 一致、**subquestions 与 cut.json 逐一对齐**、分值合计、front-matter 的 `cut_status` 同步 | pymupdf、jsonschema |
| `audit_cut_edges.py` | **切图边界体检**：逐 region 查「非空文本行 / 真图形跨过题段上下边界」——即图里某行字或某条作答横线被切成两半（页框线、页边家具、空行不计）。`validate_cut.py` 查的是数据结构自洽，本脚本查的是**画面是否切坏**，两者互补；切割脚本改完必跑，退出码可直接做门禁 | pymupdf |
| `render_subsplit.py` | 按顶层小问（a/b/c…）切分渲染子题图：`assets/qNN-<label>.webp` + `.layout.json`（题干 + 本小问 QP 段 + 分隔线 + 本小问册内作答区切片，layout `areas` 保留 a i 等层级标签）。QP 侧用 `subquestions` 标签行坐标切（QP 无独立 "(b)" 行时回退到其首个子行）；册内侧聚合 `answer_regions`（退化/缺失时降级为纯题干并在 warnings 说明）。只读坐标不回写；物理/化学通用；**MCQ 也可切**（2026-10-19 起：化学多问选择题无答题册 → `booklet=False` → 自动走 `qp_inline`，只按 QP 切点裁，不拼册、不画提示带）。可 CLI（`--json` 供打标台后端解析）或 import `render_split()`（打标台保存切分题时调用） | pymupdf、pillow |
| `backfill_mcq_subquestions.py` | **存量补丁（一次性）**：给 Edexcel 化学「多问选择题」补 `cut.json` 的 `subquestions`，并同步补 `assets/qNN.layout.json` 的归一化 box。**不重切、不重渲**——用同款解析函数按现有 `regions` 重扫 `source.pdf`，一级图一个像素都不动；`qNN.md` 也不碰（`split` 标记由打标台在切分时写）。幂等（已有小问跳过）、写前备份 `*.bak-mcqsubq`、默认干跑。layout 坐标靠 pymupdf 复算尺寸/偏移，实测与现有 `options` box 逐字节吻合；复算不符或标签未落进任何 region 时只补 cut.json 并打警告 | pymupdf |
| `import_papers.py` | 批量入库：从「真题」类源目录递归收集试卷 PDF（名含 `que`/`试卷`，排除 `rms`/`msc`/`答案`/`ms-`），按 `<year>-<session>-<paperCode>` 规则映射到 `data/` 目录，拷 `source.pdf` + 从卷面提取元数据生成 `paper.md`（单元/日期/时长/总分/卷号）。`--dry-run` 只打印映射表，`--apply` 才落盘；覆盖已有卷时把旧 cut 产物整体挪入 `_prev/`（不删）。`--csv` 导出映射表 | pymupdf |
| `batch_run.py` | 批量跑 `cut → render → validate`：`--board edexcel|cie|aqa`（按考试局自动选切割脚本，也可 `--cut` 显式指定）、`--qual`、`--subject physics|chemistry`、`--only` 指定卷、`--json` 出明细。输出通过率、耗时、题目总数与**需人工复核清单** | — |
| `import_aqa.py` | **AQA 物理**批量入库：源目录是扁平命名 `qp-<YYYYMM>-physics-u<N>.pdf`（**同时认云盘整理名 `aqa-physics-<YYYY>-<jan\|june>-u<N>-qp.pdf`**；`ms-…` / `-ms.pdf` 评分标准排除），映射 `u1..u5 → PH01..PH05`、月份 `01→jan` / `05,06→june`，落 `data/aqa/a-level/physics/<year>-<session>-<ph0N>/`；元数据（卷号/单元标题/考试日期/时长/总分/卷号水印）取自封面。**故意不并入 `import_papers.py`**：两者的收集规则无共享段，硬塞会互相牵制 | pymupdf |
| `import_aqa_chem.py` | **AQA 化学**批量入库：收集 `qp-<YYYYMM>-chemistry-u<N>.pdf` → `u1..u5 → CH01..CH05`、`data/aqa/a-level/chemistry/<year>-<session>-<ch0N>/`；封面元数据多取两项 `spec: 9620`、`level: AS/A2`（U1–U3 封面上印 INTERNATONAL AS，U4–U5 印 A2）。支持 `--only` 按卷名过滤（增量做单卷时用） | pymupdf |

运行示例见各脚本头部注释。脚本都吃 `data/<board>/<qual>/<subject>/<session>/` 目录（内含 `source.pdf`）。

**切割脚本按「考试局 × 资格 × 学科」各起一份**（2026-10-17 定规）：`cut_paper.py` = Edexcel、`cut_paper_cie.py` = CIE 9702、`cut_paper_aqa.py` = 牛津 AQA 物理、`cut_paper_aqa_chem.py` = 牛津 AQA 化学。四套锚点/版式互不兼容，**不共用代码、不互相 import**——同一学科里个别单元版式特殊时，也在该学科的脚本里分支处理，而不是去改别人的脚本。渲染与校验层（`render_questions.py` / `validate_cut.py`）仍然四局共用。

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
- 锚点策略按「考试局 × 学科」各一套：Edexcel 的 `(Total for Question N = X marks)`、CIE 9702 的 `[Total: N]`、牛津 AQA 物理的徽章 + `END OF SECTION/QUESTIONS`、牛津 AQA 化学的徽章 + `END OF QUESTIONS`（**2026-10-17 已实测同构**：76 卷题号/分值/总分 100% 吻合，故化学另起 `cut_paper_aqa_chem.py`，不并入物理脚本）。**AQA 生物尚未确认**——同样先跑只读探测，再决定是否另起一份。AQA 的 Mark Scheme 版式与题卷完全不同，不属同一课题。
- 小问标签已支持两种 Edexcel 卷式：带题号前缀 `N (a)(i)…`（WPH12 等）与无前缀 `(a)…`（WPH11 等）；U1 式接续行（`(ii)` 省略父前缀）按文档序记录，label 不回填父级（如 `ii` 而非 `a ii`），组层级时按顺序推断。
- 扫描卷（无文本层）会写 `needs_ocr` 的 `cut.json` 后终止，需人工画框或 OCR。
- 页尾空白裁剪：Edexcel 只在"最后内容是答题横线"时触发（纯空白结尾的画图作答空间**保留**，宁多勿少）；AQA 用**封顶保留**规则（末段 + 末项为正文散文 + 无导航提示 → 留 `min(版心底, 末项+260pt)`，即最多 10 行，超出才切；其余一律裁到 +8pt），选择题不封顶。
- `layout.json` 只覆盖 QP 区域与 MCQ 热区；答题册 `answer_regions` 的归一化坐标按需从 cut.json 换算（册内多为点线，需要时再扩展）。
- **作答提示文字需要中文字体**：`render_questions.py` 原先把字体硬编码为 4 个 Windows 路径，非 Windows 机器会 `sys.exit` 中断渲染（且中断点在逐题循环内 → 留半成品）。2026-10-15 改为跨平台探测（Windows / macOS / Linux 候选表 → 字体目录扫描 → 找不到就只画分隔线不画文字），并加 `--notice-font` 覆盖。该字体只在题带 `answer_regions` / `answer_block`（答题册另附卷的分区式成图）时才需要——AQA / CIE 卷全程不触发。
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
