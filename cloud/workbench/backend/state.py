"""确认状态 / 有误标记的持久化层（SQLite）。

职责边界（这条最重要）：
  · state.db 是**事实**，重建不出来，必须进备份；
  · index.db 是**抄件**，随时可删了重抄，不进备份。
  两者必须是两个文件，绝不合并 —— 否则重建索引就会把人工确认成果一起抹掉。

一致性模型（单进程，uvicorn workers=1）：
  · 只有本进程写库；
  · 启动时全量读进内存（confirmed 是 set，flags 是 dict），读路径零 IO；
  · 写操作 = 一条 SQL 事务提交成功后再更新内存（顺序不能反）；
  · 指纹校验**不在请求路径上**：详情页验单条 + 后台低频轮转巡检（见 flags.py）。

为什么不再用 JSON：
  · 旧实现每次确认都深拷贝 + 重写整个文件（10 万条时 11MB / 约 180ms），
    而且「读-改-写」在多人并发下会丢更新；
  · 现在一条 UPSERT 约 0.02ms，事务保证不丢。
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]

# 默认 <repo>/var/state.db（云端即 /opt/qbank/var/state.db）。
# 环境变量 QBANK_STATE_DB 可覆盖 —— 本地自测时指到临时路径。
_DEFAULT_DB = REPO_ROOT / "var" / "state.db"
DB_PATH = Path(os.environ.get("QBANK_STATE_DB") or _DEFAULT_DB)

SCHEMA_VERSION = 1

_lock = threading.RLock()
_con: sqlite3.Connection | None = None
_confirms: set[str] = set()
_flags: dict[str, dict[str, Any]] = {}
_loaded = False
# 写入代际：确认 / 标有误 / 撤标记 各 +1。
# 全局概览拿它和 index_db.generation() 拼成缓存键——任何写操作都会让缓存自动失效，
# 不需要在各个写入点手工清缓存（漏一个就会出现「勾完确认概览不变色」）。
_rev = 0


def rev() -> int:
    """状态库的写入代际。只增不减，进程内单调。"""
    return _rev


def _bump() -> None:
    global _rev
    _rev += 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS confirms (
  unit_id TEXT PRIMARY KEY,
  at      TEXT NOT NULL,
  by_user TEXT
);
CREATE TABLE IF NOT EXISTS flags (
  qid      TEXT PRIMARY KEY,
  at       TEXT NOT NULL,
  by_user  TEXT,
  note     TEXT,
  fp       TEXT NOT NULL,
  fp_ok_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_flags_ok ON flags(fp_ok_at);
CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT
);
"""


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# 连接与初始化
# ---------------------------------------------------------------------------

def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(DB_PATH), timeout=10, check_same_thread=False)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")   # WAL 下足够安全，且快得多
    con.execute("PRAGMA busy_timeout=5000")    # 偶发写竞争等 5 秒，不直接报错
    return con


def init() -> None:
    """建库建表 + 全量加载进内存。可重复调用（幂等）。

    schema 版本不符时**直接报错**，不静默改表：这个库是事实，
    宁可起不来让人处理，也不要半路把确认记录改坏。
    """
    global _con, _confirms, _flags, _loaded

    with _lock:
        if _con is None:
            _con = _connect()
            _con.executescript(_SCHEMA)
            _con.commit()

        row = _con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        if row is None:
            _con.execute("INSERT INTO meta(key,value) VALUES('schema_version',?)",
                         (str(SCHEMA_VERSION),))
            _con.commit()
        elif str(row[0]) != str(SCHEMA_VERSION):
            raise RuntimeError(
                f"state.db schema 版本不符：库={row[0]} 代码={SCHEMA_VERSION}。"
                "请先跑 scripts/migrate_state.py --rebuild 或人工处理，不要直接启动。"
            )

        conf = {r[0] for r in _con.execute("SELECT unit_id FROM confirms")}
        fl: dict[str, dict[str, Any]] = {}
        for qid, at, by, note, fp, _ok in _con.execute(
            "SELECT qid,at,by_user,note,fp,fp_ok_at FROM flags"
        ):
            try:
                fp_obj = json.loads(fp) if fp else {}
            except json.JSONDecodeError:
                fp_obj = {}
            fl[qid] = {"at": at, "by": by, "note": note, "fp": fp_obj}

        _confirms = conf
        _flags = fl
        _loaded = True


def is_ready() -> bool:
    return _loaded


def close() -> None:
    global _con, _loaded
    with _lock:
        if _con is not None:
            try:
                _con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.Error:
                pass
            _con.close()
        _con = None
        _loaded = False


def reload() -> None:
    """重新从库读进内存。手工用 sqlite3 命令改过库之后调用。"""
    with _lock:
        init()


def _require() -> sqlite3.Connection:
    if _con is None:
        init()
    assert _con is not None
    return _con


# ---------------------------------------------------------------------------
# 确认状态
# ---------------------------------------------------------------------------

def confirmed_ids() -> set[str]:
    """全部已确认单元 id。返回**副本**，调用方随便改不影响缓存。"""
    with _lock:
        return set(_confirms)


def confirmed_count() -> int:
    with _lock:
        return len(_confirms)


def is_confirmed(uid: str) -> bool:
    with _lock:
        return str(uid) in _confirms


