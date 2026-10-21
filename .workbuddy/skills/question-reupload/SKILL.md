---
name: question-reupload
description: "把题库里「某一道题」重新传到云端，并清理该题的审核确认（✓）、打标（tags/difficulty/primary_tag）、有误标记（flagged）。触发词：重传这题、单题重传、单独传一个题、这题图糊了重传、重切这一题、换题图、reset 某题、把这题状态清掉。适用项目：D:/Tasks/questionBank（A-Level/IGCSE 题库，云端 140.143.229.85:/opt/qbank）。核心铁律：只碰这一题的文件、先备份云端、剥标签在临时副本上做、绝不整卷 rsync --delete。"
agent_created: true
---

# 单题重传 · 云端单题更新与状态清理

把**一道题**的新版本推到云端，并把它之前的「已核对 / 已打标 / 有误」状态处理干净。

## 何时用

- 「这道题的图糊了 / 切歪了，重传一下」
- 「这题题干有错，我改好了，单独传上去」
- 「重切这一题」（本地重跑 `cut_paper.py` 只针对某题）
- 用户给了卷号 + 题号，让你更新云端那一道题

**不适用于**：整卷首入库、换源重切整卷、传 PDF → 那些走 `rsync` 整卷目录（见 `plans/题包上传接口方案.md` §10）。

## 两条铁律

1. **只碰这一题的文件**。绝不 `rsync` 整个卷目录，**绝不加 `--delete`**。云端按小问切出来的子图本地往往没有，整卷删同步会清掉它们。
2. **云端现有文件先备份**。脚本默认备份到 `_prev/reupload/<卷>-<题>-<时间戳>/`，出问题可以原地 scp 回去。

反面教材：本地直接 `rsync -av 本地卷/ 云端卷/` —— 本地那份可能是几天前的（**本地有 1265 个带 tags 的 md，云端另有老师的标注**），一覆盖就把云端标签顶掉，而 `confirmed.json` 里的 ✓ 还亮着，变成假亮。

## 两种模式

| 模式 | 动什么 | 标签/难度 | 用于 |
|---|---|---|---|
| `--mode image` | 只传 `assets/qNN.webp` + `qNN.layout.json` | **原样保留** | 只是图糊了/重渲，文字没变 |
| `--mode reset`（默认） | 连 `qNN.md` 一起传，并**剥掉** tags / difficulty / primary_tag | **清空** | 题目内容变了，要重新打标 |

## 标准流程

先 dry-run 看清单，再执行：

```bash
cd D:/Tasks/questionBank
PY=C:/Users/jnwpq/.workbuddy/binaries/python/versions/3.13.12/python.exe
S=.workbuddy/skills/question-reupload/scripts/reupload.py

# 1) 预演：只列云端现有文件 + 将上传/将删除的清单，不动任何东西
$PY $S 2020-jan-wph11 14 --mode reset --dry-run

# 2) 执行（会提示输入 yes 确认）
$PY $S 2020-jan-wph11 14 --mode reset

# 3) 只换图、保留标签
$PY $S 2020-jan-wph11 14 --mode image

# 4) 本地重切后不再分小问 → 顺带删掉云端的子题文件
$PY $S 2020-jan-wph11 14 --mode reset --drop-subs
```

脚本自动做六件事：备份云端 → 剥标注（在临时副本上，不改你的本地文件）→ scp 上传 →
（可选）删子题 → 在服务器上撤 `confirmed.json` 该题所有单元 → 清 `flagged.json` → 打印清单。

**前提**：本地 `data/*/*/*/<卷>/qNN.md` 要是你要传的那一版。脚本不会从云端下拉覆盖它。

## 三种状态的处理方式

| 状态 | 存在哪 | 重传后 |
|---|---|---|
| 标签 / 难度 / 主标签 | `qNN.md` 的 front-matter 里（**就是文件本身**） | `image` 模式原样保留；`reset` 模式被脚本剥掉 |
| 审核确认 ✓ | `data/_review/confirmed.json`，键 = 确认单元 id | **不会自动失效**，必须显式撤（脚本已做） |
| 此题有误 | `data/_review/flagged.json`，带文件指纹 | 内容变了自动失效；脚本也会主动清 |

确认单元：非切分题 = 主题 qid；切分题 = **每个子题文件的真实 id**（不是 `{qid}-{label}` 拼的，Edexcel 旧格式是 `...-q15a`）。脚本在服务器上 import `store` 拿 `sub_files`，与 `api._units_of` 口径一致。

## 验收（做完必看）

1. 打标台刷新，该题图是新的 → `/files/` 响应头是 `no-cache`，**不用清浏览器缓存**。
2. 题目列表里该题的 `status` 变成 `none`（reset 模式），`verify_state` 不是 `all`。
3. 服务器上确认：
   ```bash
   ssh -i ~/.ssh/wpq.pem ubuntu@140.143.229.85 \
     "cd /opt/qbank && /opt/qbank/venv/bin/python -c \
      \"import sys,json;sys.path.insert(0,'workbench/backend');import review;print(len(review.confirmed_ids()))\""
   ```
   （数字比之前小 = 撤掉了）
4. **不需要重启**：`read_meta` 按 (mtime, size) 校验缓存，`taxonomy` 也是按签名热加载。

## 陷阱

- **`qNN.md` 既是题目也是标注载体**。传它 = 标签跟着走。要保留标签就别传 md（用 `--mode image`）。
- **子题孤儿**：云端有 `qNN-a.md` / `qNN-a.webp`，你只传主题 → 预览里旧的 a/b/c 照旧出现。本地不再分小问时必须 `--drop-subs`。
- **重切整卷前**先删本地该卷所有 `qNN-*.md` 与 `assets/qNN-*.webp`，否则旧子图基于旧整题裁剪，与新图不配套。
- **别用内置 md 编辑器打开 `data/` 下的文件**（会回写 front-matter，历史致灾 3 文件）。
- 本机**没有 rsync**（有 ssh / scp），所以脚本用 scp 逐文件搬。若以后装了 rsync 也不要换成整目录同步。

## 手工兜底（脚本跑不通时）

```bash
P=2020-jan-wph11; K=~/.ssh/wpq.pem; H=ubuntu@140.143.229.85
R=/opt/qbank/data/edexcel/a-level/physics/$P
L=data/edexcel/a-level/physics/$P

# 备份云端该题
mkdir -p _prev/manual-$P-q14
scp -i $K $H:$R/q14.md $H:$R/assets/q14.webp $H:$R/assets/q14.layout.json _prev/manual-$P-q14/

# 传（注意 assets 要单独一次，目录不同）
scp -i $K $L/q14.md $L/assets/q14.webp $L/assets/q14.layout.json $H:$R/
scp -i $K $L/assets/q14.webp $L/assets/q14.layout.json $H:$R/assets/

# 撤确认（在服务器上）
ssh -i $K $H "/opt/qbank/venv/bin/python - <<'EOF'
import sys; sys.path.insert(0,'/opt/qbank/workbench/backend')
import store, review
qid='edexcel-a-level-physics-2020-jan-wph11-q14'
paper, qpath = store.find_question(qid)
units=[qid]+[str(store.read_meta(s)['id']) for s in paper.sub_files(qpath)]
for u in units: review.set_confirmed(u, False)
print('removed', units)
EOF"
```
