"""预览模式「审核确认」记录：data/_review/confirmed.json 的读写。

确认单元（unit）的定义（见 docs/11）：
  - 非切分题  → 主题 qid 本身（如 `edexcel-a-level-physics-2020-jan-wph11-q01`）；
  - 切分题    → 每个小问的 qid（`{parent}-{label}`，即既有子题文件的 id）。

本模块只维护这一个副文件，**不改题库正本**（qNN.md / cut.json / 词表一字不动）。

缓存按文件 (mtime_ns, size) 签名失效——手工编辑 confirmed.json 也会即时生效，
与 taxonomy.py / store.py 的既有套路一致。
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
REVIEW_DIR = REPO_ROOT / "data" / "_review"
REVIEW_PATH = REVIEW_DIR / "confirmed.json"

_COMMENT = (
    "预览模式审核确认记录。键 = 确认单元 id"
    "（整题=主题 qid；切分题=小问 qid {parent}-{label}）。不改 qNN.md。模型见 docs/11。"
)

_lock = threading.RLock()
_cache: tuple[tuple[int, int], dict] | None = None


def _empty() -> dict:
    return {"$comment": _COMMENT, "version": 1, "updated_at": None, "items": {}}


def _sig() -> tuple[int, int]:
    try:
        st = REVIEW_PATH.stat()
    except OSError:
        return (0, 0)
    return (st.st_mtime_ns, st.st_size)


def load() -> dict:
    """读确认记录（带签名缓存）。文件不存在/损坏时返回空结构，不抛异常。"""
    global _cache
    sig = _sig()
    with _lock:
        if _cache is not None and _cache[0] == sig:
            return _cache[1]

    if not REVIEW_PATH.exists():
        data = _empty()
    else:
        try:
            data = json.loads(REVIEW_PATH.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or not isinstance(data.get("items"), dict):
                data = _empty()
        except (OSError, json.JSONDecodeError):
            data = _empty()
    data.setdefault("version", 1)
    data.setdefault("items", {})
    with _lock:
        _cache = (sig, data)
    return data


def items() -> dict:
    return load()["items"]


def confirmed_ids() -> set[str]:
    """全部已确认单元 id 的集合（一次请求取一份，避免逐题重复查）。"""
    return set(items().keys())


def is_confirmed(uid: str) -> bool:
    return str(uid) in items()


def set_confirmed(uid: str, on: bool) -> dict:
    """确认 / 取消确认一个单元：读-改-写 + 原子替换。返回 {qid, confirmed, at}。"""
    global _cache
    with _lock:
        data = json.loads(json.dumps(load(), ensure_ascii=False))  # 深拷贝，避免污染缓存对象
        its = data.setdefault("items", {})
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        if on:
            its[str(uid)] = {"at": now, "by": None}
        else:
            its.pop(str(uid), None)
        data["updated_at"] = now
        _write(data)
        _cache = None  # 下一次 load 按新签名重建
        return {"qid": str(uid), "confirmed": bool(on), "at": now if on else None}


def _write(data: dict) -> None:
    REVIEW_DIR.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    fd, tmp = tempfile.mkstemp(dir=str(REVIEW_DIR), suffix=".tmp")
    try:
        # newline="" ：避免 Windows 把 \n 写成 \r\n（本项目既有坑）
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        os.replace(tmp, REVIEW_PATH)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise
