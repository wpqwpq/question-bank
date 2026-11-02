"""标签删除：从词表移除一个标签（或一整组），并按所选方式清理已有标注。

两种删除方式（2026-11-02 培琪定，语义见 plans 与弹窗文案）：

  clear_all  所有用到被删标签的单元（整题或小问，以当时打标粒度为准）
             **清空 tags**（连其它标签一起清），难度保留；
  smart      只有被删标签「含（或本身就是）主标签」时才整清；
             否则只摘掉被删的那几个，其余标签与主标签原样保留。

判定单位 = 当时打标粒度：切分题只落在子题文件（q04-a.md…）上，主文件本来就无 tags；
未切分题落在整题文件上。所以本模块只按「一个 md 文件 = 一个单元」处理，不关心 split。

为什么不在 api.py 里直接写：这段要同时碰 store（改 md）、review（撤确认）、
flags（保住有误标记）、taxonomy（改词表），塞进 api.py 会让那个已经 900 行的文件更难读。
本模块只被 api.py import，不反向依赖。

⚠️ 落盘顺序是「先改 md、后改词表」，不能反：
   词表先删 → 万一 md 写失败，会留下一批引用着词表里不存在的标签的题，
   之后这些题在任何写入路径上都会被 _validate_tags 拒（422），只能手工救。
   反过来（md 先清、词表后删）失败时词表还在，重试是幂等的。
"""
from __future__ import annotations

import argparse
import copy
import json
import sys
import time
from typing import Any

import auth
import flags
import index_db
import review
import store
import taxonomy

MODES = ("clear_all", "smart")

# 弹窗明细上限：再多也用不着逐行看，返回 truncated 让前端提示即可。
_MAX_ROWS = 300


# ---------------------------------------------------------------------------
# 目标标签
# ---------------------------------------------------------------------------

def _resolve_target(board: str, qual: str, subject: str, unit: str,
                    tag_id: str | None = None, group_id: str | None = None) -> dict[str, Any]:
    """从词表里取出「要删的标签」。返回 {unit, kind, group, tags[]}。

    不存在一律抛 LookupError，由路由转 404 —— 不做「猜一个最像的」。
    """
    tax = taxonomy.load_taxonomy(board, qual, subject)
    if not tax:
        raise LookupError(f"词表不存在：{board}/{qual}/{subject}")
    entry = tax.get(unit)
    if not isinstance(entry, dict) or "tags" not in entry:
        raise LookupError(f"词表里没有单元 {unit}")
    tags = entry.get("tags") or []
    groups = entry.get("groups") or []

    if group_id:
        g = next((x for x in groups if x.get("id") == group_id), None)
        if g is None:
            raise LookupError(f"{unit} 里没有分组 {group_id}")
        picked = [t for t in tags if t.get("group") == group_id]
        if not picked:
            raise LookupError(f"分组 {g.get('zh') or group_id} 下没有标签")
        return {"unit": unit, "kind": "group", "group": g, "tags": picked}

    if not tag_id:
        raise LookupError("必须指定 tag_id 或 group_id")
    t = next((x for x in tags if x.get("id") == tag_id), None)
    if t is None:
        raise LookupError(f"{unit} 里没有标签 {tag_id}")
    g = next((x for x in groups if x.get("id") == t.get("group")), None)
    return {"unit": unit, "kind": "tag", "group": g, "tags": [t]}


# ---------------------------------------------------------------------------
# 取数：哪些单元正在用这些标签
# ---------------------------------------------------------------------------

def _paper_qual_dirs(board: str, qual: str) -> set[str]:
    """卷目录用的资格名集合。

    词表目录是 `a-level`，而 CIE 的卷目录写 `alevel`（见 taxonomy._qual_candidates）。
    这里按「学段」互认，两种写法都能命中同一批卷。
    """
    out = {qual}
    level = auth.level_of(qual)
    root = store.DATA_DIR / board
    try:
        for d in root.iterdir():
            if d.is_dir() and auth.level_of(d.name) == level:
                out.add(d.name)
    except OSError:
        pass
    return out


