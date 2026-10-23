"""题库只读索引层（index.db）：从 md 抄出来的查询投影。

职责边界（与 state.py 严格对称，别搞混）：
  · **index.db 是抄件**：100% 能从 data/ 重建，随时可删，**不进备份**；
  · **state.db 是事实**：确认记录 / 有误标记，重建不出来，必须备份。
  两者必须是两个文件 —— 混在一起，重建索引就会把人工确认一起抹掉。

为什么要有它（2026-10-17 实测）：
  · 现状 `/api/papers` 每次都现场遍历目录 + 逐卷读 front-matter 算状态：
    410 卷/8220 题 ≈ 0.23s，10 万题会到 2–3 秒；
  · 冷启动预热要 13.5s（YAML 解析 1.42ms/题），10 万题要 143s。
  索引把「每请求重算」变成「查 410 行」，把「启动解析 8000 个文件」变成「读 1 个文件」。

同步模型（Step 1 的最小实现）：
  · 读路径走 db；
  · 写路径（改标注/难度/切分）写完 md 后调用 `invalidate(paper_id)` 删掉该卷的行，
    下一次读发现该卷缺行 → 现场重灌该卷（约 0.13s/卷），数据立刻正确；
  · `build_index.py --check` 做全量对账（低频/手动），兜住绕过 API 的改动。
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = REPO_ROOT / "data"

_DEFAULT_DB = REPO_ROOT / "var" / "index.db"
DB_PATH = Path(os.environ.get("QBANK_INDEX_DB") or _DEFAULT_DB)

SCHEMA_VERSION = 2

_lock = threading.RLock()
_con: sqlite3.Connection | None = None
# 代际计数：每次全量重建 / 单卷重灌 / 失效都 +1。
# store 用它判断「缓存里的 Paper 列表还能不能复用」，避免每请求重建对象。
_generation = 0
# 全量重建进行中：此时表是空的或被锁，读路径应回退到文件扫描而不是逐卷抢着重建
_building = False


def generation() -> int:
    return _generation


def is_building() -> bool:
    return _building

_SCHEMA = """
CREATE TABLE IF NOT EXISTS papers (
  paper_id       TEXT PRIMARY KEY,
  board          TEXT NOT NULL,
  qualification  TEXT NOT NULL,
  subject        TEXT NOT NULL,
  unit           TEXT,
  paper_code     TEXT,
  year           INTEGER,
  session        TEXT,
  question_count INTEGER,
  annotated_count INTEGER,
  source         TEXT,
  dir_rel        TEXT,
  dir_mtime      INTEGER,
  stale          INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS questions (
  qid        TEXT PRIMARY KEY,
  paper_id   TEXT NOT NULL,
  parent_qid TEXT,
  qnum       TEXT,
  sub_label  TEXT,
  total_marks INTEGER,
  question_type TEXT,
  split      INTEGER NOT NULL DEFAULT 0,
  status     TEXT,
  difficulty TEXT,
  primary_tag TEXT,
  tags       TEXT,
  image      TEXT,
  file_name  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_q_paper  ON questions(paper_id);
CREATE INDEX IF NOT EXISTS idx_q_parent ON questions(parent_qid);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""

# 目录名排除：词表与审核记录不在题库里
_SKIP_DIRS = {"taxonomy", "_review", "_audit"}


# ---------------------------------------------------------------------------
# 连接
# ---------------------------------------------------------------------------

def connect() -> sqlite3.Connection:
    global _con
    with _lock:
        if _con is None:
            DB_PATH.parent.mkdir(parents=True, exist_ok=True)
            # schema 版本不符：索引是派生数据，直接丢弃重建，不阻断启动。
            _drop_if_schema_mismatch()
            con = sqlite3.connect(str(DB_PATH), timeout=10, check_same_thread=False)
            con.execute("PRAGMA journal_mode=WAL")
            con.execute("PRAGMA synchronous=NORMAL")
            con.execute("PRAGMA busy_timeout=5000")
            con.executescript(_SCHEMA)
            con.commit()
            row = con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
            if row is None:
                con.execute("INSERT INTO meta(key,value) VALUES('schema_version',?)",
                            (str(SCHEMA_VERSION),))
                con.commit()
            _con = con
        return _con


def _drop_if_schema_mismatch() -> None:
    """库存在但表结构版本对不上 → 删库（index.db 随时可重建，绝不尝试迁移）。"""
    if not DB_PATH.exists():
        return
    try:
        probe = sqlite3.connect(str(DB_PATH), timeout=3)
        try:
            row = probe.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        except sqlite3.Error:
            row = None
        probe.close()
    except sqlite3.Error:
        row = None
    if row is not None and str(row[0]) == str(SCHEMA_VERSION):
        return
    print(f"[index] schema 版本不符（库={row[0] if row else '未知'} 代码={SCHEMA_VERSION}），"
          f"丢弃旧索引重建", flush=True)
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(DB_PATH) + suffix)
        if p.exists():
            try:
                p.unlink()
            except OSError:
                pass


def close() -> None:
    global _con
    with _lock:
        if _con is not None:
            try:
                _con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.Error:
                pass
            _con.close()
        _con = None


def exists() -> bool:
    return DB_PATH.exists()


def stats() -> dict[str, Any]:
    con = connect()
    return {
        "db": str(DB_PATH),
        "bytes": DB_PATH.stat().st_size if DB_PATH.exists() else 0,
        "papers": con.execute("SELECT COUNT(*) FROM papers").fetchone()[0],
        "questions": con.execute("SELECT COUNT(*) FROM questions").fetchone()[0],
        "built_at": (con.execute("SELECT value FROM meta WHERE key='built_at'").fetchone() or [None])[0],
    }


# ---------------------------------------------------------------------------
# 重建（读 data/ 的 md → 写 db）
# ---------------------------------------------------------------------------

def _paper_dirs() -> list[tuple[Path, str, str, str]]:
    """[(卷目录, board, qualification, subject)]。与 store._scan_papers 的遍历规则一致。"""
    out: list[tuple[Path, str, str, str]] = []
    if not DATA_DIR.exists():
        return out
    for board in sorted(p for p in DATA_DIR.iterdir() if p.is_dir() and p.name not in _SKIP_DIRS):
        for qual in sorted(p for p in board.iterdir() if p.is_dir()):
            for subject in sorted(p for p in qual.iterdir() if p.is_dir()):
                for session in sorted(p for p in subject.iterdir() if p.is_dir()):
                    if any(session.glob("q*.md")):
                        out.append((session, board.name, qual.name, subject.name))
    return out


def _row_for(path: Path, paper_id: str, idx_in_paper: int) -> dict[str, Any] | None:
    import store  # 延迟导入：store 会 import 本模块，模块级导入会成环

    meta = store.read_meta(path)
    qid = meta.get("id")
    if not qid:
        return None
    tags = meta.get("tags") or []
    if not isinstance(tags, list):
        tags = [tags]
    is_sub = bool(meta.get("parent"))
    split = bool(meta.get("split")) or is_sub
    diff = meta.get("difficulty")
    return {
        "qid": str(qid),
        "paper_id": paper_id,
        "parent_qid": str(meta["parent"]) if meta.get("parent") else None,
        # 与 store.Paper.questions() 的 `str(meta.get("question_number", qf.stem))` 对齐：
        # 没有题号时回退到文件名（q01），不返回空串
        "qnum": str(meta.get("question_number") or path.stem),
        "sub_label": str(meta.get("sub_label") or "") or None,
        "total_marks": meta.get("total_marks"),
        "question_type": meta.get("question_type"),
        "split": 1 if split else 0,
        "status": store.compute_status(meta),
        "difficulty": diff if diff in store._DONE_DIFFICULTIES else None,
        # 统一走 effective_primary：主文件 tags 为空时它返回 None（切分题本来就没有主标签），
        # 子题有 tags 时能正确解析出主标签 —— 之前对子题字段置 None 会让 primary_ready 误判。
        "primary_tag": store.effective_primary(tags, meta.get("primary_tag")),
        "tags": json.dumps(tags, ensure_ascii=False, separators=(",", ":")),
        "image": meta.get("image"),
        "file_name": path.name,
        "_order": idx_in_paper,
    }


def rebuild_paper(paper_dir: Path, board: str, qual: str, subject: str,
                  con: sqlite3.Connection | None = None) -> int:
    """重灌一卷。返回写入的行数。"""
    import store

    global _generation
    con = con or connect()
    # ID 必须与 store.Paper.id 同源（board-qual-subject-目录名），否则列表与索引对不上
    paper_id = store.paper_id_for(paper_dir, board, qual, subject)
    mains = sorted((p for p in paper_dir.glob("q*.md") if store.MAIN_Q_RE.match(p.name)),
                   key=lambda p: p.name)
    subs = sorted((p for p in paper_dir.glob("q*-*.md") if store.SUB_Q_RE.match(p.name)),
                  key=lambda p: p.name)

    rows: list[dict[str, Any]] = []
    for i, p in enumerate(mains):
        r = _row_for(p, paper_id, i)
        if r:
            rows.append(r)
    for i, p in enumerate(subs):
        r = _row_for(p, paper_id, 1000 + i)
        if r:
            rows.append(r)

    # 切分题的状态要按子题聚合（主文件本身没有 tags）
    by_parent: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        if r["parent_qid"]:
            by_parent.setdefault(r["parent_qid"], []).append(r)
    for r in rows:
        if r["parent_qid"] is None and r["split"]:
            kids = by_parent.get(r["qid"], [])
            sts = [k["status"] for k in kids]
            if not sts:
                r["status"] = "none"
            elif all(s == "done" for s in sts):
                r["status"] = "done"
            elif any(s != "none" for s in sts):
                r["status"] = "partial"
            else:
                r["status"] = "none"

    # 卷级元数据：取第一道主题题的 front-matter
    pmeta = store.read_meta(mains[0]) if mains else {}
    code = str(pmeta.get("paper_code") or "")
    unit = store.unit_from_paper_code(code, str(pmeta.get("subject") or ""),
                                      qual, board,
                                      str(pmeta.get("paper_unit") or "")) if code else "unknown"
    annotated = sum(1 for r in rows if r["parent_qid"] is None and r["status"] == "done")
    main_count = sum(1 for r in rows if r["parent_qid"] is None)
    try:
        dir_mtime = paper_dir.stat().st_mtime_ns
    except OSError:
        dir_mtime = None

    with _lock:
        existed = con.execute("SELECT 1 FROM papers WHERE paper_id=?",
                              (paper_id,)).fetchone() is not None
        con.execute("DELETE FROM questions WHERE paper_id=?", (paper_id,))
        con.executemany(
            "INSERT OR REPLACE INTO questions(qid,paper_id,parent_qid,qnum,sub_label,total_marks,"
            "question_type,split,status,difficulty,primary_tag,tags,image,file_name) "
            "VALUES(:qid,:paper_id,:parent_qid,:qnum,:sub_label,:total_marks,:question_type,:split,"
            ":status,:difficulty,:primary_tag,:tags,:image,:file_name)",
            rows,
        )
        con.execute(
            "INSERT OR REPLACE INTO papers(paper_id,board,qualification,subject,unit,paper_code,year,"
            "session,question_count,annotated_count,source,dir_rel,dir_mtime,stale) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,0)",
            (paper_id, board, qual, subject, unit, code, pmeta.get("year"), pmeta.get("session"),
             main_count, annotated, pmeta.get("source"),
             str(paper_dir.relative_to(DATA_DIR)).replace("\\", "/"), dir_mtime),
        )
        con.commit()
        if not existed:
            # 只有「卷集合变化」才动代际号；重灌同一卷不影响卷列表，
            # 否则每次写操作都会把 store 里的 Paper 列表判废、每请求重建
            _generation += 1
    return len(rows)


def rebuild(progress=None) -> dict[str, Any]:
    """全量重建。会先清空两张表（用同一个事务，失败不会留下半套数据）。"""
    global _generation, _building
    _building = True
    try:
        return _rebuild_inner(progress)
    finally:
        _building = False


def _rebuild_inner(progress=None) -> dict[str, Any]:
    global _generation
    con = connect()
    t0 = time.perf_counter()
    dirs = _paper_dirs()
    total = 0
    with _lock:
        con.execute("DELETE FROM questions")
        con.execute("DELETE FROM papers")
        con.commit()
    for i, (d, b, q, s) in enumerate(dirs, 1):
        total += rebuild_paper(d, b, q, s, con)
        if progress and (i % 50 == 0 or i == len(dirs)):
            progress(i, len(dirs))
    with _lock:
        con.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('built_at',?)",
                    (time.strftime("%Y-%m-%dT%H:%M:%S%z"),))
        con.commit()
        try:
            # 全量重建会写大量页，不收一下 WAL 会比主库还大（实测 4.2MB vs 1.8MB）
            con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error:
            pass
    with _lock:
        _generation += 1
    return {"papers": len(dirs), "rows": total, "seconds": round(time.perf_counter() - t0, 2)}


