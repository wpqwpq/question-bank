"""知识点取题接口（`/api/kb/*`）—— 给老师/AI 按二级知识点取真题、做讲义用。

设计见 `docs/129-知识点取题接口设计-20261119.md`。三个端点：

  GET /api/kb/tags       该学科有哪些知识点、各挂多少题（含模糊搜）
  GET /api/kb/questions  某个知识点下的题目清单
  GET /api/kb/question   单题详情：整题全文 + 整题图

两条口径，与打标台/导出完全一致，不要另写一套：

1. **一行 = 一个标注单元**。切分题一行一个小问（父题不单独出行），未切分题一行整题。
   小问的标签在小问自己的 md 上，父题 tags 为空 —— 按父题聚合会把标签串掉。
2. **详情永远返回整题**。查小问时 `text` / `images` 都是父题的，`sub_label` 只表示
   「你要讲哪一小问」，`focus_box` 给出它在整图上的位置。因为 CIE 物理这类学科的子问
   没有独立正文、也没有独立子图（qNN-a.md 的 front-matter 之后是空的）。
"""
from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request

import auth
import index_db
import review
import store
import taxonomy

router = APIRouter(prefix="/api/kb", tags=["知识点取题"])

MAX_LIMIT = 300
MAX_PREVIEW = 160          # stem_preview 字符数上限
_SORTS = ("year_desc", "year_asc", "difficulty")
_INCLUDES = ("tagged", "verified", "all")
_DIFF_RANK = {"very-easy": 1, "easy": 2, "medium": 3, "hard": 4, "very-hard": 5}

_SEG_RE = re.compile(r"^[a-z][a-z0-9_-]*$")


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------

def _check_segments(board: str, qual: str, subject: str) -> str:
    """校验三段路径参数，返回数据目录里实际存在的 qualification 目录名。

    这三个值会拼进文件路径，必须挡目录穿越（`..`、`/`）。

    ⚠️ A-Level 的目录名两个考试局不一样：**CIE 是 `alevel`，AQA/Edexcel 是 `a-level`**。
    不能统一归一成某一个，必须按 board 探测 —— 否则 aqa+a-level 会 404。
    """
    for name, val in (("board", board), ("qual", qual), ("subject", subject)):
        if not val or not _SEG_RE.match(val) or ".." in val:
            raise HTTPException(400, f"{name} 参数不合法：{val!r}")
    key = str(qual).lower()
    if key in ("a-level", "alevel", "al", "as"):
        cands = ("a-level", "alevel")
    elif key in ("igcse", "ig"):
        cands = ("igcse",)
    elif key == "gcse":
        cands = ("gcse",)
    else:
        raise HTTPException(400, f"qual 只能是 igcse / gcse / a-level，收到 {qual!r}")

    base = store.DATA_DIR / board
    if not base.is_dir():
        raise HTTPException(404, f"没有这个考试局：{board}")
    qdir = next((c for c in cands if (base / c).is_dir()), None)
    if qdir is None:
        raise HTTPException(404, f"没有这个学段：{board}/{qual}")
    if not (base / qdir / subject).is_dir():
        raise HTTPException(404, f"没有这个学科：{board}/{qdir}/{subject}")
    return qdir


def _canonical_subject(subject: str) -> str:
    """旧 subject 写法 → 当前目录名（编码治理兼容层）。

    2026-11-20 治理把 `combined_science` 改成了 `combined-science`，但已经发出去的
    讲义包（design/edexcel-igcse-notes-kit.rar）里 subject 枚举还写着旧值，
    老师那边直接 404。这里统一兜住：下划线一律换成连字符。
    """
    return (subject or "").strip().replace("_", "-").lower()


# 正文里的非题干内容：题图引用、渲染脚本加的固定注释。
# Edexcel 部分卷不做文本转写（正文只有这三行），不过滤的话 stem_preview 会返回
# 「![题目/题组 9](assets/q09.webp) 原卷页码：22, 23, 24」这种对老师毫无用处的东西。
_MD_IMG_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_BOILER_RE = re.compile(
    r"原卷页码[:：][^。]*。|完整坐标见[^。]*。|本交付以原卷题图为准[^。]*。"
)