def _subject_papers(board: str, qual: str, subject: str) -> list[store.Paper]:
    """本学科的全部卷。

    走 store.iter_papers()（目录扫描，5 秒缓存）而不是只查索引：目录扫描会顺带
    `_sync_index` 把「目录 mtime 变了」的卷标 stale，保证下面拿到的 stale 标记是新鲜的。
    """
    quals = _paper_qual_dirs(board, qual)
    return [p for p in store.iter_papers()
            if p.board == board and p.subject == subject and p.qualification in quals]


def _paper_info(paper: store.Paper, row: dict[str, Any] | None) -> dict[str, Any]:
    if row is not None:
        return {"paper_code": row.get("paper_code"), "year": row.get("year"),
                "session": row.get("session")}
    try:
        meta = paper.load_meta()
    except (OSError, KeyError):
        return {"paper_code": None, "year": None, "session": None}
    return {"paper_code": meta.get("paper_code"), "year": meta.get("year"),
            "session": meta.get("session")}


def _mk_unit(paper: store.Paper, info: dict[str, Any], qid: str, qnum: Any, sub_label: Any,
             tags: list[str], primary: Any, difficulty: Any, file_name: str) -> dict[str, Any]:
    return {
        "qid": str(qid),
        "paper_id": paper.id,
        "paper_code": info.get("paper_code"),
        "year": info.get("year"),
        "session": info.get("session"),
        "question_number": str(qnum) if qnum is not None else None,
        "sub_label": str(sub_label) if sub_label else None,
        "tags": [str(t) for t in tags],
        "primary_tag": store.effective_primary(tags, primary),
        "difficulty": difficulty,
        "_path": paper.path / file_name,
    }


def _units_from_index(paper: store.Paper, info: dict[str, Any],
                      ids: set[str]) -> list[dict[str, Any]]:
    out = []
    for r in index_db.questions(paper.id):
        tags = r.get("tags") or []
        if not ids.intersection(tags):
            continue
        out.append(_mk_unit(paper, info, r["qid"], r.get("qnum"), r.get("sub_label"),
                            tags, r.get("primary_tag"), r.get("difficulty"), r.get("file_name")))
    return out


def _units_from_files(paper: store.Paper, info: dict[str, Any],
                      ids: set[str]) -> list[dict[str, Any]]:
    out = []
    for qf in [*paper.qfiles, *paper.all_sub_files()]:
        try:
            meta = store.read_meta(qf)
        except OSError:
            continue
        tags = meta.get("tags") or []
        if not isinstance(tags, list) or not ids.intersection(tags):
            continue
        out.append(_mk_unit(paper, info, meta.get("id"), meta.get("question_number"),
                            meta.get("sub_label"), tags, meta.get("primary_tag"),
                            meta.get("difficulty"), qf.name))
    return out


def _verify_with_md(unit: dict[str, Any], ids: set[str]) -> bool:
    """用 md 复核一条命中（索引只是候选来源，md 才是事实源）。

    顺带把 tags / primary / difficulty 换成 md 上的真实值，保证后续写盘的判定基于事实。
    返回 False = 这条已经不成立（索引过期），调用方丢弃。
    """
    try:
        meta = store.read_meta(unit["_path"])
    except OSError:
        return False
    tags = meta.get("tags") or []
    if not isinstance(tags, list):
        tags = []
    if not ids.intersection(tags):
        return False
    unit["tags"] = [str(t) for t in tags]
    unit["primary_tag"] = store.effective_primary(tags, meta.get("primary_tag"))
    unit["difficulty"] = meta.get("difficulty")
    return True


