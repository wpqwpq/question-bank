"""题库数据访问层：扫描 data/ 目录、front-matter 解析与外科手术式写回。"""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator

import index_db

REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = REPO_ROOT / "data"
SCHEMA_PATH = REPO_ROOT / "schema" / "question.schema.json"

FM_RE = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*\r?\n", re.DOTALL)
_DONE_DIFFICULTIES = {"very-easy", "easy", "medium", "hard", "very-hard"}
MAIN_Q_RE = re.compile(r"^q\d+\.md$")       # 主题文件 q04.md
# 子题文件（split 产物）：标签可为字母（Edexcel a/b/c）或数字（AQA/CIE 1..9）
SUB_Q_RE = re.compile(r"^q\d+(-[a-z0-9]+)+\.md$")

# 按小问打标时「不切子图」的考试局：仍写子题文件（qNN-<label>.md）保留小问级标注，
# 但不产 qNN-<label>.webp 子图——预览统一回退整题图。见 api._save_split。
SUB_NO_IMAGE_BOARDS = {"aqa", "cie"}

# 学科级豁免（2026-10-19）：这些学科无论哪个考试局都不切子图。
# 会计题是表格与分录，把「题干 + 本问」连题区切出来没有阅读价值，
# 且子题图反而要多维护一份；小问级标签照旧落子题文件，只是不产图。
# 数学同理（2026-10-19 王培琪定，edexcel mathematics 282 卷）：题图本来就窄
# （纯数学一题一页、D1 还带作答册），按小问裁出来的碎片没有阅读价值。
SUB_NO_IMAGE_SUBJECTS = {"accounting", "mathematics"}


# 打标粒度（2026-10-19）：默认按顶层字母（a/b/c）打标；下列学科展开到罗马子问。
# 会计 IAL 的评分与知识点都落在 (a)(i)(ii) 这一层，一个 (a) 下常有 3–5 个分项，
# 按顶层字母打标签会粗到分不开。走 leaf 粒度后 c 问只有 "c i"/"c ii"，不再有 c。
SUB_DEEP_LABEL_SUBJECTS = {"accounting"}


def sub_label_mode(board: str, subject: str) -> str:
    """按小问打标时的粒度：'top' = 顶层字母，'leaf' = 展开罗马子问。"""
    return "leaf" if subject.lower() in SUB_DEEP_LABEL_SUBJECTS else "top"


def sub_slug(label: str) -> str:
    """子题 label → 文件名 / id 后缀："c i" → "c-i"。

    schema 的 id 模式 `^[a-z0-9]+(-[a-z0-9]+)+$` 不允许空格，而 leaf 粒度的
    label 天然带空格；sub_label 字段本身仍存原文（"c i"），只有落盘标识 slug 化。
    """
    return "-".join(label.split())


def sub_images_enabled(board: str, subject: str) -> bool:
    """按小问打标时是否真的要裁子图。

    False = 只写子题文件、预览回退整题图（与 AQA/CIE 同语义）。
    新增考试局/学科默认 True（黑名单制），不想切图就往上面两个集合里加。
    """
    return (board.lower() not in SUB_NO_IMAGE_BOARDS
            and subject.lower() not in SUB_NO_IMAGE_SUBJECTS)

_schema_validator: Draft202012Validator | None = None


def _validator() -> Draft202012Validator:
    global _schema_validator
    if _schema_validator is None:
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        _schema_validator = Draft202012Validator(schema)
    return _schema_validator


def unit_from_paper_code(code: str, subject: str = "") -> str:
    """WPH11 -> U1, WPH14A -> U4；AQA 的 PH01 / CH01 -> U1（牛津 AQA 卷号是 PH0N / CH0N）；
    CIE 的 QP11–QP23 -> P1P2、QP41–QP43 -> P4、QP51–QP54 -> P5（对齐 taxonomy/cie 词表的单元键）。

    Edexcel 取卷号中 '1' 后跟的那一位数字；AQA 的 PH01–PH05（物理）、CH01–CH05（化学）
    没有这个模式，回退到 PH0N / CH0N 形式。都匹配不到返回 'unknown'——
    卷列表不因个别坏卷号崩溃；unknown 单元无词表，保存时会被标签校验拒绝。

    CIE 化学有 QP5x（Paper 5 planning，70 卷），2026-10-18 起按 P5 认；
    QP3x（Paper 3 practical）库里暂时没有数据，按 P3 兜底、无词表即不可打标。

    Edexcel IAL 数学（2026-10-19）走自己的分支：WMA1x→P1–P4、WST0x→S1–S3、
    WME0x→M1–M3、WFM0x→F1–F3、WDM11→D1（对齐 taxonomy/edexcel/a-level/mathematics.json
    的单元键）。**必须先于下面的通用 Edexcel 规则判断**——通用规则会把 WMA11 误判成 U1，
    而 WST01 / WME01 / WFM01 压根匹配不到、直接落到 unknown（整科无法打标）。

    CIE 数学 9709（2026-10-20）**必须按 subject 分流**：它的 QP11–QP63 与物理/化学的
    卷号规则撞车，但单元是六个独立的 P1–P6（P4 力学、P5 统计1、P6 统计2），
    不能沿用物理化学的 P1P2 合用池——否则 P1/P2 混池、QP6x 还会落到 unknown。
    """
    subj = (subject or "").strip().lower()
    if subj == "mathematics":
        m = re.match(r"(?i)^QP([1-6])\d$", code.strip())
        if m:
            return f"P{m.group(1)}"
    m = re.match(r"(?i)QP([1-5])\d", code.strip())
    if m:
        return {"1": "P1P2", "2": "P1P2", "4": "P4", "5": "P5"}.get(
            m.group(1), f"P{m.group(1)}")
    m = re.match(r"(?i)^W(MA|ST|ME|FM|DM)(\d)(\d)[A-Z]?$", code.strip())
    if m:
        fam, series = m.group(1).upper(), m.group(3)
        if fam == "DM":
            return "D1"
        return {"MA": "P", "ST": "S", "ME": "M", "FM": "F"}[fam] + series
    m = re.search(r"1(\d)", code) or re.search(r"[PC]H0?(\d)", code, re.I)
    if m:
        return f"U{m.group(1)}"
    return "unknown"


