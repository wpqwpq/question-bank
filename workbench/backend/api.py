"""API 路由。"""
from __future__ import annotations

import sys
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

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
def get_taxonomy():
    """词表清单：data/taxonomy/<board>/<qual>/<subject>.json 的目录树。"""
    root = taxonomy.TAXONOMY_DIR
    tree: dict[str, dict] = {}
    if root.is_dir():
        for board in sorted(p for p in root.iterdir() if p.is_dir() and p.name != "_prev"):
            for qual in sorted(p for p in board.iterdir() if p.is_dir()):
                for f in sorted(qual.glob("*.json")):
                    tree.setdefault(board.name, {}).setdefault(qual.name, []).append(f.stem)
    return tree


@router.get("/taxonomy/{board}/{qual}/{subject}")
def get_taxonomy_file(board: str, qual: str, subject: str):
    """整份词表文件（标签管理页要看全部单元，而不只是某一卷的单元）。"""
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
    }


@router.put("/taxonomy/{board}/{qual}/{subject}")
def put_taxonomy_file(board: str, qual: str, subject: str, payload: dict):
    """整体覆盖写词表：先备份到 _prev/ 再原子替换。

    校验不过整单拒绝（422，detail 为逐条错误、换行分隔）；成功则顺手清缓存，
    打标台右栏下一次拉详情就能看到新标签。
    """
    data = payload.get("data", payload) if isinstance(payload, dict) else payload
    try:
        return taxonomy.save_taxonomy(board, qual, subject, data)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(422, str(e))


@router.get("/papers")
def list_papers():
    conf = review.confirmed_ids()
    out = []
    for p in store.iter_papers():
        s = p.summary()
        # 已确认题目数 = 全部小问都已确认的主题题数
        s["verified_count"] = sum(
            1
            for q in p.questions()
            if _verify_state(_units_of(q["qid"], q.get("split"), q.get("sub_ids") or []), conf)[0]
            == "all"
        )
        out.append(s)
    return out


@router.get("/papers/{paper_id}/questions")
def list_questions(paper_id: str):
    paper = store.find_paper(paper_id)
    if paper is None:
        raise HTTPException(404, f"paper not found: {paper_id}")
    conf = review.confirmed_ids()
    out = []
    for q in paper.questions():
        units = _units_of(q["qid"], q.get("split"), q.get("sub_ids") or [])
        state, done, total = _verify_state(units, conf)
        out.append(
            {
                **q,
                "verify_state": state,
                "confirmed_units": done,
                "total_units": total,
            }
        )
    return out


