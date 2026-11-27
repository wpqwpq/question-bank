# 作业布置平台 — 选题网页（本地版）

## 简介
基于题库服务器（`http://140.143.229.85/`「题库审核打标台」）的**只读**数据制作的选题网页。
题库只作为数据来源被读取，本平台与题库严格分离，**不向题库写入任何内容**。

## 目录结构
```
作业布置平台/
├── web/                     # 前端网页
│   ├── index.html
│   ├── style.css
│   └── app.js
├── data/                    # 题库只读快照（由脚本抓取生成）
│   ├── catalog.json         # 阶段一：全科目×全考试局×全阶段×两种模式的一级目录（约 24KB，进页面即加载）
│   ├── papers.json          # 全科目试卷列表（4231）
│   ├── questions.json       # 各试卷题目列表（56009 题）
│   ├── subjects/            # <科目>/<考试局>-<阶段>/tree.json.gz：阶段二纯文字包（45 个，不含难度）
│   │                        #   <科目>/diff/<id>.json.gz：阶段三按一级目录拆分的难度颜色（829 个）
│   │                        #   <科目>.json.gz 兼容包（前端已不用）、<科目>-difficulty.json.gz 难度筛选合并包
│   ├── prefixes.json        # 各试卷截图目录前缀（4231）
│   ├── taxonomy.json        # 全科目分类标签体系（42 个 科目|考试局|阶段 组合）
│   ├── subtags_cache_db.json# 服务器题库索引库只读导出的题目标签（127987 条）
│   └── subtags.json         # 拆分小问的主标签（127987 条）
├── textbank/                # 题目文字库：45 个 学科__考试局__阶段.jsonl + manifest.json
├── scripts/
│   ├── build_textbank.py    # 由只读快照生成 textbank/（整题粒度 JSONL，一行一题）
│   ├── fetch_all_subjects.py # 一次性抓全科目：试卷/题目/前缀/标签/子题（只读）
│   ├── build_staged_data.py # 由只读快照生成分级数据包 catalog.json + subjects/*
│   ├── fetch_bank.py        # 仅抓化学试卷+题目（旧脚本，只读）
│   ├── fetch_prefixes.py    # 抓截图路径前缀（旧脚本，只读）
│   ├── fetch_extras.py      # 补齐漏抓试卷（旧脚本）
│   ├── fetch_taxonomy.py    # 抓化学分类标签（旧脚本，只读）
│   └── proxy_connect.py     # SSH 走本地 HTTP CONNECT 代理的跳板脚本
├── data_backup_chem_20261123/# 增加全科目前的化学快照备份
├── export_engine.py         # 导出引擎：A4 排版 → PDF / DOCX（PIL）
├── server.py                # 本地服务器（静态托管 + 截图代理 + 导出接口）
├── .session_cookie_admin    # 可跨科目读取题库的会话（本地保存，勿外传）
├── .session_cookie          # 化学账号会话（回退用）
└── README.md
```

## 启动
```bash
python server.py 8080
```
浏览器打开 `http://127.0.0.1:8080`

## 线上地址
**http://140.143.229.85/assignments/**（已部署，开机自启）

部署结构（与题库完全分离，题库仍零写入）：
- 服务器目录：`/opt/assignments/`（`web/` 前端、`data/` 快照、`venv/` 独立 Python+Pillow、`.drafts/` 草稿）
- systemd 服务：`assignments.service`，监听 `127.0.0.1:8652`
- nginx：只新增 `/assignments/` 反向代理；题库原有 `/`、`/api/*`、`/files/*` 配置未改，备份在 `/opt/assignments/nginx-qbank.conf.bak-*`
- 题图与导出：服务端直接**只读**读取 `/opt/qbank/data/` 下的 webp/layout.json，不依赖会话 cookie，不会过期
- 重新安装/更新：`deploy/install.sh` + `deploy/assignments.service` + `deploy/nginx-assignments.txt`

注意：草稿目前是服务器端全局单份（`.drafts/draft.json`），多人同时编辑会互相覆盖。