def parse_front_matter(text: str) -> tuple[dict[str, Any], re.Match[str] | None]:
    m = FM_RE.match(text)
    if not m:
        return {}, None
    meta = yaml.safe_load(m.group(1)) or {}
    return meta, m


def compute_status(meta: dict[str, Any], sub_metas: list[dict[str, Any]] | None = None) -> str:
    if meta.get("split"):
        # 切分题：主文件无 tags/difficulty，状态由子题文件推导
        sts = [compute_status(m) for m in (sub_metas or [])]
        if not sts:
            return "none"
        if all(s == "done" for s in sts):
            return "done"
        if any(s != "none" for s in sts):
            return "partial"
        return "none"
    tags = meta.get("tags") or []
    # 2026-10-16：难度不再是「完成」的必要条件——AI 预打标只标知识点、难度留空，
    # 若仍要求难度，整库打完标签也会全部算未完成（卷进度、题号一直不计数）。
    # 「难度是否齐备」改由 Paper.questions() 的 difficulty_ready 单独跟踪
    # （预览模式的「待补难度（灰）」用它判），与本函数的完成态互不干扰。
    if isinstance(tags, list) and len(tags) > 0:
        return "done"
    if meta.get("difficulty") in _DONE_DIFFICULTIES:
        return "partial"
    return "none"


# ---------------------------------------------------------------------------
# 主标签 primary_tag（plans/主标签实施方案.md §2）
#   tags 为空 → 不写该键；tags 恰 1 项 → 后端自动补成该 id；tags ≥2 → 必须显式且 ∈ tags。
#   读侧统一走 effective_primary() 的双保险回退，防手改与悬空引用。
# ---------------------------------------------------------------------------

def effective_primary(tags: list[str] | None, primary: Any) -> str | None:
    """读侧解析主标签（唯一的回退规则，所有消费方共用）。

    `primary_tag ∈ tags` 时用它；否则 tags 恰好 1 项时用 tags[0]；其余（0 项、≥2 项且缺失
    或指向已不在 tags 里的悬空 id）返回 None——下游据此判断「主标签未就绪」，而不是
    拿到一个假的。
    """
    tags = tags or []
    if not isinstance(tags, list) or not tags:
        return None
    if isinstance(primary, str) and primary in tags:
        return primary
    if len(tags) == 1:
        return tags[0]
    return None


def normalize_primary(tags: list[str], primary: str | None) -> str | None:
    """写侧归一（§2.1）。api 层已对「≥2 项却缺失/越界」返回 422，这里只做兜底：
    非法值一律落成 None（不写该键），绝不把假主标签写进题库。"""
    if not tags:
        return None
    if len(tags) == 1:
        return tags[0]
    return primary if isinstance(primary, str) and primary in tags else None


def primary_ready(tags: list[str] | None, primary: Any) -> bool:
    """本单元的主标签是否就绪：有标签且能解析出主标签（≥2 标签却无主 → False）。"""
    tags = tags or []
    return bool(tags) and effective_primary(tags, primary) is not None


# ---------------------------------------------------------------------------
# 索引层：进程内缓存，避免每次请求线性扫全库
#
#   find_question() 原实现从 data/ 头开始逐卷逐个读 + YAML 解析全部 qNN.md
#   （2013 个 ≈1.8s），且成本随目标卷在目录排序中的位置线性增长
#   （第 1 卷 44ms → 排序末卷 1876ms）。快速连点时多个全库扫描并发叠加，
#   被 GIL 串行化后单个请求会涨到 5.7s。
#
#   失效策略（三层，越靠前越便宜）：
#     ① 元数据按 (mtime_ns, size) 逐文件校验 —— 改一个只重读一个；
#     ② 卷/题目集合按目录 mtime 快照校验 —— 外部新增/删除卷自动生效；
#     ③ 本进程写盘后主动更新受影响条目 —— 打标保存不触发重建。
# ---------------------------------------------------------------------------

_lock = threading.RLock()
_meta_cache: dict[str, tuple[int, int, dict[str, Any]]] = {}
_papers: list[Paper] | None = None
_papers_stamp: tuple[tuple[str, int], ...] | None = None
_papers_by_id: dict[str, Paper] = {}
_qid_index: dict[str, tuple[str, str]] = {}  # qid -> (paper_id, file_name)
_scan_at: float = 0.0                        # 上次目录扫描时刻（monotonic）
_papers_gen: int = -1                        # 上次建立 Paper 列表时的索引代际

# 目录扫描的缓存时长（秒）。卷列表仍靠扫目录，保留「上传新卷自动出现」的能力；
# 5 秒一次扫描（410 卷约 76ms）摊到每个请求几乎为零，10 万题（1430 卷）约 250ms/5s。
_SCAN_TTL = float(os.environ.get("QBANK_SCAN_TTL") or 5.0)


def read_meta(path: Path) -> dict[str, Any]:
    """读 front-matter（带 mtime/size 缓存）。文件未变则零解析开销。"""
    key = str(path)
    try:
        st = path.stat()
    except OSError:
        with _lock:
            _meta_cache.pop(key, None)
        return {}
    with _lock:
        hit = _meta_cache.get(key)
    if hit is not None and hit[0] == st.st_mtime_ns and hit[1] == st.st_size:
        return hit[2]
    meta, _ = parse_front_matter(path.read_text(encoding="utf-8"))
    with _lock:
        _meta_cache[key] = (st.st_mtime_ns, st.st_size, meta)
    return meta


def _drop_meta(path: Path) -> None:
    """本进程刚写过该文件，丢掉其元数据缓存。"""
    with _lock:
        _meta_cache.pop(str(path), None)


def _index_put(qid: Any, paper_id: str, file_name: str) -> None:
    if qid:
        with _lock:
            _qid_index[str(qid)] = (paper_id, file_name)


