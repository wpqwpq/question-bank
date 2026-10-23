# 题库打标平台 · 云端部署（一期）

本地 `D:\Tasks\questionBank` 是**维护主力**，这套 `cloud/` 是**云端那一份**。
两边代码从此分叉：本地继续单人打标、什么都能干；云端只读题库、只写标注、要登录。

> 一条铁律：**本地只负责「加新卷」，云端只负责「写标注」。**
> 已经推上去的卷，标签和难度只在服务器上改；本地不要动它。
> 两边同时改同一卷，文件级同步没有任何提示，会静默覆盖——这是整套方案里唯一能真正丢数据的操作。

---

## 0. 职责边界（2026-10-18 定）

打标台与切割链从此分开维护，**本目录是打标台的源码真源，但本地永不运行它**（本地不起服务、不装它依赖）。要跑就在云端跑。

| 归属 | 内容 | 口径 |
|---|---|---|
| 本地 `scripts/`（仓库根） | 切割链：`cut_paper*.py`、`render_questions.py`、`validate_cut.py`、`audit_cut_edges.py`、`import_*.py`、`fetch_*.py`、`preview_image.py`、`batch_run.py` | 只在这里维护，云端不存 |
| 本目录 `cloud/` | 打标台：`workbench/backend` 10 个 py、`workbench/frontend`（src + dist）、`schema/`、`ops/` | 源码真源；改完按 §3 部署，**本地不运行** |
| **双份共享**（改一边必须对另一边） | `scripts/render_questions.py`、`scripts/render_subsplit.py` → 云端 `/opt/qbank/scripts/` | 云端版带「缺 pymupdf/Pillow 的降级」与 `pdf_absent_ok` 参数，**不是同一份文件**，别直接覆盖 |
| 只跑在云端 | `cloud/scripts/build_index.py`、`cloud/scripts/migrate_state.py` | 根 `scripts/` 没有这两个，只有云端那份 |

**前端 `dist/` 必须入库**：服务器上没有 node，重建不了。前端改动只能在本地 `vite build` 后整包传上去，所以仓库里的 `dist/` 就是线上正在跑的那一版。

### 云端有就地 Git（2026-10-18 起）

`/opt/qbank` 已 `git init`，只跟踪代码：

- 跟踪：`workbench/`（后端 py + 前端 src + dist）、`schema/`、`scripts/`、`ops/`、`README.md`、`backup.sh`
- 排除：`data/`、`var/`、`logs/`、`backup/`、`venv/`、`config/`（含 `secret.key` 与 `users.yaml`，绝不入库）、`__pycache__`
- **没有 remote**，是一份「线上就地版本控制」：作用是在云上直接改也能 `git diff` 看改了什么、`git checkout -- <file>` 一键回滚，不承担与 GitHub 同步的职责。

云端日常用法：

```bash
ssh -i ~/.ssh/wpq.pem ubuntu@140.143.229.85
cd /opt/qbank && git status --short   # 线上有没有被手改过
git log --oneline -5                  # 最近改了什么
```

⚠️ 本地 `cloud/` 仍是 GitHub 仓库 `question-bank` 的一部分；云端那个 `.git` 是独立的、不同仓库。两边的一致性靠手动部署 + md5 对拍，不靠 pull。

---

## 1. 这份目录是什么形状，以及为什么不能改

```
cloud/
├── workbench/backend/      # FastAPI（store/api/review/taxonomy/auth/main/manage_users）
├── workbench/frontend/     # Vite + React，dist/ 是构建产物
├── scripts/                # 只放 render_questions / render_subsplit（后端 import 它们读 cut.json）
├── schema/                 # 三个 JSON Schema（后端启动即用）
├── ops/                    # systemd / nginx / backup / logrotate 模板
└── config/                 # 运行时生成：users.yaml、secret.key（**不入库**）
```

部署后服务器上的形状必须与之一致：

```
/opt/qbank/{workbench/{backend,frontend/dist}, scripts, schema, data, config, logs, backup}
```

原因：`store.py` 用 `Path(__file__).resolve().parents[2]` 定位 `data/`、`schema/`、`scripts/`。
把 `backend/` 单独拷到别处，它会找不到数据目录而静默返回空题库。

---

## 2. 第一次部署（从零到能登录）

