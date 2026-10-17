#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""按 decisions.json 经打标台 API 写知识点标签 + 主标签（默认 dry-run）。

用法：
    python apply_tags.py decisions.json [--apply] [--base URL] [--only 5,6] [--auto-primary]

写库路径（与人工在打标台点击完全等价）：
    PUT /api/questions/{qid}/annotation
      · 整题：{"tags": [...], "difficulty": <保留原值>, "primary_tag": <主标签|null>}
      · 拆分：{"subs": [{"label","tags","difficulty","primary_tag"}...], "difficulty": null}

主标签规则（plans/主标签实施方案.md §2.1，与后端 api._validate_primary 同口径）：
    · tags == 0  → 后端拒收；脚本前置拦下，本题不写库
    · tags == 1  → 无需指定，传 null，后端自动补成该标签（显式落盘，下游零推导）
    · tags >= 2  → **必须**显式给 primary_tag 且 ∈ tags，否则本题报错、不写库
                   确实判不出主线时用 --auto-primary 兜底取 tags[0]（结果单列，报告标黄）

⚠️ 难度一律**保留原值**：AI 只标知识点 + 主标签，不碰难度（难度由人在打标台补）。
⚠️ 绝不直接改 qNN.md：拆分题保存时后端要跑 render_subsplit 产子图，直写文件没有这个副作用。
"""
import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_BASE = "http://127.0.0.1:8642"


class PrimaryError(Exception):
    """主标签规则不满足 —— 本题整题放弃写库（不半写）。"""


def get(base, path):
    with urllib.request.urlopen(base + path, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def put(base, qid, body):
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(f"{base}/api/questions/{qid}/annotation", data=data,
                                 method="PUT", headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def pick_primary(tags, given, auto, where, autos, n=None, label=None):
    """按 §2.1 决定该单元提交的 primary_tag；返回 None = 交给后端补（恰好 1 个标签时）。

    不合规一律抛 PrimaryError，调用方把整题记为失败、不写库。
    """
    if not tags:
        raise PrimaryError(f"{where}：没有任何标签（每题/每小问至少 1 个）")
    if len(tags) == 1:
        if given and given != tags[0]:
            raise PrimaryError(f"{where}：只选了 1 个标签（{tags[0]}），却给了主标签 {given}")
        return None
    if given:
        if given not in tags:
            raise PrimaryError(f"{where}：主标签 {given} 不在已选标签内（{', '.join(tags)}）")
        return given
    if auto:
        autos.append({"n": n, "label": label, "where": where,
                      "picked": tags[0], "tags": list(tags)})
        return tags[0]
    raise PrimaryError(
        f"{where}：多标签题必须指定 primary_tag（现有 {len(tags)} 个：{', '.join(tags)}）"
        f"。回看题图判断主线；确不可判再用 --auto-primary 应急兜底")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("decisions")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--only", help="只跑这些题号，逗号分隔（调试用）")
    ap.add_argument("--auto-primary", action="store_true", dest="auto_primary",
                    help="多标签缺主标签时兜底取 tags[0]（应急；结果与报告会标黄）")
    a = ap.parse_args()

    dec = json.loads(Path(a.decisions).read_text(encoding="utf-8"))
    items = dec["items"]
    if a.only:
        want = {x.strip() for x in a.only.split(",")}
        items = [x for x in items if str(x.get("n")) in want]

    plan, skip, fail, autos = [], [], [], []
    t0 = time.time()

    # ── 第一遍：只读现状 + 校验，构造待写 body（写库前把不合格的整题挑出来）
    for it in items:
        n = it.get("n")
        qid = it.get("qid")
        reason = it.get("skip_reason")
        if reason:
            skip.append({"n": n, "reason": reason})
            print(f"[skip] q{n}：{reason}")
            continue
        if not qid:
            fail.append({"n": n, "error": "decisions.json 缺 qid（先用 fetch_paper.py 拉上下文再合并）"})
            print(f"✗ q{n}：缺 qid", file=sys.stderr)
            continue

        # ⚠️ 难度一律保留原值：AI 只标知识点，不该把别人（或预览模式）已经补好的难度抹掉。
        #    后端 write_annotation 用 difficulty=None 表示「删除该行」，所以必须回填。
        try:
            cur = get(a.base, f"/api/questions/{qid}")
            cur_diff = cur.get("difficulty")
            cur_subs = {str(s.get("label")): s.get("difficulty") for s in (cur.get("subs") or [])}
        except Exception as e:  # noqa: BLE001
            fail.append({"n": n, "error": f"读取现状失败：{e}"})
            print(f"✗ q{n}：读取现状失败 {e}", file=sys.stderr)
            continue

        subs = it.get("subs")
        try:
            if subs:
                # 个别小问 tags 为空 = 该小问无合适标签 → 留空、不提交（后端允许提交子集）
                eff = [s for s in subs if s.get("tags")]
                blank = [s["label"] for s in subs if not s.get("tags")]
                if not eff:
                    skip.append({"n": n, "reason": "所有小问都没有合适标签"})
                    print(f"[skip] q{n}：所有小问都没有合适标签")
                    continue
                sub_bodies = []
                for s in eff:
                    pri = pick_primary(s["tags"], s.get("primary_tag"), a.auto_primary,
                                       f"q{n} 小问 {s['label']}", autos, n, str(s["label"]))
                    sub_bodies.append({"label": s["label"], "tags": s["tags"],
                                       "difficulty": cur_subs.get(str(s["label"])),
                                       "primary_tag": pri})
                body = {"subs": sub_bodies, "difficulty": None}
                desc = "subs[" + ", ".join(
                    f"{b['label']}:{','.join(b['tags'])}" + (f"★{b['primary_tag']}" if b["primary_tag"] else "")
                    for b in sub_bodies) + "]" + (f"（留空 {blank}）" if blank else "")
            else:
                tags = it.get("tags") or []
                pri = pick_primary(tags, it.get("primary_tag"), a.auto_primary, f"q{n} 整题", autos, n)
                body = {"tags": tags, "difficulty": cur_diff, "primary_tag": pri}
                desc = "tags=" + ",".join(tags) + (f" ★主={pri}" if pri else " （单标签·后端自补主）")
        except PrimaryError as e:
            fail.append({"n": n, "error": str(e)})
            print(f"✗ {e}", file=sys.stderr)
            continue

        plan.append({"n": n, "qid": qid, "body": body, "desc": desc})

    # ── 第二遍：dry-run 打印 / 真正写库
    ok = []
    if not a.apply:
        for p in plan:
            print(f"q{p['n']} {p['desc']}")
        print(f"\n[dry-run] 待写 {len(plan)} 题 / 跳过 {len(skip)} / 校验不过 {len(fail)}，未写库。加 --apply 执行。")
        if autos:
            print(f"⚠️ 其中 {len(autos)} 处多标签缺主标签，将由 --auto-primary 兜底取 tags[0]：")
            for x in autos:
                print(f"    {x['where']} → {x['picked']}")
        if fail:
            sys.exit(1)
        return

    for p in plan:
        try:
            r = put(a.base, p["qid"], p["body"])
            ok.append({"n": p["n"], "status": r.get("status"), "split": r.get("split"),
                       "sub_labels": r.get("sub_labels"), "primary_tag": r.get("primary_tag")})
            print(f"✓ q{p['n']} status={r.get('status')} {p['desc']}")
        except urllib.error.HTTPError as e:
            msg = e.read().decode("utf-8")[:300]
            fail.append({"n": p["n"], "code": e.code, "error": msg})
            print(f"✗ q{p['n']} HTTP {e.code}: {msg}", file=sys.stderr)
        except Exception as e:  # noqa: BLE001
            fail.append({"n": p["n"], "error": str(e)})
            print(f"✗ q{p['n']} {e}", file=sys.stderr)

    print(f"\n✅ 成功 {len(ok)} / 跳过 {len(skip)} / 失败 {len(fail)}"
          f"（主标签兜底 {len(autos)}），耗时 {time.time() - t0:.1f}s")
    if autos:
        print("⚠️ 以下主标签是兜底取 tags[0]，建议人工复看：")
        for x in autos:
            print(f"    {x['where']} → {x['picked']}")
    side = Path(a.decisions).with_suffix(".result.json")
    with open(side, "w", encoding="utf-8", newline="") as f:
        f.write(json.dumps({"ok": ok, "skip": skip, "fail": fail, "auto_primary": autos},
                           ensure_ascii=False, indent=1) + "\n")
    print(f"   结果写到 {side}")
    if fail:
        sys.exit(1)


if __name__ == "__main__":
    main()
