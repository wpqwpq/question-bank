"""标签词表加载与按卷过滤。

词表按考试局/资格/学科一文件存放：data/taxonomy/<board>/<qual>/<subject>.json，
**该 JSON 即标签的唯一事实源**（2026-10-16 起不再由词表 TSV 生成；历史上的生成脚本
scripts/gen_tags_json.py 仅作 slug/id 规则参考，日常改标签直接改 JSON，将来走打标台前端界面）。
结构见 docs/06b 实施设计稿：

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
import os
import re
import tempfile
from datetime import datetime
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

# 跨单元的「指定组」开放：(考试局, 卷单元) -> [(外部单元, [组 id, …]), …]。
# 与 _CROSS_UNIT_SELECTABLE（整单元开放）互补，用于「只开放外面某几个组」的场景：
# CIE 老课纲 AS 阶段就要求「电场」，现行课纲把它挪到了 Paper 4；故把 P4 的电场组
# 开放给 P1P2 卷（2026-10-16）。显示顺序：本单元各组在前，外部组排在下方，
# 前端按 group.unit 在交界处插分隔线。
_CROSS_UNIT_GROUPS: dict[tuple[str, str], list[tuple[str, list[str]]]] = {
    ("cie", "P1P2"): [("P4", ["p4g-electric-field"])],
}

# 缓存：(board, qual, subject) → ((mtime_ns, size), 词表内容)。
# 2026-10-16 起按文件签名失效（旧实现是进程内永久缓存，改 JSON 必须重启打标台）：
# 手工改文件、或走标签管理页保存，都能即时生效；保存接口另会显式 invalidate 一次。
_cache: dict[tuple[str, str, str], tuple[tuple[int, int], dict]] = {}


def _file_sig(path: Path) -> tuple[int, int]:
    try:
        st = path.stat()
    except OSError:
        return (0, 0)
    return (st.st_mtime_ns, st.st_size)


def invalidate(board: str, qualification: str, subject: str) -> None:
    """丢掉某份词表的缓存（保存后调用；正常情况下签名比对已能失效）。"""
    _cache.pop((board, qualification, subject), None)


# 已知的规范资格目录名；用于把 a-level / alevel 这类历史异写法双向互认。
_CANONICAL_QUALS = ("a-level", "as-level", "igcse", "gcse")


def _qual_candidates(qualification: str) -> list[str]:
    """资格目录名的候选路径片段，按优先级：原样 → 去连字符 → 规范写法。

    历史遗留：CIE 的卷数据目录与 front-matter 用 `alevel`，词表目录建的是 `a-level`
    （Edexcel/AQA 则统一 a-level）。把两者都列入候选，任一写法都能命中同一份词表。
    """
    key = qualification.replace("-", "").lower()
    out: list[str] = []
    for q in (qualification, key, *_CANONICAL_QUALS):
        if q and q.replace("-", "").lower() == key and q not in out:
            out.append(q)
    return out


def _taxonomy_path(board: str, qualification: str, subject: str) -> Path:
    """定位词表文件。

    历史遗留：CIE 的卷数据目录与 front-matter 用 `alevel`，而词表目录建的是 `a-level`
    （Edexcel/AQA 则统一 a-level）。这里按候选依次探测，两种写法都能命中；
    都找不到时返回首选路径，由调用方按「无词表」处理（返回 {}）。
    """
    base = TAXONOMY_DIR / board
    cands = _qual_candidates(qualification)
    for q in cands:
        p = base / q / f"{subject}.json"
        if p.exists():
            return p
    return base / (cands[0] if cands else qualification) / f"{subject}.json"


def taxonomy_path(board: str, qualification: str, subject: str) -> Path:
    """词表文件路径（公开包装，供标签管理页判断文件是否存在）。"""
    return _taxonomy_path(board, qualification, subject)


def load_taxonomy(board: str, qualification: str, subject: str) -> dict:
    """返回某学科全部单元的词表；无词表文件返回 {}。

    缓存按文件 `mtime_ns + size` 失效（与 store.py 同一套路），手工改 JSON 也即时生效。
    """
    key = (board, qualification, subject)
    path = _taxonomy_path(board, qualification, subject)
    sig = _file_sig(path)
    hit = _cache.get(key)
    if hit is None or hit[0] != sig:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        _cache[key] = (sig, data)
    return _cache[key][1]


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


def _full_extra_units(board: str, qualification: str, subject: str, unit: str, tax: dict) -> list[str]:
    """整单元开放的外部单元（_CROSS_UNIT_SELECTABLE 里除自身外、词表非空的）。"""
    units = _CROSS_UNIT_SELECTABLE.get((board, unit)) or []
    return [u for u in units if u != unit and _unit_taxonomy(tax, u)["tags"]]


def _scoped_group_specs(board: str, unit: str, tax: dict) -> list[tuple[str, list[str]]]:
    """组级开放的外部单元：[(单元, [组 id…])]，仅保留词表存在的单元。"""
    return [
        (u, gids) for u, gids in _CROSS_UNIT_GROUPS.get((board, unit), [])
        if _unit_taxonomy(tax, u)["tags"]
    ]


def tags_for_units(board: str, qualification: str, subject: str, unit: str) -> list[dict]:
    """本卷可选的全部标签：本单元全部 + 整单元开放的外部单元 + 组级开放的外部组。
    每项仍带 group 键；顺序即右栏显示顺序（本单元在前）。"""
    tax = load_taxonomy(board, qualification, subject)
    out: list[dict] = list(_unit_taxonomy(tax, unit)["tags"])
    for u in _full_extra_units(board, qualification, subject, unit, tax):
        out.extend(_unit_taxonomy(tax, u)["tags"])
    for u, gids in _scoped_group_specs(board, unit, tax):
        gset = set(gids)
        out.extend(t for t in _unit_taxonomy(tax, u)["tags"] if t.get("group") in gset)
    return out


def groups_for_units(board: str, qualification: str, subject: str, unit: str) -> list[dict]:
    """本卷可选的一级组。跨单元时组中文名加「<单元> 」前缀区分同名组，并附 unit
    字段标明组所属单元（前端据此在自身单元与其他单元之间插分隔线）。
    单单元时原样返回（前端分组头不变）。"""
    tax = load_taxonomy(board, qualification, subject)
    full = _full_extra_units(board, qualification, subject, unit, tax)
    scoped = _scoped_group_specs(board, unit, tax)
    if not full and not scoped:
        return _unit_taxonomy(tax, unit)["groups"]
    out: list[dict] = []
    for u in [unit, *full]:
        for g in _unit_taxonomy(tax, u)["groups"]:
            out.append({**g, "unit": u, "zh": f"{u} {g['zh']}"})
    for u, gids in scoped:
        gset = set(gids)
        for g in _unit_taxonomy(tax, u)["groups"]:
            if g["id"] in gset:
                out.append({**g, "unit": u, "zh": f"{u} {g['zh']}"})
    return out


def tag_ids_for_units(board: str, qualification: str, subject: str, unit: str) -> set[str]:
    return {t["id"] for t in tags_for_units(board, qualification, subject, unit)}


# ---------------------------------------------------------------------------
# 标签管理页：整份词表的校验与保存（本期只支持「改」+「增」）
# ---------------------------------------------------------------------------

# 单元键：Edexcel/AQA 物理化学用 U1..U9，CIE 用 P1P2 / P4 这类形式，
# Edexcel IAL 数学用 P1–P4 / M1–M3 / S1–S3 / D1 / F1–F3（词表按学科一文件，键只在文件内唯一）。
_UNIT_KEY_RE = re.compile(r"^(?:U[1-9]|P[1-9](?:P[1-9])?|[SMFD][1-9])$")

_TAG_KEYS = ("id", "zh", "en", "group")
_GROUP_KEYS = ("id", "zh", "en")


def _norm_tag(tag: dict) -> dict:
    return {k: (tag.get(k) or "") for k in _TAG_KEYS}


def _norm_group(group: dict) -> dict:
    return {k: (group.get(k) or "") for k in _GROUP_KEYS}


def validate_taxonomy(data) -> list[str]:
    """整份词表校验。返回错误描述列表，空列表 = 通过。

    规则（见 docs/09 §4）：
      1. 单元键形如 U1 / P1P2，且至少有一个单元；
      2. 标签 id / zh / en 非空，id 全局唯一，group 指向本单元内存在的组；
      3. 组 id / zh / en 非空，id 全局唯一，且每个组至少挂 1 个标签（不留空组头）。
    """
    if not isinstance(data, dict):
        return ["词表根节点必须是对象"]
    units = [k for k in data if not k.startswith("$")]
    if not units:
        return ["词表至少要有一个单元"]

    errors: list[str] = []
    tag_owner: dict[str, str] = {}
    group_owner: dict[str, str] = {}

    for unit in units:
        entry = data[unit]
        if not _UNIT_KEY_RE.match(unit):
            errors.append(f"单元键不合法：{unit}（应形如 U1 / P1P2 / M1）")
        if not isinstance(entry, dict):
            errors.append(f"{unit}：单元内容必须是对象")
            continue
        groups, tags = entry.get("groups"), entry.get("tags")
        if not isinstance(groups, list) or not isinstance(tags, list):
            errors.append(f"{unit}：groups / tags 必须是数组")
            continue

        local_groups: set[str] = set()
        for i, g in enumerate(groups, 1):
            if not isinstance(g, dict):
                errors.append(f"{unit} 第 {i} 个分组：必须是对象")
                continue
            gid = (g.get("id") or "").strip()
            if not gid:
                errors.append(f"{unit} 第 {i} 个分组：id 不能为空")
                continue
            if gid in group_owner:
                errors.append(f"{unit} 分组 {gid}：id 与 {group_owner[gid]} 里的同名分组重复")
            group_owner[gid] = unit
            local_groups.add(gid)
            if not (g.get("zh") or "").strip():
                errors.append(f"{unit}/{gid}：分组中文名不能为空")
            if not (g.get("en") or "").strip():
                errors.append(f"{unit}/{gid}：分组英文名不能为空（用于生成 id）")

        used: dict[str, int] = dict.fromkeys(local_groups, 0)
        for i, t in enumerate(tags, 1):
            if not isinstance(t, dict):
                errors.append(f"{unit} 第 {i} 个标签：必须是对象")
                continue
            tid = (t.get("id") or "").strip()
            where = tid or f"{unit} 第 {i} 个标签"
            if not tid:
                errors.append(f"{where}：id 不能为空")
            elif tid in tag_owner:
                errors.append(f"{where}：id 与 {tag_owner[tid]} 里的标签重复")
            else:
                tag_owner[tid] = unit
            if not (t.get("zh") or "").strip():
                errors.append(f"{where}：中文名不能为空")
            if not (t.get("en") or "").strip():
                errors.append(f"{where}：英文名不能为空（用于生成 id）")
            gid = (t.get("group") or "").strip()
            if not gid:
                errors.append(f"{where}：必须指定所属分组")
            elif gid not in local_groups:
                errors.append(f"{where}：所属分组 {gid} 在本单元里不存在")
            else:
                used[gid] += 1

        for gid in local_groups:
            if used.get(gid, 0) == 0:
                errors.append(f"{unit}/{gid}：分组下没有任何标签（不留空组头）")

    return errors


def save_taxonomy(board: str, qualification: str, subject: str, data) -> dict:
    """校验 + 备份 + 原子写，整体覆盖词表 JSON。

    前端提交的内容即使不含 `$` 元数据也不打紧——原文件的 `$comment` 等键会保留在最前。
    写入顺序：先落备份到 `_prev/`（该目录已 gitignore，不入公开仓库），再原子替换正本。
    """
    path = _taxonomy_path(board, qualification, subject)
    if not path.exists():
        raise FileNotFoundError(f"词表文件不存在：{board}/{qualification}/{subject}")

    errors = validate_taxonomy(data)
    if errors:
        raise ValueError("\n".join(errors))

    old = json.loads(path.read_text(encoding="utf-8"))
    units = [k for k in data if not k.startswith("$")]
    out: dict = {k: v for k, v in old.items() if k.startswith("$")}
    for unit in units:
        entry = data[unit]
        out[unit] = {
            "groups": [_norm_group(g) for g in entry.get("groups") or []],
            "tags": [_norm_tag(t) for t in entry.get("tags") or []],
        }

    backup_dir = TAXONOMY_DIR / "_prev"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = backup_dir / f"{board}-{subject}-{stamp}.json"
    backup.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")

    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(json.dumps(out, ensure_ascii=False, indent=2) + "\n")
        os.replace(tmp_name, path)
    except BaseException:
        if os.path.exists(tmp_name):
            os.remove(tmp_name)
        raise

    invalidate(board, qualification, subject)

    root = TAXONOMY_DIR.parents[1]
    return {
        "path": path.relative_to(root).as_posix(),
        "backup": backup.relative_to(root).as_posix(),
        "units": len(units),
        "tags": sum(len(data[u].get("tags") or []) for u in units),
    }
