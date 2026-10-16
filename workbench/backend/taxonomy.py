"""标签词表加载与按卷过滤。

词表按考试局/资格/学科一文件存放：data/taxonomy/<board>/<qual>/<subject>.json，
由 scripts/gen_tags_json.py 从词表 TSV 生成（结构见 docs/06b 实施设计稿）：

    {
      "$comment": "...",
      "U1": {
        "groups": [ { "id": "u1g-kinematics", "zh": "运动学", "en": "kinematics" } ],
        "tags":   [ { "id": "u1-acceleration", "zh": "加速度", "en": "acceleration", "group": "u1g-kinematics" } ]
      }
    }

落盘（qNN.md front-matter 的 tags）只用二级 tag id；一级组不落盘，由 group 字段推导。
"""
from __future__ import annotations

import json
from pathlib import Path

TAXONOMY_DIR = Path(__file__).resolve().parents[2] / "data" / "taxonomy"

# 跨单元可选标签：(考试局, 卷单元) → 可选单元集合。
# AQA Unit 5「Physics in practice」的 Section B 是任意单元知识的情境综合题，
# 故 U5 卷放开 U1..U5 的标签；其余卷（含 Edexcel 的 U5）一律只可选自身单元。
# 列表顺序 = 右栏显示顺序：U5 自身知识点置顶（Section A 直接对应），
# U1–U4 依次排在其后（Section B 综合题按需引用）。
_CROSS_UNIT_SELECTABLE: dict[tuple[str, str], list[str]] = {
    ("aqa", "U5"): ["U5", "U1", "U2", "U3", "U4"],
}

_cache: dict[tuple[str, str, str], dict] = {}


def _taxonomy_path(board: str, qualification: str, subject: str) -> Path:
    return TAXONOMY_DIR / board / qualification / f"{subject}.json"


def load_taxonomy(board: str, qualification: str, subject: str) -> dict:
    """返回某学科全部单元的词表；无词表文件返回 {}。"""
    key = (board, qualification, subject)
    if key not in _cache:
        path = _taxonomy_path(board, qualification, subject)
        _cache[key] = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    return _cache[key]


def _unit_taxonomy(tax: dict, unit: str) -> dict:
    entry = tax.get(unit)
    if not isinstance(entry, dict) or "tags" not in entry:
        return {"groups": [], "tags": []}
    return entry


def tags_for(board: str, qualification: str, subject: str, unit: str) -> list[dict]:
    """某单元可选标签（扁平 list，每项多带 group 键）；无词表/未知单元返回空列表。"""
    tax = load_taxonomy(board, qualification, subject)
    return _unit_taxonomy(tax, unit)["tags"]


def groups_for(board: str, qualification: str, subject: str, unit: str) -> list[dict]:
    """某单元一级组列表（用于右栏分组头）；无词表返回空列表。"""
    tax = load_taxonomy(board, qualification, subject)
    return _unit_taxonomy(tax, unit)["groups"]


def tag_ids(board: str, qualification: str, subject: str, unit: str) -> set[str]:
    return {t["id"] for t in tags_for(board, qualification, subject, unit)}


# ---------------------------------------------------------------------------
# 跨单元：本卷可选标签可能来自多个单元（见 _CROSS_UNIT_SELECTABLE）
# ---------------------------------------------------------------------------

def selectable_units(board: str, qualification: str, subject: str, unit: str) -> list[str]:
    """本卷可选标签的单元列表。默认只有自身单元；命中跨单元规则时按规则展开，
    并过滤掉词表里不存在的单元（避免展开出空单元）。"""
    units = _CROSS_UNIT_SELECTABLE.get((board, unit))
    if not units:
        return [unit]
    tax = load_taxonomy(board, qualification, subject)
    return [u for u in units if _unit_taxonomy(tax, u)["tags"]]


def tags_for_units(board: str, qualification: str, subject: str, unit: str) -> list[dict]:
    """本卷可选的全部标签（跨单元时按单元顺序合并；每项仍带 group 键）。"""
    out: list[dict] = []
    for u in selectable_units(board, qualification, subject, unit):
        out.extend(tags_for(board, qualification, subject, u))
    return out


def groups_for_units(board: str, qualification: str, subject: str, unit: str) -> list[dict]:
    """本卷可选的一级组。跨单元时组中文名加「U<x> 」前缀，区分同名组，并附 unit
    字段标明组所属单元（前端据此在自身单元与其他单元之间插分隔）。
    单单元时原样返回（前端分组头不变）。"""
    units = selectable_units(board, qualification, subject, unit)
    if len(units) <= 1:
        return groups_for(board, qualification, subject, units[0]) if units else []
    out: list[dict] = []
    for u in units:
        for g in groups_for(board, qualification, subject, u):
            out.append({**g, "unit": u, "zh": f"{u} {g['zh']}"})
    return out


def tag_ids_for_units(board: str, qualification: str, subject: str, unit: str) -> set[str]:
    return {t["id"] for t in tags_for_units(board, qualification, subject, unit)}
