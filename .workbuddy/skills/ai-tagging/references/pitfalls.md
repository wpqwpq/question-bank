# 环境与坑（打标台 / 题库）

## 打标台服务

- 地址 `http://127.0.0.1:8642`（FastAPI + Vite React，代码在 `workbench/`）
- 静态题图经 `/files/...` 提供；但打标时**直接 Read 本地 `assets/qNN.webp` 更省事**

### 健康检查

```bash
curl -s -m 5 -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8642/api/papers   # 期望 200
```

### 重启后端（改过 `workbench/backend/*.py` 之后必须）

⚠️ **分寸**：只在「后端代码刚被改动」或「用户明确要求」时重启。如果只是**发现进程挂了**，
先报告用户、等他自己拉——不要擅自重启（他可能正在手动管理）。

```bash
# 1) 找 PID 并杀（Git Bash 需要把 system32 加进 PATH）
export PATH="/c/WINDOWS/system32:/c/WINDOWS:$PATH"
PID=$(netstat -ano | grep ":8642" | grep -i LISTENING | awk '{print $5}' | head -1)
[ -n "$PID" ] && taskkill //F //PID $PID

# 2) 后台起（用 Bash 工具的 run_in_background=true）
cd <项目根>/workbench/backend
PYTHONPATH=C:/Users/jnwpq/.workbuddy/binaries/python/envs/default/Lib/site-packages \
  C:/Users/jnwpq/.workbuddy/binaries/python/versions/3.13.12/python.exe \
  -m uvicorn main:app --host 127.0.0.1 --port 8642
```

- **不要**拉 `workbench/start.cmd`、不要用 `start` / `subprocess.Popen`（沙箱里会被回收）
- 杀掉旧进程后会出现一条 `task failed` 的后台任务通知——那是预期现象，忽略即可
- 预热索引约 2–3 秒；重启后第一次请求可能稍慢

### 改前端要重新构建

```bash
cd <项目根>/workbench/frontend && npm run build     # tsc --noEmit + vite build
```
构建产物 `dist/` 由后端静态托管，**改前端不需要重启后端**（每次请求读文件）。

## 数据侧的坑

### taxonomy 改动**不需要**重启

`data/taxonomy/**/*.json` 的缓存按文件签名（mtime+size）失效，改完立刻生效。用户可能在标签管理页自己加标签。

### LF 换行

Windows 上 `Path.write_text()` 会把 `\n` 写成 `\r\n`。**改数据文件一律走 `newline=""`**：

```python
with open(p, "w", encoding="utf-8", newline="") as f:
    f.write(text)
```

### 别用 md 编辑器碰 `data/`

内置 markdown 阅读器回写会毁 front-matter（历史上已毁过 3 个文件）。改标注只走 API。

### qualification 写法分裂（已缓解）

数据目录/front-matter 里 CIE 用 `alevel`，Edexcel/AQA 用 `a-level`，而 `schema/question.schema.json` 的 enum 只有 `a-level|igcse|gcse`。

- 读侧已互认（前端 `normQual`、后端 `taxonomy._qual_candidates`）
- **写侧**：子题的 schema 校验已改成与主文件做差分（`store.save_split`），否则 CIE 卷按小问拆分一律 500
- 新增 board 不要再引入第三种写法

### 数据不在 git 里

`data/` 被 gitignore（除 `taxonomy/`）。**批量写库前想清楚，没有 git 兜底**；需要回滚就先备份。

## 读图的坑

- 用 Read 工具直接读 `.webp` 即可（多模态可读）
- **一次读 4–6 张**：连续大批量读同类文件会触发客户端的「模型正在重复工作 / 疑似卡死」提示
- **看图优先于文本层**：PDF 文本层会丢图（"as shown" 的图没了）、把分式拆行、把小问分支搞乱

## 协作分寸

- 用户可能正在**同一个打标台**上手动打标；批量写库前先确认，避免覆盖他的改动
- 不主动 `git commit`；批量动作先说清范围
- 报告落 `docs/`（该目录 gitignore，不入库）
