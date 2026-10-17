---
name: ai-tagging
description: "AI 给题库里的真题打知识点标签 + 主标签（只标二级/叶子标签、不标难度），走打标台的 HTTP API 直写。触发词：AI 打标、AI 批改、打标、跑一遍某卷、给这卷打标、标注知识点、tag this paper。适用项目：D:/Tasks/questionBank（A-Level/IGCSE 题库，Edexcel/AQA/CIE）。用法特征：用户给出卷标识（如 'AQA 2019年1月 PH01'、'CIE 2016-mar-qp12'），逐题看图选标签 + 定主标签 → PUT /api/questions/{qid}/annotation → 出 HTML 报告到 docs/。"
agent_created: true
---

# AI 打标 · 题库知识点标注工作流

给题库里的真题逐题标注**知识点标签**（二级/叶子标签）**和主标签**（每个标注单元恰好一个）。
**难度一律留空**（难度由人在打标台预览模式补，AI 不碰）。

## 何时用

用户说"跑一遍 / 打标 / AI 打标 <某卷>"，且卷在 `D:/Tasks/questionBank/data/<board>/<qual>/<subject>/<session>/`。

## 前置检查（开工前必做）

1. **确认打标台后端在跑**：`curl -s -m 5 -o /dev/null -w "%{http_code}" http://127.0.0.1:8642/api/papers` 返回 200。
   没跑就按 `references/pitfalls.md` 的「重启后端」起一个；**不要拉 `workbench/start.cmd`**。
2. **确认用户此刻没在用打标台手动打标**（写库会撞车，尤其两人同时改同一卷）。
3. **只处理 `status: "none"` 的题**，不覆盖已有标注（除非用户明说"重打"）。

## 流程

### 1. 拉卷上下文

```bash
python scripts/fetch_paper.py <卷目录名> --json <临时文件>
```

卷目录名 = `data/.../<session>` 的最后一段，如 `2019-jan-ph01`。脚本输出每题的：
`qid / question_number / question_type / total_marks / split_eligible / top_labels / image_url / tags_now / available_tags / available_groups`。

### 2. 逐题看图

用 Read 工具**直接读 `assets/qNN.webp`**（多模态可读，不必转格式）。看图是唯一可靠依据——PDF 文本层会丢图、丢分式、丢分值。

**一次读 4–6 张，分多批**。连续大批量读同一类文件会触发客户端的「重复工作 / 疑似卡死」提示。

### 3. 每题做四个判断

| 判断 | 依据 | 动作 |
| --- | --- | --- |
| **图对得上吗** | 图上肉眼可见的题号 vs front-matter 的 `question_number` | 对不上 → 记 `skip_reason`，不打标 |
| **要不要拆分** | 后端给的 `split_eligible`（= cut.json 顶层小问 ≥2），**不自己判断** | true → 提交 `subs`；false → 整题 |
| **选哪些标签** | 只从该题 API 返回的 `available_tags` 里选 | 见 `references/rules.md` §二 |
| **主标签是哪个** | 每个标注单元**恰好一个**：单标签自动即主（留空即可）；≥2 标签必须显式挑一个 | 见 `references/rules.md` §三 |

### 4. 写决策表

写一个 `decisions.json`（格式见 `references/decisions-format.md`），逐题记：标签、**主标签**、
每个小问的标签与主标签、判断理由、是否低置信、跳过原因。

### 5. 写库

```bash
python scripts/apply_tags.py decisions.json            # 先 dry-run：会打印每题的「标签 ★主=…」
python scripts/apply_tags.py decisions.json --apply    # 确认无误再写
```

脚本经 `PUT /api/questions/{qid}/annotation` 写库：

- 整题：`{tags: [...], difficulty: <读回的现状值>, primary_tag: <主标签|null>}`
- 拆分：`{subs: [{label, tags, difficulty: <现状值>, primary_tag}], difficulty: null}`

三条要点：**① 难度不会被 AI 改动**（脚本先 GET 读回原值再原样提交）；
**② 多标签缺主标签 → 该题报错、整题不写库**（不会半写），确不可判时才加 `--auto-primary` 应急兜底；
**③ 绝不能直接改 `qNN.md`**——拆分题保存要后端跑 `render_subsplit` 产子图，直写文件没这个副作用。

### 6. 出报告

```bash
python scripts/make_report.py decisions.json --out <项目根>/docs/NN-AI打标报告-<board>-<卷>.html
```

报告落 `docs/`（该目录在 gitignore 内、不入库）。报告必须含：**成功清单（主标签带「主」标记）/ 低置信 /
无合适标签（留空）/ 疑似错图 / 主标签兜底**，外加本轮发现的**词表缺口**。

## 硬规则（不可违背）

1. **只用本卷 `available_tags`**（含跨单元开放的组）。挑不出合适的就**留空 + 报告**，绝不跨单元/跨卷找标签硬凑。
2. **置信度低 → 照填，但必须在报告单列「低置信」**，让用户优先复看。
3. **错图（图上题号 ≠ 元数据题号）→ 不打标 + 报告**。多页题只看首页题号。
4. **难度一律留空**（`difficulty: null` → 整行不写）。不自动 confirm，确认留用户在预览模式做。
5. **标签数量 = 该题实际考到的核心知识点个数**，不按分值设下限；分值只作**建议上限（非硬性）**：MCQ ≤2、3–5 分 ≤3、≥6 分 ≤4。详见 `references/rules.md`。
6. **主标签每标注单元恰好一个**：`tags` 只有 1 个 → `primary_tag` 留空（后端自动补）；
   `tags` ≥2 → **必须**显式给出且 ∈ `tags`，否则写库被拒。拆分题**逐个小问各判一个**，
   留空的小问不给主标签。判不出主线就回看题图；`--auto-primary` 仅应急，用了必在报告标黄。
7. **切分题里个别小问没有合适标签** → **只把那个小问留空**（`tags: []`），其余小问照常提交；报告里显示为「留空 · 无合适标签」。不要为了凑满乱填。
8. **发现词表缺口 → 报告里明确列出**（哪个知识点、哪几题、建议怎么补），不要自己改词表，等用户决定。
9. **不主动 git commit**；批量写库前先说清楚范围。

## 脚本

| 脚本 | 作用 |
| --- | --- |
| `scripts/fetch_paper.py` | 拉一卷逐题结构 + 候选标签，输出 JSON |
| `scripts/apply_tags.py` | 读 `decisions.json`，经 API 写库（默认 dry-run）。含主标签校验：多标签缺主 → 该题报错不写；`--auto-primary` 应急兜底 |
| `scripts/make_report.py` | 由 `decisions.json` 生成 HTML 报告 |

## 参考

- `references/rules.md` — 标签判定细则、数量规则、低置信/跳过的边界
- `references/decisions-format.md` — `decisions.json` 字段说明
- `references/pitfalls.md` — 环境坑：重启后端、schema qualification、LF 换行、taxonomy 缓存等
