---
name: ai-tagging
description: "AI 给题库真题打知识点标签 + 主标签（默认不动难度；用户明确要求时按 rules §八 打难度），直写**云端**打标台 HTTP API。触发词：AI 打标、AI 批改、打标、跑一遍某卷、给这卷打标、标注知识点、打难度、tag this paper。适用项目：D:/Tasks/questionBank（A-Level/IGCSE 题库，Edexcel/AQA/CIE）。用法特征：用户给出卷标识（如 'Edexcel 2020年1月 WPH11'、'AQA 2019年1月 PH01'），逐题看图选标签 + 定主标签 → PUT /api/questions/{qid}/annotation → 出 HTML 报告到 docs/。"
agent_created: true
---

# AI 打标 · 题库知识点标注工作流（云端）

给题库里的真题逐题标注**知识点标签**（二级/叶子标签）**和主标签**（每个标注单元恰好一个）。

三条不可动摇的约定：

1. **难度默认不动**：`difficulty` 原样保留，AI 不碰。**只有用户明确要求「打难度」**时，才按
   `references/rules.md` §八 判定，并在 `apply_tags.py` 加 `--difficulty` 提交；没加开关时
   脚本会把 decisions 里的难度**忽略**并提示（防止误覆盖人工已标的难度）。
2. **题目有问题不点「此题有误」**——只在报告里说明情况，标记与否由人决定。
3. **有小问的题一律按小问打标**（`top_labels ≥ 2` 就必须提交 `subs`），不许整题糊过去。

## 端点与鉴权（只打云端）

| | 地址 | 鉴权 |
| --- | --- | --- |
| 云端打标台 | `http://140.143.229.85` | `Authorization: Bearer <API Key>`，账号 `ai-tagger`（role=service） |

- 卷的**数据源就是云端**：题面图从 `/files/...` 取，标签候选从 API 取，不在本地 `data/` 上做任何事。
- API Key 解析顺序：`--key` > `QBANK_API_KEY` > `~/.qbank_key`（本机已放好，一行纯文本，**不入库**）。
  重签：服务器上 `manage_users.py newkey ai-tagger`（旧 Key 立刻失效，记得同步 `~/.qbank_key`）。
- ⚠️ **本机带 `HTTP_PROXY`/`HTTPS_PROXY`（WorkBuddy 本地代理），urllib 会照走 → 打裸 IP 一律 502**。
  两个脚本已内置 `ProxyHandler({})` 强制直连，不要改回去。
- ⚠️ 网络偶发抖动（同一接口 0.05 s 与 7 s 都可能），脚本已带 4 次退避重试；
  `curl` 手工探测要加 `--noproxy '*'`，否则假 502。
- 单次 PUT 约 1–8 s（Edexcel 拆分要走 `render_subsplit` 现裁子图），**这是正常耗时，别当超时**。

## 前置检查（开工前必做）

1. **鉴权可用**：
   ```bash
   curl -s -m 20 --noproxy '*' -H "Authorization: Bearer $(cat ~/.qbank_key)" \
        -o /dev/null -w "%{http_code}\n" http://140.143.229.85/api/papers    # 200
   ```
   401 = Key 失效，找用户重签；502 = 走了代理，加 `--noproxy '*'`。
2. **确认用户此刻没在打标台手动改同一卷**（写库会撞车）。
3. **只处理 `status: "none"` 的题**，不覆盖已有标注（除非用户明说「重打」）。
4. **串行、一卷一卷跑**，不要开多个 worker 同时写不同卷——云端索引是单进程刷新。

## 流程

### 1. 拉卷上下文 + 下载题图

```bash
python scripts/fetch_paper.py 2020-jan-wph11 --json /tmp/ctx.json --images /tmp/img
```
- 卷可以给**完整 id**（`edexcel-a-level-physics-2020-jan-wph11`）、**会话目录名**（`2020-jan-wph11`）
  或**卷号**（`wph11`）；命中多卷时会报错要求给完整 id，不瞎猜。
- `--images DIR` 把每题的 `qNN.webp` 下到 DIR（已存在非空则跳过），输出里的 `image_local` 就是给 Read 用的路径。
- 输出每题：`n / qid / type / marks / split_eligible / top_labels / image_local / tags_now /
  available_tags / available_groups`。

### 2. 逐题看图

用 Read 工具读 `image_local`（`.webp` 多模态可读，不必转格式）。

**一次读 4–6 张，分多批**。连续大批量读同一类文件会触发客户端的「重复工作 / 疑似卡死」提示。
看图是唯一可靠依据——PDF 文本层会丢图、丢分式、丢分值。

### 3. 每题做四个判断