def _clean(text: str) -> str:
    """把 md 正文压成单行：去掉题图引用与固定注释、控制字符换空格、合并连续空白。

    题库正文偶有 \\x07 / \\x1b（PDF 抽取残留），不清掉会把 JSON 弄脏。
    `" ".join(text.split())` 比 `re.sub(r"\\s+", " ", ...)` 快好几倍 —— 正文动辄上万字，
    一页 100 条时这个差别是几十毫秒。
    """
    if "![" in text:
        text = _MD_IMG_RE.sub(" ", text)
    if "原卷页码" in text or "完整坐标见" in text or "本交付以原卷题图为准" in text:
        text = _BOILER_RE.sub(" ", text)
    if any(ord(c) < 32 for c in text):
        text = "".join(" " if ord(c) < 32 else c for c in text)
    return " ".join(text.split())


def _body_of(path: Path) -> str:
    """md 文件正文（front-matter 之后的部分）。

    只切边界，**不走 `store.parse_front_matter`** —— 那个会做完整 YAML 解析（约 2.5ms/次），
    而取正文只需要知道 `---` 在哪结束。一页 100 条时这是 250ms 与 2ms 的差别。
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    m = store.FM_RE.match(text)
    return text[m.end():] if m else text


def _clip(text: str) -> str:
    text = _clean(text)
    return text[:MAX_PREVIEW] + ("…" if len(text) > MAX_PREVIEW else "")


def _preview_from_parent(parent_body: str, label: str) -> str:
    """子问没有独立正文时，从整题正文里定位到该小问，往后截一段。"""
    body = _clean(parent_body)
    if not body:
        return ""
    key = (label or "").split()[-1] if label else ""
    if key:
        m = re.search(rf"\(\s*{re.escape(key)}\s*\)", body)
        if m:
            body = body[m.start():]
    return _clip(body)


def _rows_for_scope(board: str, qual: str, subject: str,
                    tag_ids: list[str] | None = None) -> list[dict[str, Any]]:
    """该学科全部题行（含子题行）；给了 `tag_ids` 就只取挂了这些标签的行。

    标签过滤放在 SQL 里做：全学科 5700 行 vs 命中 162 行，差 30ms。
    index.db 不存在时回退逐卷扫 md（本地开发/索引被删时用），代价是慢。
    """
    if index_db.exists():
        sql, params = _ROW_SQL, [board, qual, subject]
        if tag_ids:
            sql += " AND (" + " OR ".join("q.tags LIKE ?" for _ in tag_ids) + ")"
            params += [f'%"{t}"%' for t in tag_ids]
        con = sqlite3.connect(f"file:{index_db.DB_PATH}?mode=ro", uri=True, timeout=15)
        con.execute("PRAGMA busy_timeout=8000")
        try:
            cur = con.execute(sql, params)
            cols = [c[0] for c in cur.description]
            out = []
            for r in cur:
                d = dict(zip(cols, r))
                try:
                    d["tags"] = json.loads(d["tags"] or "[]")
                except (ValueError, TypeError):
                    d["tags"] = []
                out.append(d)
            return out
        finally:
            con.close()

    want = set(tag_ids) if tag_ids else None
    out = []
    for p in store.iter_papers():
        if (p.board, p.qualification, p.subject) != (board, qual, subject):
            continue
        d = str(p.path.relative_to(store.DATA_DIR)).replace("\\", "/")
        for f in sorted(p.path.glob("q*.md")):
            m = store.read_meta(f)
            if not m or not m.get("id"):
                continue
            tags = m.get("tags") or []
            if not isinstance(tags, list):
                tags = [tags]
            if want is not None and not want & {str(t) for t in tags}:
                continue
            out.append({
                "qid": str(m["id"]),
                "parent_qid": str(m["parent"]) if m.get("parent") else None,
                "qnum": str(m.get("question_number") or f.stem),
                "sub_label": str(m.get("sub_label") or "") or None,
                "split": 1 if (m.get("split") or m.get("parent")) else 0,
                "total_marks": m.get("total_marks"),
                "question_type": m.get("question_type"),
                "status": store.compute_status(m),
                "difficulty": m.get("difficulty"),
                "primary_tag": store.effective_primary(tags, m.get("primary_tag")),
                "tags": tags,
                "image": m.get("image"),
                "file_name": f.name,
                "paper_id": p.id,
                "unit": p.unit(),
                "paper_code": m.get("paper_code"),
                "year": m.get("year"),
                "session": m.get("session"),
                "dir_rel": d,
            })
    return out


_ROW_SQL = (
    "SELECT q.qid, q.parent_qid, q.qnum, q.sub_label, q.split, q.total_marks,"
    " q.question_type, q.status, q.difficulty, q.primary_tag, q.tags, q.image,"
    " q.file_name, p.paper_id, p.unit, p.paper_code, p.year, p.session, p.dir_rel"
    " FROM questions q JOIN papers p ON q.paper_id = p.paper_id"
    " WHERE p.board=? AND p.qualification=? AND p.subject=?"
)


def _tag_index(tax: dict) -> dict[str, dict[str, Any]]:
    """tag_id → {zh, en, unit, group}。跨单元重名靠 unit 区分，id 本身不唯一。"""
    idx: dict[str, dict[str, Any]] = {}
    for unit, entry in tax.items():
        if unit.startswith("$") or not isinstance(entry, dict):
            continue
        groups = {g.get("id"): g.get("zh") for g in entry.get("groups") or []}
        for t in entry.get("tags") or []:
            tid = t.get("id")
            if not tid:
                continue
            idx[tid] = {"zh": t.get("zh") or "", "en": t.get("en") or "",
                        "unit": unit, "group": groups.get(t.get("group"), "")}
    return idx


# ---------------------------------------------------------------------------
# 标签名解析（归一化 → 精确 → 组名 → 子串 → 2-gram 相似度）
# ---------------------------------------------------------------------------

_PUNCT = " \t\r\n-_·、,，。.()（）[]【】/／:：;；'\"“”‘’"


def _norm(s: str) -> str:
    """统一到可比较形式：全半角、各种破折号、空格、标点、大小写。"""
    s = unicodedata.normalize("NFKC", str(s or ""))
    for c in "\u2013\u2014\u2010\u2011\u2212":      # – — ‐ ‑ −
        s = s.replace(c, "-")
    s = "".join("" if c in _PUNCT else c for c in s)
    return s.lower()


def _dice(a: str, b: str) -> float:
    """字符 bigram 的 Dice 系数。短中文标签上比编辑距离稳。"""
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    A = {a[i:i + 2] for i in range(len(a) - 1)} or {a}
    B = {b[i:i + 2] for i in range(len(b) - 1)} or {b}
    return 2 * len(A & B) / (len(A) + len(B))


def _resolve(tax: dict, text: str) -> dict[str, Any]:
    """把一个可能是「不准的名字」解析成标签。返回匹配结果与候选，不抛异常。

    level: exact / group / contains / fuzzy / none
    组名放在子串之前 —— 老师说的「测量与运动」是组名，但它跟组内某个标签可能
    有 0.3 左右的字面相似度，先判组名才不会误落到那个标签上。
    """
    tidx = _tag_index(tax)
    q = _norm(text)
    out: dict[str, Any] = {"input": text, "level": "none", "score": 0.0,
                           "matched": None, "candidates": []}
    if not tidx or not q:
        return out

    def _hit(tid: str) -> dict[str, Any]:
        return {"id": tid, **tidx[tid]}

    # L1 归一化后精确
    exact = [tid for tid, t in tidx.items() if _norm(t["zh"]) == q or _norm(t["en"]) == q]
    if len(exact) == 1:
        out.update(level="exact", score=1.0, matched=_hit(exact[0]))
        return out

    # L2 组名 / 单元名精确命中 → 返回该组全部标签做候选，不猜某一个
    for unit, entry in tax.items():
        if unit.startswith("$") or not isinstance(entry, dict):
            continue
        if _norm(unit) == q:
            out.update(level="group", score=1.0, candidates=[
                {"id": t["id"], "zh": t.get("zh"), "en": t.get("en"),
                 "unit": unit, "group": "", "score": 1.0}
                for t in entry.get("tags") or []])
            return out
        for g in entry.get("groups") or []:
            if _norm(g.get("zh")) == q or _norm(g.get("en")) == q:
                out.update(level="group", score=1.0, candidates=[
                    {"id": t["id"], "zh": t.get("zh"), "en": t.get("en"),
                     "unit": unit, "group": g.get("zh"), "score": 1.0}
                    for t in entry.get("tags") or [] if t.get("group") == g.get("id")])
                return out

    # L3 双向子串
    subs = [tid for tid, t in tidx.items()
            if q in _norm(t["zh"]) or _norm(t["zh"]) in q
            or (t["en"] and (q in _norm(t["en"]) or _norm(t["en"]) in q))]
    if len(subs) == 1:
        out.update(level="contains", score=0.95, matched=_hit(subs[0]))
        return out

    # L4 相似度 top 5
    scored = []
    for tid, t in tidx.items():
        s = _dice(q, _norm(t["zh"]))
        if t["en"]:
            s = max(s, _dice(q, _norm(t["en"])))
        if s >= 0.35:
            scored.append((s, tid))
    scored.sort(reverse=True)
    cands = [{"id": tid, "zh": tidx[tid]["zh"], "en": tidx[tid]["en"],
              "unit": tidx[tid]["unit"], "group": tidx[tid]["group"],
              "score": round(s, 3)} for s, tid in scored[:5]]
    for tid in subs:                       # 子串命中多个时也列出来
        if all(c["id"] != tid for c in cands):
            cands.append({"id": tid, "zh": tidx[tid]["zh"], "en": tidx[tid]["en"],
                          "unit": tidx[tid]["unit"], "group": tidx[tid]["group"],
                          "score": 0.9})
    if cands:
        out["candidates"] = cands
        best = cands[0]
        if best["score"] >= 0.45:
            out.update(level="fuzzy", score=best["score"], matched=_hit(best["id"]))
        return out

    return out


def _resolve_ids(tax: dict, tag: str | None, tag_id: str | None,
                 unit: str | None) -> tuple[list[str], dict[str, Any]]:
    """tag / tag_id / unit → 一组 tag_id + resolution 说明。多值逗号分隔（OR）。"""
    tidx = _tag_index(tax)

    if tag_id:
        ids = [x.strip() for x in tag_id.split(",") if x.strip()]
        bad = [i for i in ids if i not in tidx]
        if bad:
            raise HTTPException(404, {"detail": "tag_id_not_found",
                                      "hint": "用 /api/kb/tags 查准确 id",
                                      "unknown": bad})
        return ids, {"input": tag_id, "level": "exact", "score": 1.0,
                     "matched": {"id": ids[0], **tidx[ids[0]]}, "candidates": []}

    if not tag:
        raise HTTPException(400, "缺 tag 参数（标签中文名，可用逗号分隔多个）")

    ids: list[str] = []
    resolutions: list[dict[str, Any]] = []
    for one in [x.strip() for x in tag.split(",") if x.strip()]:
        r = _resolve(tax, one)
        if not r["matched"]:
            hint = (f"标签名没匹配上。「{one}」可能不是词表里的标签名 —— "
                    f"先 GET /api/kb/tags?board=&qual=&subject=（不带 q）拿全部 "
                    f"{len(tidx)} 条标签自己挑，再用 tag_id 查；带 q=… 只是省一次阅读，"
                    f"解决不了语义差异")
            payload: dict[str, Any] = {"detail": "tag_not_found", "hint": hint}
            if r["candidates"]:
                payload["candidates"] = r["candidates"]
                payload["hint"] = hint + "；或直接从 candidates 里挑一个 id 用 tag_id 查"
            raise HTTPException(404, payload)

        zh = _norm(r["matched"]["zh"])
        same = [t for t, v in tidx.items() if _norm(v["zh"]) == zh]
        if unit:
            scoped = [t for t in same if tidx[t]["unit"] == unit]
            if not scoped:
                raise HTTPException(404, {
                    "detail": "tag_not_in_unit",
                    "hint": f"「{r['matched']['zh']}」不在单元 {unit} 下",
                    "candidates": [{"unit": tidx[t]["unit"], "id": t, "zh": tidx[t]["zh"]}
                                   for t in same]})
            same = scoped
        if len(same) > 1 and not unit:
            raise HTTPException(409, {
                "detail": "ambiguous",
                "hint": "这个中文名在本学科存在于多个单元，请补 unit 参数",
                "candidates": [{"unit": tidx[t]["unit"], "id": t, "zh": tidx[t]["zh"]}
                               for t in same]})
        ids.append(same[0] if same else r["matched"]["id"])
        resolutions.append(r)

    head = resolutions[0]
    res: dict[str, Any] = {"input": tag, "level": head["level"], "score": head["score"],
                           "matched": head["matched"], "candidates": head["candidates"]}
    if len(resolutions) > 1:
        res["matched_all"] = [x["matched"] for x in resolutions if x["matched"]]
    if any(x["score"] < 0.75 for x in resolutions):
        res["warning"] = "标签名不确定，请复核 candidates"
    return list(dict.fromkeys(ids)), res


# ---------------------------------------------------------------------------
# 端点 1：标签清单
# ---------------------------------------------------------------------------

@router.get("/tags", summary="列出某学科的知识点标签及题量")
def list_tags(request: Request, board: str, qual: str, subject: str,
              unit: str | None = None, q: str | None = None):
    """`board`/`qual`/`subject` 必填；`unit` 收窄到单元；`q` 按中英文模糊搜。

    `count` 是该标签下的标注单元数。`total_tags` 是词表条数，`used_tags` 是实际有题的
    条数 —— 差的那几条点进去会是空的，先说清楚。
    """
    subject = _canonical_subject(subject)
    qdir = _check_segments(board, qual, subject)
    auth.require_scope(request, subject, qdir)
    _assert_not_building()

    tax = taxonomy.load_taxonomy(board, qdir, subject)
    if not tax:
        raise HTTPException(404, {
            "detail": "no_taxonomy",
            "hint": f"{board}/{qdir}/{subject} 还没有知识点词表，这个学科无法按标签取题",
        })

    counts = _tag_counts(board, qdir, subject)
    items: list[dict[str, Any]] = []
    for unit_key, entry in tax.items():
        if unit_key.startswith("$") or not isinstance(entry, dict):
            continue
        if unit and unit_key != unit:
            continue
        groups = {g.get("id"): g.get("zh") for g in entry.get("groups") or []}
        for t in entry.get("tags") or []:
            tid = t.get("id")
            if not tid:
                continue
            items.append({
                "zh": t.get("zh"), "en": t.get("en"), "id": tid,
                "unit": unit_key, "group": groups.get(t.get("group"), ""),
                "count": counts.get(tid, 0),
            })

    total_tags = len(items)
    # used_tags 在模糊过滤前算：它表示「这个学科有多少标签挂了题」，与 q 无关
    used = sum(1 for it in items if it["count"])
    if q:
        scored = []
        for it in items:
            s = _dice(_norm(q), _norm(it["zh"]))
            if it["en"]:
                s = max(s, _dice(_norm(q), _norm(it["en"])))
            if _norm(q) in _norm(it["zh"]) or _norm(it["zh"]) in _norm(q):
                s = max(s, 0.9)
            if s >= 0.3:
                scored.append((s, it))
        scored.sort(key=lambda x: (-x[0], -(x[1]["count"] or 0)))
        items = [dict(it, score=round(s, 3)) for s, it in scored]

    return {
        "scope": {"board": board, "qual": qdir, "subject": subject},
        "total_tags": total_tags,
        "used_tags": used,
        "items": items,
    }


def _tag_counts(board: str, qual: str, subject: str) -> dict[str, int]:
    """tag_id → 标注单元数。一次遍历该学科题行，Python 里计数。"""
    out: dict[str, int] = {}
    for row in _rows_for_scope(board, qual, subject):
        for tid in row.get("tags") or []:
            out[str(tid)] = out.get(str(tid), 0) + 1
    return out


# ---------------------------------------------------------------------------
# 端点 2：题目清单
# ---------------------------------------------------------------------------

@router.get("/questions", summary="按知识点标签查询题目清单")
def list_questions(request: Request, board: str, qual: str, subject: str,
                   tag: str | None = None, tag_id: str | None = None,
                   unit: str | None = None,
                   difficulty: str | None = None,
                   year_from: int | None = None, year_to: int | None = None,
                   include: str = "tagged", sort: str = "year_desc",
                   limit: int = 100, offset: int = 0):
    """一行 = 一个标注单元（切分题一行一个小问）。

    `tag` 用中文名，可逗号分隔多值（OR）；名字不准就用 `tag_id`（先用 `/api/kb/tags`
    查一次）。`include` 取值 tagged / verified / all。`limit` 上限 300。
    """
    subject = _canonical_subject(subject)
    qdir = _check_segments(board, qual, subject)
    auth.require_scope(request, subject, qdir)
    _assert_not_building()

    if sort not in _SORTS:
        raise HTTPException(422, f"sort 只能是 {' / '.join(_SORTS)}")
    if include not in _INCLUDES:
        raise HTTPException(422, f"include 只能是 {' / '.join(_INCLUDES)}")
    if limit < 1 or limit > MAX_LIMIT:
        raise HTTPException(422, f"limit 必须在 1–{MAX_LIMIT} 之间")
    if offset < 0:
        raise HTTPException(422, "offset 不能为负")

    tax = taxonomy.load_taxonomy(board, qdir, subject)
    if not tax:
        raise HTTPException(404, {"detail": "no_taxonomy",
                                  "hint": f"{board}/{qdir}/{subject} 还没有知识点词表"})

    ids, resolution = _resolve_ids(tax, tag, tag_id, unit)
    wanted = set(ids)
    tidx = _tag_index(tax)
    diffs = {x.strip() for x in (difficulty or "").split(",") if x.strip()}
    confirmed = review.confirmed_ids() if include == "verified" else set()

    rows = []
    for row in _rows_for_scope(board, qdir, subject, ids):
        tags = [str(t) for t in (row.get("tags") or [])]
        if not wanted & set(tags):
            continue
        if diffs and (row.get("difficulty") or "") not in diffs:
            continue
        if year_from and (row.get("year") or 0) < year_from:
            continue
        if year_to and (row.get("year") or 0) > year_to:
            continue
        if include == "tagged" and not tags:
            continue
        if include == "verified" and (row.get("parent_qid") or row["qid"]) not in confirmed:
            continue
        rows.append(row)

    def _key(r):
        y = r.get("year") or 0
        qn = str(r.get("qnum") or "")
        head = qn.split(".")[0]
        num = int(head) if head.isdigit() else 0
        pid = str(r.get("paper_id") or "")
        label = str(r.get("sub_label") or "")
        if sort == "year_desc":
            return (-y, pid, num, label)
        if sort == "year_asc":
            return (y, pid, num, label)
        return (_DIFF_RANK.get(r.get("difficulty") or "", 9), -y, pid, num, label)

    rows.sort(key=_key)
    total = len(rows)
    page = rows[offset:offset + limit]

    by_qid = {r["qid"]: r for r in rows}
    items = []
    for r in page:
        label = r.get("sub_label") or ""
        path = store.DATA_DIR / (r.get("dir_rel") or "") / (r.get("file_name") or "")
        preview = _clip(_body_of(path))
        if not preview:
            parent = by_qid.get(r.get("parent_qid") or "")
            if parent is None and r.get("parent_qid"):
                pf = store.find_question(r["parent_qid"])
                if pf is not None:
                    preview = _preview_from_parent(_body_of(pf[1]), label)
            elif parent is not None:
                ppath = (store.DATA_DIR / (parent.get("dir_rel") or "")
                         / (parent.get("file_name") or ""))
                preview = _preview_from_parent(_body_of(ppath), label)
        tags = [str(t) for t in (r.get("tags") or [])]
        items.append({
            "qid": r["qid"],
            "parent_qid": r.get("parent_qid") or "",
            "paper_id": r.get("paper_id"),
            "paper_code": r.get("paper_code"),
            "year": r.get("year"),
            "session": r.get("session"),
            "question_number": str(r.get("qnum") or "").split(".")[0],
            "sub_label": label,
            "marks": r.get("total_marks"),
            "difficulty": r.get("difficulty") or "",
            "tags_zh": [tidx.get(t, {}).get("zh", t) for t in tags],
            "primary_tag_zh": tidx.get(str(r.get("primary_tag") or ""), {}).get("zh", ""),
            "stem_preview": preview,
            "has_text": bool(preview),
            "detail": f"/api/kb/question?qid={r['qid']}",
        })

    return {
        "tag": resolution.get("matched"),
        "resolution": resolution,
        "scope": {"board": board, "qual": qdir, "subject": subject},
        "total": total,
        "returned": len(items),
        "offset": offset,
        "index_gen": index_db.generation(),
        "items": items,
    }


# ---------------------------------------------------------------------------
# 端点 3：单题详情
# ---------------------------------------------------------------------------

@router.get("/question", summary="按 qid 取单题的整题全文与整题图")
def get_question(request: Request, qid: str):
    """**返回的是整题**：`text` 是父题全文，`images` 是父题整图。

    查小问时 `sub_label` 告诉你查的是哪一问，`focus_box` 给出它在整图上的位置
    （归一化 0–1，只用于定位，不要拿去裁剪）。
    """
    _assert_not_building()
    found = store.find_question(qid)
    if found is None:
        raise HTTPException(404, {
            "detail": "not found",
            "hint": "qid 不对。先用 /api/kb/questions 拿清单，里面的 qid 可直接用",
        })
    paper, qpath = found
    auth.require_scope(request, paper.subject, paper.qualification)

    meta = store.read_meta(qpath)
    parent_qid = meta.get("parent")
    parent_path, parent_meta = qpath, meta
    if parent_qid:
        pf = store.find_question(parent_qid)
        if pf is not None:
            parent_path, parent_meta = pf[1], store.read_meta(pf[1])

    qual = paper.qualification
    tax = taxonomy.load_taxonomy(paper.board, qual, paper.subject)
    tidx = _tag_index(tax)

    tags = meta.get("tags") or []
    if not isinstance(tags, list):
        tags = [tags]

    paper_dir = parent_path.parent.relative_to(store.DATA_DIR).as_posix()
    images = []
    img = parent_meta.get("image")
    if img:
        images.append({"role": "question",
                       "url": f"/files/{paper_dir}/{str(img).lstrip('/')}"})

    label = meta.get("sub_label") or ""
    confirmed = review.confirmed_ids()
    whole_text = _body_of(parent_path).strip("\n")
    # 调用方可能用旧 id 查（走 store 的兼容层），这里一律回当前 id，
    # 否则它会一直拿着旧 id 继续请求，永远命中兼容层。
    canonical = str(meta.get("id") or qid)
    return {
        "qid": canonical,
        "parent_qid": parent_qid or "",
        "scope": "whole_question",
        "paper": {
            "paper_id": paper.id,
            "board": paper.board, "qual": qual, "subject": paper.subject,
            "paper_code": meta.get("paper_code") or parent_meta.get("paper_code"),
            "year": meta.get("year") or parent_meta.get("year"),
            "session": meta.get("session") or parent_meta.get("session"),
            "question_number": str(parent_meta.get("question_number")
                                   or parent_path.stem).split(".")[0],
            "source": parent_meta.get("source") or "",
        },
        "sub_label": label,
        "marks": meta.get("total_marks"),
        "difficulty": meta.get("difficulty") or "",
        "tags_zh": [tidx.get(str(t), {}).get("zh", str(t)) for t in tags],
        "primary_tag_zh": tidx.get(
            str(store.effective_primary(tags, meta.get("primary_tag")) or ""), {}).get("zh", ""),
        "status": store.compute_status(meta),
        "verified": canonical in confirmed or (bool(parent_qid) and parent_qid in confirmed),
        "text": whole_text,
        "has_text": bool(_clean(whole_text)),
        "images": images,
        "focus_box": _focus_box(parent_path, parent_meta, label),
        "siblings": _siblings(parent_path, paper, tidx),
    }


def _focus_box(parent_path: Path, parent_meta: dict, label: str) -> dict[str, Any] | None:
    """该小问在整图上的位置（layout.json 的 subquestions[].box，归一化 0–1）。"""
    if not label:
        return None
    rel = parent_meta.get("layout") or f"assets/{parent_path.stem}.layout.json"
    try:
        data = json.loads((parent_path.parent / rel).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    want = {label, " ".join(label.split()), "-".join(label.split())}
    for s in data.get("subquestions") or []:
        lab = str(s.get("label") or "")
        if lab in want:
            box = s.get("box") or {}
            return {"label": lab, **{k: box.get(k) for k in ("x", "y", "w", "h")}}
    return None


def _siblings(parent_path: Path, paper: store.Paper, tidx: dict) -> list[dict[str, Any]]:
    """同题的其他小问（只给 label / qid / 标签，供 AI 判断上下文）。"""
    out = []
    for sf in paper.sub_files(parent_path):
        m = store.read_meta(sf)
        if not m:
            continue
        out.append({
            "label": m.get("sub_label") or sf.stem,
            "qid": m.get("id"),
            "tags_zh": [tidx.get(str(t), {}).get("zh", str(t))
                        for t in (m.get("tags") or [])],
        })
    return out


def _assert_not_building() -> None:
    """索引重建期间返回 503 而不是空列表 —— 否则看起来像「题库没题」。"""
    if index_db.is_building():
        raise HTTPException(503, "索引重建中，约 30 秒后重试",
                            headers={"Retry-After": "30"})
