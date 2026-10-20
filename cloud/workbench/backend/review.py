"""预览模式「审核确认」记录。

确认单元（unit）的定义（见 docs/11）：
  - 非切分题  → 主题 qid 本身（如 `edexcel-a-level-physics-2020-jan-wph11-q01`）；
  - 切分题    → 每个小问的 qid（`{parent}-{label}`，即既有子题文件的 id）。

存储已从 data/_review/confirmed.json 迁到 var/state.db（见 state.py 顶部说明）：
  · 旧实现每次确认都要深拷贝 + 重写整个 JSON（10 万条时约 180ms，且并发会丢更新）；
  · 现在一条 UPSERT（约 0.02ms），事务保证不丢。

本模块只维护「确认状态」，**不改题库正本**（qNN.md / cut.json / 词表一字不动）。
对外函数名与签名保持不变（set_confirmed 多了可选的 by），api.py 无需改动。
"""
from __future__ import annotations

from typing import Any

import state


def items() -> dict[str, dict[str, Any]]:
    """兼容旧接口：{unit_id: {"at", "by"}}。读库，别在热路径上用。

    热路径请用 confirmed_ids()（走内存 set，零 IO）。
    """
    return state.confirmed_items()


def load() -> dict[str, Any]:
    """兼容旧接口：返回旧的 JSON 形状（version/updated_at/items）。读库。"""
    return {
        "version": state.SCHEMA_VERSION,
        "updated_at": state.now_iso(),
        "items": items(),
    }


def confirmed_ids() -> set[str]:
    """全部已确认单元 id 的集合（内存副本，一次请求取一份）。"""
    return state.confirmed_ids()


def is_confirmed(uid: str) -> bool:
    return state.is_confirmed(uid)


def set_confirmed(uid: str, on: bool, by: str | None = None) -> dict[str, Any]:
    """确认 / 取消确认一个单元。返回 {qid, confirmed, at, by}。"""
    return state.set_confirmed(uid, on, by=by)
