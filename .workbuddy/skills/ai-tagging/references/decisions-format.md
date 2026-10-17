# decisions.json 格式

一份决策表同时驱动 **写库**（`apply_tags.py`）和 **报告**（`make_report.py`）。先把 `fetch_paper.py` 的输出拿在手上，逐题看图后填这张表。

## 顶层字段

```json
{
  "paper": "2019-jan-ph01",
  "board": "aqa",
  "qual": "a-level",
  "subject": "physics",
  "title": "AQA Physics 2019 January · PH01",
  "subtitle": "AS Paper 1 · 23 题 · 单元 U1 · 会话内看图 + 打标台 API 直写",
  "items": [ ... ],
  "vocabulary_gaps": [ ... ],
  "notes": [ ... ]
}
```

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `paper` | ✅ | 卷目录名（与 `fetch_paper.py` 的一致） |
| `board` / `qual` / `subject` | ✅ | 用于报告里定位词表（其实报告是扫全库词表，填上更清楚） |
| `title` / `subtitle` | 建议 | 报告标题与副标题 |
| `items` | ✅ | 逐题决策 |
| `vocabulary_gaps` | 可选 | 词表缺口清单，**必须是对象数组**：`{"point": 知识点, "where": 题号(分值), "detail": 建议+当前近似}` |
| `notes` | 可选 | 报告补充说明块，**必须是对象数组**：`{"kind": "ok"\|"warn"\|"info", "title": 标题, "html": 正文}` |

> ⚠️ 这两个字段**不能写纯字符串**——`make_report.py` 渲染时会对每项调 `.get()`，
> 写字符串会抛 `AttributeError: 'str' object has no attribute 'get'`（缺键会 KeyError）。

## item（逐题）

**打标（整题，`split_eligible == false`）**

```json
{ "n": 5, "qid": "aqa-a-level-physics-2019-jan-ph01-q05",
  "marks": 5, "tags": ["u1-elastic-strain-energy"], "primary_tag": null,
  "why": "力–伸长图求弹性势能（单标签：主标签留空，后端自动补成它）" }

{ "n": 8, "qid": "aqa-a-level-physics-2019-jan-ph01-q08",
  "marks": 6, "tags": ["u1-moments", "u1-resolving"], "primary_tag": "u1-moments",
  "why": "力矩是主问，力的分解只是手段（多标签：主标签必填）" }
```

**打标（拆分，`split_eligible == true`）**　— 每个小问各带一个主标签

```json
{ "n": 6, "qid": "aqa-a-level-physics-2019-jan-ph01-q06", "marks": 12,
  "subs": [
    { "label": "1", "tags": ["u1-free-body"], "primary_tag": null, "why": "…", "low": false },
    { "label": "2", "tags": ["u1-moments", "u1-resolving"], "primary_tag": "u1-moments",
      "why": "…", "low": true }
  ] }
```

> `label` 必须来自 `fetch_paper.py` 给的 `top_labels`，逐字一致。

**跳过（打不了标）**

```json
{ "n": 7, "qid": "…", "marks": 12,
  "skip_reason": "P4 词表无「数字信号」标签", "skip_kind": "no-tag", "why": "整题两小问都无对应标签" }
```

`skip_kind` 取值：`"no-tag"`（无合适标签）/ `"image"`（疑似错图）。缺省按 `no-tag` 计。

## 主标签 `primary_tag`

每个**标注单元**（整题 = 1 个单元；拆分题 = 每个小问各 1 个）恰好一个主标签。规则：

| `tags` 个数 | `primary_tag` 怎么填 |
| --- | --- |
| 1 | **留空**（`null` 或不写）——后端自动补成该标签并显式落盘 |
| ≥2 | **必填**，且必须是 `tags` 里的一个；缺了 → `apply_tags.py` 报错、后端 422 拒收 |
| 0（跳过/留空） | 不填 |

怎么挑主线考点见 `rules.md` §三。**不要机械取 `tags[0]`** —— 那是 `--auto-primary` 的应急兜底，
只在你反复看图仍判不出时才用，用了会在报告里标黄提醒人工复看。

## 字段速查

| 字段 | 适用 | 说明 |
| --- | --- | --- |
| `n` | 全部 | 题号（字符串或数字，报告原样显示） |
| `qid` | 全部 | 从 `fetch_paper.py` 输出拷；**写库必需** |
| `marks` | 全部 | 分值 |
| `tags` | 整题 | 标签 id 数组 |
| `primary_tag` | 整题 / `subs[]` | 主标签 id：**1 个标签留空、≥2 个必填且 ∈ tags**（见上一节） |
| `subs` | 拆分题 | `[{label, tags, why, low}]` |
| `why` | 全部 | 判断依据，显示在报告"依据"列；跳过题写在这里 |
| `low` | 可选 | `true` = 低置信，报告会标黄并计入统计 |
| `skip_reason` | 跳过 | 非空即视为跳过，不写库 |
| `skip_kind` | 跳过 | `no-tag` / `image` |
| `sub_prefix` | 可选 | 小问标签前缀（如写 `"("` 让报告显示 `(a)`） |

## 常见工作流

```bash
# 1) 拉上下文
python scripts/fetch_paper.py 2019-jan-ph01 --json /tmp/pp.json

# 2) 读图 + 手写 decisions.json（可先把 fetch 输出的 items 拷进去，逐题补 tags/subs）

# 3) 先 dry-run 看一遍
python scripts/apply_tags.py /tmp/dec.json

# 4) 写库
python scripts/apply_tags.py /tmp/dec.json --apply

# 5) 只重跑某几题（补词表后）
python scripts/apply_tags.py /tmp/dec.json --apply --only 5,6

# 5b) 实在判不出主标签时的应急兜底（取 tags[0]，结果与报告会标黄）
python scripts/apply_tags.py /tmp/dec.json --apply --auto-primary

# 6) 出报告
python scripts/make_report.py /tmp/dec.json --out <root>/docs/14-AI打标报告-aqa-2019-jan-ph01.html
```