| 判断 | 依据 | 动作 |
| --- | --- | --- |
| **题目本身有没有问题** | 图上题号与 `n` 对不上 / 题干或图被截断 / 选项缺失 / 小问结构明显对不上 | 记 `issue`（一句话说清），**不打标、不点有误**，报告里单列 |
| **要不要按小问打标** | `top_labels`（= cut.json 顶层小问）**≥2 就必须按小问** | 提交 `subs`，每个小问一条 |
| **选哪些标签** | 只从该题 API 返回的 `available_tags` 里选 | 见 `references/rules.md` §二 |
| **主标签是哪个** | 每个标注单元**恰好一个**：单标签自动即主（留空即可）；≥2 标签必须显式挑一个 | 见 `references/rules.md` §三 |

> `split_eligible` 与 `top_labels ≥ 2` 同源（都来自 cut.json 顶层小问）。有小问却没被切分的卷，
> 提交 `subs` 后云端会自动补切子图（Edexcel）或只写子题文件（AQA/CIE）。
> 若图上明明有 (a)(b)(c) 而 `top_labels` 为空 → 属切割数据缺失，记 `issue`，按整题打标并在报告说明。

### 4. 写决策表

写 `decisions.json`（格式见 `references/decisions-format.md`），逐题记：标签、**主标签**、
每个小问的标签与主标签、判断理由、是否低置信、`issue` 说明。

### 5. 写库

```bash
python scripts/apply_tags.py /tmp/dec.json            # 先 dry-run：会打印每题的「标签 ★主=…」
python scripts/apply_tags.py /tmp/dec.json --apply    # 确认无误再写
```

脚本经 `PUT /api/questions/{qid}/annotation` 写库（与人工点击同一条代码路径）：

- 整题：`{tags: [...], difficulty: <读回的现状值>, primary_tag: <主标签|null>}`
- 按小问：`{subs: [{label, tags, difficulty: <现状值>, primary_tag}], difficulty: null}`
- 云端会落 `annotated_by=ai-tagger` 与审计日志 `data/_audit/YYYY-MM-DD.jsonl`，人工可追溯

脚本会**主动拦下**这几种情况（不写库）：
- 有小问却整题提交（`top_labels ≥ 2` 但没有 `subs`）；
- 没小问却提交 `subs`；
- `subs` 没覆盖全部小问（除非 `--allow-partial`）；
- 多标签缺主标签（`--auto-primary` 可应急兜底，会标黄）；
- 带 `issue` 的题（直接跳过）。

⚠️ **拆分题一次提交本题全部小问**（2026-10-19 起语义已变，规则仍保留）：
云端 `api._save_split` 现在对「没提交的小问」**保持原样**（只在本题从没有过该小问文件时
才补一个空壳），所以部分提交不再有破坏性；但 AI 的判定单位是整题，漏写的小问等于
「这一问没有结论」，仍要求全覆盖。

⚠️ **留空必须显式写 `tags: []`**：以前「不提交」等于「清空」，现在「不提交」等于
「保持原样」。要让某问真的留空（尤其是重跑时清掉上一轮的旧标签），必须把它放进
`subs` 且 `tags: []`。`apply_tags.py` 已按此实现，手写 payload 时别漏。

### 5b. 打难度（仅在用户明确要求时）

默认**不做**。用户说「打难度 / 顺便把难度标了」时：

1. 按 `references/rules.md` §八 逐单元判一个难度：`very-easy` / `easy` / `medium` /
   `hard` / `very-hard`。
2. 写进 `decisions.json` 的 `difficulty`（整题单元写在 item 上，拆分题写进每个 `subs[]`）。
3. 提交时**必须加 `--difficulty`**：
   ```bash
   python scripts/apply_tags.py /tmp/dec.json --apply --difficulty
   ```
   不加开关 = 「只打标签、难度保留原值」，decisions 里写了难度也会被忽略并提示。

⚠️ 难度是**整体感觉**，不是标签数量或分值的函数：6 分简答再简单也 `medium` 起步；
`very-easy` **只给「直接读/直接背/纯概念判断」的题**（要对应到对象、要换算的都不算，1 分题同样会到 medium/hard）。
已用 2024-06 / 2024-10 / 2025-01 / 2025-06 四卷人工值校准，判错率 **47% → 26% → 18% → 13%**，
判定五级顺序与全锚点见 `references/rules.md` §八。
**`very-hard` 的判据是「有一个几乎没人想得到的洞察」，不是算得多**（四卷 118 单元只出现 2 个）。

