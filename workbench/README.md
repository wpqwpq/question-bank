# workbench · 审核打标台

题库 Web 工作台。一期只有**审核打标台**：给已切割的题目逐题打知识点标签 + 难度。

## 启动（本地单机后端）

双击 `start.cmd`，或命令行运行。启动后**只有 API**（<http://127.0.0.1:8642/docs>），不再提供页面。

要求：Python 依赖 fastapi/uvicorn/pyyaml 已装在 WorkBuddy 托管 venv（`envs/default`）。

## 前端在哪

> **唯一前端源码 = `cloud/workbench/frontend/`**，在这里 `npm run dev` / `npm run build`。

2026-10-20 删除了原来的单机版前端 `workbench/frontend/`。理由：它停在 10-16、落后 4 天，
而且 `node_modules` 是空目录（装不了依赖、跑不起来），长期造成「本地前端落后线上」的误判。
git 里仍保留其历史，需要时 `git checkout HEAD -- workbench/frontend` 可完整恢复。

npm install 注意：本机 esbuild postinstall 会 EBUSY，用 `npm install --ignore-scripts`。

## 结构

- `backend/` — FastAPI。`store.py`（data/ 扫描 + front-matter 外科手术式写回）、`api.py`（路由 + 校验）、`main.py`（入口，`frontend/dist` 存在时顺带托管静态页）、`taxonomy.py`（标签词表）。
- `start.cmd` — 一键启动后端。

## API 一览

| 方法 | 路径 | 作用 |
|------|------|------|
| GET | `/api/papers` | 卷列表（含打标进度） |
| GET | `/api/papers/{pid}/questions` | 题目列表（含打标状态） |
| GET | `/api/questions/{qid}` | 单题详情 + 本单元可选标签 |
| PUT | `/api/questions/{qid}/annotation` | 写盘（≥1 标签且难度必选，标签限本卷单元） |
| GET | `/api/taxonomy` | 标签词表 |
| 静态 | `/files/*` | 题图（→ `data/`） |

> `/` 的 SPA 兜底只在 `frontend/dist/` 存在时挂载；目录已删，本地只剩 API。

## 约定（重要）

- **写回只动 `tags` / `difficulty`**：front-matter 其余行与正文一字不动，临时文件原子替换，
  写后做 schema **差分校验**（只拦新引入的错误——存量数据 `question_number` 是整数、
  schema 要求是字符串，属历史违规，不背责、不静默修）。
- **标签 id 带单元前缀**（`u1-momentum` / `u4-momentum`），全局唯一，落盘值即 id；
  词表唯一来源是 `data/taxonomy/tags.json`。
- 单元映射：`paper_code` 中 `1X` → `UX`（WPH11→U1 … WPH16A→U6），后端推导。
- 不自动 git commit。
