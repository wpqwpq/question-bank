# -*- coding: utf-8 -*-
"""IG 生物三考试局词表生成（2026-11-15，方案 A：0970 与 0610 同映射）。

输入：_tmp/ig_bio_rows.csv —— 从王培琪提供的 IG.xlsx 只读导出的 399 行
      （列：考试局,学段,Paper标记,组zh,组en,标签zh,标签en；无表头）。
输出：data/taxonomy/{edexcel,aqa,cie}/igcse/biology.json（新建，不覆盖现有文件）。

池划分（培琪 2026-11-15 定）：
  EDX  Paper 1 + Paper 2 并集 → U1U2 单池（124 标签 / 22 组）
  AQA  Paper1,2              → P1P2 单池（114 / 27）
  CAIE Paper 1,3             → P13（110 / 21）
  CAIE Paper 2,4             → P24（34 / 15，跨单元开 P13 由 taxonomy 现有规则负责）
  CAIE Paper 5,6             → P56（17 / 1）

id 规则沿用 gen_tags_json.slug()：标签 <unit>-<英文slug>、组 <unit>g-<英文slug>。
组/标签顺序 = 表内行序（EDX 合池 = P1 全部行在前、P2 行在后；同名组自动归并）。
生成后跑 workbench/backend/taxonomy.validate_taxonomy() 全量校验，任一错误即中止。
"""
from __future__ import annotations

import csv
import json
import re
import sys
from collections import OrderedDict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CSV_PATH = ROOT / "_tmp" / "ig_bio_rows.csv"
sys.path.insert(0, str(ROOT / "cloud" / "workbench" / "backend"))
import taxonomy  # noqa: E402


def slug(text: str) -> str:
    """与 scripts/gen_tags_json.py 完全一致。"""
    s = text.lower().replace("'", "").replace(".", "").replace("\u2019", "")
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")


# (考试局, Paper标记) → 单元键。标记写法不统一（空格/逗号），按表内实际值精确匹配。
MARK_TO_UNIT = {
    ("EDX", "Paper 1"): "U1U2",
    ("EDX", "Paper 2"): "U1U2",
    ("AQA", "Paper1,2"): "P1P2",
    ("CAIE", "Paper 1,3"): "P13",
    ("CAIE", "Paper 2,4"): "P24",
    ("CAIE", "Paper 5,6"): "P56",
}

BOARD_OF = {"EDX": "edexcel", "AQA": "aqa", "CAIE": "cie"}

COMMENT = (
    "IGCSE 生物知识点词表（{board}）。来源：王培琪提供的 IG.xlsx（2026-11-15，399 行），"
    "经 scripts/gen_ig_biology_taxonomy.py 生成。"
    "EDX 两 paper 并集为 U1U2 单池；AQA P1P2 单池；"
    "CIE P13=core（Paper 1/3）、P24=extended 增量（Paper 2/4，跨单元开 P13 见 "
    "taxonomy._CROSS_UNIT_SELECTABLE）、P56=实践（Paper 5/6）。"
    "CIE 0610 与 0970 同映射（方案 A，培琪定）。id 一经生成即冻结。"
)


def build_unit(rows: list[list[str]], unit: str) -> dict:
    """把若干行组装成一个单元 {groups, tags}；组按首次出现序，标签按行序。"""
    prefix = unit.lower()
    groups: "OrderedDict[str, dict]" = OrderedDict()
    tags: list[dict] = []
    for r in rows:
        g_zh, g_en, t_zh, t_en = (r[3].strip(), r[4].strip(), r[5].strip(), r[6].strip())
        gid = f"{prefix}g-{slug(g_en)}"
        tid = f"{prefix}-{slug(t_en)}"
        assert gid and tid, f"空 slug: {r}"
        if gid not in groups:
            groups[gid] = {"id": gid, "zh": g_zh, "en": g_en}
        else:
            # 同名组（中英全等）才允许归并；不一致说明源表有脏数据
            assert groups[gid]["zh"] == g_zh and groups[gid]["en"] == g_en, f"组名冲突: {r}"
        tags.append({"id": tid, "zh": t_zh, "en": t_en, "group": gid})
    return {"groups": list(groups.values()), "tags": tags}


def main() -> None:
    rows = list(csv.reader(CSV_PATH.open(encoding="utf-8-sig")))
    rows = [r for r in rows if any(c.strip() for c in r)]
    assert len(rows) == 399, f"行数异常: {len(rows)}"
    for r in rows:
        assert len(r) == 7 and all(c.strip() for c in r), f"坏行: {r}"
        assert (r[0], r[2]) in MARK_TO_UNIT, f"未知标记: {r[:3]}"
        assert r[1] == "IGCSE", f"学段异常: {r}"

    # 按 board → unit 收集行（保持表内行序）
    by_board: dict[str, dict[str, list]] = {}
    for r in rows:
        b = BOARD_OF[r[0]]
        u = MARK_TO_UNIT[(r[0], r[2])]
        by_board.setdefault(b, {}).setdefault(u, []).append(r)

    # 单元顺序：edexcel/aqa 单单元；cie 按 P13/P24/P56
    unit_order = {"edexcel": ["U1U2"], "aqa": ["P1P2"], "cie": ["P13", "P24", "P56"]}
    for board in ("edexcel", "aqa", "cie"):
        data: dict = {"$comment": COMMENT.format(board=board)}
        for u in unit_order[board]:
            data[u] = build_unit(by_board[board][u], u)
        errors = taxonomy.validate_taxonomy(data)
        assert not errors, f"{board} 校验失败:\n" + "\n".join(errors)
        out = ROOT / "data" / "taxonomy" / board / "igcse" / "biology.json"
        assert not out.exists(), f"目标已存在，拒绝覆盖: {out}"
        out.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        n_g = sum(len(data[u]["groups"]) for u in unit_order[board])
        n_t = sum(len(data[u]["tags"]) for u in unit_order[board])
        print(f"[OK] {board}: {n_g} 组 / {n_t} 标签 → {out.relative_to(ROOT)}")
        for u in unit_order[board]:
            print(f"     {u}: {len(data[u]['groups'])} 组 / {len(data[u]['tags'])} 标签")


if __name__ == "__main__":
    main()