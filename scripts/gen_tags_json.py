"""从词表 TSV 生成 taxonomy JSON：data/taxonomy/<board>/<qual>/<subject>.json

⚠️ 已停用（2026-10-16）：标签的唯一事实源改为 taxonomy JSON 本身，不再维护词表 TSV；
改标签直接改 JSON（将来由打标台前端界面写入）。本脚本降级为**历史迁移工具 + id 规则参考**
（下方 slug() 即标签/组 id 的生成规则），不要再拿它覆盖现有 JSON。

词表格式（docs/06b-物理词表.tsv、docs/06b-CIE物理词表.tsv，TSV，5 列）：
    unit  group_zh  group_en  tag_zh  tag_en
每行都带完整 unit 与组名（展开式，无延续行）。unit 取 U<号>（Edexcel 单元）
或 P<号>（CIE paper：P1P2 = paper 1+2 共用词表，P4 = paper 4；P3/P5 不分类）。

生成规则（06b 设计稿 §3）：
- 一级组 id：u<单元号>g-<组英文slug>；二级 tag id：u<单元号>-<标签英文slug>（D1 全量重生成）。
- tag 携带 group 引用；一级组不落盘到 qNN.md（06 §3）。
- 所有单元统一写 {groups, tags}（06b D3=A），U3/U6 单组也写，前端单组不渲染组头。

用法：
    python scripts/gen_tags_json.py docs/06b-物理词表.tsv edexcel ial physics
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def slug(text: str) -> str:
    """英文短语 → url-safe slug：去撇号/点，非字母数字连字符化。"""
    s = text.lower().replace("'", "").replace(".", "").replace("’", "")
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")


def main() -> None:
    if len(sys.argv) != 5:
        sys.exit(__doc__)
    tsv, board, qual, subject = sys.argv[1:5]

    rows = [
        line.rstrip("\n").split("\t")
        for line in Path(tsv).read_text(encoding="utf-8-sig").splitlines()
        if line.strip() and not line.startswith("unit\t")
    ]
    assert all(len(r) == 5 for r in rows), "TSV 必须恰好 5 列"

    units: dict[str, dict] = {}
    group_ids: set[str] = set()
    tag_ids: set[str] = set()
    errors: list[str] = []

    for no, (unit, gz, ge, tz, te) in enumerate(rows, 2):
        if not re.fullmatch(r"U\d+|P\d+(?:P\d+)?", unit):
            errors.append(f"L{no}: 单元号非法 {unit!r}")
            continue
        if not tz:
            errors.append(f"L{no}: 标签中文为空")
            continue
        u = units.setdefault(unit, {"groups": [], "tags": []})
        gid = f"{unit.lower()}g-{slug(ge or gz)}"
        g = next((g for g in u["groups"] if g["id"] == gid), None)
        if g is None:
            if any(g["zh"] == gz for g in u["groups"]):
                errors.append(f"L{no}: 组中文名在本单元重复: {gz}")
            g = {"id": gid, "zh": gz, "en": ge}
            u["groups"].append(g)
            if gid in group_ids:
                errors.append(f"L{no}: 组 id 全局重复: {gid}")
            group_ids.add(gid)
        tid = f"{unit.lower()}-{slug(te or tz)}"
        if tid in tag_ids:
            errors.append(f"L{no}: 标签 id 全局重复: {tid}（{tz}）")
        tag_ids.add(tid)
        u["tags"].append({"id": tid, "zh": tz, "en": te, "group": gid})

    for unit, u in units.items():
        known = {g["id"] for g in u["groups"]}
        for t in u["tags"]:
            if t["group"] not in known:
                errors.append(f"{unit}: 标签 {t['id']} 引用了不存在的组 {t['group']}")
        for g in u["groups"]:
            if not any(t["group"] == g["id"] for t in u["tags"]):
                errors.append(f"{unit}: 组 {g['id']}（{g['zh']}）没有任何标签")

    if errors:
        print("\n".join(errors))
        sys.exit(f"词表校验失败，共 {len(errors)} 处")

    out = {
        "$comment": "知识点标签词表（按考试局/资格/学科一文件）。结构见 docs/06b-标签两级化实施设计-物理新词表.md；id 由 scripts/gen_tags_json.py 从词表 TSV 生成，请勿手改。",
        **units,
    }
    dest = REPO / "data" / "taxonomy" / board / qual / f"{subject}.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    n_tags = sum(len(u["tags"]) for u in units.values())
    n_groups = sum(len(u["groups"]) for u in units.values())
    print(f"OK {dest}")
    print(f"units={len(units)} groups={n_groups} tags={n_tags}")
    for unit in sorted(units):
        u = units[unit]
        print(f"  {unit}: {len(u['groups'])} 组 / {len(u['tags'])} 标签")


if __name__ == "__main__":
    main()