## 会话 / 数据快照说明
线上部署后，题图与导出直接只读访问服务器 `/opt/qbank/data/`，不再依赖登录 cookie。
本地运行时若题库文件不在本机，图片代理才回退到 `.session_cookie_admin` / `.session_cookie`（12 小时有效）。
题干文本与难度快照由 `scripts/export_md_meta.py` 只读导出，生成 `data/question_text.json.gz`；
线上接口支持 gzip Content-Encoding，前端无需额外处理。

页面采用四级按需加载（题库快照更新后运行 `python scripts/build_staged_data.py` 重建）：
1. **进入页面、未选科目**：加载 `catalog.json` —— 全部科目 × 全部考试局 × 全部阶段 × 按套卷/按类别两种模式的**一级目录名**（按套卷=年月，按类别=单元，约 24KB）；不下载题号、难度、题图；
2. **下拉选定科目后**：加载 `subjects/<科目>/<考试局>-<阶段>/tree.json.gz` —— 仅当前组合的二/三/四级**文字**（卷号、大题号、小题号、标签、题干），**不含难度颜色**；切换考试局/阶段时再取对应组合（按套卷与按类别共用同一份文字包）。45 个组合包共约 12.4MB，单次只下载其中一个（化学 cie-a-level 约 0.7MB）；
3. **展开某个一级目录**：才加载 `subjects/<科目>/diff/<id>.json.gz` —— 该一级目录下全部题目的难度颜色（按「模式\|考试局\|阶段\|一级目录」拆分，单个通常 1~2KB），题号随即显示底色；按套卷=展开「年月」，按类别=展开「单元」。反复展开同一目录不重复下载。难度筛选需要全局数据时，才另取科目级合并包 `subjects/<科目>-difficulty.json.gz`。共 829 个难度分片、合计约 0.79MB。
4. **展开某个二级目录**：预取该目录下全部题目截图到浏览器图片缓存（按套卷=展开卷号，按类别=展开一级标签/「未分配题目」）；同一大题的多个小问共用一张图，按路径去重；4 路并发避免卡界面。不写入本地磁盘。

服务器 SSH 登录（`ubuntu` 用户 + 密钥）需走本机 HTTP CONNECT 代理，示例：
```bash
ssh -i "<密钥.pem>" \
  -o "ProxyCommand=python scripts/proxy_connect.py %h %p" \
  ubuntu@140.143.229.85
```
重新签发全科目只读会话（在服务器上执行，仅读取签名密钥，不写题库）：
```bash
cd /opt/qbank/workbench/backend && PYTHONPATH=. /opt/qbank/venv/bin/python -c "import auth; print(auth.make_session('admin'))"
```
把输出的 token 写入本地 `.session_cookie_admin`。

## 重新抓取数据（题库有更新时）
```bash
python scripts/fetch_all_subjects.py                 # 全部阶段：数据/前缀/标签/子题
python scripts/fetch_all_subjects.py data            # 仅试卷+题目
python scripts/fetch_all_subjects.py prefixes        # 仅截图前缀
python scripts/fetch_all_subjects.py taxonomy        # 仅分类标签
python scripts/fetch_all_subjects.py subtags         # 仅拆分小问标签
```
脚本全程只发 GET，绝不向题库写入。数据已按科目区分，科目下拉自动列出全部可用科目。

拆分小问标签（subtags）若需刷新，可走服务器只读数据库导出（比逐题 GET 快很多）：
```bash
ssh -i "<密钥.pem>" -o "ProxyCommand=python scripts/proxy_connect.py %h %p" ubuntu@140.143.229.85 \
  'sudo /opt/qbank/venv/bin/python -c "
import sqlite3,json
con=sqlite3.connect(\"file:/opt/qbank/var/index.db?mode=ro\",uri=True)
out={}
for qid,tags,primary in con.execute(\"select qid,tags,primary_tag from questions\"):
    out[qid]={\"tags\": json.loads(tags) if tags else [], \"primary\": primary}
print(json.dumps(out,ensure_ascii=False,separators=(\",\",\":\")))
"' > data/subtags_cache_db.json
```
然后在本地按 `subtags.json` 的 `sid -> {primary}` 结构转换并回填 `questions.json`。

