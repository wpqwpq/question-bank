"""API 路由。"""
from __future__ import annotations

import sys
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import store
import taxonomy

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))
import render_subsplit  # noqa: E402  依赖与后端同一 venv（pymupdf/PIL）

router = APIRouter(prefix="/api")

VALID_DIFFICULTIES = {"very-easy", "easy", "medium", "hard", "very-hard"}


class SubAnnotation(BaseModel):
    label: str
    tags: list[str]
    difficulty: str


class AnnotationPayload(BaseModel):
    tags: list[str] | None = None
    difficulty: str | None = None
    subs: list[SubAnnotation] | None = None  # 非空 = 按小问切分保存


def _validate_tags(paper: store.Paper, tags: list[str], where: str) -> None:
    if not tags:
        raise HTTPException(422, f"{where}: 至少勾选 1 个知识点标签")
    if len(set(tags)) != len(tags):
        raise HTTPException(422, f"{where}: 标签存在重复")
    allowed = taxonomy.tag_ids(paper.board, paper.qualification, paper.subject, paper.unit())
    illegal = [t for t in tags if t not in allowed]
    if illegal:
        raise HTTPException(422, f"{where}: 标签不属于本卷单元 {paper.unit()}: {', '.join(illegal)}")


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


@router.get("/papers")
def list_papers():
    return [p.summary() for p in store.iter_papers()]


@router.get("/papers/{paper_id}/questions")
def list_questions(paper_id: str):
    paper = store.find_paper(paper_id)
    if paper is None:
        raise HTTPException(404, f"paper not found: {paper_id}")
    return paper.questions()


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
    subs_out = []
    if split_active:
        for sm in paper.sub_metas(parent_path):
            subs_out.append({
                "qid": sm.get("id"),
                "label": sm.get("sub_label"),
                "tags": sm.get("tags") or [],
                "difficulty": sm.get("difficulty") if sm.get("difficulty") in VALID_DIFFICULTIES else None,
                "status": store.compute_status(sm),
                "image_url": store.question_image_url(paper, sm),  # 预览模式切小问图用
            })

    if is_sub:  # 子题直接打开时返回子题自身的标注
        tags = meta.get("tags") or []
        difficulty = meta.get("difficulty") if meta.get("difficulty") in VALID_DIFFICULTIES else None
    elif split_active:
        tags, difficulty = [], None
    else:
        tags = meta.get("tags") or []
        difficulty = meta.get("difficulty") if meta.get("difficulty") in VALID_DIFFICULTIES else None

    return {
        "qid": qid,
        "paper_id": paper.id,
        "unit": unit,
        "meta": pmeta,
        "body": body,
        "image_url": store.question_image_url(paper, pmeta),
        "tags": tags,
        "difficulty": difficulty,
        "available_tags": taxonomy.tags_for(paper.board, paper.qualification, paper.subject, unit),
        "available_groups": taxonomy.groups_for(paper.board, paper.qualification, paper.subject, unit),
        "split_eligible": split_eligible,
        "top_labels": top_labels,
        "split_active": split_active,
        "subs": subs_out,
    }


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
    if set(sub_labels) != set(top_labels):
        raise HTTPException(
            422,
            f"小问标签必须完整覆盖 {top_labels}，当前提交: {sub_labels}",
        )
    for s in payload.subs:
        _validate_tags(paper, s.tags, f"小问 {s.label}")
        if s.difficulty not in VALID_DIFFICULTIES:
            raise HTTPException(422, f"小问 {s.label}: 难度必须为 very-easy / easy / medium / hard / very-hard 之一")

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
    return result


def _save_whole(paper: store.Paper, qpath, meta, payload: AnnotationPayload):
    _validate_tags(paper, payload.tags or [], "整题")
    if payload.difficulty not in VALID_DIFFICULTIES:
        raise HTTPException(422, "难度必须为 very-easy / easy / medium / hard / very-hard 之一")

    was_split = bool(meta.get("split"))
    if was_split:
        store.cleanup_subs(paper, qpath)  # 关闭切分：先清子文件与派生图
    try:
        result = store.write_annotation(qpath, payload.tags, payload.difficulty)
    except ValueError as e:
        raise HTTPException(500, str(e))
    if was_split:
        result["split_removed"] = True
        result["split"] = False  # 显式回传，前端列表立即摘掉切分角标
        result["sub_labels"] = []
    return result