**审核后必做**：用户说「审核完了 / 学习一下」时，跑
`python scripts/diff_difficulty.py <decisions.json>`（只读），把新的错法补进
`rules.md` §八 的锚点表与判定细则——这是唯一让难度越打越准的机制，别跳过。

### 6. 出报告

```bash
python scripts/make_report.py /tmp/dec.json --out <项目根>/docs/NN-AI打标报告-<board>-<卷>.html
```

报告落 `docs/`（gitignore，不入库）。必须含：**成功清单（主标签带「主」标记）/ 低置信 /
无合适标签（留空）/ 题目存疑 / 主标签兜底**，外加本轮发现的**词表缺口**。

**题目存疑**一节是本技能对「有问题」的唯一出口：列出题号 + 一句话说明 + 判断依据，
并明确写「没有点『此题有误』，留给你判断」。

## 硬规则（不可违背）

1. **只用本卷 `available_tags`**（含跨单元开放的组）。挑不出精确的就按第 14 条借主题邻近标签 + `low`；
   完全不沾才留空 + 报告。**绝不跨单元/跨卷找标签硬凑**。
2. **难度默认一个字都不动**：脚本先 GET 读回原值再原样提交。只有用户明确要求打难度、且命令带
   `--difficulty` 时才写（见 5b 与 `references/rules.md` §八）。
3. **绝不调用 `/flag`（此题有误）**。题目有问题 → `issue` 字段 + 报告；标记是人的权力。
4. **有小问（`top_labels ≥ 2`）一律按小问打标**，每个小问单独判标签与主标签；不许整题提交。
5. **置信度低 → 照填，但必须在报告单列「低置信」**，让用户优先复看。
6. **题号对不上（错图）→ 不打标 + `issue`**。多页题只看首页题号。
7. **标签数量 = 该题实际考到的核心知识点个数**，不按分值设下限；分值只作**建议上限（非硬性）**：MCQ ≤2、3–5 分 ≤3、≥6 分 ≤4。详见 `references/rules.md`。
8. **主标签每标注单元恰好一个**：`tags` 只有 1 个 → 留空（后端自动补）；≥2 → **必须**显式给出且 ∈ `tags`。
   拆分题**逐个小问各判一个**，留空的小问不给主标签。判不出主线就回看题图；`--auto-primary` 仅应急，用了必在报告标黄。
9. **切分题里个别小问没有精确标签** → 先按第 14 条借主题邻近标签并标 `low: true`；只有完全不沾才
   把该小问留空（`tags: []`），其余小问照常提交；报告里显示为「留空 · 无合适标签」。
10. **发现词表缺口 → 报告里明确列出**（哪个知识点、哪几题、建议怎么补），不要自己改词表，等用户决定。
11. **不主动 git commit**；批量写库前先说清范围（哪卷、几题、预计多久）。
12. **抛体不挂匀加速**：平抛/斜抛题只标 `*-projectile-*`，**绝不再叠 `uniformly-accelerated-linear-motion`**；
    后者只给真正沿一条直线做匀加速的题（刹车、斜面直滑、自由落体、测 g）。详见 `references/rules.md` §二。
13. **终端速度只给「已经匀速」的题**：只有题干明确达到终端速度 / 要求求终端速度 / 处于匀速段才标
    `terminal-velocity`。**有阻力但仍在加速的题不标**（改标黏性、浮力等阻力相关标签）。
    2026-10-20 审核数据：该标签被用得最多（27 次）也被删得最多（3 次），是最易滥用的一个。
14. **留空是最后手段**：审核显示「留空」3/3 被人补，而标 `low` 的借位标签 5/5 被接受。
    确实没有精确标签时，**借该题所属主题的邻近标签 + `low: true`**，不要空着——空着等于把判断推给人。

## 脚本

| 脚本 | 作用 |
| --- | --- |
| `scripts/fetch_paper.py` | 拉一卷逐题结构 + 候选标签，可选下载题图（**云端唯一入口**） |
| `scripts/apply_tags.py` | 读 `decisions.json`，经 API 写库（默认 dry-run）；有小问必须给 subs；带 `issue` 的跳过 |
| `scripts/make_report.py` | 由 `decisions.json` 生成 HTML 报告（含「题目存疑」一节） |
| `scripts/diff_difficulty.py` | **审核后校准**：把 AI 初判难度与人工审核后的难度逐单元对比，输出分布对照 + 判低/判高明细（只读） |

## 参考

- `references/rules.md` — 标签判定细则、数量规则、低置信/题目存疑的边界
- `references/decisions-format.md` — `decisions.json` 字段说明（含 `issue` / `subs`）
- `references/pitfalls.md` — 环境坑：代理变量、网络抖动、Bearer 题图、云端拆分耗时、state/index 分离