```bash
# 0) 本机 → 服务器
KEY=~/.ssh/wpq.pem
HOST=ubuntu@140.143.229.85

# 1) 系统依赖
ssh -i $KEY $HOST 'sudo apt-get update -qq && sudo apt-get install -y python3.12-venv nginx rsync'

# 2) 目录
ssh -i $KEY $HOST 'sudo mkdir -p /opt/qbank/{data,config,logs,backup,scripts,schema,workbench/backend,workbench/frontend} && sudo chown -R ubuntu:ubuntu /opt/qbank'

# 3) 代码（注意排除 node_modules / config / data / .tmp）
cd /d/Tasks/questionBank/cloud
tar cf - --exclude=node_modules --exclude=.tmp --exclude=config --exclude=data \
        --exclude=__pycache__ workbench scripts schema ops README.md \
  | ssh -i $KEY $HOST 'tar xf - -C /opt/qbank'

# 4) Python 环境（**不装 pymupdf / Pillow** —— 服务器不生产图片）
ssh -i $KEY $HOST 'python3 -m venv /opt/qbank/venv && /opt/qbank/venv/bin/pip install -q -i https://mirrors.aliyun.com/pypi/simple/ fastapi "uvicorn[standard]" pyyaml jsonschema'

# 5) 题库数据（不含 PDF，约 670 MB）
cd /d/Tasks/questionBank
tar cf - --exclude='*.pdf' data | ssh -i $KEY $HOST 'tar xf - -C /opt/qbank --strip-components=0'

# 6) 账号（83 个）+ 初始密码 CSV —— 只在服务器上生成，明文不落地到本机
ssh -i $KEY $HOST 'cd /opt/qbank/workbench/backend && PYTHONPATH=. /opt/qbank/venv/bin/python manage_users.py init'

# 7) systemd + nginx
scp -i $KEY cloud/ops/qbank.service $HOST:/tmp/
ssh -i $KEY $HOST 'sudo install -m644 /tmp/qbank.service /etc/systemd/system/qbank.service && sudo systemctl daemon-reload && sudo systemctl enable --now qbank'
scp -i $KEY cloud/ops/nginx-qbank.conf $HOST:/tmp/
ssh -i $KEY $HOST 'sudo install -m644 /tmp/nginx-qbank.conf /etc/nginx/sites-available/qbank && sudo ln -sf /etc/nginx/sites-available/qbank /etc/nginx/sites-enabled/qbank && sudo rm -f /etc/nginx/sites-enabled/default && sudo nginx -t && sudo systemctl reload nginx'

# 8) 备份 + 日志轮转
scp -i $KEY cloud/ops/backup.sh cloud/ops/qbank-logrotate.conf $HOST:/tmp/
ssh -i $KEY $HOST 'install -m755 /tmp/backup.sh /opt/qbank/backup.sh && sudo install -m644 /tmp/qbank-logrotate.conf /etc/logrotate.d/qbank && (crontab -l 2>/dev/null | grep -v backup.sh; echo "0 3 * * * /opt/qbank/backup.sh >> /opt/qbank/logs/backup.log 2>&1") | crontab -'
```

**成功判据**：浏览器打开 `http://140.143.229.85` 看到登录页（不是超时，也不是 nginx 欢迎页）。

---

## 3. 日常运维

### 上传新切好的一卷（不用重启后端）

```bash
# 整个 session 目录一起传，不要只传 webp —— 图和 md/layout 是配套的
rsync -av -e "ssh -i ~/.ssh/wpq.pem" \
  data/edexcel/a-level/physics/2024-june/ \
  ubuntu@140.143.229.85:/opt/qbank/data/edexcel/a-level/physics/2024-june/
```

后端每次请求会比对目录 mtime 快照，新卷下一个请求自动出现。**不需要重启。**

真正需要重启的只有一种：**改了后端 `.py`**（`sudo systemctl restart qbank`）。
改 taxonomy json 也不用重启（按文件签名热加载）。

### 改前端

```bash
cd cloud/workbench/frontend && npm run build     # 生成 dist/
rsync -av --delete dist/ ubuntu@140.143.229.85:/opt/qbank/workbench/frontend/dist/
```

### 账号

