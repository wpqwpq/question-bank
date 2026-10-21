#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""按 decisions.json 经**云端**打标台 API 写知识点标签 + 主标签（默认 dry-run）。

用法：
    python apply_tags.py decisions.json [--apply] [--base URL] [--only 5,6] [--auto-primary]

写库路径（与人工在打标台点击完全等价）：
    PUT /api/questions/{qid}/annotation
      · 整题：{"tags": [...], "difficulty": <保留原值>, "primary_tag": <主标签|null>}
      · 拆分：{"subs": [{"label","tags","difficulty","primary_tag"}...], "difficulty": null}

硬规则：
    · 难度**默认保留原值**（AI 只标知识点 + 主标签）。仅当用户明确要求「打难度」且命令带
      `--difficulty` 时，才从 decisions.json 的 difficulty 字段写入；判定口径见
      references/rules.md §八（1 分定义题→very-easy；6 分简答中等起步；其余按「完整思路」判）
    · **有小问（top_labels ≥2）必须按小问提交 subs**，整题提交会被本脚本拦下
    · 题目有问题 → decisions.json 写 `issue`，本脚本**跳过不写库**，更不会调 /flag
      （打标台的「此题有误」由人决定，AI 只在报告里说明情况）
    · 绝不直接改 qNN.md：拆分题保存时后端要跑 render_subsplit 产子图

主标签规则（plans/主标签实施方案.md §2.1，与后端 api._validate_primary 同口径）：
    · tags == 0  → 后端拒收；脚本前置拦下，本题不写库
    · tags == 1  → 无需指定，传 null，后端自动补成该标签（显式落盘，下游零推导）
    · tags >= 2  → **必须**显式给 primary_tag 且 ∈ tags，否则本题报错、不写库
                   确实判不出主线时用 --auto-primary 兜底取 tags[0]（结果单列，报告标黄）
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# 默认打云端；云端是本技能唯一支持的目标
DEFAULT_BASE = os.environ.get("QBANK_BASE", "http://140.143.229.85")
TRIES = 4

# 合法难度档（打标台的五档；空 = 未标）
DIFFS = {"very-easy", "easy", "medium", "hard", "very-hard"}

def _api_key() -> str:
    """API Key 解析顺序：--key > 环境变量 QBANK_API_KEY > ~/.qbank_key"""
    k = os.environ.get("QBANK_API_KEY", "").strip()
    if k:
        return k
    p = os.path.expanduser("~/.qbank_key")
    if os.path.exists(p):
        return Path(p).read_text(encoding="utf-8").strip()
    return ""


_AUTH: dict[str, str] = {}

# ⚠️ 强制不走代理：本机 HTTP_PROXY/HTTPS_PROXY 指向 WorkBuddy 的本地代理，
#    urllib 照走会拿 502 Bad Gateway（打标台是裸 IP，不走外网代理）。
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class PrimaryError(Exception):
    """主标签规则不满足 —— 本题整题放弃写库（不半写）。"""


class DifficultyError(Exception):
    """难度值不合法 —— 本题整题放弃写库。"""


def pick_difficulty(unit, cur_val, use, where):
    """决定该单元提交的难度。

    use=False（默认）→ **原样保留**当前值，AI 不碰难度。
    use=True（--difficulty）→ 取 decisions.json 里的 difficulty；没写就保留原值。
    """
    if not use or "difficulty" not in unit:
        return cur_val
    d = unit.get("difficulty")
    if d is None or d == "":
        return None
    if d not in DIFFS:
        raise DifficultyError(f"{where}：难度 {d!r} 不合法（只能是 {', '.join(sorted(DIFFS))}）")
    return d


