#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""拉取一张试卷的打标上下文（云端打标台）：逐题结构 + 候选标签 + 题图。

用法：
    python fetch_paper.py <卷> [--json out.json] [--images DIR] [--base URL]

`<卷>` 可以给三种写法，自动解析成云端 paper id：
    · 完整 id：edexcel-a-level-physics-2020-jan-wph11
    · 会话目录名：2020-jan-wph11（跨考试局重名时要求给完整 id）
    · 末尾卷号：wph11（唯一命中时可用）

输出 JSON：
    {paper, board, qual, subject, unit, count, images_dir, items:[
        {n, qid, type, marks, split_eligible, top_labels, image_url, image_local,
         tags_now, primary_now, status,
         available_tags:[{id,zh,en,group}], available_groups:[{id,zh,unit}]}
    ]}

`primary_now` 是现状主标签（读侧已按 effective_primary 回退解析）；打新标签时**不要沿用**它，
必须按 references/rules.md「主标签怎么选」重新判一次。

`available_tags` 每项带 `en`（英文术语名，语义拿不准时以它为准）与 `group`（所属组 id）。

⚠️ 云端题图在 `/files/...`，**必须带 Bearer**。加 `--images DIR` 会把每题的 qNN.webp
   下载到 DIR（已存在且非空则跳过），输出里的 `image_local` 就是给 Read 工具看的路径。
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# 默认打云端；云端是本技能唯一支持的目标
DEFAULT_BASE = os.environ.get("QBANK_BASE", "http://140.143.229.85")
TRIES = 4
TIMEOUT = 60


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

# ⚠️ 本机常带 HTTP_PROXY/HTTPS_PROXY（WorkBuddy 自带的本地代理），urllib 会照走，
#    打云端裸 IP 一律 502 Bad Gateway。这里**强制不走代理**，只直连打标台。
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _request(url: str, data: bytes | None = None, method: str | None = None):
    return urllib.request.Request(url, data=data, method=method, headers=_AUTH)


