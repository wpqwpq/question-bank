"""导出：打标信息 / 知识点体系。

设计要点（与 docs/22-导出API说明.md v1.1 对应）：

1. **只出不进**。本模块不写任何文件（除了调用方指定的输出路径），不碰 md / 词表 / state.db。
2. **一行一个标注单元**。切分题的父行没有标签，一律跳过；子题行带 parent_qid 指回主题。
3. **不导出 tag id**。老师可以把某个 id 下的中英文改成完全无关的内容，id 已不代表语义。
   中英文两列一一对应，同一单元内各自唯一（2026-11-04 清理后 0 处重名）。
4. **数据源是 index.db**（与打标台列表页、概览页同一份），不逐题读 md —— 82,180 行 2–3 秒，
   没有这个量级就跑不动。

CLI（先用来验证产物，HTTP 路由是后面那层薄壳）：

    python export.py annotations --out DIR [--board B --qual Q --subject S] [--format csv|jsonl]
    python export.py taxonomy    --out DIR [--board B --qual Q --subject S] [--all] [--format csv|json]
    python export.py all         --out DIR
"""
from __future__ import annotations

import csv
import io
import json
import sys
import time
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Iterator

import auth
import review
import store
import taxonomy

# 多值分隔符。**不能用顿号**：214 个中文标签名里本身就含「、」
# （如「市场细分、目标市场与定位」），用顿号分列会把一个标签拆成两个。
# 竖线在所有 5,980 个标签的中英文名里出现 0 次（2026-11-04 全库核对）。
SEP = "|"

SCHEMA_ANNOT = "qbank.export.v1"
SCHEMA_TAX = "qbank.taxonomy.v1"

# 与前端 api.ts 同一份口径，不另起一套
BOARD_ZH = {"edexcel": "Edexcel", "aqa": "AQA", "cie": "CIE"}
SUBJECT_ZH = {
    "physics": "物理", "chemistry": "化学", "biology": "生物",
    "mathematics": "数学", "further-mathematics": "进阶数学",
    "economics": "经济", "accounting": "会计", "business": "商务",
    "combined_science": "综合科学", "psychology": "心理学",
}
SESSION_ZH = {
    "jan": "1月", "mar": "3月", "may-jun": "5-6月", "june": "6月",
    "oct": "10月", "oct-nov": "10-11月", "nov": "11月",
}
DIFF_ZH = {"very-easy": "极易", "easy": "简单", "medium": "中等",
           "hard": "困难", "very-hard": "极难"}
STATUS_ZH = {"done": "已打标", "partial": "部分", "none": "未打标"}
QUAL_ZH = {"ig": "IGCSE", "al": "A-Level"}

COLUMNS_ZH = [
    "考试局", "学段", "学科", "单元", "卷号", "年份", "季次", "题号", "小问",
    "卷ID", "题目ID", "父题ID",
    "标签（中文）", "标签（英文）", "主标签（中文）", "主标签（英文）",
    "难度", "状态", "已确认", "有误",
]
COLUMNS_EN = [
    "board", "qualification", "subject", "unit", "paper_code", "year", "session",
    "question_number", "sub_label", "paper_id", "qid", "parent_qid",
    "tags_zh", "tags_en", "primary_tag_zh", "primary_tag_en",
    "difficulty", "status", "verified", "flagged",
]
TAX_COLUMNS_ZH = ["考试局", "学段", "学科", "单元", "组（中文）", "组（英文）",
                  "标签（中文）", "标签（英文）", "使用题数"]
TAX_COLUMNS_EN = ["board", "qualification", "subject", "unit", "group_zh", "group_en",
                  "tag_zh", "tag_en", "usage"]


@dataclass
class Filters:
    board: str | None = None
    qual: str | None = None
    subject: str | None = None
    unit: str | None = None
    year: int | None = None
    session: str | None = None
    paper_id: str | None = None
    tags: list[str] = field(default_factory=list)          # 中文标签名，OR
    difficulties: list[str] = field(default_factory=list)
    statuses: list[str] = field(default_factory=list)
    only_tagged: bool = False
    only_flagged: bool = False       # 只输出「此题有误」的行（见 iter_annotation_rows 说明）
    lang: str = "zh"
    level: str | None = None          # ig / al，只收敛学段，不要求给学科


# ---------------------------------------------------------------------------
# 范围
# ---------------------------------------------------------------------------

def resolve_scope(ident: auth.Ident | None, f: Filters) -> list[tuple[str, str, str]]:
    """把权限 + 请求参数收敛成一组 (board, qualification, subject)。

    ident 传 None 时表示"不限"（CLI 用）。
    qualification 用**目录名**（cie 是 alevel），后面拼 SQL 时按它比。
    """
    if f.board or f.qual or f.subject:
        if not (f.board and f.qual and f.subject):
            raise ValueError("board / qual / subject 必须同时给出")
    out: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for p in store.iter_papers():
        key = (p.board, p.qualification, p.subject)
        if key in seen:
            continue
        if ident is not None and not ident.can_paper(p.subject, p.qualification):
            continue
        if f.board and (p.board != f.board or p.subject != f.subject):
            continue
        if f.qual and auth.level_of(p.qualification) != auth.level_of(f.qual):
            continue
        seen.add(key)
        out.append(key)
    if f.level:
        out = [k for k in out if auth.level_of(k[1]) == f.level]
    if (f.board or f.level) and not out:
        raise LookupError(f"没有可导出的学科：{f.board or ''}/{f.qual or ''}/"
                          f"{f.subject or ''} level={f.level or ''}")
    return sorted(out)


# ---------------------------------------------------------------------------
# 标签字典：id → (zh, en)，按学科分桶
# ---------------------------------------------------------------------------

class TagIndex:
    """(board, qual, subject) → {tag_id: (zh, en)}，以及反向 {zh: id}。

    ⚠️ 必须按学科分桶：tag id 不是全局唯一的（583 个 id 跨文件重复，
    如 u1-carbohydrates 在 AQA 是「碳水化合物」、Edexcel 是「糖类」）。
    """

    def __init__(self) -> None:
        self._by_key: dict[tuple[str, str, str], dict[str, tuple[str, str]]] = {}

    def get(self, board: str, qual: str, subject: str) -> dict[str, tuple[str, str]]:
        key = (board, qual, subject)
        hit = self._by_key.get(key)
        if hit is None:
            tax = taxonomy.load_taxonomy(board, qual, subject)  # CIE 的 alevel/a-level 由它兜住
            hit = {}
            for unit, entry in tax.items():
                if unit.startswith("$"):
                    continue
                for t in entry.get("tags") or []:
                    hit[str(t.get("id"))] = ((t.get("zh") or "").strip(),
                                             (t.get("en") or "").strip())
            self._by_key[key] = hit
        return hit


def _ids_for_names(tag_index: TagIndex, scope: Iterable[tuple[str, str, str]],
                   names: list[str]) -> dict[tuple[str, str, str], set[str]]:
    """中文标签名 → 各学科下的 id 集合。用于 SQL 侧 LIKE 过滤。"""
    want = {n.strip() for n in names if n.strip()}
    out: dict[tuple[str, str, str], set[str]] = {}
    for key in scope:
        hit = {tid for tid, (zh, _en) in tag_index.get(*key).items() if zh in want}
        if hit:
            out[key] = hit
    return out


# ---------------------------------------------------------------------------
# 打标信息
# ---------------------------------------------------------------------------

_ROW_SQL = """
SELECT q.qid, q.parent_qid, q.qnum, q.sub_label, q.split, q.total_marks,
       q.question_type, q.status, q.difficulty, q.primary_tag, q.tags,
       p.paper_id, p.board, p.qualification, p.subject, p.unit,
       p.paper_code, p.year, p.session
FROM questions q JOIN papers p ON p.paper_id = q.paper_id
WHERE NOT (q.split = 1 AND q.parent_qid IS NULL)
"""


def _read_conn():
    """本次导出**独占**的只读连接。

    ⚠️ 不能用 index_db.connect() 的全局连接：它是 check_same_thread=False 的共享连接，
    而导出是"边查边吐"的长游标，别的请求在中间往同一连接上发查询会打乱游标状态。
    只读 URI 打开，WAL 下也不影响写。

    ⚠️ `check_same_thread=False` 是必须的（2026-11-20 实测踩到）：导出走
    StreamingResponse 的生成器，FastAPI 在线程池里切线程执行，而 sqlite3 默认禁止
    跨线程复用连接 —— `finally: con.close()` 一旦落在另一个线程就抛
    `ProgrammingError: SQLite objects created in a thread can only be used in that same thread`，
    客户端表现为「下载到一半连接被关」（curl 18）。连接只在本生成器内串行使用，
    跨线程但不同时，所以关掉检查是安全的。
    """
    import sqlite3
    import index_db
    con = sqlite3.connect(f"file:{index_db.DB_PATH}?mode=ro", uri=True, timeout=15,
                          check_same_thread=False)
    con.execute("PRAGMA busy_timeout=8000")
    return con


def iter_annotation_rows(scope: list[tuple[str, str, str]], f: Filters,
                         tag_index: TagIndex | None = None) -> Iterator[dict[str, Any]]:
    tag_index = tag_index or TagIndex()
    con = _read_conn()
    scope_clause = " AND (" + " OR ".join(
        "(p.board=? AND p.qualification=? AND p.subject=?)" for _ in scope) + ")"
    params: list[Any] = [x for triple in scope for x in triple]

    where = scope_clause
    if f.unit:
        where += " AND p.unit=?"
        params.append(f.unit)
    if f.year:
        where += " AND p.year=?"
        params.append(int(f.year))
    if f.session:
        where += " AND p.session=?"
        params.append(f.session)
    if f.paper_id:
        where += " AND p.paper_id=?"
        params.append(f.paper_id)
    if f.only_tagged:
        where += " AND q.tags IS NOT NULL AND q.tags NOT IN ('', '[]')"
    if f.difficulties:
        where += " AND q.difficulty IN (" + ",".join("?" * len(f.difficulties)) + ")"
        params.extend(f.difficulties)
    if f.statuses:
        where += " AND q.status IN (" + ",".join("?" * len(f.statuses)) + ")"
        params.extend(f.statuses)

    ids_by_key: dict[tuple[str, str, str], set[str]] = {}
    if f.tags:
        ids_by_key = _ids_for_names(tag_index, scope, f.tags)
        if not ids_by_key:
            con.close()
            return
        ors, oparams = [], []
        for key, ids in ids_by_key.items():
            for tid in ids:
                ors.append("(p.board=? AND p.qualification=? AND p.subject=? AND q.tags LIKE ?)")
                oparams.extend([*key, f'%"{tid}"%'])
        where += " AND (" + " OR ".join(ors) + ")"
        params.extend(oparams)

    sql = _ROW_SQL + where + (
        " ORDER BY p.board, p.qualification, p.subject, p.year, p.session,"
        " p.paper_id, CAST(q.qnum AS INTEGER), q.qnum, q.sub_label"
    )

    conf = review.confirmed_ids()
    from flags import flagged_ids
    flagged = flagged_ids()

    cur = con.execute(sql, params)
    try:
        for r in cur:
            (qid, parent_qid, qnum, sub_label, split, _marks, _qtype,
             status, difficulty, primary, tags_json,
             paper_id, board, qual, subject, unit, paper_code, year, session) = r
            try:
                tag_ids = json.loads(tags_json or "[]")
            except (ValueError, TypeError):
                tag_ids = []
            mapping = tag_index.get(board, qual, subject)
            pairs = [mapping.get(str(t), ("", "")) for t in tag_ids]
            zh_list = [p[0] for p in pairs]
            en_list = [p[1] for p in pairs]
            pzh, pen = mapping.get(str(primary), ("", "")) if primary else ("", "")

            qnum_text = str(qnum or "")
            if sub_label and qnum_text.endswith(f".{sub_label}"):
                qnum_text = qnum_text[: -(len(sub_label) + 1)]

            # 「有误」要看父子两侧：
            #   前端标记用的是**主题 qid**（切分题点「标记有误」→ flags 键 = 父 qid），
            #   而本函数一行一个标注单元 —— 切分题父行被 _ROW_SQL 排除，只剩 qNN-a 这类子行。
            #   只比 qid 会让切分题的标记 100% 漏报（2026-11-16 实测：economics 4EC1/01R
            #   q01–q04 全标了，导出 23 行全是「否」）。口径与 api.list_questions 一致。
            is_flagged = str(qid) in flagged or (
                bool(parent_qid) and str(parent_qid) in flagged)
            if f.only_flagged and not is_flagged:
                continue

            yield {
                "board": board, "qualification": qual, "subject": subject, "unit": unit or "",
                "paper_code": paper_code, "year": year, "session": session,
                "question_number": qnum_text, "sub_label": str(sub_label) if sub_label else "",
                "paper_id": paper_id, "qid": qid, "parent_qid": parent_qid or "",
                "tags_zh": zh_list, "tags_en": en_list,
                "primary_tag_zh": pzh, "primary_tag_en": pen,
                "difficulty": difficulty or "", "status": status or "",
                "verified": str(qid) in conf, "flagged": is_flagged,
            }
    finally:
        try:
            con.close()
        except Exception:  # noqa: BLE001  关连接失败不能盖掉真正的异常
            pass