def _scan_units(board: str, qual: str, subject: str,
                ids: set[str]) -> tuple[list[dict[str, Any]], int]:
    """列出正在使用 ids 的全部单元。返回 (units, 索引与 md 不一致的条数)。

    索引未过期 → 走索引枚举（快）；索引缺失 / 该卷 stale → 直接读 md。
    两种来源的结果都用 md 复核一遍，故索引里的旧 tags 不会导致误伤，
    只会导致「漏」（见文件头 docstring 的风险说明）。
    """
    units: dict[str, dict[str, Any]] = {}
    discrepancies = 0
    for paper in _subject_papers(board, qual, subject):
        row = index_db.paper(paper.id) if index_db.exists() else None
        info = _paper_info(paper, row)
        if row is None or row.get("stale"):
            got = _units_from_files(paper, info, ids)
        else:
            got = _units_from_index(paper, info, ids)
        for u in got:
            if not u["qid"] or u["qid"] == "None":
                continue
            if ids.intersection(u["tags"]) and not _verify_with_md(u, ids):
                discrepancies += 1
                continue
            if u["qid"] not in units:
                units[u["qid"]] = u
    return list(units.values()), discrepancies


# ---------------------------------------------------------------------------
# 判定：一个单元在新旧两种方式下变成什么样
# ---------------------------------------------------------------------------

def _plan_tags(tags: list[str], primary: Any, ids: set[str],
               mode: str) -> tuple[list[str], str, str | None, bool]:
    """返回 (新 tags, action, 要写入的 primary, 是否顺手钉了主标签)。

    action ∈ {"clear", "strip"}：
      clear = 该单元变成「无标签」；strip = 只摘掉被删的标签、其余保留。
    """
    tags = [str(t) for t in (tags or []) if isinstance(t, str)]
    if mode == "clear_all":
        return [], "clear", None, False

    cur = store.effective_primary(tags, primary)
    # 被删标签里有主标签（单标签时主标签必然就是它）→ 整清。
    if cur in ids:
        return [], "clear", None, False
    rest = [t for t in tags if t not in ids]
    if not rest:
        return [], "clear", None, False
    if len(rest) == 1:
        # 只剩一个：后端 normalize_primary 会自动补成它，不用显式传
        return rest, "strip", None, False
    if cur is not None:
        # cur 不在 ids 里 ⇒ cur ∈ rest，主标签不变
        return rest, "strip", cur, False
    # 原本就是「多标签但没有可解析的主标签」（历史脏数据）。
    # 摘完还剩 ≥2 个却仍无主，会制造新的「多标签无主」→ 钉第一个当主标签。
    return rest, "strip", rest[0], True


def _tally(units: list[dict[str, Any]], ids: set[str],
           mode: str) -> dict[str, int]:
    clear = strip = removed = 0
    for u in units:
        _, action, _, _ = _plan_tags(u["tags"], u["primary_tag"], ids, mode)
        if action == "clear":
            clear += 1
        else:
            strip += 1
        removed += len([t for t in u["tags"] if t in ids])
    return {"clear": clear, "strip": strip, "tags_removed": removed}


def _remaining_groups(entry: dict[str, Any], ids: set[str]) -> list[dict[str, str]]:
    """删掉 ids 之后会变成空组、因而被连带删除的分组。"""
    keep_tags = [t for t in (entry.get("tags") or []) if t.get("id") not in ids]
    used = {t.get("group") for t in keep_tags}
    return [{"id": g.get("id"), "zh": g.get("zh") or g.get("id")}
            for g in (entry.get("groups") or []) if g.get("id") not in used]


# ---------------------------------------------------------------------------
# 只读：影响面
# ---------------------------------------------------------------------------

