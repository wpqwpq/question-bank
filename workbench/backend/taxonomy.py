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