def _index_drop(qid: Any) -> None:
    if qid:
        with _lock:
            _qid_index.pop(str(qid), None)


def _scan_papers() -> tuple[list[Paper], tuple[tuple[str, int], ...]]:
    """遍历 data/ 得到 Paper 列表 + 各级目录 mtime 快照。"""
    papers: list[Paper] = []
    stamp: list[tuple[str, int]] = []
    if not DATA_DIR.exists():
        return papers, ()
    try:
        stamp.append(("", DATA_DIR.stat().st_mtime_ns))
    except OSError:
        pass
    # 排除非考试局目录：taxonomy（标签词表）、_review（审核确认记录，见 docs/11）
    for board in sorted(
        p for p in DATA_DIR.iterdir() if p.is_dir() and p.name not in ("taxonomy", "_review")
    ):
        stamp.append((board.name, board.stat().st_mtime_ns))
        for qualification in sorted(p for p in board.iterdir() if p.is_dir()):
            qkey = f"{board.name}/{qualification.name}"
            stamp.append((qkey, qualification.stat().st_mtime_ns))
            for subject in sorted(p for p in qualification.iterdir() if p.is_dir()):
                skey = f"{qkey}/{subject.name}"
                stamp.append((skey, subject.stat().st_mtime_ns))
                for session in sorted(p for p in subject.iterdir() if p.is_dir()):
                    try:
                        st = session.stat()
                    except OSError:
                        continue
                    stamp.append((f"{skey}/{session.name}", st.st_mtime_ns))
                    # 用 scandir 判「有没有题」比 glob 快；顺带把目录 mtime 交给 Paper
                    if any(f.name.startswith("q") and f.name.endswith(".md")
                           for f in os.scandir(session)):
                        papers.append(Paper(session, board.name, qualification.name,
                                            subject.name, dir_mtime=st.st_mtime_ns))
    return papers, tuple(stamp)


def paper_id_for(path: Path, board: str, qualification: str, subject: str) -> str:
    """卷的唯一 ID = `<board>-<qual>-<subject>-<会话目录名>`。

    原先直接用会话目录名，CIE 物理（qp11–qp43）与化学（qp11–qp54）共用卷号命名，
    189 卷同名 → 索引主键冲突、find_paper 取错科、学科隔离失效（2026-10-18 实测）。
    格式与 qid（board-qual-subject-session-qNN）对齐，只是少了末尾题号。
    """
    return f"{board}-{qualification}-{subject}-{path.name}"