## 功能
- 顶部：作业名称、接受班级（暂空）、**科目（化学/物理/生物/数学/进阶数学/经济/商务/会计/综合科学/心理学）**；模式（按套卷/按类别/关键词）、考试局、阶段筛选。**打开页面默认按套卷 + Edexcel + A-Level**（草稿只恢复作业名/班级/已选题，不改变这三个按钮）；切换科目时优先保留当前组合，只有该科目在此组合无数据才自动落到它有数据的考试局/阶段。
- **难度筛选在三种模式下都可见、都生效**：无论当前处于按套卷/按类别/关键词（搜索前或搜索后），点选难度即筛选左下题目列表；关键词模式直接重筛已缓存的搜索结果，不重新搜索。按套卷模式下，按小问打标的题在筛选后只显示匹配难度的小问；空卷/空年月自动隐藏。
- **关键词模式**：模式切到「关键词」后，左栏顶部出现关键词搜索框。输入题干关键词（空格分隔=全部命中）并点「搜索」/回车，服务端按当前「科目 + 考试局 + 阶段」从 `textbank/<subject>__<board>__<level>.jsonl` 逐行扫描，命中一条即经 NDJSON 流返回一条，前端即时以**整题题号**追加到左侧题目列表（不等待全部完成）；**检索全部完成后自动把命中题图送入预载队列**。结果排序：先单元（卷），再年月，最后卷内题号。题号可勾选加入作业、点击预览截图。
- 左侧题库树：**默认全部折叠**，逐级点开；标题旁「全部折叠」一键收起；**单击题/卷即预览**（无需勾选）
  - 整套 = 年月→卷号（勾卷）；拆分 = 到大题/小题（勾小题）
  - 分类 = **单元→一级标签→二级标签→题号**；二级标签旁勾选框可整批选该标签下全部题目
  - 多标签题按标签列表顺序归入最后一个标签；无标签题归入「未分类/待分类题目」，保证全库可见
- 中间：已选题目清单，「清空」在标题右侧；仅用条目旁 ▲▼ 小箭头排序，× 删除
- 右侧：题目截图预览，**支持缩放（适应宽度/±）**，整体预览分页
- 底部：预览卷面、输出 Word、输出 PDF、发布作业（二级页，每题带题号）
- 题号格式：`E/C/A-年份-月份-试卷编码-大题号-小题号`

## 题目文字库（JSONL，整题粒度）
由本地只读快照生成，不访问、不写入题库：
```bash
python scripts/build_textbank.py
```
输出 `textbank/`：`45` 个 `<subject>__<board>__<qualification>.jsonl`（共 56,009 行，一行一题）
加 `manifest.json`（组合清单、每题计数、文件字节数、SHA-256、缺失题清单）。
每行字段：`qid / board / qualification / subject / year / session / paper_code / unit /
question_number / total_marks / question_type / tags / primary_tag / difficulty /
sub_ids / image_path / text_match / text_chars / text`。

- `text_match`：`exact`（题号直接命中）/ `paper`（CIE 长数字 ID 经试卷+题号命中）/ `none`（无文字）
- 小问文字已并入所属大题，不拆成单独行；卷级说明页已排除。
- 唯一无文字题：`edexcel-a-level-accounting-2023-jan-wac12-q04`（题库文字源文件本身缺失）。
- 纯文字经过 Markdown 清洗，公式/图表/表格会丢失，答题横线仍保留；**不能替代截图预览**。
- 服务器备查副本：`/opt/assignments/textbank/`（与题库 `/opt/qbank/` 完全分离，题库零写入），
  45 文件 / 56,009 行 / 0 坏 JSON / SHA-256 全部一致，已核验。

## Word / PDF 导出规则
- 真实生成文件（不是占位）：A4 页面、150DPI
- **选择题**：紧凑排列（题间无空隙）；单题为不可分割块，一页放不下整题挪到下一页，不切断
- **大题**：同一大题只导出一次图（避免共同题干重复）；自动检测原图是否已含虚线/方格答题区——
  - 已含 → 不再预留空白
  - 未含 → 按该题分数 ×2 行补「一行行虚线」答题区
- 生成在服务端完成（`export_engine.py`），浏览器直接下载