def _row_to_cells(r: dict[str, Any], lang: str) -> list[str]:
    if lang == "en":
        return [
            r["board"], r["qualification"], r["subject"], r["unit"],
            r["paper_code"] or "", str(r["year"] or ""), r["session"] or "",
            r["question_number"], r["sub_label"],
            r["paper_id"], r["qid"], r["parent_qid"],
            SEP.join(r["tags_zh"]), SEP.join(r["tags_en"]),
            r["primary_tag_zh"], r["primary_tag_en"],
            r["difficulty"], r["status"],
            "1" if r["verified"] else "0", "1" if r["flagged"] else "0",
        ]
    return [
        BOARD_ZH.get(r["board"], r["board"]),
        QUAL_ZH.get(auth.level_of(r["qualification"]), r["qualification"]),
        SUBJECT_ZH.get(r["subject"], r["subject"]),
        r["unit"],
        r["paper_code"] or "", str(r["year"] or ""),
        SESSION_ZH.get(r["session"] or "", r["session"] or ""),
        r["question_number"], r["sub_label"],
        r["paper_id"], r["qid"], r["parent_qid"],
        SEP.join(r["tags_zh"]), SEP.join(r["tags_en"]),
        r["primary_tag_zh"], r["primary_tag_en"],
        DIFF_ZH.get(r["difficulty"], r["difficulty"]),
        STATUS_ZH.get(r["status"], r["status"]),
        "是" if r["verified"] else "否", "是" if r["flagged"] else "否",
    ]


def render_annotations_csv(rows: Iterator[dict[str, Any]], lang: str = "zh",
                           meta: str | None = None) -> Iterator[str]:
    """流式 CSV。首块带 BOM（Excel 打开中文不乱码），每 500 行吐一次。"""
    cols = COLUMNS_EN if lang == "en" else COLUMNS_ZH
    buf = io.StringIO()
    w = csv.writer(buf)
    if meta:
        w.writerow([meta])
    w.writerow(cols)
    n = 0
    for r in rows:
        w.writerow(_row_to_cells(r, lang))
        n += 1
        if n % 500 == 0:
            yield buf.getvalue()
            buf.seek(0)
            buf.truncate(0)
    yield buf.getvalue()


def render_annotations_jsonl(rows: Iterator[dict[str, Any]], filters: dict[str, Any]) -> Iterator[str]:
    import hashlib

    head = {"kind": "header", "schema": SCHEMA_ANNOT,
            "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "filters": filters}
    yield json.dumps(head, ensure_ascii=False) + "\n"
    h = hashlib.sha256()
    n = 0
    for r in rows:
        line = json.dumps({"kind": "row", **r}, ensure_ascii=False) + "\n"
        h.update(line.encode("utf-8"))
        n += 1
        yield line
    yield json.dumps({"kind": "footer", "rows": n, "sha256": h.hexdigest()},
                     ensure_ascii=False) + "\n"


# ---------------------------------------------------------------------------
# 知识点体系
# ---------------------------------------------------------------------------

def _tax_files() -> list[tuple[str, str, str, Path]]:
    """(board, qual_dir, subject, path)，跳过 _prev。"""
    out = []
    root = taxonomy.TAXONOMY_DIR
    if not root.is_dir():
        return out
    for board in sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith("_")):
        for qual in sorted(p for p in board.iterdir() if p.is_dir()):
            for f in sorted(qual.glob("*.json")):
                out.append((board.name, qual.name, f.stem, f))
    return out


