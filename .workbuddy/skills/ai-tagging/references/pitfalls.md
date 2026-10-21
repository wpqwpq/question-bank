# 环境与坑（云端打标台）

## 打标台服务（只打云端）

**目标就是云端 `http://140.143.229.85`**（`/opt/qbank`，nginx + systemd `qbank.service`，workers=1）。

```bash
# 健康 + 鉴权（curl 必须加 --noproxy，否则被本机代理搅成假 502）
curl -s -m 20 --noproxy '*' -H "Authorization: Bearer $(cat ~/.qbank_key)" \
     -o /dev/null -w "%{http_code}\n" http://140.143.229.85/api/papers   # 200
```

- **不走鉴权就是 401**（`{"detail":"未登录或登录已过期"}`），不是服务挂了。
- `ai-tagger` 是 `role=service`、`subjects=['*']`，读写全学科；重签用服务器上
  `manage_users.py newkey ai-tagger`（旧 Key 立即失效）。
- 本机 `~/.qbank_key` 存一行纯文本 Key；**不要写入仓库**。
- 云端比本地慢：单次 PUT 约 1–8 s（Edexcel 拆分要现裁子图）；`/api/papers` 首访可能 10 s+。

### 代理变量会让 urllib 一律 502（2026-10-19 实测）

本机 `HTTP_PROXY` / `HTTPS_PROXY` 指向 WorkBuddy 的本地代理，`urllib.request.urlopen()` 会照走，
打**裸 IP** 的云端一律 `502 Bad Gateway`。两个脚本已改成
`urllib.request.build_opener(ProxyHandler({}))` 强制直连——**改代码时别把 opener 换回 `urlopen`**。

```bash
env | grep -i proxy          # 有输出就说明存在被代理的风险
```

### 网络抖动要重试，批量别在前台死等

同一接口实测 0.05 s 与 7 s 都出现过，偶尔直接连接超时（`WinError 10060`）。
脚本内置 4 次退避重试；**手工批量跑（>10 次请求）建议放后台**，否则容易撞 Bash 工具超时被 SIGTERM。

### 题图在云端，必须带 Bearer

`/files/<board>/<qual>/<subject>/<session>/assets/qNN.webp` 同样受鉴权保护：

- 不带 Header → 401；`<img src>` 直接引用也打不开。
- 正确姿势：`fetch_paper.py --images DIR`，脚本带 Bearer 下载到本地，再用 Read 读 `image_local`。

### 改了 `cloud/` 下的后端代码才需要重启

```bash
ssh ubuntu@140.143.229.85 "sudo systemctl restart qbank"     # 仅改过 cloud/ 的 .py
```

⚠️ **分寸**：只在「后端代码刚被改动」或「用户明确要求」时重启。发现进程挂了先报告用户。

### 状态是两套库，别混

- `var/state.db`：确认/有误（**事实**，必须备份，改完要重启才重载内存）
- `var/index.db`：列表索引（**抄件**，可删可重建，写盘后由 `store._atomic_write` 标脏）
- 批量修复脚本绕开 API 直接改 md 后，收尾跑 `build_index.py --paper <会话目录名>` 刷索引
  （⚠️ `--paper` 收的是**会话目录名** `2020-june-wph15`，给完整 paper_id 会报「找不到卷」）

## ⚠️ 云端拆分题：一次提交全部小问（2026-10-17 踩过）

云端 `api._save_split`（`cloud/workbench/backend/api.py`）在写库时，会把**本次没提交的顶层小问**
补成**空壳子题文件**（`tags: []`）——这是给打标台「保存，下一问」做进度可见用的设计。

后果：**只提交 1 个小问 = 把同题其他小问的标签清空**。2026-10-17 回洗时踩了，
2023-jan q10、2024-jan q4/q10、2025-jan q9、2025-june q7、2026-jan q6、2026-june q2 七道题的其余小问被清空。

- `apply_tags.py` 已加保护：`subs` 未覆盖该题 `top_labels` 就报错拦下（`--allow-partial` 可强过）。
- 修法：以 `decisions.json` 里该题**完整** subs（含原 primary_tag）重提交一次。
- 判据：提交后 `GET /api/questions/{qid}` 的 `subs[].tags` 应全部非空；空的就是被清了。

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

`data/` 被 gitignore（除 `taxonomy/`）。**批量写库前想清楚，没有 git 兜底**；需要回滚就先备份
（云端 `/opt/qbank/backup/` 有每日快照，`backup.sh` 用 `VACUUM INTO` 拍 state.db）。

## 读图的坑

- 用 Read 工具直接读下载到本地的 `.webp` 即可（多模态可读）
- **一次读 4–6 张**：连续大批量读同类文件会触发客户端的「模型正在重复工作 / 疑似卡死」提示
- **看图优先于文本层**：PDF 文本层会丢图（"as shown" 的图没了）、把分式拆行、把小问分支搞乱

## 协作分寸

- 用户可能正在**同一个打标台**上手动打标；批量写库前先确认，避免覆盖他的改动
- 不主动 `git commit`；批量动作先说清范围
- 报告落 `docs/`（该目录 gitignore，不入库）
- **题目有问题只写报告，不点「此题有误」**——标记留给人工