class Paper:
    """一卷。`qfiles` 是懒加载 property：Step 1 之后列表页只查索引，不碰目录。

    注意 qfiles 是 property，不要赋值；要让它重新 glob 就调 invalidate()。
    """

    def __init__(self, path: Path, board: str, qualification: str, subject: str,
                 dir_mtime: int | None = None):
        self.path = path
        self.board = board
        self.qualification = qualification
        self.subject = subject
        self.id = paper_id_for(path, board, qualification, subject)
        # 卷目录的 mtime：目录扫描时顺手 stat 到了，用来判断「这卷内容被动过」（Step 3 自愈）
        self.dir_mtime = dir_mtime
        self._qfiles: list[Path] | None = None
        self._sub_files: list[Path] | None = None

    @property
    def qfiles(self) -> list[Path]:
        if self._qfiles is None:
            self._qfiles = sorted(
                (p for p in self.path.glob("q*.md") if MAIN_Q_RE.match(p.name)),
                key=lambda p: p.name,
            )
        return self._qfiles

    def all_sub_files(self) -> list[Path]:
        """本卷全部子题文件（q04-a.md …），一次 glob 后缓存。"""
        if self._sub_files is None:
            self._sub_files = sorted(
                p for p in self.path.glob("q*-*.md") if SUB_Q_RE.match(p.name)
            )
        return self._sub_files

    def invalidate(self) -> None:
        """本进程增删过本卷的文件后调用。"""
        self._sub_files = None
        self._qfiles = None

    def sub_files(self, qfile: Path) -> list[Path]:
        """主题文件对应的子题文件（q04.md → q04-a.md …），按文件名序。"""
        stem = qfile.stem
        return [p for p in self.all_sub_files() if p.stem.split("-", 1)[0] == stem]

    def sub_metas(self, qfile: Path) -> list[dict[str, Any]]:
        return [m for m in (read_meta(p) for p in self.sub_files(qfile)) if m]

    def load_meta(self) -> dict[str, Any]:
        return read_meta(self.qfiles[0])

    def unit(self) -> str:
        meta = self.load_meta()
        return unit_from_paper_code(str(meta["paper_code"]), str(meta.get("subject") or ""))

    def _ensure_indexed(self) -> dict[str, Any] | None:
        """确保本卷在索引里且未过期。索引不可用/重灌失败时返回 None（调用方回退扫文件）。"""
        if not index_db.exists():
            return None
        # 全量重建进行中（启动那 10 来秒）：别逐卷抢写锁，先走文件扫描
        if index_db.is_building():
            return None
        row = index_db.paper(self.id)
        if row is None or row.get("stale"):
            try:
                index_db.rebuild_paper(self.path, self.board, self.qualification, self.subject)
                row = index_db.paper(self.id)
            except Exception as e:  # noqa: BLE001  索引坏了不能让列表页 500
                print(f"[index] 重灌 {self.id} 失败，回退扫文件：{e}", flush=True)
                return None
        return row

    def summary(self) -> dict[str, Any]:
        """卷摘要。优先查索引（不读任何 md），索引不可用时回退现场扫描。"""
        row = self._ensure_indexed()
        if row is not None:
            return {
                "id": self.id,
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
        return self._summary_from_files()

    def _summary_from_files(self) -> dict[str, Any]:
        meta = self.load_meta()
        statuses = [compute_status(read_meta(qf), self.sub_metas(qf)) for qf in self.qfiles]
        return {
            "id": self.id,
            "board": self.board,
            "qualification": self.qualification,
            "subject": self.subject,
            "paper_code": meta.get("paper_code"),
            "year": meta.get("year"),
            "session": meta.get("session"),
            "unit": unit_from_paper_code(str(meta["paper_code"]), str(meta.get("subject") or "")),
            "question_count": len(self.qfiles),
            "annotated_count": statuses.count("done"),
            "source": meta.get("source"),
        }

    def questions(self) -> list[dict[str, Any]]:
        """本卷题目列表。优先查索引，索引不可用（或该卷被写操作标脏）时回退现场扫描。"""
        row = self._ensure_indexed()
        if row is not None:
            rows = index_db.questions(self.id)
            if rows:
                return _questions_from_rows(rows)
        return self._questions_from_files()

    def _questions_from_files(self) -> list[dict[str, Any]]:
        out = []
        for qf in self.qfiles:
            meta = read_meta(qf)
            is_split = bool(meta.get("split"))
            subs = self.sub_metas(qf) if is_split else []
            tags = [] if is_split else (meta.get("tags") or [])
            difficulty = None if is_split else (
                meta.get("difficulty") if meta.get("difficulty") in _DONE_DIFFICULTIES else None
            )
            # 「单元是否齐备」——预览列表判据（docs/13 + plans/主标签实施方案 §2）：切分题按小问聚合
            if is_split:
                tags_ready = bool(subs) and all((m.get("tags") or []) for m in subs)
                difficulty_ready = bool(subs) and all(
                    m.get("difficulty") in _DONE_DIFFICULTIES for m in subs
                )
                any_tagged = any((m.get("tags") or []) for m in subs)
                # 切分题主文件不带 tags/主标签，逐问落在子题文件里
                primary = None
                primary_ok = bool(subs) and all(
                    primary_ready(m.get("tags") or [], m.get("primary_tag")) for m in subs
                )
            else:
                tags_ready = bool(tags)
                difficulty_ready = difficulty is not None
                any_tagged = bool(tags)
                primary = effective_primary(tags, meta.get("primary_tag"))
                primary_ok = primary_ready(tags, meta.get("primary_tag"))
            out.append(
                {
                    "qid": meta["id"],
                    "question_number": str(meta.get("question_number", qf.stem)),
                    "status": compute_status(meta, subs),
                    "total_marks": meta.get("total_marks"),
                    "question_type": meta.get("question_type"),
                    "tags": tags,
                    "primary_tag": primary,
                    "difficulty": difficulty,
                    "tags_ready": tags_ready,
                    "difficulty_ready": difficulty_ready,
                    "primary_ready": primary_ok,
                    "any_tagged": any_tagged,
                    "split": is_split,
                    "sub_labels": [str(m.get("sub_label")) for m in subs] or (
                        [str(x) for x in meta.get("subquestions")] if is_split else []
                    ),
                    # 子题文件真实 id —— 审核确认聚合必须用它，不能由 label 拼
                    # （Edexcel 旧格式是 `...-q15a`，与 `{qid}-{label}` 不同）
                    "sub_ids": [str(m.get("id")) for m in subs],
                }
            )
        return out


def _questions_from_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把索引行还原成原先 `Paper.questions()` 的返回结构（字段一个不差）。

    rows 含该卷全部行（主题 + 子题），这里按 parent_qid 分组，只输出主题级条目。
    「齐备」三个判据（tags_ready / difficulty_ready / primary_ready）沿用原逻辑：
    切分题按小问聚合，非切分题看自身。
    """
    subs_by: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        if r.get("parent_qid"):
            subs_by.setdefault(str(r["parent_qid"]), []).append(r)

    out: list[dict[str, Any]] = []
    for r in rows:
        if r.get("parent_qid"):
            continue
        is_split = bool(r.get("split"))
        subs = subs_by.get(str(r["qid"]), []) if is_split else []
        tags = [] if is_split else (r.get("tags") or [])
        difficulty = None if is_split else r.get("difficulty")

        if is_split:
            tags_ready = bool(subs) and all((s.get("tags") or []) for s in subs)
            difficulty_ready = bool(subs) and all(s.get("difficulty") for s in subs)
            # 任意一个小问有标签就算「打过标」：预览模式据此把题放进列表（2026-10-18 起）
            any_tagged = any((s.get("tags") or []) for s in subs)
            # 切分题主文件不带 tags/主标签，逐问落在子题文件里
            primary = None
            primary_ok = bool(subs) and all(
                primary_ready(s.get("tags") or [], s.get("primary_tag")) for s in subs
            )
        else:
            tags_ready = bool(tags)
            difficulty_ready = difficulty is not None
            any_tagged = bool(tags)
            primary = r.get("primary_tag")
            primary_ok = primary_ready(tags, primary)

        out.append(
            {
                "qid": r["qid"],
                "question_number": r.get("qnum"),
                "status": r.get("status"),
                "total_marks": r.get("total_marks"),
                "question_type": r.get("question_type"),
                "tags": tags,
                "primary_tag": primary,
                "difficulty": difficulty,
                "tags_ready": tags_ready,
                "difficulty_ready": difficulty_ready,
                "primary_ready": primary_ok,
                "any_tagged": any_tagged,
                "split": is_split,
                "sub_labels": [str(s.get("sub_label")) for s in subs],
                # 子题真实 id —— 审核确认聚合必须用它，不能由 label 拼
                "sub_ids": [str(s["qid"]) for s in subs],
            }
        )
    return out


def _sync_index(papers: list[Paper]) -> None:
    """把「磁盘上的卷」与「索引里的卷」对齐（Step 3 自愈，默认每 5 秒随目录扫描跑一次）。

    三种情况分别处理：
      · 目录 mtime 变了 → 标 stale，下次读**只重灌这一卷**（题包重传、切割脚本改文件都走这条）；
      · 磁盘上已经没有的卷 → 从索引删除（否则列表会出现幽灵卷）；
      · 磁盘上有、索引里没有的新卷 → 不在这里处理，读到时 `_ensure_indexed` 自动重建。

    只比 stat，不解析任何 md —— 1500 卷也就几毫秒。
    """
    if not index_db.exists() or not papers:
        return
    try:
        known = index_db.paper_mtimes()
        live = {p.id: p.dir_mtime for p in papers}
        for pid, mt in live.items():
            if pid in known and known[pid] != mt:
                index_db.invalidate(pid)
        for pid in set(known) - set(live):
            index_db.drop_paper(pid)
    except Exception as e:  # noqa: BLE001  对账失败不能让读路径挂掉
        print(f"[index] 扫描对账失败（不影响读）：{e}", flush=True)


def iter_papers() -> list[Paper]:
    """全部试卷。

    卷列表仍以**目录扫描**为准（保住「上传新卷不用重启、几秒内自动出现」的能力），
    但扫描结果缓存 _SCAN_TTL 秒；每卷的题目明细一律走 index.db，不再解析 md。
    每次真正扫描时会顺带与索引对账（`_sync_index`），实现「改了文件自动失效」。
    """
    global _papers, _papers_stamp, _papers_by_id, _scan_at, _papers_gen
    now = time.monotonic()
    gen = index_db.generation()
    with _lock:
        if _papers is not None and _papers_gen == gen and (now - _scan_at) < _SCAN_TTL:
            return _papers
    papers, stamp = _scan_papers()
    _sync_index(papers)
    gen = index_db.generation()
    with _lock:
        _papers = papers
        _papers_stamp = stamp
        _papers_by_id = {p.id: p for p in papers}
        _scan_at = time.monotonic()
        _papers_gen = gen
        return papers


def find_paper(paper_id: str) -> Paper | None:
    iter_papers()
    with _lock:
        hit = _papers_by_id.get(paper_id)
    if hit is not None:
        return hit
    # 索引里有、但目录扫描还没轮到（刚上传 / TTL 内新增）→ 直接按索引行构造
    return _paper_from_index(paper_id)


def _paper_from_index(paper_id: str) -> Paper | None:
    if not index_db.exists():
        return None
    row = index_db.paper(paper_id)
    if row is None or not row.get("dir_rel"):
        return None
    p = Paper(DATA_DIR / str(row["dir_rel"]), row["board"], row["qualification"], row["subject"])
    with _lock:
        _papers_by_id[paper_id] = p
    return p


def _rebuild_qid_index() -> None:
    """全量重建 qid → (卷, 文件名)。仅在首次请求或索引过期时执行。"""
    papers = iter_papers()
    idx: dict[str, tuple[str, str]] = {}
    for p in papers:
        for qf in p.qfiles:
            qid = read_meta(qf).get("id")
            if qid:
                idx[str(qid)] = (p.id, qf.name)
        for sf in p.all_sub_files():  # 子题文件（split 产物，不在 qfiles 里）
            qid = read_meta(sf).get("id")
            if qid:
                idx[str(qid)] = (p.id, sf.name)
    with _lock:
        _qid_index.clear()
        _qid_index.update(idx)


def find_question(qid: str) -> tuple[Paper, Path] | None:
    """按 qid 取 (卷, 文件路径)。

    Step 1 起走 index.db 主键查询（微秒级），不再需要全量 qid 索引；
    索引里没有时回退旧的全量扫描（刚新增、还没进索引的文件）。
    """
    key = str(qid)
    if index_db.exists():
        loc = index_db.find_qid(key)
        if loc is not None:
            paper_id, fname = loc
            paper = find_paper(paper_id) or _paper_from_index(paper_id)
            if paper is not None:
                qpath = paper.path / fname
                if qpath.exists():
                    return paper, qpath

    # 回退：全量扫一遍（首次或索引缺失时才走到这里）
    with _lock:
        empty = not _qid_index
    if empty:
        _rebuild_qid_index()
    for attempt in (0, 1):
        with _lock:
            loc = _qid_index.get(key)
            paper = _papers_by_id.get(loc[0]) if loc else None
        if loc is not None and paper is not None:
            qpath = paper.path / loc[1]
            # 关键：用 front-matter 的 id 复核，外部改名/换文件也拦得住
            if qpath.exists() and read_meta(qpath).get("id") == key:
                return paper, qpath
        if attempt == 0:  # 索引过期（外部增删/改名）→ 重建一次
            _rebuild_qid_index()
    return None


def warm() -> None:
    """启动预热。

    Step 1 起的行为变了：
      · 索引存在且有数据 → 只打开它（毫秒级），**不再全量解析 md**；
      · 索引不存在/为空 → 现场建一次（410 卷约 12s；10 万题约 2.5 分钟，只发生一次）。
    索引是派生数据，删掉就会自动重建，所以「构建失败」只影响速度不影响正确性。
    """
    t0 = time.perf_counter()
    if index_db.exists():
        try:
            st = index_db.stats()
            if st["papers"] > 0:
                n = len(iter_papers())
                print(
                    f"[warmup] 索引已就绪：{st['papers']} 卷 / {st['questions']} 题"
                    f"（{st['bytes'] / 1024 / 1024:.1f} MB），{n} 卷已装载，"
                    f"耗时 {time.perf_counter() - t0:.2f}s（未解析 md）",
                    flush=True,
                )
                return
        except Exception as e:  # noqa: BLE001  索引坏了就重建
            print(f"[warmup] 索引读取失败，准备重建：{e}", flush=True)

    print("[warmup] 索引不存在或为空，开始全量构建…", flush=True)
    res = index_db.rebuild()
    papers = iter_papers()
    print(
        f"[warmup] 索引构建完成：{res['papers']} 卷 / {res['rows']} 行，"
        f"装载 {len(papers)} 卷，耗时 {time.perf_counter() - t0:.2f}s",
        flush=True,
    )


def question_image_url(paper: Paper, meta: dict[str, Any]) -> str | None:
    image = meta.get("image")
    if not image:
        return None
    # URL 里的卷目录段必须是磁盘真实目录名（paper.path.name）。
    # 不能用 paper.id —— 那是索引主键（board-qual-subject-<目录名>，2026-10-18 为区分
    # CIE 物理/化学同名卷而改）。/files 是 StaticFiles 直接挂在 DATA_DIR 上、按 URL 原样
    # 找文件，带前缀就 404：2026-10-18 23:04 部署后全站题图打不开，原因即此。
    return (
        f"/files/{paper.board}/{paper.qualification}/{paper.subject}"
        f"/{paper.path.name}/{image}"
    )


# ---------------------------------------------------------------------------
# 写回：外科手术式只动 tags / difficulty / split 标记，front-matter 其余行与正文一字不动
# ---------------------------------------------------------------------------

def _strip_annotation_lines(fm_lines: list[str]) -> list[str]:
    out: list[str] = []
    in_tags_block = False
    for line in fm_lines:
        if in_tags_block:
            if re.match(r"^[ \t]+-[ \t]", line):
                continue  # 旧 tags 列表项，丢弃
            in_tags_block = False
        if re.match(r"^tags:[ \t]*$", line):
            in_tags_block = True
            continue
        if re.match(r"^tags:[ \t]*\[", line):
            # 内联数组形式 tags: [...]；空数组整行丢弃，非空保留原样（理论不出现）
            inner = line.split(":", 1)[1].strip()
            if inner and inner != "[]":
                out.append(line)
            continue
        if re.match(r"^difficulty:[ \t]*", line):
            continue
        if re.match(r"^primary_tag:[ \t]*", line):
            # 主标签（plans/主标签实施方案 §2）：与 tags 同生共死——剥掉旧行，由
            # _build_annotation_lines 按新 tags 重写，否则关切分/降级后会残留悬空主标签
            continue
        if re.match(r"^split:[ \t]*", line):
            continue
        if re.match(r"^subquestions:[ \t]*", line):
            continue
        out.append(line)
    return out


def _build_annotation_lines(tags: list[str], difficulty: str | None,
                            primary_tag: str | None = None) -> list[str]:
    """难度可留空（docs/13）：为 None 时整行不写，而不是写 `difficulty: None`。
    主标签（plans/主标签实施方案 §2.1）：None 时整键不写（只有 tags 为空才会出现）。
    行序遵循全库约定：tags: 块 → primary_tag: → difficulty:。

    空 tags 必须写成**内联空数组** `tags: []`：裸 `tags:` 会被 YAML 解析成 None，
    撞 schema（tags 必须是 array）→ 写后差分校验抛 ValueError，调用方（脚本、
    未来的「撤销打标」入口）拿到的是 500 而不是一次正常的清空。库内既有的空文件
    也都是 `tags: []`，这里与之同形。
    """
    if not tags:
        return ["tags: []"]
    lines = ["tags:"]
    lines += [f"  - {t}" for t in tags]
    if primary_tag is not None:
        lines.append(f"primary_tag: {primary_tag}")
    if difficulty is not None:
        lines.append(f"difficulty: {difficulty}")
    return lines


def _schema_errors(meta: dict[str, Any]) -> set[str]:
    return {
        f"{list(e.path)}: {e.message}" for e in _validator().iter_errors(meta)
    }


def write_annotation(qpath: Path, tags: list[str], difficulty: str | None,
                     primary_tag: str | None = None) -> dict[str, Any]:
    text = qpath.read_text(encoding="utf-8")
    meta, m = parse_front_matter(text)
    if m is None:
        raise ValueError(f"{qpath.name}: 缺少 front-matter")

    fm_lines = m.group(1).splitlines()
    fm_lines = _strip_annotation_lines(fm_lines)
    fm_lines += _build_annotation_lines(tags, difficulty, normalize_primary(tags, primary_tag))

    new_text = "---\n" + "\n".join(fm_lines) + "\n---\n" + text[m.end():]

    # 写后差分校验：只拦截本次写盘「新引入」的 schema 错误。
    # 存量数据可能本就有历史违规（如 question_number 为整数），不由写盘背责。
    new_meta, _ = parse_front_matter(new_text)
    new_errors = _schema_errors(new_meta) - _schema_errors(meta)
    if new_errors:
        raise ValueError("写回引入 schema 错误: " + "; ".join(sorted(new_errors)[:3]))

    # 原子写：临时文件 + os.replace。
    # 必须走 _atomic_write（它负责通知索引失效）——这里曾经内联过一份，
    # 导致「改了标注但列表页不刷新」，2026-10-18 端到端测试时抓到。
    _atomic_write(qpath, new_text)

    _drop_meta(qpath)
    new_tags = new_meta.get("tags") or []
    return {
        "qid": new_meta["id"],
        "tags": new_tags,
        "primary_tag": effective_primary(new_tags, new_meta.get("primary_tag")),
        "difficulty": new_meta.get("difficulty"),
        "status": compute_status(new_meta),
    }


def write_difficulty(qpath: Path, difficulty: str | None) -> dict[str, Any]:
    """只增删改 front-matter 的 `difficulty:` 一行，**不碰 tags 与其余任何行**。

    预览模式「就地补难度」（docs/13）走这里——把写库口子收到单字段：
    - `difficulty=None` → 删掉该行；
    - 其余照旧（原子写 + schema 差分校验）。
    对整题文件与子题文件（split 产物）同样适用。
    """
    text = qpath.read_text(encoding="utf-8")
    meta, m = parse_front_matter(text)
    if m is None:
        raise ValueError(f"{qpath.name}: 缺少 front-matter")

    fm_lines = [
        line for line in m.group(1).splitlines()
        if not re.match(r"^difficulty:[ \t]*", line)
    ]
    if difficulty is not None:
        fm_lines.append(f"difficulty: {difficulty}")

    new_text = "---\n" + "\n".join(fm_lines) + "\n---\n" + text[m.end():]
    new_meta, _ = parse_front_matter(new_text)
    new_errors = _schema_errors(new_meta) - _schema_errors(meta)
    if new_errors:
        raise ValueError("写回引入 schema 错误: " + "; ".join(sorted(new_errors)[:3]))

    _atomic_write(qpath, new_text)
    _drop_meta(qpath)
    new_tags = new_meta.get("tags") or []
    return {
        "qid": new_meta["id"],
        "tags": new_tags,
        "primary_tag": effective_primary(new_tags, new_meta.get("primary_tag")),
        "difficulty": new_meta.get("difficulty"),
        "status": compute_status(new_meta),
    }


# ---------------------------------------------------------------------------
# split（按小问切分打标）：主文件打标记、子题文件整文件写入、关闭时清理
# ---------------------------------------------------------------------------

def _paper_id_of_file(qpath: Path) -> str | None:
    """由题目文件的磁盘位置反推卷 id：DATA_DIR/<board>/<qual>/<subject>/<session>/<file>。

    `index.db` 的主键是 `board-qual-subject-<会话目录名>`，**不是**会话目录名本身
    （2026-10-18 为区分 CIE 物理/化学的同名卷 qp1x 改的）。写盘后标脏必须用这个
    完整 id，否则 UPDATE 命中 0 行、索引不失效，列表页会继续显示旧状态。
    结构不符（文件不在 data/ 下）时返回 None，由调用方兜底。
    """
    try:
        rel = qpath.parent.relative_to(DATA_DIR)
    except (ValueError, OSError):
        return None
    parts = rel.parts
    if len(parts) < 4:  # board / qual / subject / session
        return None
    return paper_id_for(qpath.parent, parts[0], parts[1], parts[2])


def _atomic_write(qpath: Path, text: str) -> None:
    fd, tmp_name = tempfile.mkstemp(dir=str(qpath.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        os.replace(tmp_name, qpath)
    except BaseException:
        if os.path.exists(tmp_name):
            os.remove(tmp_name)
        raise
    # 索引失效：所有写 md 的路径都经过这里（改标注/难度/切分），
    # 标记该卷 stale，下次读时现场重灌（约 0.13s），列表页不会显示旧状态。
    # ⚠️ 必须传完整 paper_id：这里曾经传 `qpath.parent.name`（只有会话目录名），
    #    UPDATE 命中 0 行 = 静默不生效，全靠 5 秒后的目录 mtime 扫描兜底（2026-10-18 体检发现）。
    if index_db.exists():
        pid = _paper_id_of_file(qpath)
        try:
            if pid is None:
                print(f"[index] {qpath} 不在 data/ 下，跳过标脏", flush=True)
            else:
                index_db.invalidate(pid)
        except Exception as e:  # noqa: BLE001  索引失效失败不该让写操作失败
            print(f"[index] 标记 {pid or qpath.parent.name} 过期失败：{e}", flush=True)


def build_sub_meta(main_meta: dict[str, Any], label: str,
                   image: str | None, layout: str | None) -> dict[str, Any]:
    """子题元数据。image/layout 为 None 时（AQA/CIE 不切子图）随后的过滤会省略这两键。"""
    m = {
        # id 用 slug：leaf 粒度的 label 带空格（"c i"），schema 不允许
        "id": f"{main_meta['id']}-{sub_slug(label)}",
        "parent": main_meta["id"],
        "board": main_meta["board"],
        "qualification": main_meta["qualification"],
        "subject": main_meta["subject"],
        "paper_code": main_meta.get("paper_code"),
        "year": main_meta.get("year"),
        "session": main_meta.get("session"),
        "question_number": f"{main_meta['question_number']}.{label}",
        "question_type": main_meta.get("question_type"),
        "sub_label": label,
        "image": image,
        "layout": layout,
        "source": main_meta.get("source"),
        "cut_status": "split",
        "language": main_meta.get("language", "en"),
    }
    return {k: v for k, v in m.items() if v is not None}


def _yaml_dump_meta(meta: dict[str, Any]) -> str:
    """子题文件 front-matter：标量直排，tags 列表块式（与主文件风格一致）。"""
    lines = []
    for k, v in meta.items():
        if k == "tags":
            continue
        if v is None:
            continue  # 难度可留空（docs/13）：None 整键省略，别写成 "k: None"
        # 纯数字样字符串也要加引号：sub_label "1" / question_number "1.1" 若直排，
        # YAML 会读成 int/float，与 cut.json 侧类型不一致（打标模式按 label 配对小问会整片失配）。
        if isinstance(v, str) and (":" in v or v.strip() == "" or v != v.strip()
                                   or re.fullmatch(r"\d+(?:\.\d+)?", v)):
            lines.append(f"{k}: {json.dumps(v, ensure_ascii=False)}")
        else:
            lines.append(f"{k}: {v}")
    tags = meta.get("tags") or []
    if not tags:
        # 同 _build_annotation_lines：裸 `tags:` 解析成 None，落盘即 schema 非法
        lines.append("tags: []")
    else:
        lines.append("tags:")
        lines += [f"  - {t}" for t in tags]
    return "---\n" + "\n".join(lines) + "\n---\n"


def save_split(paper: Paper, qpath: Path, subs: list[dict[str, Any]],
               render_results: list[dict[str, Any]]) -> dict[str, Any]:
    """写入子题文件 + 主文件打 split 标记。subs=[{label,tags,difficulty}]，
    render_results=render_split 的 results（label→image/layout 映射）。"""
    text = qpath.read_text(encoding="utf-8")
    meta, m = parse_front_matter(text)
    if m is None:
        raise ValueError(f"{qpath.name}: 缺少 front-matter")
    img_map = {r["label"]: r for r in render_results}

    # 先写子题文件（幂等覆盖），最后写主文件标记
    # 只记「本次新建」的路径：回滚时只能删它们。被覆盖的旧文件在 _atomic_write 里
    # 已经是「旧内容已被新内容替换」，删掉等于把历史标注一起删干净（2026-10-19 修）。
    created: list[Path] = []
    try:
        for s in subs:
            r = img_map[s["label"]]
            sub_meta = build_sub_meta(meta, s["label"], r["image"], r["layout"])
            sub_meta["tags"] = s["tags"]
            # 主标签（plans/主标签实施方案 §2.1）：与 tags 一起落进子题文件；
            # None（只可能出现在 tags 为空时）整键不写
            pri = normalize_primary(s["tags"], s.get("primary_tag"))
            if pri is not None:
                sub_meta["primary_tag"] = pri
            if s.get("difficulty") is not None:  # 难度可留空（docs/13）
                sub_meta["difficulty"] = s["difficulty"]
            # schema 差分校验：子题字段继承自主文件，主文件既有的历史违规不该由子题背责
            # （典型：CIE 全库用 qualification: alevel，而 schema enum 只有 a-level；
            #  主文件靠 write_annotation 的差分校验逃过，子题是「新文件」若走全量校验就会
            #  一律 500 → CIE 卷根本没法按小问拆分）。子题独有字段（sub_label/parent/…）仍照查。
            errors = _schema_errors(sub_meta) - _schema_errors(meta)
            if errors:
                raise ValueError(f"子题 {s['label']} schema 错误: " + "; ".join(sorted(errors)[:3]))
            sub_text = _yaml_dump_meta(sub_meta)
            # 落盘前再核一次「序列化结果」：内存里的 dict 合法，不等于写出来的 YAML 合法
            # （典型：空 tags 写成裸 `tags:` 会被解析成 None）。主文件那条差分校验只看得见
            # 传入的 dict，看不见序列化器自己引入的错误，所以这里补一道。
            dumped, _ = parse_front_matter(sub_text)
            dumped_errors = _schema_errors(dumped) - _schema_errors(meta)
            if dumped_errors:
                raise ValueError(
                    f"子题 {s['label']} 序列化后 schema 错误: "
                    + "; ".join(sorted(dumped_errors)[:3])
                )
            spath = qpath.parent / f"{qpath.stem}-{sub_slug(s['label'])}.md"
            if not spath.exists():
                created.append(spath)
            _atomic_write(spath, sub_text + "\n")
            _drop_meta(spath)
            _index_put(sub_meta["id"], paper.id, spath.name)

        # 主文件：剥掉 tags/difficulty/split/subquestions，追加 split 标记。
        # subquestions 记「实际存在的子题文件」而非本次提交的——打标台「保存，下一问」
        # 是增量保存（可能只提交一部分），若照提交写，前端角标会与 sub_metas 对不上。
        paper.invalidate()  # 让 sub_files 看到刚写入的子文件
        all_labels = [str(read_meta(p).get("sub_label") or "") for p in paper.sub_files(qpath)]
        all_labels = [l for l in all_labels if l] or [s["label"] for s in subs]
        fm_lines = _strip_annotation_lines(m.group(1).splitlines())
        fm_lines.append("split: per_subquestion")
        fm_lines.append("subquestions: [" + ", ".join(all_labels) + "]")
        new_text = "---\n" + "\n".join(fm_lines) + "\n---\n" + text[m.end():]
        new_meta, _ = parse_front_matter(new_text)
        new_errors = _schema_errors(new_meta) - _schema_errors(meta)
        if new_errors:
            raise ValueError("写回引入 schema 错误: " + "; ".join(sorted(new_errors)[:3]))
        _atomic_write(qpath, new_text)
    except BaseException:
        # 回滚只删**本次新建**的子题文件（主文件未动）。
        # ⚠️ 不能把「被覆盖过的旧文件」也删掉：那等于连历史标注一起毁。
        for p in created:
            if p.exists():
                p.unlink()
                _drop_meta(p)
        paper.invalidate()  # 回滚后子题集合也变了
        raise
    _drop_meta(qpath)
    paper.invalidate()  # 子题文件集合变了
    sub_infos = []
    for p in paper.sub_files(qpath):
        sm, _ = parse_front_matter(p.read_text(encoding="utf-8"))
        if sm:
            sub_infos.append({
                "qid": sm.get("id"),
                # 契约统一为 str：YAML 读回的 sub_label 可能是 int，前端按 label 配对小问会失配
                "label": str(sm.get("sub_label")),
                "tags": sm.get("tags") or [],
                "primary_tag": effective_primary(sm.get("tags") or [], sm.get("primary_tag")),
                "difficulty": sm.get("difficulty"),
                "status": compute_status(sm),
            })
    # 状态必须按「已带 split 标记」的 meta 推导：首次切分时写盘前的 meta 无 split 键，
    # 直接用它会把切分题算成 none（不写这一句，前端要刷新才变绿 + 卷进度不涨）。
    effective = dict(meta)
    effective["split"] = "per_subquestion"
    return {
        "qid": meta["id"],
        "tags": [],
        "primary_tag": None,  # 切分题主文件不带 tags/主标签，逐问落在子题文件里
        "difficulty": None,
        "split": True,
        "sub_labels": [i["label"] for i in sub_infos],
        "subs": sub_infos,
        "status": compute_status(effective, paper.sub_metas(qpath)),
    }


def cleanup_subs(paper: Paper, qpath: Path) -> list[str]:
    """删除某主题题目的全部子题文件与其派生图/layout（关闭开关重存时调用）。"""
    removed: list[str] = []
    for spath in paper.sub_files(qpath):
        sm = read_meta(spath)
        for key in ("image", "layout"):
            rel = sm.get(key)
            if rel:
                f = paper.path / rel
                if f.exists():
                    f.unlink()
                    removed.append(str(f.relative_to(paper.path)))
        _index_drop(sm.get("id"))
        _drop_meta(spath)
        removed.append(spath.name)
        spath.unlink()
    paper.invalidate()
    # 删文件不走 _atomic_write，这里单独让索引失效
    if removed and index_db.exists():
        try:
            index_db.invalidate(paper.id)
        except Exception as e:  # noqa: BLE001
            print(f"[index] 标记 {paper.id} 过期失败：{e}", flush=True)
    return removed


def cut_question(paper: Paper, qnum: Any) -> dict[str, Any] | None:
    """读 cut.json 中对应题的切割数据（无 cut.json 或无此题 → None）。"""
    cp = paper.path / "cut.json"
    if not cp.exists():
        return None
    try:
        cut = json.loads(cp.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    for q in cut.get("questions", []):
        if str(q.get("number")) == str(qnum):
            return q
    return None
