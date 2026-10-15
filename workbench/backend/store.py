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

REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = REPO_ROOT / "data"
SCHEMA_PATH = REPO_ROOT / "schema" / "question.schema.json"

FM_RE = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*\r?\n", re.DOTALL)
_DONE_DIFFICULTIES = {"very-easy", "easy", "medium", "hard", "very-hard"}
MAIN_Q_RE = re.compile(r"^q\d+\.md$")       # 主题文件 q04.md
SUB_Q_RE = re.compile(r"^q\d+-[a-z]+\.md$")  # 子题文件 q04-a.md（split 产物）

_schema_validator: Draft202012Validator | None = None


def _validator() -> Draft202012Validator:
    global _schema_validator
    if _schema_validator is None:
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        _schema_validator = Draft202012Validator(schema)
    return _schema_validator


def unit_from_paper_code(code: str) -> str:
    """WPH11 -> U1, WPH14A -> U4。取卷号中 '1' 后跟的那一位数字。"""
    m = re.search(r"1(\d)", code)
    if not m:
        raise ValueError(f"无法从卷号推导单元: {code!r}")
    return f"U{m.group(1)}"


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
    diff = meta.get("difficulty")
    has_tags = isinstance(tags, list) and len(tags) > 0
    has_diff = diff in _DONE_DIFFICULTIES
    if has_tags and has_diff:
        return "done"
    if has_tags or has_diff:
        return "partial"
    return "none"


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
    for board in sorted(p for p in DATA_DIR.iterdir() if p.is_dir() and p.name != "taxonomy"):
        stamp.append((board.name, board.stat().st_mtime_ns))
        for qualification in sorted(p for p in board.iterdir() if p.is_dir()):
            qkey = f"{board.name}/{qualification.name}"
            stamp.append((qkey, qualification.stat().st_mtime_ns))
            for subject in sorted(p for p in qualification.iterdir() if p.is_dir()):
                skey = f"{qkey}/{subject.name}"
                stamp.append((skey, subject.stat().st_mtime_ns))
                for session in sorted(p for p in subject.iterdir() if p.is_dir()):
                    stamp.append((f"{skey}/{session.name}", session.stat().st_mtime_ns))
                    if list(session.glob("q*.md")):
                        papers.append(Paper(session, board.name, qualification.name, subject.name))
    return papers, tuple(stamp)


class Paper:
    def __init__(self, path: Path, board: str, qualification: str, subject: str):
        self.path = path
        self.board = board
        self.qualification = qualification
        self.subject = subject
        self.id = path.name
        self.qfiles = sorted(
            (p for p in path.glob("q*.md") if MAIN_Q_RE.match(p.name)), key=lambda p: p.name
        )
        self._sub_files: list[Path] | None = None

    def all_sub_files(self) -> list[Path]:
        """本卷全部子题文件（q04-a.md …），一次 glob 后缓存。"""
        if self._sub_files is None:
            self._sub_files = sorted(
                p for p in self.path.glob("q*-*.md") if SUB_Q_RE.match(p.name)
            )
        return self._sub_files

    def invalidate(self) -> None:
        """本进程增删过本卷的子题文件后调用。"""
        self._sub_files = None

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
        return unit_from_paper_code(str(meta["paper_code"]))

    def summary(self) -> dict[str, Any]:
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
            "unit": unit_from_paper_code(str(meta["paper_code"])),
            "question_count": len(self.qfiles),
            "annotated_count": statuses.count("done"),
            "source": meta.get("source"),
        }

    def questions(self) -> list[dict[str, Any]]:
        out = []
        for qf in self.qfiles:
            meta = read_meta(qf)
            subs = self.sub_metas(qf) if meta.get("split") else []
            is_split = bool(meta.get("split"))
            out.append(
                {
                    "qid": meta["id"],
                    "question_number": str(meta.get("question_number", qf.stem)),
                    "status": compute_status(meta, subs),
                    "total_marks": meta.get("total_marks"),
                    "question_type": meta.get("question_type"),
                    "tags": [] if is_split else (meta.get("tags") or []),
                    "difficulty": None if is_split else (
                        meta.get("difficulty") if meta.get("difficulty") in _DONE_DIFFICULTIES else None
                    ),
                    "split": is_split,
                    "sub_labels": [str(m.get("sub_label")) for m in subs] or (
                        [str(x) for x in meta.get("subquestions")] if is_split else []
                    ),
                }
            )
        return out


def iter_papers() -> list[Paper]:
    """全部试卷。目录 mtime 快照未变时直接复用缓存对象（连带保住子题 glob 缓存）。"""
    global _papers, _papers_stamp, _papers_by_id
    papers, stamp = _scan_papers()
    with _lock:
        if _papers is not None and stamp == _papers_stamp:
            return _papers
        _papers = papers
        _papers_stamp = stamp
        _papers_by_id = {p.id: p for p in papers}
        return _papers


def find_paper(paper_id: str) -> Paper | None:
    iter_papers()
    with _lock:
        return _papers_by_id.get(paper_id)


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
    """按 qid 取 (卷, 文件路径)。命中索引时只做 1 次 stat，不再扫全库。"""
    key = str(qid)
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
    """预热索引：建元数据缓存 + qid 索引。

    `main.py` 在进程启动时用后台线程调用，把首次请求才付的全库扫描
    （142 卷 / 2039 个文件，≈2.7s）挪出用户的关键路径。
    也可以手动跑；不预热不影响正确性，索引会按需构建。
    """
    t0 = time.perf_counter()
    print("[warmup] 开始预热索引…", flush=True)
    papers = iter_papers()
    for p in papers:
        p.summary()
    _rebuild_qid_index()
    print(
        f"[warmup] 预热完成：{len(papers)} 卷 / {len(_qid_index)} 题，"
        f"耗时 {time.perf_counter() - t0:.2f}s",
        flush=True,
    )