def confirmed_items() -> dict[str, dict[str, Any]]:
    """{unit_id: {"at","by"}}。读库，给兼容接口用，别放到热路径上。"""
    with _lock:
        con = _require()
        return {
            r[0]: {"at": r[1], "by": r[2]}
            for r in con.execute("SELECT unit_id, at, by_user FROM confirms")
        }


def set_confirmed(uid: str, on: bool, by: str | None = None) -> dict[str, Any]:
    """确认 / 取消确认一个单元。先落库，再改内存。"""
    uid = str(uid)
    at = now_iso()
    with _lock:
        con = _require()
        if on:
            con.execute(
                "INSERT INTO confirms(unit_id,at,by_user) VALUES(?,?,?) "
                "ON CONFLICT(unit_id) DO UPDATE SET at=excluded.at, by_user=excluded.by_user",
                (uid, at, by),
            )
        else:
            con.execute("DELETE FROM confirms WHERE unit_id=?", (uid,))
        con.commit()
        if on:
            _confirms.add(uid)
        else:
            _confirms.discard(uid)
        _bump()
    return {"qid": uid, "confirmed": bool(on), "at": at if on else None, "by": by if on else None}


# ---------------------------------------------------------------------------
# 有误标记
# ---------------------------------------------------------------------------

def flagged_all() -> dict[str, dict[str, Any]]:
    """全部标记（浅拷贝：外层 dict 新，里面的记录对象共用，调用方不要改写）。"""
    with _lock:
        return dict(_flags)


def flagged_ids() -> set[str]:
    with _lock:
        return set(_flags)


def flagged_count() -> int:
    with _lock:
        return len(_flags)


def get_flag(qid: str) -> dict[str, Any] | None:
    with _lock:
        return _flags.get(str(qid))


def is_flagged(qid: str) -> bool:
    with _lock:
        return str(qid) in _flags


def set_flag(qid: str, on: bool, by: str | None = None, note: str | None = None,
             fp: dict[str, Any] | None = None) -> dict[str, Any]:
    """打 / 撤标记。on=True 时必须给 fp（标记当时算出的文件指纹）。"""
    qid = str(qid)
    at = now_iso()
    fp = fp or {}
    fp_text = json.dumps(fp, ensure_ascii=False, separators=(",", ":"))
    with _lock:
        con = _require()
        if on:
            con.execute(
                "INSERT INTO flags(qid,at,by_user,note,fp,fp_ok_at) VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(qid) DO UPDATE SET at=excluded.at, by_user=excluded.by_user, "
                "note=excluded.note, fp=excluded.fp, fp_ok_at=excluded.fp_ok_at",
                (qid, at, by, note, fp_text, at),
            )
        else:
            con.execute("DELETE FROM flags WHERE qid=?", (qid,))
        con.commit()
        if on:
            _flags[qid] = {"at": at, "by": by, "note": note, "fp": fp}
        else:
            _flags.pop(qid, None)
        _bump()
    return {"qid": qid, "flagged": bool(on), "at": at if on else None,
            "by": by if on else None, "note": note if on else None}


def drop_flag(qid: str) -> bool:
    """删一条标记（指纹失效时用）。返回是否真的删掉了。"""
    qid = str(qid)
    with _lock:
        con = _require()
        cur = con.execute("DELETE FROM flags WHERE qid=?", (qid,))
        con.commit()
        existed = cur.rowcount > 0
        _flags.pop(qid, None)
        if existed:
            _bump()
    return existed


def touch_flag(qid: str, fp: dict[str, Any]) -> None:
    """指纹内容没变、只是文件被重写过：刷新指纹与校验时间，避免每次重算 sha256。"""
    qid = str(qid)
    at = now_iso()
    text = json.dumps(fp, ensure_ascii=False, separators=(",", ":"))
    with _lock:
        con = _require()
        con.execute("UPDATE flags SET fp=?, fp_ok_at=? WHERE qid=?", (text, at, qid))
        con.commit()
        rec = _flags.get(qid)
        if rec is not None:
            rec["fp"] = fp


def flags_stale(limit: int = 200) -> list[str]:
    """挑最久没校验过的 N 条（给后台巡检用）。"""
    with _lock:
        con = _require()
        return [r[0] for r in con.execute(
            "SELECT qid FROM flags ORDER BY fp_ok_at IS NOT NULL, fp_ok_at ASC LIMIT ?",
            (int(limit),),
        )]


# ---------------------------------------------------------------------------
# 运维辅助
# ---------------------------------------------------------------------------

def dump() -> dict[str, Any]:
    """导出成和旧 JSON 一样的结构，给人看 / 给脚本比对。只读。"""
    with _lock:
        return {
            "version": SCHEMA_VERSION,
            "updated_at": now_iso(),
            "confirms": {uid: {"by": None} for uid in sorted(_confirms)},
            "flags": {qid: rec for qid, rec in sorted(_flags.items())},
        }


def stats() -> dict[str, Any]:
    with _lock:
        return {
            "db": str(DB_PATH),
            "db_exists": DB_PATH.exists(),
            "db_bytes": DB_PATH.stat().st_size if DB_PATH.exists() else 0,
            "confirmed": len(_confirms),
            "flagged": len(_flags),
        }