def _usage_counts() -> dict[tuple[str, str, str, str, str], int]:
    """{(board, qual_dir, subject, 标签归属单元, tag_id): 题数}。一次扫 index.db。

    2026-11-18 修两个失配（此前 usage 大面积记 0）：
    1. **按标签归属单元计数，不按题目所在单元**。跨单元引用很普遍
       （Edexcel IG 物理 P1P2 卷引 P1/P2、CIE IG P24 卷引 P13、AQA U5 综合卷引 U1–U4），
       旧口径把计数记在题目单元名下，词表行按归属单元查不到 → 整单元归零。
    2. **学段键归一**。index.db 里 CIE A-Level 存 `alevel`，词表目录叫 `a-level`，
       字符串不等 → CIE A-Level 全 8 科 usage 全零。统一过 auth.level_of() 归到 ig/al。
    口径：「使用题数」= 全库有多少个题目单元用了这个标签，不管题在哪张卷。
    """
    # tag_id → 词表归属单元（键用归一学段；tag id 只在学科内唯一，键必须带学科）
    owner: dict[tuple[str, str, str, str], str] = {}
    for board, qual_dir, subject, path in _tax_files():
        lv = auth.level_of(qual_dir)
        data = json.loads(path.read_text(encoding="utf-8"))
        for unit, entry in data.items():
            if unit.startswith("$"):
                continue
            for t in entry.get("tags") or []:
                owner[(board, lv, subject, str(t.get("id")))] = unit
    con = _read_conn()
    out: dict[tuple[str, str, str, str, str], int] = {}
    for board, qual, subject, _unit, tags in con.execute(
        "SELECT p.board, p.qualification, p.subject, p.unit, q.tags "
        "FROM questions q JOIN papers p ON p.paper_id=q.paper_id "
        "WHERE q.tags IS NOT NULL AND q.tags NOT IN ('', '[]')"
    ):
        try:
            ids = json.loads(tags)
        except (ValueError, TypeError):
            continue
        lv = auth.level_of(qual)
        for t in ids:
            tid = str(t)
            tu = owner.get((board, lv, subject, tid))
            if tu is None:
                continue  # 引用了词表里已不存在的 id：无处可记，跳过
            k = (board, lv, subject, tu, tid)
            out[k] = out.get(k, 0) + 1
    con.close()
    return out


def collect_taxonomy(scope: list[tuple[str, str, str]] | None = None,
                     with_usage: bool = True) -> list[dict[str, Any]]:
    """扁平化词表：一行一个标签。scope=None 表示全部。"""
    want = None
    if scope is not None:
        want = {(b, auth.level_of(q), s) for b, q, s in scope}
    usage = _usage_counts() if with_usage else {}
    rows: list[dict[str, Any]] = []
    for board, qual_dir, subject, path in _tax_files():
        if want is not None and (board, auth.level_of(qual_dir), subject) not in want:
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        for unit, entry in data.items():
            if unit.startswith("$"):
                continue
            groups = {g.get("id"): g for g in (entry.get("groups") or [])}
            for t in entry.get("tags") or []:
                g = groups.get(t.get("group")) or {}
                rows.append({
                    "board": board, "qualification": qual_dir, "subject": subject,
                    "unit": unit,
                    "group_zh": (g.get("zh") or "").strip(),
                    "group_en": (g.get("en") or "").strip(),
                    "tag_zh": (t.get("zh") or "").strip(),
                    "tag_en": (t.get("en") or "").strip(),
                    "usage": usage.get((board, auth.level_of(qual_dir), subject, unit, str(t.get("id"))), 0),
                })
    return rows


def render_taxonomy_csv(rows: list[dict[str, Any]], lang: str = "zh") -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(TAX_COLUMNS_EN if lang == "en" else TAX_COLUMNS_ZH)
    for r in rows:
        if lang == "en":
            w.writerow([r["board"], r["qualification"], r["subject"], r["unit"],
                        r["group_zh"], r["group_en"], r["tag_zh"], r["tag_en"], r["usage"]])
        else:
            w.writerow([BOARD_ZH.get(r["board"], r["board"]),
                        QUAL_ZH.get(auth.level_of(r["qualification"]), r["qualification"]),
                        SUBJECT_ZH.get(r["subject"], r["subject"]), r["unit"],
                        r["group_zh"], r["group_en"], r["tag_zh"], r["tag_en"], r["usage"]])
    return "\ufeff" + buf.getvalue()