def plan(board: str, qual: str, subject: str, unit: str,
         tag_id: str | None = None, group_id: str | None = None) -> dict[str, Any]:
    """影响面：两种方式各自会清多少、摘多少，附明细行（最多 _MAX_ROWS 条）。"""
    target = _resolve_target(board, qual, subject, unit, tag_id, group_id)
    ids = {str(t["id"]) for t in target["tags"]}
    units, discrepancies = _scan_units(board, qual, subject, ids)

    rows = []
    flagged = 0
    for u in sorted(units, key=lambda x: (str(x.get("paper_code") or ""), str(x.get("qid")))):
        is_flagged = flags.is_flagged(u["qid"])
        if is_flagged:
            flagged += 1
        ca_tags, ca_action, _, _ = _plan_tags(u["tags"], u["primary_tag"], ids, "clear_all")
        sm_tags, sm_action, _, _ = _plan_tags(u["tags"], u["primary_tag"], ids, "smart")
        rows.append({
            "qid": u["qid"],
            "paper_code": u["paper_code"],
            "year": u["year"],
            "session": u["session"],
            "question_number": u["question_number"],
            "sub_label": u["sub_label"],
            "tags": u["tags"],
            "primary_tag": u["primary_tag"],
            "flagged": is_flagged,
            "clear_all": ca_tags,
            "smart": sm_tags,
            "clear_all_action": ca_action,
            "smart_action": sm_action,
        })

    entry = taxonomy.load_taxonomy(board, qual, subject).get(unit) or {}
    unit_tags_after = len([t for t in (entry.get("tags") or []) if str(t.get("id")) not in ids])
    return {
        "unit": unit,
        "target": {
            "kind": target["kind"],
            "id": target["tags"][0]["id"] if target["kind"] == "tag" else target["group"]["id"],
            "zh": target["tags"][0].get("zh") if target["kind"] == "tag" else target["group"].get("zh"),
            "en": target["tags"][0].get("en") if target["kind"] == "tag" else target["group"].get("en"),
            "group": (target["group"] or {}).get("zh"),
        },
        "tags": [{"id": t.get("id"), "zh": t.get("zh"), "en": t.get("en"),
                  "group": t.get("group")} for t in target["tags"]],
        "total_units": len(units),
        "flagged_units": flagged,
        "paper_count": len({u["paper_id"] for u in units}),
        "by_mode": {m: _tally(units, ids, m) for m in MODES},
        "removed_groups": _remaining_groups(entry, ids),
        "unit_tags_after": unit_tags_after,
        "rows": rows[:_MAX_ROWS],
        "truncated": len(rows) > _MAX_ROWS,
        "discrepancies": discrepancies,
        "sig": taxonomy.file_sig(board, qual, subject),
    }


# ---------------------------------------------------------------------------
# 落盘
# ---------------------------------------------------------------------------

class PartialFailure(Exception):
    """部分 md 写失败：词表**没有**动，md 上成功的那部分是幂等的，可原样重试。"""

    def __init__(self, failed: list[dict[str, str]], done: int):
        super().__init__(f"{len(failed)} 处标注写入失败（已成功 {done} 处），词表未改动")
        self.failed = failed
        self.done = done


class VocabConflict(Exception):
    """标注已经清完、存词表时才发现版本冲突（极罕见：scan 与 save 之间有人存了词表）。

    重试是安全的：已清过的单元不再持有该标签，重试只会把词表改动落下去。
    """

    def __init__(self, current: str, done: int):
        super().__init__("词表在你确认期间被其他账号保存过")
        self.current = current
        self.done = done