@router.get("/questions/{qid}")
def get_question(qid: str):
    found = store.find_question(qid)
    if found is None:
        raise HTTPException(404, f"question not found: {qid}")
    paper, qpath = found
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
    if cut_q and cut_q.get("question_type") != "mcq":
        top_labels = render_subsplit.top_labels(cut_q)
        split_eligible = len(top_labels) >= 2

    split_active = bool(pmeta.get("split"))
    conf = review.confirmed_ids()
    subs_out = []
    if split_active:
        for sm in paper.sub_metas(parent_path):
            subs_out.append({
                "qid": sm.get("id"),
                "label": sm.get("sub_label"),
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

    return {
        "qid": qid,
        "paper_id": paper.id,
        "unit": unit,
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


@router.put("/questions/{qid}/confirm")
def put_confirm(qid: str, payload: ConfirmPayload):
    """确认 / 取消确认一个单元（qid 可为主题 id 或小问 id）。

    确认记录只写 `data/_review/confirmed.json`；但当请求带了 `difficulty`（docs/13 预览就地补难度）
    时，先把它写进该单元对应的 `qNN.md` / 子题文件（**只动 difficulty 一行，不碰 tags**），再记确认。
    取消确认（confirmed=false）不回退难度——难度是标注，不该被审核动作抹掉。
    """
    found = store.find_question(qid)
    if found is None:
        raise HTTPException(404, f"question not found: {qid}")
    _, qpath = found

    # 主标签门禁（plans/主标签实施方案 §2 决策 #2）：多标签却无主标签的单元不许确认。
    # 与前端把确认框置灰是双保险——脚本/直调 API 绕不过这一层。
    if payload.confirmed:
        unit_meta, _ = store.parse_front_matter(qpath.read_text(encoding="utf-8"))
        unit_tags = unit_meta.get("tags") or []
        if len(unit_tags) >= 2 and store.effective_primary(
            unit_tags, unit_meta.get("primary_tag")
        ) is None:
            raise HTTPException(422, "本题有多个标签但未指定主标签，请先在打标模式指定主标签")

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

    out = review.set_confirmed(qid, payload.confirmed)
    out["difficulty"] = difficulty_out
    out["status"] = status_out
    return out


@router.put("/questions/{qid}/annotation")
def put_annotation(qid: str, payload: AnnotationPayload):
    found = store.find_question(qid)
    if found is None:
        raise HTTPException(404, f"question not found: {qid}")
    paper, qpath = found
    meta, _ = store.parse_front_matter(qpath.read_text(encoding="utf-8"))

    if meta.get("parent") and payload.subs is not None:
        raise HTTPException(422, "子题文件不支持再次切分，请对主题操作")

    if payload.subs is not None:
        return _save_split(paper, qpath, meta, payload)
    return _save_whole(paper, qpath, meta, payload)


def _save_split(paper: store.Paper, qpath, meta, payload: AnnotationPayload):
    cut_q = store.cut_question(paper, meta.get("question_number"))
    if cut_q is None:
        raise HTTPException(422, "该题缺少 cut.json 切割数据，无法切分")
    if cut_q.get("question_type") == "mcq":
        raise HTTPException(422, "MCQ 不支持小问切分")
    top_labels = render_subsplit.top_labels(cut_q)
    if len(top_labels) < 2:
        raise HTTPException(422, f"顶层小问不足 2 个（{top_labels}），无法切分")

    sub_labels = [s.label for s in payload.subs]
    if len(set(sub_labels)) != len(sub_labels):
        raise HTTPException(422, "小问标签存在重复")
    # 允许只提交「本题小问的子集」：打标台的「下一问」是增量保存，逐个小问落库。
    # 但绝不能提交不属于本题的小问——那会把内容写到别的题的文件名上。
    illegal = [l for l in sub_labels if l not in top_labels]
    if illegal:
        raise HTTPException(422, f"小问标签不属于本题（本题为 {top_labels}）: {illegal}")
    for s in payload.subs:
        _validate_tags(paper, s.tags, f"小问 {s.label}")
        _validate_primary(s.tags, s.primary_tag, f"小问 {s.label}")
        if s.difficulty is not None and s.difficulty not in VALID_DIFFICULTIES:
            raise HTTPException(422, f"小问 {s.label}: 难度必须为 very-easy / easy / medium / hard / very-hard 之一")

    # 保存前快照：用于判断各小问的标签/难度/主标签是否真的变了（变了才撤销确认）
    before = {
        str(sm.get("sub_label")): (
            tuple(sm.get("tags") or []),
            sm.get("difficulty"),
            store.effective_primary(sm.get("tags") or [], sm.get("primary_tag")),
        )
        for sm in paper.sub_metas(qpath)
    }

    # 渲染子图：AQA/CIE 按小问打标但「不切子图」——子题文件不带 image/layout，
    # 预览自动回退整题图（前端 imageOverride ?? null）。Edexcel 维持切子图。
    if paper.board in store.SUB_NO_IMAGE_BOARDS:
        render_res: dict = {
            "results": [{"label": l, "image": None, "layout": None} for l in top_labels],
            "warnings": [],
        }
    else:
        # 先渲染（失败无副作用），再写盘
        try:
            render_res = render_subsplit.render_split(paper.path, meta["question_number"], top_labels)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(500, f"子题渲染失败: {e}")

    try:
        result = store.save_split(
            paper, qpath,
            subs=[s.model_dump() for s in payload.subs],
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
