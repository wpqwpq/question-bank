#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成分级懒加载所需的数据包（只读现有快照，不访问/写入题库）。

阶段一 catalog.json ：进入页面即加载，全部科目 × 全部考试局 × 全部阶段 × 两种模式的一级目录（纯目录名）。
阶段二 subjects/<科目>/<考试局>-<阶段>/tree.json.gz ：选定科目后按当前考试局+阶段加载二/三/四级目录文字
       （卷号、大题号、小题号、标签、题干），不含任何难度颜色。切换考试局/阶段时再取对应包。
阶段三 subjects/<科目>/diff/<id>.json.gz ：展开某个一级目录时才加载该目录下全部题目的难度颜色。
       subjects/<科目>-difficulty.json.gz ：科目级合并包，仅在用户主动使用难度筛选时加载。
阶段四（前端行为）：展开二级目录时预取该目录下全部题目截图（浏览器图片缓存，不落盘）。

保留 subjects/<科目>.json.gz 作为兼容包（全组合文字数据），前端已不再使用它。
"""
import json, gzip, os, re, base64, collections

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")


def load(name):
    path = os.path.join(DATA, name)
    opener = gzip.open if name.endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8") as f:
        return json.load(f)


def dump(obj, name, quiet=False):
    path = os.path.join(DATA, name)
    if name.endswith(".gz"):
        f = gzip.open(path, "wt", encoding="utf-8", compresslevel=6)
    else:
        f = open(path, "wt", encoding="utf-8")
    with f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
    if not quiet:
        print(f"  {name}: {os.path.getsize(path)/1024/1024:.2f} MB")
    return os.path.getsize(path)


def norm_level(x):
    return "a-level" if x in ("alevel", "a-level") else x


# 与前端 SESSION_NORM 一致：5/6 月归 june，10/11 月归 oct
SESSION_NORM = {"may": "june", "may-jun": "june", "june": "june", "june_july": "june",
                "june-july": "june", "oct": "oct", "nov": "oct",
                "oct-nov": "oct", "oct_nov": "oct"}


def norm_session(x):
    return SESSION_NORM.get(x, x)


def scope_slug(scope_key):
    """把 scope key 编码成 URL/文件名安全的 base64url（与前端 scopeId 一致）。"""
    return base64.urlsafe_b64encode(scope_key.encode("utf-8")).decode("ascii").rstrip("=")


def norm_qid(qid):
    """与前端 normQid 一致：去掉旧试卷题号中的长数字段。"""
    return re.sub(r"-\d{10,}", "", qid)


def pid_of(qid, known_pids):
    """从题号反推试卷 id（试卷编码本身可含连字符，按已知试卷 id 从长到短匹配）。"""
    parts = qid.split("-")
    for i in range(len(parts) - 1, 4, -1):
        cand = "-".join(parts[:i])
        if cand in known_pids:
            return cand
    return None


def main():
    papers = load("papers.json")
    prefixes = load("prefixes.json")
    tax = load("taxonomy.json")
    tv = load("taxonomy_v2.json")
    questions = load("questions.json")
    subtags = load("subtags.json")
    difficulty = load("difficulty.json")
    text = load("question_text.json.gz")
    submeta = load("submeta.json.gz")

    all_pids = {p["id"] for p in papers}
    subj_of_pid = {p["id"]: p["subject"] for p in papers}
    by_subject = collections.defaultdict(list)
    for p in papers:
        by_subject[p["subject"]].append(p)

    # 题号 → 试卷 id，用于把文字/子题标签按「考试局+阶段」拆分
    qid2pid = {}
    for pid, arr in questions.items():
        for q in arr:
            for k in [q.get("qid")] + list(q.get("sub_ids") or []):
                if not k:
                    continue
                qid2pid[k] = pid
                qid2pid.setdefault(norm_qid(k), pid)

    # ---------- 阶段一：年月目录索引 ----------
    years = collections.defaultdict(lambda: collections.Counter())
    for p in papers:
        key = f'{p["subject"]}|{p["board"]}|{norm_level(p["qualification"])}'
        years[key][f'{p["year"]}|{p["session"]}'] += 1
    years_out = {}
    for key, counter in years.items():
        rows = []
        for ym, n in counter.items():
            y, s = ym.split("|")
            rows.append({"y": int(y), "s": s, "n": n})
        rows.sort(key=lambda r: (r["y"], r["s"]), reverse=True)
        years_out[key] = rows

    # 按类别模式的顶层单元索引（展示名映射后去重）
    units = {}
    for combo, subs in tv["taxonomy"].items():
        for subj, raws in subs.items():
            k = f"{combo}|{subj}"
            disp = (tv["display"].get(combo) or {})
            names = sorted({disp.get(r, r) or r for r in raws})
            if names:
                units[k] = names

    # 每个科目的默认考试局/阶段：优先 A-Level 下试卷最多的组合
    defaults = {}
    for subj, ps in by_subject.items():
        cnt = collections.Counter((p["board"], norm_level(p["qualification"])) for p in ps)
        best = max(cnt.items(), key=lambda kv: (kv[0][1] == "a-level", kv[1]))
        defaults[subj] = {"board": best[0][0], "level": best[0][1]}

    catalog = {
        "subjects": sorted(by_subject),
        "years": years_out,
        "units": units,
        "defaults": defaults,
        "unitDisplay": tv["display"],
    }
    print("阶段一：")
    dump(catalog, "catalog.json")

    # ---------- 阶段二/三：按科目拆分 ----------
    # 先给所有题号建立 试卷id → 科目 的映射，避免对每个科目全表扫描
    buckets = {k: {"subtags": {}, "difficulty": {}, "text": {}, "submeta": {}}
               for k in by_subject}
    for name, src in (("subtags", subtags), ("difficulty", difficulty),
                      ("text", text), ("submeta", submeta)):
        cache = {}
        for qid, val in src.items():
            pid = cache.get(qid)
            if pid is None:
                pid = pid_of(qid, all_pids)
                cache[qid] = pid
            subj = subj_of_pid.get(pid) if pid else None
            if subj:
                buckets[subj][name][qid] = val

    print("阶段二/三：")
    os.makedirs(os.path.join(DATA, "subjects"), exist_ok=True)
    for subj, ps in sorted(by_subject.items()):
        pids = {p["id"] for p in ps}
        tax_sub = {}
        for key, obj in tax.items():
            pieces = key.split("|")
            if len(pieces) == 3 and pieces[0] == subj:
                tax_sub[pieces[1] + "|" + pieces[2]] = obj
        tax_v2_sub = {combo: subs[subj] for combo, subs in tv["taxonomy"].items() if subj in subs}
        # 阶段二不携带任何难度字段：难度统一放到阶段三，展开时才加载
        qs = {}
        for pid in pids:
            arr = questions.get(pid)
            if not arr:
                continue
            qs[pid] = [{k: v for k, v in q.items() if k != "difficulty"} for q in arr]
        bundle = {
            "papers": ps,
            "prefixes": {k: v for k, v in prefixes.items() if k in pids},
            "questions": qs,
            "subtags": buckets[subj]["subtags"],
            "submeta": buckets[subj]["submeta"],
            "text": buckets[subj]["text"],
            "taxonomy": tax_sub,
            "taxonomyV2": tax_v2_sub,
            "unitDisplay": tv["display"],
        }
        dump(bundle, f"subjects/{subj}.json.gz")

        # 阶段二（实际使用）：按「考试局-阶段」拆分的纯文字包，选中科目后只取当前组合。
        by_combo = collections.defaultdict(list)
        for p in ps:
            by_combo[(p["board"], norm_level(p["qualification"]))].append(p)
        for (board, level), cps in sorted(by_combo.items()):
            cpids = {p["id"] for p in cps}
            cqs = {pid: [{k: v for k, v in q.items() if k != "difficulty"}
                         for q in questions.get(pid, [])] for pid in cpids}
            aux = {}
            for name in ("subtags", "submeta", "text"):
                aux[name] = {k: v for k, v in buckets[subj][name].items()
                             if qid2pid.get(k) in cpids}
            tax_combo = {}
            for key, obj in tax.items():
                pieces = key.split("|")
                if len(pieces) == 3 and pieces[0] == subj and pieces[1] == board \
                        and norm_level(pieces[2]) == level:
                    tax_combo[board + "|" + level] = obj
            combo_key = board + "|" + level
            tax_v2_combo = {}
            if subj in tv["taxonomy"].get(combo_key, {}):
                tax_v2_combo[combo_key] = tv["taxonomy"][combo_key][subj]
            tree = {
                "papers": cps,
                "prefixes": {k: v for k, v in prefixes.items() if k in cpids},
                "questions": cqs,
                "subtags": aux["subtags"], "submeta": aux["submeta"], "text": aux["text"],
                "taxonomy": tax_combo, "taxonomyV2": tax_v2_combo,
                "unitDisplay": tv["display"],
            }
            combo_dir = os.path.join(DATA, "subjects", subj, board + "-" + level)
            os.makedirs(combo_dir, exist_ok=True)
            dump(tree, f"subjects/{subj}/{board}-{level}/tree.json.gz", quiet=True)
        print(f"    {subj}: {len(by_combo)} 个考试局/阶段文字包")

        # 题库数据库里的整题难度有些未同步到 Markdown 快照，补进阶段三包，避免漏色
        diff_bundle = dict(buckets[subj]["difficulty"])
        for pid in pids:
            for q in questions.get(pid, []):
                qid = q.get("qid")
                lvl = q.get("difficulty")
                if qid and lvl:
                    diff_bundle.setdefault(norm_qid(qid), lvl)
                    diff_bundle.setdefault(qid, lvl)
        dump(diff_bundle, f"subjects/{subj}-difficulty.json.gz")

        # 阶段三（真正按需）：按「模式|考试局|阶段|一级目录」拆分难度。
        # 按套卷模式一级目录 = 年月；按类别模式一级目录 = 单元（展示名）。
        # 文件 id 用 base64url(scope key)，与前端 scopeId() 完全一致，无需额外索引文件。
        scope_diff = collections.defaultdict(dict)
        for p in ps:
            board, level = p["board"], norm_level(p["qualification"])
            paper_scope = f"paper|{board}|{level}|{p['year']}-{norm_session(p['session'])}"
            combo = f"{board}|{level}"
            raw_unit = p.get("unit") or ""
            unit_disp = (tv["display"].get(combo) or {}).get(raw_unit, raw_unit or "其他")
            cat_scope = f"category|{board}|{level}|{unit_disp}"
            for q in questions.get(p["id"], []):
                keys = []
                for k in [q.get("qid")] + list(q.get("sub_ids") or []):
                    if not k:
                        continue
                    keys.append(k)
                    nk = norm_qid(k)
                    if nk != k:
                        keys.append(nk)
                for k in keys:
                    if k in diff_bundle:
                        scope_diff[paper_scope].setdefault(k, diff_bundle[k])
                        scope_diff[cat_scope].setdefault(k, diff_bundle[k])
        # 保证每个可展开的一级目录都有分片：无难度数据的目录写空 {}，避免前端 404
        for p in ps:
            board, level = p["board"], norm_level(p["qualification"])
            scope_diff[f"paper|{board}|{level}|{p['year']}-{norm_session(p['session'])}"]
            combo = f"{board}|{level}"
            raw_unit = p.get("unit") or ""
            unit_disp = (tv["display"].get(combo) or {}).get(raw_unit, raw_unit or "其他")
            scope_diff[f"category|{board}|{level}|{unit_disp}"]
        for key, units in catalog.get("units", {}).items():
            b, lv, s2 = key.split("|")
            if s2 != subj:
                continue
            for u in units:
                scope_diff[f"category|{b}|{lv}|{u}"]

        diff_dir = os.path.join(DATA, "subjects", subj, "diff")
        os.makedirs(diff_dir, exist_ok=True)
        for scope, obj in scope_diff.items():
            dump(obj, f"subjects/{subj}/diff/{scope_slug(scope)}.json.gz", quiet=True)
        print(f"    {subj}: {len(scope_diff)} 个一级目录难度分片")


if __name__ == "__main__":
    main()