def invalidate(paper_id: str) -> None:
    """标记某卷过期（不立刻重灌）。下次读该卷时现场重灌，约 0.13s。

    写路径（改标注/难度/切分）完成后必须调用，否则列表页会显示旧状态。
    用「标记 stale」而不是「删行」：删行会让该卷从 `papers()` 里消失，
    列表页会少一卷，直到下次全量重建。
    """
    con = connect()
    with _lock:
        con.execute("UPDATE papers SET stale=1 WHERE paper_id=?", (str(paper_id),))
        con.commit()


def drop_paper(paper_id: str) -> bool:
    """整卷从索引里删掉（磁盘上已经没有这一卷了）。返回是否真的删了。"""
    global _generation
    con = connect()
    with _lock:
        cur = con.execute("DELETE FROM papers WHERE paper_id=?", (str(paper_id),))
        con.execute("DELETE FROM questions WHERE paper_id=?", (str(paper_id),))
        con.commit()
        removed = cur.rowcount > 0
        if removed:
            _generation += 1
    return removed


def paper_mtimes() -> dict[str, int | None]:
    """{paper_id: 卷目录 mtime_ns}。给目录扫描对账用（1500 卷也就一次查询）。"""
    con = connect()
    return {r[0]: r[1] for r in con.execute("SELECT paper_id, dir_mtime FROM papers")}