def question_image_url(paper: Paper, meta: dict[str, Any]) -> str | None:
    image = meta.get("image")
    if not image:
        return None
    return f"/files/{paper.board}/{paper.qualification}/{paper.subject}/{paper.id}/{image}"


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
        if re.match(r"^split:[ \t]*", line):
            continue
        if re.match(r"^subquestions:[ \t]*", line):
            continue
        out.append(line)
    return out


def _build_annotation_lines(tags: list[str], difficulty: str) -> list[str]:
    lines = ["tags:"]
    lines += [f"  - {t}" for t in tags]
    lines.append(f"difficulty: {difficulty}")
    return lines


def _schema_errors(meta: dict[str, Any]) -> set[str]:
    return {
        f"{list(e.path)}: {e.message}" for e in _validator().iter_errors(meta)
    }


def write_annotation(qpath: Path, tags: list[str], difficulty: str) -> dict[str, Any]:
    text = qpath.read_text(encoding="utf-8")
    meta, m = parse_front_matter(text)
    if m is None:
        raise ValueError(f"{qpath.name}: 缺少 front-matter")

    fm_lines = m.group(1).splitlines()
    fm_lines = _strip_annotation_lines(fm_lines)
    fm_lines += _build_annotation_lines(tags, difficulty)

    new_text = "---\n" + "\n".join(fm_lines) + "\n---\n" + text[m.end():]

    # 写后差分校验：只拦截本次写盘「新引入」的 schema 错误。
    # 存量数据可能本就有历史违规（如 question_number 为整数），不由写盘背责。
    new_meta, _ = parse_front_matter(new_text)
    new_errors = _schema_errors(new_meta) - _schema_errors(meta)
    if new_errors:
        raise ValueError("写回引入 schema 错误: " + "; ".join(sorted(new_errors)[:3]))

    # 原子写：临时文件 + os.replace
    fd, tmp_name = tempfile.mkstemp(dir=str(qpath.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(new_text)
        os.replace(tmp_name, qpath)
    except BaseException:
        if os.path.exists(tmp_name):
            os.remove(tmp_name)
        raise

    _drop_meta(qpath)
    return {
        "qid": new_meta["id"],
        "tags": new_meta.get("tags") or [],
        "difficulty": new_meta.get("difficulty"),
        "status": compute_status(new_meta),
    }


# ---------------------------------------------------------------------------
# split（按小问切分打标）：主文件打标记、子题文件整文件写入、关闭时清理
# ---------------------------------------------------------------------------

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


def build_sub_meta(main_meta: dict[str, Any], label: str, image: str, layout: str) -> dict[str, Any]:
    m = {
        "id": f"{main_meta['id']}{label}",
        "parent": main_meta["id"],
        "board": main_meta["board"],
        "qualification": main_meta["qualification"],
        "subject": main_meta["subject"],
        "paper_code": main_meta.get("paper_code"),
        "year": main_meta.get("year"),
        "session": main_meta.get("session"),
        "question_number": f"{main_meta['question_number']}{label}",
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
        if isinstance(v, str) and (":" in v or v.strip() == "" or v != v.strip()):
            lines.append(f"{k}: {json.dumps(v, ensure_ascii=False)}")
        else:
            lines.append(f"{k}: {v}")
    tags = meta.get("tags") or []
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
    written: list[Path] = []
    try:
        for s in subs:
            r = img_map[s["label"]]
            sub_meta = build_sub_meta(meta, s["label"], r["image"], r["layout"])
            sub_meta["tags"] = s["tags"]
            sub_meta["difficulty"] = s["difficulty"]
            # 写后 schema 差分校验（子文件为新文件，直接全量校验）
            errors = _schema_errors(sub_meta)
            if errors:
                raise ValueError(f"子题 {s['label']} schema 错误: " + "; ".join(sorted(errors)[:3]))
            spath = qpath.parent / f"{qpath.stem}-{s['label']}.md"
            _atomic_write(spath, _yaml_dump_meta(sub_meta) + "\n")
            _drop_meta(spath)
            _index_put(sub_meta["id"], paper.id, spath.name)
            written.append(spath)

        # 主文件：剥掉 tags/difficulty/split/subquestions，追加 split 标记
        fm_lines = _strip_annotation_lines(m.group(1).splitlines())
        labels = [s["label"] for s in subs]
        fm_lines.append("split: per_subquestion")
        fm_lines.append("subquestions: [" + ", ".join(labels) + "]")
        new_text = "---\n" + "\n".join(fm_lines) + "\n---\n" + text[m.end():]
        new_meta, _ = parse_front_matter(new_text)
        new_errors = _schema_errors(new_meta) - _schema_errors(meta)
        if new_errors:
            raise ValueError("写回引入 schema 错误: " + "; ".join(sorted(new_errors)[:3]))
        _atomic_write(qpath, new_text)
    except BaseException:
        for p in written:  # 回滚本次新写的子文件（主文件未动）
            if p.exists():
                p.unlink()
                _drop_meta(p)
        raise
    _drop_meta(qpath)
    paper.invalidate()  # 子题文件集合变了
    sub_infos = []
    for p in paper.sub_files(qpath):
        sm, _ = parse_front_matter(p.read_text(encoding="utf-8"))
        if sm:
            sub_infos.append({
                "qid": sm.get("id"),
                "label": sm.get("sub_label"),
                "tags": sm.get("tags") or [],
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
        "difficulty": None,
        "split": True,
        "sub_labels": [s["label"] for s in subs],
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