```bash
cd /opt/qbank/workbench/backend
export PYTHONPATH=.
V=/opt/qbank/venv/bin/python

$V manage_users.py list
$V manage_users.py passwd phy03        # 重置密码（打印新密码，首登强制改）
$V manage_users.py disable phy03       # 停用（下一次请求立即 401）
$V manage_users.py enable  phy03
$V manage_users.py add teacher_x --role lead --subject physics
$V manage_users.py newkey ai-tagger    # 重签 AI 的 API Key（旧 Key 立刻失效）
```

角色：`admin` 全部 / `lead` 本学科+可改词表 / `tagger` 本学科打标确认+可改本学科词表 / `viewer` 只读 / `service` AI（不能改词表）。

> 词表编辑权 2026-10-19 起放开到 `tagger`（viewer / service 除外）。整份覆盖写靠
> `sig` 乐观锁防互相覆盖：GET 返回 `sig`，PUT 回传 `base_sig`，不一致 → 409 +
> 响应头 `X-QB-Current-Sig`，前端提示重新加载。备份落 `data/taxonomy/_prev/`（带微秒）。

### AI 打标

接口契约一字未改，只换 base_url 和加一个请求头：

```python
BASE = "http://140.143.229.85"
HEAD = {"Authorization": "Bearer qbk_xxxxxxxx"}   # manage_users.py newkey 得到
```

`PUT /api/questions/{qid}/annotation` 与人工点击保存走同一条代码路径——主标签补全、
多标签缺主 422、确认失效、索引更新全都一致。写入落 `annotated_by`，审计日志能查到。

三条纪律：只处理 `status=none`（天然幂等）、串行一卷一卷跑、别和老师同时标同一卷。

### 备份与恢复

```bash
# 手动跑一次
/opt/qbank/backup.sh

# 看今天的快照对不对（两侧文件数应一致）
find /opt/qbank/data -name '*.md' | wc -l
find /opt/qbank/backup/latest -name '*.md' | wc -l

# 恢复某一天
sudo systemctl stop qbank
# ⚠️ 整个 data/ 回滚会把 data/taxonomy/ 一起回滚成那天快照的样子。标签词表是老师在
#    打标台实时改的（2026-10-23 起已移出 git，线上是唯一正本），只想恢复题目数据时
#    必须排除词表，否则会丢快照之后的全部改词动作：
sudo rsync -a --delete --exclude 'taxonomy/' /opt/qbank/backup/2026-10-20/ /opt/qbank/data/
sudo cp /opt/qbank/backup/users-2026-10-20.yaml /opt/qbank/config/users.yaml
sudo systemctl start qbank
```

### 索引 index.db（2026-10-18 起）

列表页不再现场解析 md，改查 `/opt/qbank/var/index.db`（SQLite，**纯抄件，可随时删**）。

```bash
# 快速对账（只比目录 stat，0.1 秒）——日常用这个
python scripts/build_index.py --verify
# 深度对账（逐文件核对状态，约 12 秒 / 8000 题）——怀疑数据不对时用
python scripts/build_index.py --check
# 单卷重灌（题包替换后最省事的刷新方式）
python scripts/build_index.py --paper 2019-jan-ph01
# 全量重建（410 卷约 12s）
python scripts/build_index.py
# 删了重来（索引丢了不影响任何数据）
rm -f /opt/qbank/var/index.db* && sudo systemctl restart qbank
```

实测（410 卷 / 8220 题）：

| | 之前 | 现在 |
|---|---|---|
| 启动预热 | 12.0 s（全量解析 md） | **0.04 s**（打开索引） |
| `GET /api/papers` | 0.23 s | **0.12 s** |
| `GET /papers/{id}/questions` | 0.086 s | **0.0034 s** |
| 改标注后 | 立即生效（读 md） | 该卷标 stale，下次读时重灌（约 0.15s） |

**四条纪律**：

1. **它是抄件，不是事实**。删了重跑即可，**不进备份**。确认记录/有误标记在 `state.db`（那是事实）。
   表结构版本对不上时会**自动删库重建**（不阻断启动），因为重建本来就没有损失。
2. **写 md 后必须让它失效**。`store._atomic_write()` 里已经做了（标该卷 stale），所以
   **新代码不要绕开 `_atomic_write` 自己写盘**——`write_annotation` 曾经内联过一份，导致改了标注列表不刷新。