def _json_req(url, data=None, method=None, timeout=60, tries=TRIES):
    """单次 JSON 请求 + 重试。

    GET 遇到 5xx / 连接错误重试；写（PUT）同样重试——同一个 body 重复提交是幂等的
    （annotation 是覆盖写，不是累加），但 4xx 一律不重试（那是内容不合规，重试也没用）。
    """
    last = None
    for i in range(tries):
        try:
            hdr = dict(_AUTH)
            if data is not None:
                hdr["Content-Type"] = "application/json"
            req = urllib.request.Request(url, data=data, method=method, headers=hdr)
            with _OPENER.open(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code < 500:
                raise
            last = e
        except Exception as e:  # noqa: BLE001  连接超时 / 重置 / 抖动
            last = e
        time.sleep(1.5 * (i + 1))
    raise last


def get(base, path):
    return _json_req(base + path)


def put(base, qid, body):
    data = json.dumps(body).encode("utf-8")
    return _json_req(f"{base}/api/questions/{qid}/annotation", data=data, method="PUT")


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
    ap.add_argument("--key", help="API Key；缺省读 QBANK_API_KEY 或 ~/.qbank_key")
    ap.add_argument("--allow-partial", action="store_true", dest="allow_partial",
                    help="允许只提交部分小问（⚠️ 云端会把未提交的小问清成空壳，默认禁止）")
    ap.add_argument("--auto-primary", action="store_true", dest="auto_primary",
                    help="多标签缺主标签时兜底取 tags[0]（应急；结果与报告会标黄）")
    ap.add_argument("--difficulty", action="store_true", dest="difficulty",
                    help="同时写难度：从 decisions.json 的 difficulty 字段取值（默认绝不碰难度）")
    a = ap.parse_args()
    k = a.key or _api_key()
    if k:
        _AUTH["Authorization"] = f"Bearer {k}"

    dec = json.loads(Path(a.decisions).read_text(encoding="utf-8"))
    items = dec["items"]
    if a.only:
        want = {x.strip() for x in a.only.split(",")}
        items = [x for x in items if str(x.get("n")) in want]

    plan, skip, fail, autos = [], [], [], []
    ignored_diff = []
    t0 = time.time()

    # ── 第一遍：只读现状 + 校验，构造待写 body（写库前把不合格的整题挑出来）
    for it in items:
        n = it.get("n")
        qid = it.get("qid")
        # 「题目有问题」只在报告里说明，**绝不**调 /flag（本题照样不打标）
        if it.get("issue"):
            skip.append({"n": n, "kind": "issue", "reason": it["issue"]})
            print(f"[issue] q{n}：{it['issue']}")
            continue
        reason = it.get("skip_reason")
        if reason:
            skip.append({"n": n, "reason": reason})
            print(f"[skip] q{n}：{reason}")
            continue
        if not qid:
            fail.append({"n": n, "error": "decisions.json 缺 qid（先用 fetch_paper.py 拉上下文再合并）"})
            print(f"✗ q{n}：缺 qid", file=sys.stderr)
            continue

        # ⚠️ 难度默认保留原值：AI 只标知识点，不该把别人（或预览模式）已经补好的难度抹掉。
        #    后端 write_annotation 用 difficulty=None 表示「删除该行」，所以必须回填。
        #    只有显式 --difficulty 时才用 decisions.json 的值（见 pick_difficulty）。
        if not a.difficulty and (it.get("difficulty") or any(
                s.get("difficulty") for s in (it.get("subs") or []))):
            ignored_diff.append(n)
        try:
            cur = get(a.base, f"/api/questions/{qid}")
            cur_diff = cur.get("difficulty")
            cur_subs = {str(s.get("label")): s.get("difficulty") for s in (cur.get("subs") or [])}
        except Exception as e:  # noqa: BLE001
            fail.append({"n": n, "error": f"读取现状失败：{e}"})
            print(f"✗ q{n}：读取现状失败 {e}", file=sys.stderr)
            continue

        subs = it.get("subs")
        top = [str(x) for x in (cur.get("top_labels") or [])]
        # 硬规则：有小问的题必须按小问打标（云端 top_labels ≥2 即可拆）。
        # 用整题提交会把「按小问」的信息丢掉，后续也无法逐问复核。
        if len(top) >= 2 and not subs:
            fail.append({"n": n, "error": f"本题有 {len(top)} 个小问（{top}），必须按小问打标"})
            print(f"✗ q{n}：本题有 {len(top)} 个小问 {top}，不许整题提交；"
                  f"请按小问分别给 subs", file=sys.stderr)
            continue
        if subs and len(top) < 2:
            fail.append({"n": n, "error": f"本题不可拆（top_labels={top}），却提交了 subs"})
            print(f"✗ q{n}：本题不可拆（top_labels={top}），不该提交 subs", file=sys.stderr)
            continue
        try:
            if subs:
                # 云端自 2026-10-19 起「未提交的小问 = 保持原样」，部分提交不再毁数据；
                # 但 AI 的判定单位是整题（每问都要给结论），所以仍要求覆盖全部小问，
                # 免得「这题没打全」被误当成「这题只剩这几问」。
                sub_labels = [str(x["label"]) for x in subs]
                missing = [l for l in top if l not in sub_labels]
                if missing and not a.allow_partial:
                    fail.append({"n": n, "error": f"subs 未覆盖全部小问，缺 {missing}"})
                    print(f"✗ q{n}：subs 缺小问 {missing}——补全后重跑"
                          f"（未写的会保持原样，不会被清空）；确需部分提交加 --allow-partial",
                          file=sys.stderr)
                    continue
                # 个别小问 tags 为空 = 该小问无合适标签 → **显式提交 tags: []**。
                # ⚠️ 云端自 2026-10-19 起「未提交的小问 = 保持原样」（不再一律补空壳），
                #    所以留空必须显式表达，否则上一次的旧标签会被保留下来，
                #    报告写「留空」而库里还有标签。
                eff = [s for s in subs if s.get("tags")]
                blank = [s["label"] for s in subs if not s.get("tags")]
                if not eff:
                    skip.append({"n": n, "reason": "所有小问都没有合适标签"})
                    print(f"[skip] q{n}：所有小问都没有合适标签")
                    continue
                sub_bodies = []
                for s in subs:
                    d_val = pick_difficulty(s, cur_subs.get(str(s["label"])), a.difficulty,
                                            f"q{n} 小问 {s['label']}")
                    if not s.get("tags"):
                        sub_bodies.append({"label": s["label"], "tags": [],
                                           "difficulty": d_val,
                                           "primary_tag": None})
                        continue
                    pri = pick_primary(s["tags"], s.get("primary_tag"), a.auto_primary,
                                       f"q{n} 小问 {s['label']}", autos, n, str(s["label"]))
                    sub_bodies.append({"label": s["label"], "tags": s["tags"],
                                       "difficulty": d_val,
                                       "primary_tag": pri})
                body = {"subs": sub_bodies, "difficulty": None}
                desc = "subs[" + ", ".join(
                    f"{b['label']}:{','.join(b['tags']) or '∅'}" + (f"★{b['primary_tag']}" if b["primary_tag"] else "")
                    + (f"·{b['difficulty']}" if a.difficulty and b["difficulty"] else "")
                    for b in sub_bodies) + "]" + (f"（留空 {blank}）" if blank else "")
            else:
                tags = it.get("tags") or []
                pri = pick_primary(tags, it.get("primary_tag"), a.auto_primary, f"q{n} 整题", autos, n)
                d_val = pick_difficulty(it, cur_diff, a.difficulty, f"q{n}")
                body = {"tags": tags, "difficulty": d_val, "primary_tag": pri}
                desc = "tags=" + ",".join(tags) + (f" ★主={pri}" if pri else " （单标签·后端自补主）")
                if a.difficulty and d_val:
                    desc += f" · {d_val}"
        except (PrimaryError, DifficultyError) as e:
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
        if ignored_diff:
            print(f"ℹ️ 有 {len(ignored_diff)} 题在 decisions 里写了难度，但未加 --difficulty，"
                  f"本次**不写难度**、保留原值：{ignored_diff}")
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