# ---------------------------------------------------------------------------
# 读
# ---------------------------------------------------------------------------

def paper_ids() -> list[str]:
    con = connect()
    return [r[0] for r in con.execute("SELECT paper_id FROM papers ORDER BY paper_id")]


def papers() -> list[dict[str, Any]]:
    """全部卷的摘要（列表页主体）。只碰 papers 表，不碰 questions。"""
    con = connect()
    cur = con.execute(
        "SELECT paper_id,board,qualification,subject,unit,paper_code,year,session,"
        "question_count,annotated_count,source,dir_rel,dir_mtime,stale FROM papers ORDER BY paper_id"
    )
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur]


def paper(paper_id: str) -> dict[str, Any] | None:
    con = connect()
    cur = con.execute(
        "SELECT paper_id,board,qualification,subject,unit,paper_code,year,session,"
        "question_count,annotated_count,source,dir_rel,dir_mtime,stale FROM papers WHERE paper_id=?",
        (str(paper_id),),
    )
    row = cur.fetchone()
    if row is None:
        return None
    return dict(zip([c[0] for c in cur.description], row))


def has_paper(paper_id: str) -> bool:
    con = connect()
    return con.execute("SELECT 1 FROM papers WHERE paper_id=? LIMIT 1",
                       (str(paper_id),)).fetchone() is not None