3. **文件增删会自动感知**（每 5 秒一次目录扫描，比对卷目录 mtime）：
   新卷自动入索引、整卷删除自动清理、卷内增删文件自动标 stale 并重灌。
   **唯一感知不到的**是「原地改已有 md 的 tags/difficulty」——目录 mtime 不变。
   批量修复脚本绕开 API 直接改 md 时，收尾记得跑 `--paper <卷名>` 或 `--check`。
4. **上传新卷不用重启**：卷列表靠目录扫描（缓存 5 秒），新卷 5 秒内自动出现并自动入索引。

### 状态库 state.db（2026-10-18 起）

确认记录与「有误」标记不再是 `data/_review/*.json`，改存 `/opt/qbank/var/state.db`（SQLite）。

```bash
# 查某人确认了多少题
sqlite3 /opt/qbank/var/state.db "SELECT by_user, COUNT(*) FROM confirms GROUP BY 1 ORDER BY 2 DESC;"
# 查某题是否已确认 / 是否被标为有误
sqlite3 /opt/qbank/var/state.db "SELECT * FROM confirms WHERE unit_id LIKE '%ph01-q01%';"
sqlite3 /opt/qbank/var/state.db "SELECT qid,at,by_user,note FROM flags;"
# 手工改过库之后，让服务重读内存缓存（或直接重启）
sudo systemctl restart qbank
```

**三条纪律**：

1. **它和 index.db 是两回事**。state.db 是事实（重建不出来，必须备份）；index.db 是抄件（可删可重抄，不进备份）。**绝不合并**——否则重建索引会把人工确认抹掉。
2. **备份不能直接 rsync**。正在写的 WAL 库直接拷会拿到不一致快照；`backup.sh` 用 `VACUUM INTO` 拍原子快照到 `backup/state-<日期>.db`。
3. **恢复 data/ 时别忘了它**。state.db 单独存在 var/ 下，不随 data/ 快照走。

服务权限也相应加了 `/opt/qbank/var`（`ReadWritePaths`）。首次部署要 `mkdir -p /opt/qbank/var`。

旧 JSON 已改名 `confirmed.json.migrated-20261018` / `flagged.json.migrated-20261018` 留档，确认无误后可删。

### 看谁改了什么

```bash
# 命令行
tail -n 50 /opt/qbank/data/_audit/$(date +%F).jsonl | python3 -m json.tool --json-lines

# 或者管理员账号直接取
curl -s --noproxy '*' -b "qb_session=<你的cookie>" \
  "http://140.143.229.85/api/auth/audit?limit=100"
```

---

## 4. 这一版刻意没做的四件事（以及为什么）

| 没做 | 理由 |
|---|---|
| **不跑切割、不渲整题** | 服务器只读题库、只写标注。切卷与整题渲染一律在本地做完再传（不传 PDF、不装 pymupdf）。**唯一例外是按小问打标时的子图裁剪**：装了 Pillow，直接在已有一级图 `qNN.webp` 上裁（`render_subsplit` v2，不需要 PDF）；且只有 Edexcel physics / chemistry 会真的裁，AQA、CIE 以及 accounting 学科只写子题文件、不产子图（判定在 `store.sub_images_enabled`），预览回退整题图。 |
| **不加 index.db** | 一期 8200 题、列表页几十毫秒。等到 2 万题、或列表接口 > 1 秒再上索引层。提前优化只会增加出错面。 |
| **不做 HTTPS** | 没有域名、没过备案。密码走明文 HTTP，内部教学场景可接受；有域名后立刻上证书（`secure=True` 一并打开）。 |
| **不传 PDF** | 省 531 MB，且 PDF 是版权最敏感的原卷。代价：切分脚本里那道 sha256 校验改成「PDF 在就验、不在就跳过」。 |

## 5. 已知风险

- **磁盘**：题库 9 GB + 7 天备份 ≈ 18 GB，59 GB 盘剩 21 GB。**占用过 85% 必须扩容**（backup.sh 会自己喊）。
- **备份同盘**：防误删、防改错，不防磁盘/整机故障。真要灾备，得再推一份到对象存储。
- **单点**：机器挂了，本地那份 `D:\Tasks\questionBank\data` 是最后一道保险，**不要删**。
- **单进程**：`--workers 1` 是硬约束，不是建议。缓存和锁都在进程内存里。