def build_taxonomy_json(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """嵌套结构；组与标签都只用中英文名做标识（不含 id）。"""
    data: dict[str, Any] = {}
    sig: dict[str, str] = {}
    for board, qual_dir, subject, path in _tax_files():
        sig[f"{board}/{qual_dir}/{subject}"] = taxonomy.file_sig(board, qual_dir, subject)
    for r in rows:
        level = auth.level_of(r["qualification"])
        node = (data.setdefault(BOARD_ZH.get(r["board"], r["board"]), {})
                    .setdefault(QUAL_ZH.get(level, level), {})
                    .setdefault(SUBJECT_ZH.get(r["subject"], r["subject"]), {}))
        unit = node.setdefault(r["unit"], {"groups": [], "tags": []})
        gname = r["group_zh"]
        if gname and gname not in [g["zh"] for g in unit["groups"]]:
            unit["groups"].append({"zh": gname, "en": r["group_en"]})
        unit["tags"].append({"zh": r["tag_zh"], "en": r["tag_en"], "group": gname})
    return {
        "schema": SCHEMA_TAX,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "sig": sig,
        "data": data,
    }


def write_taxonomy_zip(out: Path, rows: list[dict[str, Any]], label: str = "") -> int:
    """每学科一个 CSV + 一份 JSON + README。返回文件数。"""
    missing = _subjects_without_taxonomy()
    if label:
        missing = [m for m in missing if auth.level_of(m[1]) == auth.level_of(label)]
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("taxonomy.json", json.dumps(build_taxonomy_json(rows), ensure_ascii=False, indent=1))
        z.writestr("README.txt", _tax_readme(rows, missing, label))
        by_subject: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
        for r in rows:
            by_subject.setdefault((r["board"], r["qualification"], r["subject"]), []).append(r)
        for (board, qual_dir, subject), items in sorted(by_subject.items()):
            z.writestr(f"csv/{board}_{qual_dir}_{subject}.csv", render_taxonomy_csv(items))
    return len(by_subject)


def _subjects_without_taxonomy() -> list[tuple[str, str, str]]:
    have = {(b, auth.level_of(q), s) for b, q, s, _p in _tax_files()}
    out = set()
    for p in store.iter_papers():
        k = (p.board, auth.level_of(p.qualification), p.subject)
        if k not in have:
            out.add(k)
    return sorted(out)


def _tax_readme(rows: list[dict[str, Any]], missing: list[tuple[str, str, str]],
                label: str = "") -> str:
    lines = [
        "题库知识点体系导出",
        f"生成时间：{datetime.now().astimezone().isoformat(timespec='seconds')}",
        f"范围：{label or '全部学段'}",
        f"标签总数：{len(rows)}",
        "",
        "文件说明：",
        "  taxonomy.json   全量嵌套结构（含词表版本号 sig）",
        "  csv/*.csv       每学科一份长表，一行一个标签；用 Excel 打开时请直接用（含 UTF-8 BOM）",
        "",
        "列说明（csv）：",
        "  考试局 / 学段 / 学科 / 单元 / 组（中文）/ 组（英文）/ 标签（中文）/ 标签（英文）/ 使用题数",
        "  「使用题数」= 全库用了这个标签的题目单元数，含跨单元引用（一题挂 3 个标签，则 3 个标签各 +1）",
        "",
        "约定：",
        "  · 不含标签 id。标签以「学科 + 单元 + 中文名」定位；同一单元内中文名唯一。",
        "",
    ]
    if missing:
        lines.append(f"以下 {len(missing)} 个学科有试卷但尚无词表文件，导出中不会出现：")
        for b, lv, s in missing:
            lines.append(f"  - {BOARD_ZH.get(b, b)} / {QUAL_ZH.get(lv, lv)} / {SUBJECT_ZH.get(s, s)}")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse(argv: list[str]) -> dict[str, Any]:
    import argparse
    ap = argparse.ArgumentParser(description="题库导出（离线落盘，用于验证产物）")
    ap.add_argument("what", choices=["annotations", "taxonomy", "all"])
    ap.add_argument("--out", required=True)
    ap.add_argument("--board")
    ap.add_argument("--qual")
    ap.add_argument("--subject")
    ap.add_argument("--unit")
    ap.add_argument("--year", type=int)
    ap.add_argument("--session")
    ap.add_argument("--paper-id")
    ap.add_argument("--tag", action="append", default=[])
    ap.add_argument("--difficulty", action="append", default=[])
    ap.add_argument("--status", action="append", default=[])
    ap.add_argument("--only-tagged", action="store_true")
    ap.add_argument("--flagged", action="store_true", help="只导出被标「此题有误」的题")
    ap.add_argument("--format", choices=["csv", "jsonl", "json"], default="csv")
    ap.add_argument("--lang", choices=["zh", "en"], default="zh")
    ap.add_argument("--all-subjects", action="store_true")
    ap.add_argument("--level", choices=["ig", "al"], help="只导出某个学段（ig / al）")
    return vars(ap.parse_args(argv))


def main(argv: list[str]) -> int:
    import state
    state.init()          # ⚠️ 不 init 的话 confirmed/flagged 两个集合恒为空，看着像"库里没确认"

    a = _parse(argv)
    out_dir = Path(a["out"])
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d")
    f = Filters(board=a["board"], qual=a["qual"], subject=a["subject"], unit=a["unit"],
                year=a["year"], session=a["session"], paper_id=a["paper_id"],
                tags=a["tag"], difficulties=a["difficulty"], statuses=a["status"],
                only_tagged=a["only_tagged"], only_flagged=a["flagged"],
                lang=a["lang"], level=a["level"])
    scope = resolve_scope(None, f)
    print(f"范围：{len(scope)} 个学科组合", flush=True)

    t0 = time.perf_counter()
    if a["what"] in ("annotations", "all"):
        rows = iter_annotation_rows(scope, f)
        name_bits = [a["board"], a["subject"]] if a["board"] else []
        stem = "annotations_" + ("_".join(x for x in name_bits if x) + "_" if name_bits else "all_") + stamp
        if a["format"] == "jsonl":
            p = out_dir / f"{stem}.jsonl"
            with p.open("w", encoding="utf-8", newline="\n") as fh:
                for chunk in render_annotations_jsonl(rows, {"board": a["board"], "qual": a["qual"],
                                                             "subject": a["subject"]}):
                    fh.write(chunk)
        else:
            p = out_dir / f"{stem}.csv"
            with p.open("w", encoding="utf-8", newline="") as fh:
                for i, chunk in enumerate(render_annotations_csv(rows, a["lang"])):
                    fh.write(("\ufeff" if i == 0 else "") + chunk)
        print(f"annotations -> {p}  {p.stat().st_size/1048576:.2f} MB", flush=True)

    if a["what"] in ("taxonomy", "all"):
        level = a["level"]
        # 给了 --level 就按学段出 zip；--all-subjects 出全量 zip；否则按 scope 出单文件
        tax_scope = None if (a["all_subjects"] and not level) else scope
        rows = collect_taxonomy(tax_scope)
        if a["format"] == "json":
            p = out_dir / f"taxonomy_{level or 'all'}_{stamp}.json"
            p.write_text(json.dumps(build_taxonomy_json(rows), ensure_ascii=False, indent=1),
                         encoding="utf-8")
        elif a["all_subjects"] or level:
            suffix = level or "all"
            p = out_dir / f"taxonomy_{suffix}_{stamp}.zip"
            n = write_taxonomy_zip(p, rows, label=(QUAL_ZH.get(level, level) if level else ""))
            print(f"taxonomy zip -> {p}  {n} 个学科  {len(rows)} 个标签  "
                  f"{p.stat().st_size/1024:.0f} KB", flush=True)
        else:
            p = out_dir / f"taxonomy_{stamp}.csv"
            p.write_text(render_taxonomy_csv(rows, a["lang"]), encoding="utf-8")
        print(f"taxonomy -> {p}", flush=True)

    print(f"耗时 {time.perf_counter()-t0:.1f}s", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
