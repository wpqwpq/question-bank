"""API 路由。"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

import auth
import flags
import index_db
import state
import store
import taxonomy
import review

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))
import render_subsplit  # noqa: E402  依赖与后端同一 venv（pymupdf/PIL）

router = APIRouter(prefix="/api")

VALID_DIFFICULTIES = {"very-easy", "easy", "medium", "hard", "very-hard"}


class SubAnnotation(BaseModel):
    label: str
    tags: list[str]
    difficulty: str | None = None  # 可留空（docs/13）
    # 主标签（plans/主标签实施方案 §2）：多标签必填且必须 ∈ tags；单标签可留空由后端补全
    primary_tag: str | None = None


class AnnotationPayload(BaseModel):
    tags: list[str] | None = None
    difficulty: str | None = None  # 可留空（docs/13）
    primary_tag: str | None = None  # 同上
    subs: list[SubAnnotation] | None = None  # 非空 = 按小问切分保存


class ConfirmPayload(BaseModel):
    confirmed: bool
    # 预览模式就地补/改难度（docs/13）：给了就先写库再记确认；None = 旧行为（只记确认）
    difficulty: str | None = None


class FlagPayload(BaseModel):
    """「此题有误」标记。note 可选，留给以后做备注。"""
    flagged: bool
    note: str | None = None


# ---------------------------------------------------------------------------
# 审核确认（docs/11）：确认单元 = 非切分题的主题 qid / 切分题的各小问 id。
# 聚合逻辑留在本层，store 不依赖 review（避免循环依赖）。
# ---------------------------------------------------------------------------

def _invalidate_confirm(units: list[str], unchanged: set[str]) -> list[str]:
    """保存标注后，把**内容确有变化**的确认单元的勾选撤掉。

    docs/12 §③：「确认必须能失效」——AI/人工改了标签或难度后，原来的「我已核对」
    就在说谎。这里只撤销真正变了的单元（`unchanged` 里的是内容未变、跳过），
    避免重复保存把用户的确认白白清掉。返回被撤掉的单元 id 列表。
    """
    cleared: list[str] = []
    for uid in dict.fromkeys(units):  # 去重且保序
        if not uid or uid in unchanged:
            continue
        if review.is_confirmed(uid):
            review.set_confirmed(uid, False)
            cleared.append(uid)
    return cleared


def _units_of(qid: str, split: bool, sub_ids: list[str]) -> list[str]:
    """一个主题题目包含的确认单元 id 列表。

    切分题用小问文件的**真实 id**（Edexcel 旧格式为 `{qid}{label}` 如 `...-q15a`，
    新格式/其它考试局为 `{qid}-{label}`），不能用 label 拼——见 store.Paper.questions()。
    """
    if split:
        return list(sub_ids)
    return [qid]


def _verify_state(units: list[str], conf: set[str]) -> tuple[str, int, int]:
    """(verify_state, 已确认单元数, 单元总数)。all / partial / none。"""
    total = len(units)
    done = sum(1 for u in units if u in conf)
    state = "all" if total and done == total else "partial" if done else "none"
    return state, done, total


def _labels_for(paper: store.Paper, cut_q: dict) -> list[str]:
    """本题的「小问单元」列表。

    默认是顶层字母（a/b/c）——物理/化学/AQA/CIE 都走这条；
    会计走 leaf 粒度：把 (a)(i)(ii) 展平成 "a i"/"a ii"，有罗马子项的字母本身不再成单元。
    粒度开关在 store.sub_label_mode（按学科）。
    """
    if store.sub_label_mode(paper.board, paper.subject) == "leaf":
        return render_subsplit.leaf_labels(cut_q)
    return render_subsplit.top_labels(cut_q)


def _validate_tags(paper: store.Paper, tags: list[str], where: str) -> None:
    if not tags:
        raise HTTPException(422, f"{where}: 至少勾选 1 个知识点标签")
    if len(set(tags)) != len(tags):
        raise HTTPException(422, f"{where}: 标签存在重复")
    allowed = taxonomy.tag_ids_for_units(paper.board, paper.qualification, paper.subject, paper.unit())
    illegal = [t for t in tags if t not in allowed]
    if illegal:
        raise HTTPException(422, f"{where}: 标签不属于本卷可选单元 {paper.unit()}: {', '.join(illegal)}")


def _validate_primary(tags: list[str], primary: str | None, where: str) -> str | None:
    """主标签唯一真源规则（plans/主标签实施方案 §2.1）。返回归一化后的主标签。

    - tags 为空：不写该键（调用方已先拦「至少 1 个标签」，这里只兜底）；
    - 恰好 1 个：后端自动补成该 id，前端可不传（决策 #1：显式落盘，下游零推导）；
    - ≥2 个：必须显式提供且必须 ∈ tags，否则 422 拒收（校验在写盘前，不落盘）。
    """
    if not tags:
        return None
    if len(tags) == 1:
        return tags[0]
    if not primary:
        raise HTTPException(422, f"{where}: 多标签题必须指定主标签")
    if primary not in tags:
        raise HTTPException(422, f"{where}: 主标签必须是已选标签之一")
    return primary


@router.get("/taxonomy")
def get_taxonomy(request: Request):
    """词表清单：data/taxonomy/<board>/<qual>/<subject>.json 的目录树。

    严格按学科隔离：taggers 只看到自己学科的词表文件（admin/service 看全部）。
    """
    ident = auth.current(request)
    root = taxonomy.TAXONOMY_DIR
    tree: dict[str, dict] = {}
    if root.is_dir():
        for board in sorted(p for p in root.iterdir() if p.is_dir() and p.name != "_prev"):
            for qual in sorted(p for p in board.iterdir() if p.is_dir()):
                for f in sorted(qual.glob("*.json")):
                    if not ident.can(f.stem):
                        continue
                    tree.setdefault(board.name, {}).setdefault(qual.name, []).append(f.stem)
    return tree


@router.get("/taxonomy/{board}/{qual}/{subject}")
def get_taxonomy_file(request: Request, board: str, qual: str, subject: str):
    """整份词表文件（标签管理页要看全部单元，而不只是某一卷的单元）。"""
    auth.require_subject(request, subject)
    path = taxonomy.taxonomy_path(board, qual, subject)
    if not path.exists():
        raise HTTPException(404, f"词表不存在：{board}/{qual}/{subject}")
    root = taxonomy.TAXONOMY_DIR.parents[1]
    return {
        "board": board,
        "qual": qual,
        "subject": subject,
        "path": path.relative_to(root).as_posix(),
        "data": taxonomy.load_taxonomy(board, qual, subject),
        # 乐观锁版本号：保存时原样回传 base_sig，中途有人存过就 409
        "sig": taxonomy.file_sig(board, qual, subject),
    }


@router.put("/taxonomy/{board}/{qual}/{subject}")
def put_taxonomy_file(request: Request, board: str, qual: str, subject: str, payload: dict):
    """整体覆盖写词表：先备份到 _prev/ 再原子替换。

    校验不过整单拒绝（422，detail 为逐条错误、换行分隔）；成功则顺手清缓存，
    打标台右栏下一次拉详情就能看到新标签。
    权限：admin / lead / tagger 都能改，但只能改**自己学科**的词表（跨科 404，
    隔离在 require_subject 里）。viewer 只读、service（AI）不能改词表。
    并发：payload 带 `base_sig`（GET 时拿到的版本号），与磁盘不一致 → 409，
    响应头 X-QB-Current-Sig 给出当前版本号。不带 base_sig 时不校验（脚本兼容）。
    """
    auth.require_taxonomy(request)
    ident = auth.require_subject(request, subject)
    data = payload.get("data", payload) if isinstance(payload, dict) else payload
    base_sig = payload.get("base_sig") if isinstance(payload, dict) else None
    try:
        res = taxonomy.save_taxonomy(board, qual, subject, data, expect_sig=base_sig)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    except taxonomy.TaxonomyConflict as e:
        # 冲突也是信号：说明同科真的有人在同时改词表，值得留痕（不记内容，只记版本）
        auth.audit(ident, "taxonomy.conflict", f"{board}/{qual}/{subject}",
                   {"base_sig": base_sig, "current_sig": e.current}, request)
        raise HTTPException(
            409,
            "这份词表刚被其他账号保存过，你手里的版本已过期。请重新加载后再改（未保存的修改会丢）。",
            headers={"X-QB-Current-Sig": e.current},
        )
    except ValueError as e:
        raise HTTPException(422, str(e))
    auth.audit(ident, "taxonomy.save", f"{board}/{qual}/{subject}",
               {"units": res.get("units"), "tags": res.get("tags"),
                "backup": res.get("backup")}, request)
    return res


def paper_progress(paper: store.Paper, conf: set[str], flagged: set[str],
                   question_count: int | None = None) -> dict:
    """一卷的审核进度——列表页的对钩与全局概览**共用这一处判定**。

    口径（docs/11、docs/12）：
      · 确认单元 = 非切分题的主题 qid / 切分题的各小问 id；
      · 标了「有误」的题**退出核对待办**：既不算待确认，也不算已确认，
        所以分母是 question_count − flagged_count；
      · 开工痕迹 = 任意一题 any_tagged（≥1 个标签）；难度不参与判断。

    state: all / some / none。all 与侧边栏那个 ✓ 完全同义——别在别处再写一套。
    """
    qs = paper.questions()
    total = question_count if question_count is not None else len(qs)
    verified = 0
    flagged_n = 0
    tagged_any = False
    for q in qs:
        units = _units_of(q["qid"], q.get("split"), q.get("sub_ids") or [])
        if q.get("any_tagged"):
            tagged_any = True
        # 主题本身被标，或它任意一个小问被标，都算「这题有误」
        hit = str(q["qid"]) in flagged or any(u in flagged for u in units)
        if hit:
            flagged_n += 1
        elif _verify_state(units, conf)[0] == "all":
            verified += 1
    active = max(0, total - flagged_n)
    if active > 0 and verified >= active:
        st = "all"
    elif tagged_any or verified:
        st = "some"
    else:
        st = "none"
    return {
        "state": st,
        "verified_count": verified,
        "flagged_count": flagged_n,
        "question_count": total,
        "tagged": tagged_any,
    }


# ---------------------------------------------------------------------------
# 列表页的批量取数（2026-10-21）
#
# 原实现每卷两次往返：summary() 查 papers 表、paper_progress() → questions() 查 questions 表。
# 云端 2020 卷 / 27000 题实测 380–485ms、响应体 629KB，每次刷新整车重算。
# 现在改成两次全表查询（papers + progress_rows）在内存里分组——**数据与判定口径一字未改**，
# 「已打标几道 / 已核对几道 / 对勾」全部照旧由 make_progress 算出。
#
# 缓存键 = (索引代际, 状态库写入代际, 是否管理员, 学科范围)：
# 保存标注 / 确认 / 标有误 / 撤标记都会推代际，所以不会出现「勾完确认对勾不变」；
# 不同学科的用户各存一份，不会串号。TTL 比 /overview 短——列表是主路径，宁可多算一次。
# ---------------------------------------------------------------------------
_PAPERS_TTL = 15.0
_papers_lock = threading.Lock()
_papers_cache: dict = {"key": None, "at": 0.0, "payload": None}


def _progress_from_rows(rows, conf: set[str], flagged: set[str],
                        question_count: int | None) -> dict:
    """用 index_db.progress_rows() 的行算一卷进度，口径与 paper_progress 逐条对齐。

    行形状 (qid, parent_qid, split, has_tags)。切分题的确认单元 = 各子题文件真实 id，
    非切分题 = 主题 qid；被标「有误」的题退出分母（docs/11、docs/12）。
    """
    subs_by: dict[str, list[tuple[str, int]]] = {}
    mains: list[tuple[str, int, int]] = []
    for qid, parent, split, has_tags in rows:
        if parent:
            subs_by.setdefault(str(parent), []).append((qid, has_tags))
        else:
            mains.append((qid, split, has_tags))

    verified = 0
    flagged_n = 0
    tagged_any = False
    for qid, split, has_tags in mains:
        if split:
            subs = subs_by.get(qid, ())
            units = [s for s, _ in subs]
            any_tagged = any(t for _, t in subs)
        else:
            units = [qid]
            any_tagged = bool(has_tags)
        if any_tagged:
            tagged_any = True
        # 主题本身被标，或它任意一个小问被标，都算「这题有误」
        hit = qid in flagged or any(u in flagged for u in units)
        if hit:
            flagged_n += 1
        elif units and all(u in conf for u in units):
            verified += 1

    total = question_count if question_count is not None else len(mains)
    active = max(0, total - flagged_n)
    if active > 0 and verified >= active:
        st = "all"
    elif tagged_any or verified:
        st = "some"
    else:
        st = "none"
    return {
        "state": st,
        "verified_count": verified,
        "flagged_count": flagged_n,
        "question_count": total,
        "tagged": tagged_any,
    }


def _summary_from_row(paper: store.Paper, row: dict) -> dict:
    """卷摘要：直接从批量查到的 papers 行映射，字段与 Paper.summary() 完全一致。"""
    return {
        "id": paper.id,
        "board": row["board"],
        "qualification": row["qualification"],
        "subject": row["subject"],
        "paper_code": row["paper_code"],
        "year": row["year"],
        "session": row["session"],
        "unit": row["unit"],
        "question_count": row["question_count"],
        "annotated_count": row["annotated_count"],
        "source": row["source"],
    }


def _bulk_list_rows() -> tuple[dict[str, dict], dict[str, list]] | None:
    """一次取全库「卷摘要 + 题目投影」。

    索引缺失或正在全量重建时返回 None，调用方回落逐卷（慢但正确）。
    """
    if not index_db.exists() or index_db.is_building():
        return None
    try:
        return ({r["paper_id"]: r for r in index_db.papers()}, index_db.progress_rows())
    except Exception as e:  # noqa: BLE001  索引坏了只是慢，不能让列表 500
        print(f"[papers] 批量取数失败，回落逐卷：{e}", flush=True)
        return None


@router.get("/papers")
def list_papers(request: Request):
    ident = auth.current(request)
    key = (index_db.generation(), state.rev(), ident.is_admin, tuple(sorted(ident.subjects)))
    now = time.monotonic()
    with _papers_lock:
        c = _papers_cache
        if c["payload"] is not None and c["key"] == key and (now - c["at"]) < _PAPERS_TTL:
            return c["payload"]

    conf = review.confirmed_ids()
    flagged = flags.flagged_ids()   # 一处取全，别在循环里反复读文件
    bulk = _bulk_list_rows()
    by_id, qrows = bulk if bulk is not None else ({}, {})
    out = []
    for p in store.iter_papers():
        if not ident.can(p.subject):  # 学科隔离：目录级过滤，跨科卷根本不出现
            continue
        row = by_id.get(p.id)
        qr = qrows.get(p.id)
        # 只有「索引里这一卷齐备且未过期」才走快路径，其余一律回落逐卷（会顺带重灌）
        if row is None or row.get("stale") or qr is None:
            s = p.summary()
            pr = paper_progress(p, conf, flagged, s.get("question_count"))
        else:
            s = _summary_from_row(p, row)
            pr = _progress_from_rows(qr, conf, flagged, s.get("question_count"))
        s["flagged_count"] = pr["flagged_count"]
        s["verified_count"] = pr["verified_count"]
        out.append(s)
    with _papers_lock:
        _papers_cache.update({"key": key, "at": time.monotonic(), "payload": out})
    return out


# ---------------------------------------------------------------------------
# 全局概览（管理员）：每卷只回一个三态标记，前端画格子墙。
#
# 性能约束（2026-10-19 压测，7500 卷 / 10 万单元）：
#   · 复用上面的 paper_progress —— 逐卷构造 dict 约 337ms，10 万题时明显；
#   · 但概览允许 60 秒陈旧，缓存命中后 <5ms，冷启动才付那一次钱。
# 缓存键 = (索引代际, 状态库写入代际)：保存标注 / 确认 / 标有误都会自动失效，
# 不用在各个写入点手工清缓存（漏一个就会出现「勾完确认概览不变色」）。
# ---------------------------------------------------------------------------
_OVERVIEW_TTL = 60.0
_overview_lock = threading.Lock()
_overview_cache: dict = {"key": None, "at": 0.0, "payload": None}


@router.get("/overview")
def get_overview(request: Request, force: bool = False):
    ident = auth.current(request)
    if not ident.is_admin:
        raise HTTPException(403, "全局概览仅管理员可见")

    key = (index_db.generation(), state.rev())
    now = time.monotonic()
    with _overview_lock:
        c = _overview_cache
        # force=1：用户点了「刷新」，跳过读取但结果仍写回缓存
        if not force and c["payload"] is not None and c["key"] == key and (now - c["at"]) < _OVERVIEW_TTL:
            return c["payload"]

    conf = review.confirmed_ids()
    flagged = flags.flagged_ids()
    papers = []
    counts = {"all": 0, "some": 0, "none": 0}
    for p in store.iter_papers():
        s = p.summary()
        pr = paper_progress(p, conf, flagged, s.get("question_count"))
        counts[pr["state"]] = counts.get(pr["state"], 0) + 1
        papers.append({
            "id": s["id"],
            "board": s["board"],
            "qualification": s["qualification"],
            "subject": s["subject"],
            "paper_code": s.get("paper_code"),
            "year": s.get("year"),
            "session": s.get("session"),
            "unit": s.get("unit"),
            "state": pr["state"],
            "question_count": pr["question_count"],
            "verified_count": pr["verified_count"],
            "flagged_count": pr["flagged_count"],
        })
    payload = {
        "papers": papers,
        "summary": {"total": len(papers), **counts},
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    with _overview_lock:
        _overview_cache.update({"key": key, "at": time.monotonic(), "payload": payload})
    return payload



@router.get("/papers/{paper_id}/questions")
def list_questions(request: Request, paper_id: str):
    paper = store.find_paper(paper_id)
    if paper is None:
        raise HTTPException(404, f"paper not found: {paper_id}")
    auth.require_subject(request, paper.subject)
    conf = review.confirmed_ids()
    flagged = flags.flagged_ids()
    out = []
    for q in paper.questions():
        units = _units_of(q["qid"], q.get("split"), q.get("sub_ids") or [])
        state, done, total = _verify_state(units, conf)
        # 与 list_papers 同口径：主题本身或它任意一个小问被标「有误」都算本题有误。
        # 只认主题 qid 会让「小问被标」的题在列表里显示正常、点进去却处处 409（2026-10-18 体检）。
        hit = str(q["qid"]) in flagged or any(u in flagged for u in units)
        out.append(
            {
                **q,
                "verify_state": state,
                "confirmed_units": done,
                "total_units": total,
                "flagged": hit,
            }
        )
    return out


@router.get("/questions/{qid}")
def get_question(request: Request, qid: str):
    found = store.find_question(qid)
    if found is None:
        raise HTTPException(404, f"question not found: {qid}")
    paper, qpath = found
    # 学科隔离检查点②：qid 归属校验，跨科一律 404（不用 403，避免泄露存在性）
    auth.require_subject(request, paper.subject)
    text = qpath.read_text(encoding="utf-8")
    meta, fm = store.parse_front_matter(text)
    unit = paper.unit()
    body = text[fm.end():] if fm else text

    is_sub = bool(meta.get("parent"))
    parent_path = qpath
    if is_sub:  # 子题详情挂到主题上（打标台总以主题为单位展示）
        pm = store.find_question(meta["parent"])
        if pm is None:
            raise HTTPException(404, f"parent question not found: {meta['parent']}")
        paper, parent_path = pm
        pmeta = store.read_meta(parent_path)
    else:
        pmeta = meta

    # 小问切分信息（读 cut.json）
    top_labels: list[str] = []
    split_eligible = False
    booklet_warn: list[str] = []
    cut_q = store.cut_question(paper, pmeta.get("question_number"))
    # MCQ 也可能有顶层小问：化学 Section A 的「(a)(b)(c) 各带一套 A–D」
    # （2026-10-19 起切割层会把这类题的标签写进 cut.json.subquestions）。
    # 单问 MCQ 的 subquestions 仍为空 → top_labels 空 → split_eligible=False，行为不变。
    if cut_q:
        top_labels = _labels_for(paper, cut_q)
        split_eligible = len(top_labels) >= 2

    split_active = bool(pmeta.get("split"))
    conf = review.confirmed_ids()
    subs_out = []
    if split_active:
        for sm in paper.sub_metas(parent_path):
            subs_out.append({
                "qid": sm.get("id"),
                # ⚠️ 必须 str()：子题 front-matter 里 sub_label 写作无引号的 `sub_label: 1`，
                #    YAML 读回是 int，而 top_labels 来自 cut.json 是 str。前端按 label 配对
                #    小问（top_labels ↔ subs）时 `1 === '1'` 恒 false → 打标模式回填不出已有标签。
                "label": str(sm.get("sub_label")),
                "tags": sm.get("tags") or [],
                # 已解析的主标签（读侧双保险回退，§2.1）；多标签未指定主时是 None
                "primary_tag": store.effective_primary(sm.get("tags") or [], sm.get("primary_tag")),
                "difficulty": sm.get("difficulty") if sm.get("difficulty") in VALID_DIFFICULTIES else None,
                "status": store.compute_status(sm),
                "image_url": store.question_image_url(paper, sm),  # 预览模式切小问图用
                "verified": str(sm.get("id")) in conf,
            })

    if is_sub:  # 子题直接打开时返回子题自身的标注
        tags = meta.get("tags") or []
        difficulty = meta.get("difficulty") if meta.get("difficulty") in VALID_DIFFICULTIES else None
        primary_tag = store.effective_primary(tags, meta.get("primary_tag"))
    elif split_active:
        tags, difficulty, primary_tag = [], None, None
    else:
        tags = meta.get("tags") or []
        difficulty = meta.get("difficulty") if meta.get("difficulty") in VALID_DIFFICULTIES else None
        primary_tag = store.effective_primary(tags, meta.get("primary_tag"))

    # 主标签是否就绪（plans/主标签实施方案 §2 决策 #2）：切分题按小问聚合，与 tags_ready 同口径。
    # 预览模式据此把确认框置灰（后端 put_confirm 另有硬校验双保险）。
    if split_active:
        primary_ok = bool(subs_out) and all(
            s["tags"] and s["primary_tag"] is not None for s in subs_out
        )
    else:
        primary_ok = store.primary_ready(tags, meta.get("primary_tag"))

    # 审核确认状态：顶层给主题聚合（切分题用 subs[].verified 逐问判断）
    units = [s["qid"] for s in subs_out] if split_active else [str(pmeta.get("id"))]
    verify_state, confirmed_units, total_units = _verify_state(units, conf)

    # 「此题有误」标记（审核意见，不写进 md；存在 var/state.db）
    # 指纹校验只在这里做（单条、按需）——列表页不再逐条 stat 全库标记。
    if flags.is_flagged(qid):
        flags.verify_one(qid)
    frec = flags.get(qid)

    return {
        "qid": qid,
        "paper_id": paper.id,
        "unit": unit,
        "flagged": frec is not None,
        "flagged_at": (frec or {}).get("at"),
        "flagged_by": (frec or {}).get("by"),
        "flagged_note": (frec or {}).get("note"),
        "meta": pmeta,
        "body": body,
        "image_url": store.question_image_url(paper, pmeta),
        "tags": tags,
        "primary_tag": primary_tag,
        "primary_ready": primary_ok,
        "difficulty": difficulty,
        "available_tags": taxonomy.tags_for_units(paper.board, paper.qualification, paper.subject, unit),
        "available_groups": taxonomy.groups_for_units(paper.board, paper.qualification, paper.subject, unit),
        "split_eligible": split_eligible,
        "top_labels": top_labels,
        "split_active": split_active,
        "subs": subs_out,
        "verified": str(qid) in conf,
        "verify_state": verify_state,
        "confirmed_units": confirmed_units,
        "total_units": total_units,
    }


@router.put("/questions/{qid}/flag")
def put_flag(request: Request, qid: str, payload: FlagPayload):
    """标记 / 取消「此题有误」。

    标记是**审核意见**，只写 var/state.db，绝不碰 qNN.md。
    一旦标上，该题的标注与确认都被后端拒绝（409），直到有人取消标记。
    标记会自动失效：题目文件或题图被重新上传后指纹对不上，记录随即删除
    （见 flags.fingerprint_matches；校验发生在打开详情时与后台巡检，不在列表请求里）。
    """
    found = store.find_question(qid)
    if found is None:
        raise HTTPException(404, f"question not found: {qid}")
    paper, _ = found
    ident = auth.require_write(request)
    auth.require_subject(request, paper.subject)
    try:
        res = flags.set_flag(qid, payload.flagged, by=ident.name or ident.username,
                             note=payload.note)
    except KeyError:
        raise HTTPException(404, f"question not found: {qid}")
    auth.audit(ident, "flag.set" if payload.flagged else "flag.clear", qid,
               {"note": payload.note}, request)
    return res


@router.put("/questions/{qid}/confirm")
def put_confirm(request: Request, qid: str, payload: ConfirmPayload):
    """确认 / 取消确认一个单元（qid 可为主题 id 或小问 id）。

    确认记录只写 `var/state.db`（旧版是 data/_review/confirmed.json）；但当请求带了 `difficulty`（docs/13 预览就地补难度）
    时，先把它写进该单元对应的 `qNN.md` / 子题文件（**只动 difficulty 一行，不碰 tags**），再记确认。
    取消确认（confirmed=false）不回退难度——难度是标注，不该被审核动作抹掉。
    """
    found = store.find_question(qid)
    if found is None:
        raise HTTPException(404, f"question not found: {qid}")
    paper, qpath = found
    # 学科隔离检查点③-1：写接口再校验一次学科与角色
    ident = auth.require_subject(request, paper.subject)
    if not ident.can_confirm:
        raise HTTPException(403, f"当前角色（{ident.role}）不能做审核确认")
    if flags.is_flagged(qid):
        raise HTTPException(409, "本题已标记为「有误」，请先取消标记再做审核确认")

    # 核对门禁（plans/主标签实施方案 §2 决策 #2）：
    # 「核对完成」这个勾的含义是**标签和难度都看过了**，所以三个条件缺一不可：
    #   ① 至少 1 个知识点标签 —— 否则是一道什么都没标的题被勾了核对；
    #   ② 能解析出主标签 —— ≥2 标签却没指定主，下游没法排序；
    #   ③ 有难度 —— 前端 PreviewPanel 的 ready 判据就是「难度 + 主标签」，
    #      接口不跟着拦，就会出现「AI/脚本勾了、老师自己却勾不上」的两套标准，
    #      而整卷 ✓ 恰恰是拿这些勾算出来的，那样钩就是假的。
    # 与前端把确认框置灰是双保险——脚本/直调 API 绕不过这一层。
    if payload.confirmed:
        unit_meta, _ = store.parse_front_matter(qpath.read_text(encoding="utf-8"))
        unit_tags = unit_meta.get("tags") or []
        if not unit_tags:
            raise HTTPException(422, "本题还没有知识点标签，不能标记为核对完成")
        if store.effective_primary(unit_tags, unit_meta.get("primary_tag")) is None:
            raise HTTPException(422, "本题有多个标签但未指定主标签，请先在打标模式指定主标签")
        if unit_meta.get("difficulty") not in VALID_DIFFICULTIES and payload.difficulty is None:
            raise HTTPException(422, "本题还没有难度，不能标记为核对完成（可在此直接选难度）")

    difficulty_out: str | None = None
    status_out: str | None = None
    if payload.confirmed and payload.difficulty is not None:
        if payload.difficulty not in VALID_DIFFICULTIES:
            raise HTTPException(422, "难度必须为 very-easy / easy / medium / hard / very-hard 之一")
        try:  # ValueError 是「有副作用的部分」——写盘在最后一步，校验失败不落盘
            res = store.write_difficulty(qpath, payload.difficulty)
        except ValueError as e:
            raise HTTPException(500, str(e))
        difficulty_out = res["difficulty"]
        status_out = res["status"]

    # 记确认时带上操作人——旧版这里没传，导致 confirmed.json 里 by 恒为 null，
    # 十人打标时查不出是谁确认的。
    out = review.set_confirmed(qid, payload.confirmed, by=ident.name or ident.username)
    out["difficulty"] = difficulty_out
    out["status"] = status_out
    auth.audit(ident, "confirm" if payload.confirmed else "unconfirm", qid,
               {"difficulty": difficulty_out}, request)
    return out


@router.put("/questions/{qid}/annotation")
def put_annotation(request: Request, qid: str, payload: AnnotationPayload):
    found = store.find_question(qid)
    if found is None:
        raise HTTPException(404, f"question not found: {qid}")
    paper, qpath = found
    # 学科隔离检查点③-2：写接口再校验一次学科与角色
    ident = auth.require_write(request)
    auth.require_subject(request, paper.subject)
    # 「有误」的题冻结：不许打标、不许改难度、不许调切分，直到取消标记
    if flags.is_flagged(qid):
        raise HTTPException(409, "本题已标记为「有误」，请先取消标记再修改")
    meta, _ = store.parse_front_matter(qpath.read_text(encoding="utf-8"))

    # 审计要记「改了什么」，所以在写盘前把旧值取出来
    before = {
        "tags": meta.get("tags") or [],
        "primary_tag": store.effective_primary(meta.get("tags") or [], meta.get("primary_tag")),
        "difficulty": meta.get("difficulty"),
        "split": bool(meta.get("split")),
    }

    if meta.get("parent") and payload.subs is not None:
        raise HTTPException(422, "子题文件不支持再次切分，请对主题操作")

    if payload.subs is not None:
        res = _save_split(paper, qpath, meta, payload)
        after = {
            "subs": [
                {"label": s["label"], "tags": s["tags"],
                 "primary_tag": s.get("primary_tag"), "difficulty": s.get("difficulty")}
                for s in (res.get("subs") or [])
            ]
        }
    else:
        res = _save_whole(paper, qpath, meta, payload)
        after = {
            "tags": res.get("tags") or [],
            "primary_tag": res.get("primary_tag"),
            "difficulty": res.get("difficulty"),
            "split": res.get("split"),
        }
    auth.audit(ident, "annotation.save", qid, {"before": before, "after": after}, request)
    return res


def _save_split(paper: store.Paper, qpath, meta, payload: AnnotationPayload):
    cut_q = store.cut_question(paper, meta.get("question_number"))
    if cut_q is None:
        raise HTTPException(422, "该题缺少 cut.json 切割数据，无法切分")
    # MCQ 现在也允许切分（化学多问选择题，(a)(b)(c) 各一套 A–D 选项）。
    # 真正的闸是下面的「顶层小问 ≥ 2」——单问 MCQ 没有 subquestions，照样切不了。
    top_labels = _labels_for(paper, cut_q)
    if len(top_labels) < 2:
        raise HTTPException(422, f"顶层小问不足 2 个（{top_labels}），无法切分")

    sub_labels = [s.label for s in payload.subs]
    if not sub_labels:
        raise HTTPException(422, "至少要给一个小问打标签再保存")
    if len(set(sub_labels)) != len(sub_labels):
        raise HTTPException(422, "小问标签存在重复")
    # 允许只提交「本题小问的子集」：打标台的「下一问」是增量保存，逐个小问落库；
    # 也允许整题里有小问先跳过不标。
    # 但绝不能提交不属于本题的小问——那会把内容写到别的题的文件名上。
    illegal = [l for l in sub_labels if l not in top_labels]
    if illegal:
        raise HTTPException(422, f"小问标签不属于本题（本题为 {top_labels}）: {illegal}")
    for s in payload.subs:
        # tags 为空 = **显式清空该小问**（前端勾掉了全部标签 / AI 判定「无合适标签」）。
        # 这种提交必须放行：以前「清空」靠「没提交 → 后端补空壳」的副作用实现，
        # 而空壳语义已经收窄成「未提交且本小问从没有文件」才算，
        # 所以清空必须能显式表达，否则标签删不掉。
        if s.tags:
            _validate_tags(paper, s.tags, f"小问 {s.label}")
            _validate_primary(s.tags, s.primary_tag, f"小问 {s.label}")
        elif s.primary_tag:
            raise HTTPException(422, f"小问 {s.label}: 没有标签却指定了主标签")
        if s.difficulty is not None and s.difficulty not in VALID_DIFFICULTIES:
            raise HTTPException(422, f"小问 {s.label}: 难度必须为 very-easy / easy / medium / hard / very-hard 之一")

    # 没提交的小问**保留原文件**，只给「本题还从没有过该小问文件」的补空壳。
    # 空壳的三个作用不变：① 让「还有小问没打标」在文件层面可见（status 停在 partial）；
    # ② 确认单元以子题文件为据（api._units_of），空壳没标签 → 后端 422 不给确认
    #    → 只标了 1 个小问的题**永远拿不到整卷 ✓**；③ 文件集合恒等于 top_labels 全集，
    #    列表页的 tags_ready/any_tagged 聚合口径不受影响。
    # ⚠️ 为什么不再一律补空壳：打标台的「保存，下一问」是分次提交，
    #    整体替换会让先发后到的旧快照把刚打好的小问抹回空壳（2026-10-19 培琪反馈
    #    「中途切走修改就没了」）。现在「未提交」= 「别动」，重放/乱序都不再有破坏性。
    submitted = {s.label: s for s in payload.subs}
    existing = {str(sm.get("sub_label")): sm for sm in paper.sub_metas(qpath)}
    write_subs: list[dict] = []
    for label in top_labels:
        s = submitted.get(label)
        if s is not None:
            write_subs.append(s.model_dump())
        elif label in existing:
            continue  # 未提交且已有文件：原样保留（本次不碰它）
        else:
            write_subs.append({"label": label, "tags": [], "difficulty": None, "primary_tag": None})

    # 保存前快照：用于判断各小问的标签/难度/主标签是否真的变了（变了才撤销确认）
    before = {
        str(sm.get("sub_label")): (
            tuple(sm.get("tags") or []),
            sm.get("difficulty"),
            store.effective_primary(sm.get("tags") or [], sm.get("primary_tag")),
        )
        for sm in paper.sub_metas(qpath)
    }

    # 渲染子图：AQA/CIE，以及 accounting 学科（任何考试局）按小问打标但「不切子图」——
    # 子题文件不带 image/layout，预览自动回退整题图（前端 imageOverride ?? null）。
    # 其余（Edexcel physics/chemistry）维持切子图。判定统一走 store.sub_images_enabled。
    if not store.sub_images_enabled(paper.board, paper.subject):
        render_res: dict = {
            "results": [{"label": l, "image": None, "layout": None} for l in top_labels],
            "warnings": [],
        }
    else:
        # Edexcel 物理/化学：每个小问要连题区一起切出来单独成题，所以真的会裁图（只需 Pillow）。
        # 已有子图**沿用原路径，不再重渲**——打标只改 front-matter 的 tags，
        # 图本身一个像素都不变；重切一遍白费 0.3–1 s/题。
        existing = {
            str(sm.get("sub_label")): (sm.get("image"), sm.get("layout"))
            for sm in paper.sub_metas(qpath)
        }
        results = []
        missing: list[str] = []
        for l in top_labels:
            img, lay = existing.get(l, (None, None))
            if img and lay and (paper.path / str(img)).exists() and (paper.path / str(lay)).exists():
                results.append({"label": l, "image": img, "layout": lay})
            else:
                results.append({"label": l, "image": None, "layout": None})
                missing.append(l)
        warnings: list[str] = []
        if missing:
            # 第一次切这张题（或新补了某个小问）才真裁图。渲染失败不落盘。
            # pdf_absent_ok=True：云端按政策不传 PDF，缺它是常态，别把「跳过了源校验」
            # 这条内部细节弹给老师（他改不了，也不需要关心）。
            try:
                fresh = render_subsplit.render_split(
                    paper.path, meta["question_number"], top_labels, pdf_absent_ok=True
                )
            except Exception as e:  # noqa: BLE001
                raise HTTPException(500, f"子题渲染失败：{e}")
            fmap = {r["label"]: r for r in fresh.get("results") or []}
            for i, r in enumerate(results):
                if r["image"] is None and r["label"] in fmap:
                    results[i] = fmap[r["label"]]
            warnings = fresh.get("warnings") or []
        render_res = {"results": results, "warnings": warnings}

    try:
        result = store.save_split(
            paper, qpath,
            subs=write_subs,
            render_results=render_res["results"],
        )
    except ValueError as e:
        raise HTTPException(500, str(e))
    result["warnings"] = render_res["warnings"]

    # 标签/难度/主标签动过的小问 → 取消其「核对完成」；切分本身就是形态变化，
    # 所以主文件那条整题确认（若有）一律作废。
    unchanged: set[str] = set()
    units: list[str] = [str(meta.get("id"))]
    for sm in result.get("subs") or []:
        uid = str(sm.get("qid"))
        units.append(uid)
        # result["subs"][].primary_tag 已是 resolved 值（store.save_split 回传），
        # 与 before 侧口径一致，避免单标签场景来回白清确认
        new = (tuple(sm.get("tags") or []), sm.get("difficulty"), sm.get("primary_tag"))
        if before.get(str(sm.get("label"))) == new:
            unchanged.add(uid)
    result["confirm_cleared"] = _invalidate_confirm(units, unchanged)
    return result


def _save_whole(paper: store.Paper, qpath, meta, payload: AnnotationPayload):
    _validate_tags(paper, payload.tags or [], "整题")
    _validate_primary(payload.tags or [], payload.primary_tag, "整题")
    if payload.difficulty is not None and payload.difficulty not in VALID_DIFFICULTIES:
        raise HTTPException(422, "难度必须为 very-easy / easy / medium / hard / very-hard 之一")

    was_split = bool(meta.get("split"))
    before = (
        tuple(meta.get("tags") or []),
        meta.get("difficulty"),
        store.effective_primary(meta.get("tags") or [], meta.get("primary_tag")),
    )
    # 关闭切分时，原挂在各小问上的确认也要一并作废
    old_sub_qids = [str(m.get("id")) for m in paper.sub_metas(qpath)] if was_split else []
    if was_split:
        store.cleanup_subs(paper, qpath)  # 关闭切分：先清子文件与派生图
    try:
        result = store.write_annotation(qpath, payload.tags, payload.difficulty, payload.primary_tag)
    except ValueError as e:
        raise HTTPException(500, str(e))
    if was_split:
        result["split_removed"] = True
        result["split"] = False  # 显式回传，前端列表立即摘掉切分角标
        result["sub_labels"] = []

    # 标签/难度/主标签动过 → 原来的「核对完成」勾选自动作废（docs/12 §③）
    qid = str(meta.get("id"))
    after = (tuple(result.get("tags") or []), result.get("difficulty"), result.get("primary_tag"))
    result["confirm_cleared"] = _invalidate_confirm(
        [qid, *old_sub_qids], {qid} if before == after else set()
    )
    return result
