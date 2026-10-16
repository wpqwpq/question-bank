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


class AnnotationPayload(BaseModel):
    tags: list[str] | None = None
    difficulty: str | None = None  # 可留空（docs/13）
    subs: list[SubAnnotation] | None = None  # 非空 = 按小问切分保存


class ConfirmPayload(BaseModel):
    confirmed: bool
    # 预览模式就地补/改难度（docs/13）：给了就先写库再记确认；None = 旧行为（只记确认）
    difficulty: str | None = None


# ---------------------------------------------------------------------------
# 审核确认（docs/11）：确认单元 = 非切分题的主题 qid / 切分题的各小问 id。
# 聚合逻辑留在本层，store 不依赖 review（避免循环依赖）。
# ---------------------------------------------------------------------------

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
                "difficulty": sm.get("difficulty") if sm.get("difficulty") in VALID_DIFFICULTIES else None,
                "status": store.compute_status(sm),
                "image_url": store.question_image_url(paper, sm),  # 预览模式切小问图用
                "verified": str(sm.get("id")) in conf,
            })

    if is_sub:  # 子题直接打开时返回子题自身的标注
        tags = meta.get("tags") or []
        difficulty = meta.get("difficulty") if meta.get("difficulty") in VALID_DIFFICULTIES else None
    elif split_active:
        tags, difficulty = [], None
    else:
        tags = meta.get("tags") or []
        difficulty = meta.get("difficulty") if meta.get("difficulty") in VALID_DIFFICULTIES else None

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
        if s.difficulty is not None and s.difficulty not in VALID_DIFFICULTIES:
            raise HTTPException(422, f"小问 {s.label}: 难度必须为 very-easy / easy / medium / hard / very-hard 之一")

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
    return result


def _save_whole(paper: store.Paper, qpath, meta, payload: AnnotationPayload):
    _validate_tags(paper, payload.tags or [], "整题")
    if payload.difficulty is not None and payload.difficulty not in VALID_DIFFICULTIES:
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