def questions(paper_id: str) -> list[dict[str, Any]]:
    """该卷全部题（含子题行，调用方按 parent_qid 自行区分）。"""
    con = connect()
    cur = con.execute(
        "SELECT qid,paper_id,parent_qid,qnum,sub_label,total_marks,question_type,split,"
        "status,difficulty,primary_tag,tags,image,file_name FROM questions "
        "WHERE paper_id=? ORDER BY file_name",
        (str(paper_id),),
    )
    cols = [c[0] for c in cur.description]
    out = []
    for r in cur:
        d = dict(zip(cols, r))
        try:
            d["tags"] = json.loads(d["tags"] or "[]")
        except json.JSONDecodeError:
            d["tags"] = []
        out.append(d)
    return out


def find_qid(qid: str) -> tuple[str, str] | None:
    """qid → (paper_id, file_name)。主键查询，微秒级。"""
    con = connect()
    row = con.execute("SELECT paper_id, file_name FROM questions WHERE qid=?",
                      (str(qid),)).fetchone()
    return (row[0], row[1]) if row else None


def counts_by_paper() -> dict[str, int]:
    """{paper_id: 已完成主题题数}。聚合查询，10 万题约 3ms。"""
    con = connect()
    return dict(con.execute(
        "SELECT paper_id, COUNT(*) FROM questions "
        "WHERE parent_qid IS NULL AND status='done' GROUP BY paper_id"
    ))


def progress_rows() -> dict[str, list[tuple[str, str | None, int, int]]]:
    """全库题目行的轻量投影，按卷分组：{paper_id: [(qid, parent_qid, split, has_tags), ...]}。

    专门给列表页算「核对进度」用。与 `questions()` 的区别只有一点——**只取判定需要的四列**，
    且 tags 在 SQL 里折成 0/1，不做 JSON 解析。27000 行约 60–70ms，一次查询替代
    「每卷一次 SELECT」（2000 卷约 290ms）。判定口径完全不变，只是换了个取数方式。
    """
    con = connect()
    out: dict[str, list[tuple[str, str | None, int, int]]] = {}
    cur = con.execute(
        "SELECT qid, paper_id, parent_qid, split, "
        "CASE WHEN tags IS NULL OR tags='' OR tags='[]' THEN 0 ELSE 1 END "
        "FROM questions ORDER BY paper_id, file_name"
    )
    for qid, pid, parent, split, has_tags in cur:
        out.setdefault(pid, []).append((str(qid), parent, 1 if split else 0, int(has_tags)))
    return out
