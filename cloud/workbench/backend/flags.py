"""题目「有误」标记。

为什么单独存、而不是塞进 qNN.md：
  · 标「有误」是**审核意见**，不是题库内容。写进 md 就等于改题库，
    本地/云端会发生同步冲突，而且一改 md 就触发确认失效，副作用太大。
  · 全部科目共用一个地方，方便一眼看全：「哪些题是坏的、谁标的、什么时候」。

存储已从 data/_review/flagged.json 迁到 var/state.db（见 state.py）：
  · 旧实现每打一次标记就重写整个 JSON，且**每个列表请求**都要把所有标记逐条
    校验一遍指纹（2000 条标记约 40–100ms/请求，还会在读请求里写盘）；
  · 现在写一条 = 一条 UPSERT；列表页直接读内存，完全不碰文件系统。

**指纹校验被移出请求路径**（这是本次改造的重点），分三处触发：
  ① verify_one(qid)  —— 打开单题详情时验那一条（用户真要看时才验）；
  ② sweep(limit)     —— 后台每 5 分钟轮转验最久没验的 N 条；
  ③ 题包重传后由脚本调 verify_one。

**自动失效**的实现没变：标记时记下 md 的 (mtime,size,sha256) + 题图的 (mtime,size)，
校验时先比 stat（几乎零成本），对不上才算 sha256；文件真被换过就删掉标记。
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import state

# ---------------------------------------------------------------------------
# 指纹
# ---------------------------------------------------------------------------

def fingerprint(qpath: Path, meta: dict[str, Any]) -> dict[str, Any]:
    """标记时的文件指纹。md 记 sha256（兜底判定内容是否真的变了），题图只记 stat。"""
    st = qpath.stat()
    fp: dict[str, Any] = {
        "mt": st.st_mtime_ns,
        "sz": st.st_size,
        "sha": hashlib.sha256(qpath.read_bytes()).hexdigest(),
    }
    rel = meta.get("image")
    if rel:
        try:
            s = (qpath.parent / str(rel)).stat()
            fp["imt"], fp["isz"] = s.st_mtime_ns, s.st_size
        except OSError:
            fp["imt"] = fp["isz"] = None
    return fp


def fingerprint_matches(fp: dict[str, Any] | None, qpath: Path, meta: dict[str, Any]) -> bool:
    """指纹是否还对得上（对不上 = 题被换过了，标记该失效）。

    先比 mtime+size：一致就直接认（省掉读文件算哈希）；
    不一致再算 sha256 —— 内容没变（只是重写/搬运）时保留标记，并把 stat 刷新掉。
    """
    if not isinstance(fp, dict):
        return False
    try:
        st = qpath.stat()
    except OSError:
        return False
    # 题图：换图也算「重新上传」
    rel = meta.get("image")
    if rel and ("imt" in fp or "isz" in fp):
        try:
            s = (qpath.parent / str(rel)).stat()
            if (fp.get("imt"), fp.get("isz")) != (s.st_mtime_ns, s.st_size):
                return False
        except OSError:
            if fp.get("imt") is not None:
                return False
    if (fp.get("mt"), fp.get("sz")) == (st.st_mtime_ns, st.st_size):
        return True
    try:
        sha = hashlib.sha256(qpath.read_bytes()).hexdigest()
    except OSError:
        return False
    if sha != fp.get("sha"):
        return False
    # 内容一致，只是文件被重写过：刷新 stat，免得每次都算哈希
    fp["mt"], fp["sz"] = st.st_mtime_ns, st.st_size
    return True


# ---------------------------------------------------------------------------
# 查
# ---------------------------------------------------------------------------

def items() -> dict[str, dict]:
    return state.flagged_all()


def flagged_ids() -> set[str]:
    return state.flagged_ids()


def get(qid: str) -> dict[str, Any] | None:
    return state.get_flag(str(qid))


def is_flagged(qid: str) -> bool:
    return state.is_flagged(str(qid))


# ---------------------------------------------------------------------------
# 写
# ---------------------------------------------------------------------------

def set_flag(qid: str, on: bool, by: str | None = None,
             note: str | None = None) -> dict[str, Any]:
    """打 / 撤标记。on=True 时用当前文件指纹记账；on=False 直接删记录。"""
    if not on:
        return state.set_flag(str(qid), False)

    import store
    found = store.find_question(str(qid))
    if found is None:
        raise KeyError(qid)
    _, qpath = found
    meta = store.read_meta(qpath)
    return state.set_flag(str(qid), True, by=by, note=note, fp=fingerprint(qpath, meta))


# ---------------------------------------------------------------------------
# 指纹校验（不在请求热路径上）
# ---------------------------------------------------------------------------

def verify_one(qid: str) -> bool:
    """校验单条标记。指纹对不上 / 文件没了 → 删除记录并返回 False。

    对得上返回 True（即使没有这条标记也返回 False）。
    """
    qid = str(qid)
    rec = state.get_flag(qid)
    if rec is None:
        return False

    import store
    found = store.find_question(qid)
    if found is None:
        state.drop_flag(qid)
        return False
    _, qpath = found
    try:
        meta = store.read_meta(qpath)
    except OSError:
        state.drop_flag(qid)
        return False

    fp = rec.get("fp")
    if not fingerprint_matches(fp, qpath, meta):
        state.drop_flag(qid)
        return False
    # 内容没变但 stat 被刷新过 → 落库，下回不用再算 sha256
    if isinstance(fp, dict) and (fp.get("mt"), fp.get("sz")) != (
        qpath.stat().st_mtime_ns, qpath.stat().st_size
    ):
        state.touch_flag(qid, fp)
    return True


def sweep(limit: int = 200) -> int:
    """轮转校验最久没验过的 limit 条，返回本次被清掉的条数。"""
    cleaned = 0
    for qid in state.flags_stale(limit):
        if not verify_one(qid):
            cleaned += 1
    return cleaned


def prune() -> dict[str, Any]:
    """兼容旧接口：全量校验一遍（脚本/手动用，**不要**放进请求路径）。"""
    cleaned = 0
    for qid in list(state.flagged_ids()):
        if not verify_one(qid):
            cleaned += 1
    return {"cleaned": cleaned, "remaining": state.flagged_count()}
