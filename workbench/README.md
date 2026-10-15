# workbench · 审核打标台

题库 Web 工作台。一期只有**审核打标台**：给已切割的题目逐题打知识点标签 + 难度。

## 启动（生产形态，本机单人）

双击 `start.cmd`，或命令行运行后打开 <http://127.0.0.1:8642>。

要求：前端已构建（`frontend/dist/` 存在，已随仓库提交）；Python 依赖 fastapi/uvicorn/pyyaml 已装在
WorkBuddy 托管 venv（`envs/default`）。

## 开发模式（改前端时用）

```bash
# 终端 1：后端
cd workbench/backend
PYTHONPATH="C:/Users/jnwpq/.workbuddy/binaries/python/envs/default/Lib/site-packages" \
  "C:/Users/jnwpq/.workbuddy/binaries/python/versions/3.13.12/python.exe" \
  -m uvicorn main:app --host 127.0.0.1 --port 8642

# 终端 2：前端热更新（Vite 代理 /api、/files 到 8642）
cd workbench/frontend
npm run dev   # 需先补 PATH coreutils，npm 用管理版 node 的 npm.cmd
```

改完前端要重新 `npm run build` 并把 `dist/` 提交（约定：dist 纳入仓库，两台电脑免构建直接起服务）。

npm install 注意：本机 esbuild postinstall 会 EBUSY，用 `npm install --ignore-scripts`。

## 结构

- `backend/` — FastAPI。`store.py`（data/ 扫描 + front-matter 外科手术式写回）、`api.py`（路由 + 校验）、`main.py`（入口 + 托管 dist）、`taxonomy.py`（标签词表）。
- `frontend/` — Vite + React 19 + TS(strict) + Tailwind v4。无 UI 组件库，样式手写。
- `start.cmd` — 一键启动。

## API 一览

| 方法 | 路径 | 作用 |
|------|------|------|
| GET | `/api/papers` | 卷列表（含打标进度） |
| GET | `/api/papers/{pid}/questions` | 题目列表（含打标状态） |
| GET | `/api/questions/{qid}` | 单题详情 + 本单元可选标签 |
| PUT | `/api/questions/{qid}/annotation` | 写盘（≥1 标签且难度必选，标签限本卷单元） |
| GET | `/api/taxonomy` | 标签词表 |
| 静态 | `/files/*` → `data/`、`/` → frontend/dist | |

## 约定（重要）

- **写回只动 `tags` / `difficulty`**：front-matter 其余行与正文一字不动，临时文件原子替换，
  写后做 schema **差分校验**（只拦新引入的错误——存量数据 `question_number` 是整数、
  schema 要求是字符串，属历史违规，不背责、不静默修）。
- **标签 id 带单元前缀**（`u1-momentum` / `u4-momentum`），全局唯一，落盘值即 id；
  词表唯一来源是 `data/taxonomy/tags.json`。
- 单元映射：`paper_code` 中 `1X` → `UX`（WPH11→U1 … WPH16A→U6），后端推导。
- 不自动 git commit。