def apply(board: str, qual: str, subject: str, unit: str, mode: str,
          tag_id: str | None = None, group_id: str | None = None,
          expect_sig: str | None = None,
          actor: str | None = None) -> dict[str, Any]:
    """执行删除：先清标注，再从词表移除标签，最后连带清掉变空的分组。"""
    if mode not in MODES:
        raise ValueError(f"mode 必须是 {' / '.join(MODES)}")
    t0 = time.perf_counter()

    target = _resolve_target(board, qual, subject, unit, tag_id, group_id)
    ids = {str(t["id"]) for t in target["tags"]}

    # 前置校验：版本号先对一次，避免「改了一半 md 才发现词表被别人存过」
    if expect_sig is not None:
        cur_sig = taxonomy.file_sig(board, qual, subject)
        if expect_sig != cur_sig:
            raise taxonomy.TaxonomyConflict(cur_sig)

    units, discrepancies = _scan_units(board, qual, subject, ids)

    # ① 改 md
    done: list[dict[str, Any]] = []
    failed: list[dict[str, str]] = []
    cleared = stripped = primary_fixed = flagged_n = 0
    confirm_cleared: list[str] = []
    for u in units:
        new_tags, action, new_primary, fixed = _plan_tags(
            u["tags"], u["primary_tag"], ids, mode)
        try:
            store.write_annotation(u["_path"], new_tags, u["difficulty"], new_primary)
        except Exception as e:  # noqa: BLE001  逐条收集，别让一条坏文件毁掉整批
            failed.append({"qid": u["qid"], "error": str(e)})
            continue
        if action == "clear":
            cleared += 1
        else:
            stripped += 1
        if fixed:
            primary_fixed += 1
        if u.get("flagged") or flags.is_flagged(u["qid"]):
            flagged_n += 1
            # 文件被我们合法重写过，指纹必然变了。不刷新的话，5 分钟内的巡检
            # 或下次打开详情会把「有误」标记当成「题被换过」自动删掉。
            try:
                flags.refresh(u["qid"])
            except Exception as e:  # noqa: BLE001
                print(f"[tag_delete] 刷新 {u['qid']} 的有误指纹失败：{e}", flush=True)
        if review.is_confirmed(u["qid"]):
            review.set_confirmed(u["qid"], False, by=actor)
            confirm_cleared.append(u["qid"])
        done.append(u)

    if failed:
        raise PartialFailure(failed, len(done))

    # ② 改词表（md 全部成功才走到这里）
    data = copy.deepcopy(taxonomy.load_taxonomy(board, qual, subject))
    entry = data.get(unit) or {}
    entry["tags"] = [t for t in (entry.get("tags") or []) if str(t.get("id")) not in ids]
    keep = {t.get("group") for t in entry["tags"]}
    before_groups = list(entry.get("groups") or [])
    entry["groups"] = [g for g in before_groups if g.get("id") in keep]
    removed_groups = [{"id": g.get("id"), "zh": g.get("zh") or g.get("id")}
                      for g in before_groups if g.get("id") not in keep]
    data[unit] = entry

    try:
        res = taxonomy.save_taxonomy(board, qual, subject, data, expect_sig=expect_sig)
    except taxonomy.TaxonomyConflict as e:
        # md 已经改完了，词表没动。重试安全（已清过的单元不再命中）——见 VocabConflict。
        raise VocabConflict(e.current, len(done)) from e

    return {
        "ok": True,
        "mode": mode,
        "unit": unit,
        "kind": target["kind"],
        "tags": [{"id": t.get("id"), "zh": t.get("zh")} for t in target["tags"]],
        "total_units": len(units),
        "cleared": cleared,
        "stripped": stripped,
        "primary_fixed": primary_fixed,
        "flagged": flagged_n,
        "removed_groups": removed_groups,
        "confirm_cleared": confirm_cleared,
        "discrepancies": discrepancies,
        "backup": res.get("backup"),
        "path": res.get("path"),
        "sig": res.get("sig"),
        "seconds": round(time.perf_counter() - t0, 2),
    }


# ---------------------------------------------------------------------------
# 命令行：dry-run 核对影响面（部署后先用它看一眼，再决定要不要点界面）
# ---------------------------------------------------------------------------

def _main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="标签删除 dry-run / 执行")
    ap.add_argument("--board", required=True)
    ap.add_argument("--qual", required=True)
    ap.add_argument("--subject", required=True)
    ap.add_argument("--unit", required=True)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--tag")
    g.add_argument("--group")
    ap.add_argument("--apply", choices=MODES, help="给了才真删；不给只打印影响面")
    ap.add_argument("--sig", default=None, help="乐观锁版本号（--apply 时建议带上）")
    args = ap.parse_args(argv)

    p = plan(args.board, args.qual, args.subject, args.unit, args.tag, args.group)
    print(json.dumps({k: p[k] for k in
                      ("unit", "target", "total_units", "flagged_units", "paper_count",
                       "by_mode", "removed_groups", "unit_tags_after", "truncated",
                       "discrepancies", "sig")},
                     ensure_ascii=False, indent=2))
    if not args.apply:
        print(f"\n[dry-run] 涉及 {p['total_units']} 处；加 --apply {MODES[0]}|{MODES[1]} 才真删",
              flush=True)
        return 0

    res = apply(args.board, args.qual, args.subject, args.unit, args.apply,
                args.tag, args.group, expect_sig=args.sig, actor="cli")
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