def get(url, tries=TRIES, timeout=TIMEOUT):
    """GET + 重试。云端网络偶发抖动（单请求 0.05–7 s 都出现过），一次失败不代表服务挂。"""
    last = None
    for i in range(tries):
        try:
            with _OPENER.open(_request(url), timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code < 500:            # 401/404/422 重试没有意义，直接抛
                raise
            last = e
        except Exception as e:          # noqa: BLE001  连接超时 / 重置 / 抖动
            last = e
        time.sleep(1.5 * (i + 1))
    raise last


def download(url, dest: Path, tries=TRIES, timeout=TIMEOUT):
    """下载题图（带 Bearer）。已存在且非空就跳过。"""
    if dest.exists() and dest.stat().st_size > 0:
        return False
    last = None
    for i in range(tries):
        try:
            with _OPENER.open(_request(url), timeout=timeout) as r:
                blob = r.read()
            dest.write_bytes(blob)
            return True
        except Exception as e:          # noqa: BLE001
            last = e
        time.sleep(1.5 * (i + 1))
    raise last


def resolve_paper(papers: list[dict], arg: str) -> dict:
    """把用户给的写法解析成唯一的卷。命中多个一律报错，绝不瞎猜。"""
    arg = arg.strip()
    exact = [p for p in papers if p["id"] == arg]
    if exact:
        return exact[0]
    suffix = [p for p in papers if p["id"].endswith("-" + arg)]
    if len(suffix) == 1:
        return suffix[0]
    if len(suffix) > 1:
        raise SystemExit(
            f"「{arg}」命中 {len(suffix)} 卷，请给完整 id：\n  "
            + "\n  ".join(p["id"] for p in suffix[:12]))
    tail = [p for p in papers if re.search(rf"-{re.escape(arg)}$", p["id"])]
    if len(tail) == 1:
        return tail[0]
    near = [p["id"] for p in papers if arg in p["id"]][:8]
    raise SystemExit(f"找不到卷 {arg}。" + (f" 相近候选：{near}" if near else ""))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("paper", help="卷：完整 id / 会话目录名 / 卷号，如 2020-jan-wph11")
    ap.add_argument("--json", help="输出文件；缺省打到 stdout")
    ap.add_argument("--images", help="把每题题图下载到这个目录（推荐给，Read 工具要用）")
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--key", help="API Key；缺省读 QBANK_API_KEY 或 ~/.qbank_key")
    a = ap.parse_args()

    k = a.key or _api_key()
    if not k:
        raise SystemExit("缺 API Key：放一行到 ~/.qbank_key，或传 --key / QBANK_API_KEY")
    _AUTH["Authorization"] = f"Bearer {k}"

    try:
        papers = get(a.base + "/api/papers")
    except urllib.error.HTTPError as e:
        raise SystemExit(f"打标台拒绝（HTTP {e.code}）：云端要 API Key，401 = Key 失效。见 references/pitfalls.md")
    except Exception as e:  # noqa: BLE001
        raise SystemExit(f"打标台不可达（{a.base}）：{e}\n见 references/pitfalls.md（代理变量 / 网络抖动）")

    p = resolve_paper(papers, a.paper)
    qs = get(f"{a.base}/api/papers/{p['id']}/questions")

    img_dir = Path(a.images) if a.images else None
    if img_dir:
        img_dir.mkdir(parents=True, exist_ok=True)

    out = {"paper": p["id"], "board": p["board"], "qual": p["qualification"],
           "subject": p["subject"], "unit": p["unit"], "count": len(qs),
           "images_dir": str(img_dir) if img_dir else None, "items": []}

    n_tags = 0
    for i, q in enumerate(qs, 1):
        d = get(f"{a.base}/api/questions/{q['qid']}")
        m = d["meta"]
        n_tags = len(d["available_tags"])
        n = m.get("question_number")
        local = None
        if img_dir and d.get("image_url"):
            safe = re.sub(r"[^0-9A-Za-z._-]", "_", str(n))
            dest = img_dir / f"q{safe.zfill(2)}.webp"
            try:
                download(a.base + d["image_url"], dest)
                local = str(dest)
            except Exception as e:  # noqa: BLE001
                print(f"  ! q{n} 题图下载失败：{e}", file=sys.stderr)
        out["items"].append({
            "n": n,
            "qid": q["qid"],
            "type": m.get("question_type"),
            "marks": m.get("total_marks"),
            "split_eligible": d["split_eligible"],
            "top_labels": d["top_labels"],
            "image_url": d["image_url"],
            "image_local": local,
            "tags_now": d["tags"],
            "primary_now": d.get("primary_tag"),
            "status": q.get("status"),
            "available_tags": [{"id": t["id"], "zh": t["zh"], "en": t.get("en") or "",
                                "group": t.get("group") or ""} for t in d["available_tags"]],
            "available_groups": [{"id": g["id"], "zh": g["zh"], "unit": g.get("unit")}
                                 for g in d["available_groups"]],
        })
        print(f"  [{i}/{len(qs)}] q{n}" + ("  (可拆)" if d["split_eligible"] else ""),
              file=sys.stderr)

    text = json.dumps(out, ensure_ascii=False, indent=1)
    if a.json:
        with open(a.json, "w", encoding="utf-8", newline="") as f:
            f.write(text + "\n")
        n_split = sum(1 for x in out["items"] if x["split_eligible"])
        print(f"✅ {out['paper']}：{out['count']} 题（按小问打标 {n_split}）"
              f"· 单元 {out['unit']} · 候选标签 {n_tags} 个 → {a.json}")
        if img_dir:
            got = sum(1 for x in out["items"] if x["image_local"])
            print(f"   题图：{got}/{out['count']} 张 → {img_dir}")
    else:
        print(text)


if __name__ == "__main__":
    main()
