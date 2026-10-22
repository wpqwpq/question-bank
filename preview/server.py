#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""preview/server.py — 切割效果预览（只读，纯标准库）

用法：
    双击 start.cmd   或   python preview/server.py
    然后浏览器会自动打开 http://127.0.0.1:8645

它只做三件事：
    1. 列出 data/ 下有哪些「考试局 / 资格考试 / 学科」，让你先选范围
    2. 列出选中学科的所有试卷
    3. 把某一卷的 qNN.webp 按题号顺序显示出来

不建索引、不生成缩略图、不跑任何自动检查。
不写 data/ 下任何一个字节 —— 全部 open(..., 'rb') 只读。
"""

import json
import os
import re
import socket
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

APP_DIR = Path(__file__).resolve().parent
DATA = APP_DIR.parent / "data"

PORT_FIRST = 8645
PORT_TRIES = 10

SESSION_ORDER = {"jan": 1, "mar": 3, "may": 5, "june": 6, "oct": 10, "nov": 11, "july": 7}
SESSION_LABEL = {
    "jan": "Jan", "mar": "Mar", "may": "May", "june": "June",
    "july": "July", "oct": "Oct", "nov": "Nov",
}
# 罗马数字 token：用于把小问标签行折叠到一级（(b)(ii) → b）
ROMAN_TOKENS = {"i", "ii", "iii", "iv", "v", "vi", "vii", "viii", "ix", "x", "xi", "xii"}

SAFE = re.compile(r"^(?!.*\.\.)[A-Za-z0-9_.\-]+$")
QNAME = re.compile(r"^q(\d+)\.(webp|png|jpg|jpeg)$", re.I)


# ----------------------------------------------------------------------------
# 数据访问
# ----------------------------------------------------------------------------

def list_tree():
    """board -> {qual: {subject: 卷数}}，只收真的有卷目录的分支。"""
    out = {}
    if not DATA.is_dir():
        return out
    for board in sorted(DATA.iterdir()):
        if not board.is_dir() or board.name.startswith("_") or board.name == "taxonomy":
            continue
        quals = {}
        for qual in sorted(board.iterdir()):
            if not qual.is_dir() or qual.name.startswith("_"):
                continue
            subjects = {}
            for subj in sorted(qual.iterdir()):
                if not subj.is_dir() or subj.name.startswith("_"):
                    continue
                try:
                    with os.scandir(subj) as it:
                        n = sum(
                            1 for e in it
                            if e.is_dir() and not e.name.startswith("_")
                            and parse_paper_name(e.name)
                        )
                except OSError:
                    n = 0
                if n:
                    subjects[subj.name] = n
            if subjects:
                quals[qual.name] = subjects
        if quals:
            out[board.name] = quals
    return out


def parse_paper_name(name):
    parts = name.split("-")
    if len(parts) < 3 or not parts[0].isdigit():
        return None
    return {
        "year": int(parts[0]),
        "session": parts[1].lower(),
        "code": "-".join(parts[2:]),
    }


def count_questions(paper_dir):
    """数 assets/ 下 qNN.* 的个数，不读文件内容。"""
    assets = paper_dir / "assets"
    if not assets.is_dir():
        return 0
    try:
        with os.scandir(assets) as it:
            return sum(1 for e in it if QNAME.match(e.name))
    except OSError:
        return 0


def list_papers(board, qual, subject):
    base = DATA / board / qual / subject
    if not base.is_dir():
        return []
    items = []
    for p in base.iterdir():
        if not p.is_dir() or p.name.startswith("_"):
            continue
        meta = parse_paper_name(p.name)
        if not meta:
            continue
        meta["name"] = p.name
        meta["n"] = count_questions(p)
        items.append(meta)
    items.sort(
        key=lambda x: (
            -x["year"],
            -SESSION_ORDER.get(x["session"], 0),
            x["code"],
        )
    )
    return items


def load_paper(board, qual, subject, paper):
    """题列表以 assets/ 为准；cut.json 只用来补分值/题型/状态/跨页段数。"""
    base = DATA / board / qual / subject / paper
    assets = base / "assets"
    if not base.is_dir():
        raise FileNotFoundError(paper)

    by_no = {}
    cut = {}
    cut_path = base / "cut.json"
    if cut_path.is_file():
        try:
            with open(cut_path, "rb") as f:
                data = json.load(f)
            for q in data.get("questions") or []:
                by_no[str(q.get("number"))] = q
            cut["page_size"] = data.get("page_size")
            cut["source_pages"] = (data.get("source") or {}).get("pages")
        except (OSError, ValueError):
            pass

    questions = []
    entries = []
    if assets.is_dir():
        with os.scandir(assets) as it:
            entries = list(it)
    for e in entries:
        m = QNAME.match(e.name)
        if not m:
            continue
        no = int(m.group(1))
        info = by_no.get(str(no)) or {}
        regions = info.get("regions") or []
        try:
            st = e.stat()
            size, mtime = st.st_size, int(st.st_mtime)
        except OSError:
            size, mtime = 0, 0

        # 小问线：取自 layout.json，坐标已归一化到题图（0-1），直接按百分比画
        subs = []
        areas = []
        opts = []
        bottoms = {}
        block_y = None
        lay = assets / (e.name.rsplit(".", 1)[0] + ".layout.json")
        if lay.is_file():
            try:
                with open(lay, "rb") as f:
                    L = json.load(f)
                for s in L.get("subquestions") or []:
                    b = s.get("box") or {}
                    if isinstance(b.get("y"), (int, float)):
                        subs.append({
                            "label": str(s.get("label", "")),
                            "y": round(float(b["y"]), 4),
                            "h": round(float(b.get("h") or 0), 4),
                        })
                # 作答区（答题册另附卷 / 会计 01A）：每个小问一个 label，
                # boxes 可能有多条（点线压缩会把一段切成几条），逐条画线，
                # 只在每条 label 的第一条上打标签。
                for a in L.get("areas") or []:
                    lbl = str(a.get("label") or "")
                    ys = [float(b["y"]) for b in (a.get("boxes") or [])
                          if isinstance(b.get("y"), (int, float))]
                    if not ys:
                        continue
                    for b in a.get("boxes") or []:
                        if isinstance(b.get("y"), (int, float)):
                            areas.append({
                                "label": lbl,
                                "y": round(float(b["y"]), 4),
                                "h": round(float(b.get("h") or 0), 4),
                            })
                    # 该标签作答区的底（给下一条「向上扩到上一组底部」算切点用）
                    bottoms[lbl] = round(max(
                        float(b["y"]) + float(b.get("h") or 0)
                        for b in a["boxes"] if isinstance(b.get("y"), (int, float))), 4)
                # 选项热区（选择题）：render_questions 只在 question_type=mcq 且
                # cut.json 有 option_hotzones 时写 options，box 已归一化到题图。
                # 化学一题可有多套 A–D（part = a/b…），按套别上色。
                for o in L.get("options") or []:
                    b = o.get("box") or {}
                    lbl = str(o.get("label") or "")
                    if not lbl or not all(
                            isinstance(b.get(k), (int, float)) for k in ("x", "y", "w", "h")):
                        continue
                    opts.append({
                        "label": lbl,
                        "part": str(o.get("part") or ""),
                        "x": round(float(b["x"]), 4),
                        "y": round(float(b["y"]), 4),
                        "w": round(float(b["w"]), 4),
                        "h": round(float(b["h"]), 4),
                    })
                # 真实的切片起点：render_subsplit 的册内侧从 answer_block 顶
                # （册内题号行，含 "(a)(i)" 与右对齐分值行）开始，比 areas 高
                # 一行到两行。预览要画「切点」就必须用这个坐标。
                blk = info.get("answer_block") or []
                if blk and (L.get("src_map") or {}).get("segs"):
                    rg = blk[0]
                    for s in L["src_map"]["segs"]:
                        if s.get("kind") != "answer_block" or s.get("page") != rg.get("page"):
                            continue
                        pt0, pt1 = s.get("pt") or [0, 0]
                        i0, i1 = s.get("img") or [0, 0]
                        y = rg["bbox"][1]
                        if pt0 - 0.5 <= y <= pt1 + 0.5:
                            py = i0 if pt1 <= pt0 else i0 + (y - pt0) / (pt1 - pt0) * (i1 - i0)
                            hh = (L.get("size") or {}).get("h") or 1
                            block_y = round(py / hh, 4)
                            break
            except (OSError, ValueError):
                subs, areas, opts, bottoms, block_y = [], [], [], {}, None
        subs.sort(key=lambda x: x["y"])
        areas.sort(key=lambda x: x["y"])
        seen_lbl = set()
        for a in areas:               # 标出每个 label 的第一条，前端只在那里写标签
            a["first"] = a["label"] not in seen_lbl
            a["bottom"] = bottoms.get(a["label"], a["y"] + a["h"])
            seen_lbl.add(a["label"])

        questions.append({
            "no": no,
            "file": e.name,
            "bytes": size,
            # 重切重渲后文件名不变，浏览器只认 URL——带上 mtime 让 img 的查询串随之变化，
            # 换图一定重新下载（2026-10-19 两次「改完看不到」都是这个原因）。
            "ver": mtime,
            "marks": info.get("total_marks"),
            "type": info.get("question_type") or "",
            "section": info.get("section") or "",
            "status": info.get("status") or "",
            "regions": len(regions),
            "pages": sorted({r.get("page") for r in regions if r.get("page")}),
            "subs": subs,
            "areas": areas,
            "opts": opts,
            "n_opts": len(opts),
            "n_areas": len({a["label"] for a in areas}),
            "block_y": block_y,
            # 「N 个小问」按**一级**计数（(a)(b)(c)），与打标台 top_labels 同口径；
            # 虚线仍画全部层级——二级线用于判断内容/笔迹落在哪个 (i)(ii) 下，
            # 只是计数不该把它算成独立小问（2016 qp22 Q1 有 3 个一级、8 个标签行）。
            "n_subs": len({str(x.get("label", "")).split()[0] for x in subs
                           if str(x.get("label", "")).split()
                           and str(x.get("label", "")).split()[0] not in ROMAN_TOKENS})
            or len(info.get("subquestions") or []),
        })
    questions.sort(key=lambda x: x["no"])

    return {
        "board": board,
        "qual": qual,
        "subject": subject,
        "paper": paper,
        "paper_name": paper,
        "year": int(paper.split("-")[0]) if paper.split("-")[0].isdigit() else None,
        "session": paper.split("-")[1] if len(paper.split("-")) > 1 else "",
        "code": "-".join(paper.split("-")[2:]) if len(paper.split("-")) > 2 else "",
        "page_size": cut.get("page_size"),
        "source_pages": cut.get("source_pages"),
        "has_cut": bool(by_no),
        "questions": questions,
    }


# ----------------------------------------------------------------------------
# HTTP
# ----------------------------------------------------------------------------

MIME = {
    ".webp": "image/webp",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
}


def safe_path(*parts):
    """把用户传来的段拼成 DATA 下的真实路径；越界一律拒绝。"""
    for p in parts:
        if not p or not SAFE.match(p):
            raise PermissionError(p)
    target = DATA.joinpath(*parts).resolve()
    root = DATA.resolve()
    if target != root and root not in target.parents:
        raise PermissionError(str(target))
    return target


class Handler(BaseHTTPRequestHandler):
    server_version = "QBankPreview/1.0"
    protocol_version = "HTTP/1.1"

    # -- 工具 --
    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _err(self, code, msg):
        self._json({"error": msg}, code)

    def _file(self, path):
        try:
            st = path.stat()
        except OSError:
            return self._err(404, "not found")
        ctype = MIME.get(path.suffix.lower(), "application/octet-stream")
        etag = f'"{int(st.st_mtime)}-{st.st_size}"'
        if self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(st.st_size))
        self.send_header("ETag", etag)
        # no-cache 不是「不缓存」，而是「用前必须校验」——命中 ETag 回 304，未命中回新图。
        # 原先写 max-age=600：重切重渲后浏览器 10 分钟内一直拿旧图，看预览会以为没生效
        # （2026-10-19 经济卷踩过）。本机服务，每次校验的成本可以忽略。
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        try:
            with open(path, "rb") as f:
                while True:
                    chunk = f.read(262144)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        except (OSError, BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, fmt, *args):
        pass

    # -- 路由 --
    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            if u.path in ("/", "/index.html"):
                return self._html()
            if u.path == "/api/tree":
                return self._json(list_tree())
            if u.path == "/api/papers":
                b, ql, s = q.get("b", ""), q.get("q", ""), q.get("s", "")
                if not all(SAFE.match(x or "") for x in (b, ql, s)):
                    return self._err(400, "bad params")
                return self._json(list_papers(b, ql, s))
            if u.path == "/api/paper":
                b, ql, s, p = q.get("b", ""), q.get("q", ""), q.get("s", ""), q.get("p", "")
                if not all(SAFE.match(x or "") for x in (b, ql, s, p)):
                    return self._err(400, "bad params")
                try:
                    return self._json(load_paper(b, ql, s, p))
                except FileNotFoundError:
                    return self._err(404, "paper not found")
            if u.path == "/img":
                b, ql, s, p, f = (q.get("b", ""), q.get("q", ""), q.get("s", ""),
                                  q.get("p", ""), q.get("f", ""))
                return self._file(safe_path(b, ql, s, p, "assets", f))
            return self._err(404, "no route")
        except PermissionError:
            return self._err(403, "forbidden")
        except Exception as exc:  # noqa: BLE001
            return self._err(500, f"{type(exc).__name__}: {exc}")

    def _html(self):
        body = PAGE.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


# ----------------------------------------------------------------------------
# 页面
# ----------------------------------------------------------------------------

PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>切割预览</title>
<style>
:root{
  --ink:#16202c; --muted:#697786; --faint:#9aa6b2;
  --line:#e4e9ef; --bg:#f4f6f8; --panel:#fff;
  --accent:#1f5fa8; --soft:#eaf2fb; --soft2:#f0f4f8;
}
*{box-sizing:border-box}
html,body{height:100%}
body{margin:0;background:var(--bg);color:var(--ink);
  font:14px/1.6 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif;
  -webkit-font-smoothing:antialiased;overflow:hidden}
button{font:inherit;color:inherit;cursor:pointer;border:none;background:none}
h1,h2,h3{margin:0;font-weight:600}
.mono{font-family:Consolas,"Cascadia Mono",Menlo,monospace;font-variant-numeric:tabular-nums}

/* ---------- 选择页 ---------- */
#pick{height:100%;display:flex;align-items:center;justify-content:center;padding:32px}
.pick-card{width:100%;max-width:760px;background:var(--panel);border:1px solid var(--line);
  border-radius:14px;padding:36px 40px 32px;box-shadow:0 2px 16px rgba(20,32,48,.05)}
.pick-card h1{font-size:22px;letter-spacing:-.2px}
.pick-card .sub{color:var(--muted);font-size:13px;margin-top:6px}
.step{margin-top:26px}
.step-h{display:flex;align-items:baseline;gap:10px;margin-bottom:10px}
.step-n{width:20px;height:20px;border-radius:50%;background:var(--soft);color:var(--accent);
  font-size:11.5px;font-weight:600;display:flex;align-items:center;justify-content:center;flex:none}
.step-t{font-size:13px;font-weight:600}
.step-hint{font-size:12px;color:var(--faint)}
.opts{display:flex;flex-wrap:wrap;gap:8px;padding-left:30px;min-height:36px}
.opt{padding:7px 16px;border:1px solid var(--line);border-radius:8px;background:#fbfcfd;
  font-size:13.5px;transition:all .12s;display:flex;align-items:baseline;gap:8px}
.opt:hover{border-color:#c3d4e8;background:var(--soft2)}
.opt.on{border-color:var(--accent);background:var(--soft);color:var(--accent);font-weight:600}
.opt .cnt{font-size:11.5px;color:var(--faint);font-weight:400}
.opt.on .cnt{color:var(--accent);opacity:.7}
.dim{opacity:.45;pointer-events:none}
.go-row{margin-top:30px;padding-top:22px;border-top:1px solid var(--line);
  display:flex;align-items:center;justify-content:space-between}
.crumb{font-size:12.5px;color:var(--muted)}
.crumb b{color:var(--ink);font-weight:600}
.btn-go{padding:9px 26px;border-radius:8px;background:var(--accent);color:#fff;
  font-size:13.5px;font-weight:600;transition:opacity .12s}
.btn-go:disabled{background:#c9d3de;cursor:not-allowed}
.btn-go:not(:disabled):hover{opacity:.9}

/* ---------- 主界面 ---------- */
#app{height:100%;display:none}
#app.on{display:flex;flex-direction:column}
.topbar{height:50px;flex:none;background:var(--panel);border-bottom:1px solid var(--line);
  display:flex;align-items:center;gap:14px;padding:0 16px}
.back{display:flex;align-items:center;gap:6px;padding:5px 12px;border-radius:6px;
  font-size:13px;color:var(--muted);transition:background .12s}
.back:hover{background:var(--soft2);color:var(--ink)}
.bc{font-size:13.5px;font-weight:600}
.bc .sep{color:var(--faint);font-weight:400;margin:0 6px}
.bc .lite{font-weight:400;color:var(--muted)}
.top-right{margin-left:auto;display:flex;align-items:center;gap:10px}
.tool{padding:5px 12px;border:1px solid var(--line);border-radius:6px;font-size:12.5px;
  color:var(--muted);transition:all .12s}
.tool:hover{border-color:#c3d4e8;color:var(--ink)}
.tool.on{border-color:var(--accent);color:var(--accent);background:var(--soft)}
.stat{font-size:12.5px;color:var(--faint)}

.body{flex:1;display:flex;min-height:0}
/* 左栏 */
#side{width:262px;flex:none;background:var(--panel);border-right:1px solid var(--line);
  display:flex;flex-direction:column;min-height:0}
.search{padding:10px 12px;border-bottom:1px solid var(--line)}
.search input{width:100%;padding:6px 10px;border:1px solid var(--line);border-radius:6px;
  font:inherit;font-size:12.5px;outline:none}
.search input:focus{border-color:#b9cde4}
#papers{flex:1;overflow-y:auto;padding:6px 8px 20px;scrollbar-gutter:stable}
.yr{font-size:11px;font-weight:600;color:var(--faint);letter-spacing:.4px;
  padding:12px 8px 5px;position:sticky;top:0;background:var(--panel);z-index:1}
.prow{display:flex;align-items:center;gap:8px;padding:6px 9px;border-radius:6px;cursor:pointer;transition:background .1s}
.prow:hover{background:var(--soft2)}
.prow.on{background:var(--soft)}
.prow.on .pcode{color:var(--accent);font-weight:600}
.psess{font-size:11.5px;color:var(--faint);width:38px;flex:none}
.pcode{font-size:12.5px;font-family:Consolas,Menlo,monospace;flex:1;overflow:hidden;
  text-overflow:ellipsis;white-space:nowrap}
.pn{font-size:11px;color:var(--faint);font-variant-numeric:tabular-nums;flex:none}
.pn.zero{color:#d08a72}
/* 主区 */
#view{flex:1;min-width:0;overflow-y:auto;scroll-behavior:smooth;scrollbar-gutter:stable;
  background:var(--bg);position:relative}
.phead{position:sticky;top:0;z-index:6;background:rgba(244,246,248,.94);
  backdrop-filter:blur(6px);border-bottom:1px solid var(--line);padding:10px 24px 9px;
  display:flex;align-items:center;gap:14px}
.phead .t{font-size:15px;font-weight:600}
.phead .m{font-size:12px;color:var(--faint)}
.jump{margin-left:auto;display:flex;gap:3px;flex-wrap:wrap;justify-content:flex-end;max-width:52%}
.jd{width:20px;height:20px;border-radius:4px;background:#e7ecf2;color:var(--muted);
  font-size:10px;font-variant-numeric:tabular-nums;display:flex;align-items:center;
  justify-content:center;transition:all .1s}
.jd:hover{background:#cddcec}
.jd.on{background:var(--accent);color:#fff}
.qcard{margin:18px 24px;background:var(--panel);border:1px solid var(--line);border-radius:10px;
  overflow:hidden;scroll-margin-top:64px}
.qcard>header{display:flex;align-items:baseline;gap:10px;padding:9px 16px;
  border-bottom:1px solid #eef2f6;background:#fbfcfd}
.qno{font-size:13px;font-weight:600}
.qmeta{font-size:11.5px;color:var(--faint)}
.qmeta .warn{color:#c0703f}
.qimg{padding:14px 0 0;background:#fff;overflow-x:auto;overflow-y:hidden}
.qwrap{position:relative;width:var(--zoom,100%);margin:0 auto}
.qimg img{display:block;width:100%;height:auto;cursor:zoom-in}
.subs{position:absolute;inset:0;pointer-events:none;overflow:hidden}
.sline{position:absolute;left:0;right:0;height:0;border-top:3px dashed #e8590c}
.sline i{position:absolute;right:10px;top:-11px;font-style:normal;font-size:11px;line-height:1.5;
  font-weight:600;font-family:Consolas,Menlo,monospace;background:#e8590c;color:#fff;
  padding:0 6px;border-radius:3px}
.areas{position:absolute;inset:0;pointer-events:none;overflow:hidden}
.aline{position:absolute;left:0;right:0;height:0;border-top:2px dashed #1c7ed6}
.aline i{position:absolute;left:10px;top:-10px;font-style:normal;font-size:11px;line-height:1.5;
  font-weight:600;font-family:Consolas,Menlo,monospace;background:#1c7ed6;color:#fff;
  padding:0 6px;border-radius:3px}
#view.nosubs .subs{display:none}
#view.noareas .areas{display:none}
#view.nohots .hots{display:none}
/* 选项热区：选择题每个 A–D 选项一整块，绿色实线框；多套（a/b…）按套别换色 */
.hots{position:absolute;inset:0;pointer-events:none;overflow:hidden}
.hbox{position:absolute;border:2px solid #2f9e44;border-radius:3px;background:rgba(47,158,68,.07)}
.hbox i{position:absolute;right:100%;top:-2px;margin-right:2px;font-style:normal;font-size:10px;
  line-height:15px;font-weight:600;font-family:Consolas,Menlo,monospace;color:#fff;
  background:#2f9e44;padding:0 4px;border-radius:3px;white-space:nowrap}
.qmeta .an{color:#1c7ed6;font-weight:600}
.qmeta .sn{color:#e8590c;font-weight:600}
.qmeta .hn{color:#2f9e44;font-weight:600}
.zoom{display:flex;align-items:center;border:1px solid var(--line);border-radius:6px;overflow:hidden;background:var(--panel)}
.zb{width:26px;height:26px;font-size:15px;line-height:1;color:var(--muted);
  display:flex;align-items:center;justify-content:center}
.zb:hover{background:var(--soft2);color:var(--ink)}
.zv{min-width:54px;height:26px;font-size:12px;font-family:Consolas,Menlo,monospace;
  color:var(--muted);border-left:1px solid var(--line);border-right:1px solid var(--line);padding:0 6px}
.zv:hover{color:var(--ink);background:var(--soft2)}
.empty{padding:60px 24px;text-align:center;color:var(--faint);font-size:13px}
/* 放大 */
#lb{position:fixed;inset:0;background:rgba(18,26,36,.9);display:none;z-index:50;
  overflow:auto;padding:24px;cursor:zoom-out}
#lb.on{display:block}
#lb img{display:block;margin:0 auto;background:#fff;border-radius:4px;max-width:none}
::-webkit-scrollbar{width:10px;height:10px}
::-webkit-scrollbar-thumb{background:#cfd8e2;border-radius:5px;border:2px solid var(--bg)}
::-webkit-scrollbar-track{background:transparent}
</style>
</head>
<body>

<div id="pick">
  <div class="pick-card">
    <h1>切割预览</h1>
    <div class="sub">选一个范围，然后逐卷看图。只读，不改任何数据。</div>

    <div class="step">
      <div class="step-h"><span class="step-n">1</span><span class="step-t">考试局</span></div>
      <div class="opts" id="opt-board"></div>
    </div>
    <div class="step" id="step-qual">
      <div class="step-h"><span class="step-n">2</span><span class="step-t">资格考试</span>
        <span class="step-hint" id="hint-qual">先选考试局</span></div>
      <div class="opts dim" id="opt-qual"></div>
    </div>
    <div class="step" id="step-subj">
      <div class="step-h"><span class="step-n">3</span><span class="step-t">学科</span>
        <span class="step-hint" id="hint-subj">先选资格考试</span></div>
      <div class="opts dim" id="opt-subj"></div>
    </div>

    <div class="go-row">
      <div class="crumb" id="crumb">尚未选择</div>
      <button class="btn-go" id="go" disabled>进入</button>
    </div>
  </div>
</div>

<div id="app">
  <div class="topbar">
    <button class="back" id="back">&#8592; 重选</button>
    <div class="bc" id="bc"></div>
    <div class="top-right">
      <div class="stat" id="stat"></div>
      <button class="tool on" id="subsbtn" title="显示 / 隐藏小问切割线">小问线</button>
      <button class="tool on" id="areasbtn" title="显示 / 隐藏答题区切割线（答题册另附卷）">作答区线</button>
      <button class="tool on" id="hotsbtn" title="显示 / 隐藏选择题选项热区（A–D 选项框，只有切出了热区的选择题才有）">热区</button>
      <div class="zoom">
        <button class="zb" id="zout" title="缩小 5%">&#8722;</button>
        <button class="zv" id="zval" title="点击回到 100%">100%</button>
        <button class="zb" id="zin" title="放大 5%">+</button>
      </div>
    </div>
  </div>
  <div class="body">
    <aside id="side">
      <div class="search"><input id="q" placeholder="筛选卷号，如 wph11"></div>
      <div id="papers"></div>
    </aside>
    <main id="view"></main>
  </div>
</div>

<div id="lb"><img alt=""></div>

<script>
const $ = (s) => document.querySelector(s);
const TREE = {};
const S = { b: null, q: null, s: null, papers: [], cur: null, meta: null };
const SLABEL = { jan:'Jan', mar:'Mar', may:'May', june:'June', july:'July', oct:'Oct', nov:'Nov' };
// 选项热区的套别配色（part=a → 绿，part=b → 紫…）；与橙(小问线)/蓝(作答区线)区分开
const HOTPART = ['#2f9e44', '#7048e8', '#0c8599', '#d6336c'];

function esc(s){ return String(s == null ? '' : s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }
function fmtK(n){ return n >= 1000 ? (n/1000).toFixed(1).replace(/\.0$/,'') + 'k' : String(n); }

async function api(path){
  const r = await fetch(path);
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

/* ---------------- 选择页 ---------------- */
function chip(label, sub, on){
  return `<button class="opt${on?' on':''}">${esc(label)}${sub?`<span class="cnt">${esc(sub)}</span>`:''}</button>`;
}

function sumSub(v){ let n = 0; for (const s in v) n += v[s]; return n; }

function renderBoard(){
  const boards = Object.keys(TREE);
  $('#opt-board').innerHTML = boards.map(b => {
    let n = 0;
    for (const q in TREE[b]) n += sumSub(TREE[b][q]);
    return chip(b.toUpperCase(), n + ' 卷', S.b === b);
  }).join('');
  [...$('#opt-board').children].forEach((el, i) => {
    el.onclick = () => {
      S.b = boards[i]; S.q = null; S.s = null;
      renderBoard(); renderQual(); renderSubj(); renderCrumb();
    };
  });
}

function renderQual(){
  const box = $('#opt-qual');
  if (!S.b){ box.innerHTML = ''; box.classList.add('dim'); $('#hint-qual').textContent = '先选考试局'; return; }
  const quals = Object.keys(TREE[S.b]);
  box.classList.remove('dim');
  $('#hint-qual').textContent = quals.length + ' 个';
  box.innerHTML = quals.map(q => chip(q, sumSub(TREE[S.b][q]) + ' 卷', S.q === q)).join('');
  [...box.children].forEach((el, i) => {
    el.onclick = () => { S.q = quals[i]; S.s = null; renderQual(); renderSubj(); renderCrumb(); };
  });
}

function renderSubj(){
  const box = $('#opt-subj');
  if (!S.b || !S.q){ box.innerHTML = ''; box.classList.add('dim'); $('#hint-subj').textContent = '先选资格考试'; return; }
  const subs = Object.keys(TREE[S.b][S.q]);
  box.classList.remove('dim');
  $('#hint-subj').textContent = subs.length + ' 个';
  box.innerHTML = subs.map(s => chip(s, TREE[S.b][S.q][s] + ' 卷', S.s === s)).join('');
  [...box.children].forEach((el, i) => {
    el.onclick = () => { S.s = subs[i]; renderSubj(); renderCrumb(); };
  });
}

function renderCrumb(){
  const ok = S.b && S.q && S.s;
  $('#go').disabled = !ok;
  $('#crumb').innerHTML = ok
    ? `<b>${esc(S.b)}</b> · ${esc(S.q)} · ${esc(S.s)}`
    : '尚未选择';
}

$('#go').onclick = () => enter();

/* ---------------- 主界面 ---------------- */
async function enter(prefer){
  $('#pick').style.display = 'none';
  $('#app').classList.add('on');
  $('#bc').innerHTML = `<span>${esc(S.b.toUpperCase())}</span><span class="sep">/</span>
    <span class="lite">${esc(S.q)}</span><span class="sep">/</span><span>${esc(S.s)}</span>`;
  $('#stat').textContent = '加载中…';
  try{
    S.papers = await api(`/api/papers?b=${S.b}&q=${S.q}&s=${S.s}`);
  }catch(e){
    S.papers = [];
  }
  const totalQ = S.papers.reduce((a, p) => a + p.n, 0);
  $('#stat').textContent = `${S.papers.length} 卷 · ${fmtK(totalQ)} 题`;
  renderPapers();
  if (!S.papers.length){
    $('#view').innerHTML = '<div class="empty">这个学科下没有卷</div>';
    return;
  }
  const hit = prefer && S.papers.some(p => p.name === prefer) ? prefer : S.papers[0].name;
  openPaper(hit);
}

$('#back').onclick = () => {
  $('#app').classList.remove('on');
  $('#pick').style.display = 'flex';
  $('#view').innerHTML = '';
  S.cur = null; S.meta = null;
  history.replaceState(null, '', location.pathname);
};

function renderPapers(){
  const kw = $('#q').value.trim().toLowerCase();
  const list = kw ? S.papers.filter(p => p.name.toLowerCase().includes(kw)) : S.papers;
  let html = '', lastYear = null;
  for (const p of list){
    if (p.year !== lastYear){ html += `<div class="yr">${p.year}</div>`; lastYear = p.year; }
    html += `<div class="prow${S.cur===p.name?' on':''}" data-p="${esc(p.name)}">
      <span class="psess">${esc(SLABEL[p.session] || p.session)}</span>
      <span class="pcode">${esc(p.code.toUpperCase())}</span>
      <span class="pn${p.n?'':' zero'}">${p.n||'—'}</span></div>`;
  }
  $('#papers').innerHTML = html || '<div class="empty">没有匹配的卷</div>';
  [...$('#papers').querySelectorAll('.prow')].forEach(el => {
    el.onclick = () => openPaper(el.dataset.p);
  });
}

$('#q').oninput = () => renderPapers();

async function openPaper(name){
  S.cur = name;
  history.replaceState(null, '', '#' + [S.b, S.q, S.s, name].join('/'));
  [...$('#papers').querySelectorAll('.prow')].forEach(el => {
    el.classList.toggle('on', el.dataset.p === name);
  });
  const v = $('#view');
  v.innerHTML = '<div class="empty">读取中…</div>';
  try{
    S.meta = await api(`/api/paper?b=${S.b}&q=${S.q}&s=${S.s}&p=${encodeURIComponent(name)}`);
  }catch(e){
    v.innerHTML = '<div class="empty">这一卷读不出来：' + esc(e.message) + '</div>';
    return;
  }
  const m = S.meta;
  const qs = m.questions;
  const totalMarks = qs.reduce((a, x) => a + (x.marks || 0), 0);
  const jumps = qs.map(x => `<button class="jd" data-n="${x.no}">${x.no}</button>`).join('');
  v.innerHTML = `
    <div class="phead">
      <span class="t">${m.year} ${esc(SLABEL[m.session]||m.session)} · ${esc((m.code||'').toUpperCase())}</span>
      <span class="m">${qs.length} 题${totalMarks?` · 共 ${totalMarks} 分`:''}${m.source_pages?` · 原卷 ${m.source_pages} 页`:''}${m.has_cut?'':' · 无 cut.json'}</span>
      <span class="jump">${jumps}</span>
    </div>
    ${qs.length ? '' : '<div class="empty">这一卷还没有切好的题图</div>'}
    ${qs.map(x => {
      const bits = [];
      if (x.marks != null) bits.push(x.marks + ' 分');
      if (x.type) bits.push(esc(x.type.toUpperCase()));
      if (x.section) bits.push('Section ' + esc(x.section));
      if (x.n_subs) bits.push(`<span class="sn">${x.n_subs} 个小问</span>`);
      if (x.n_areas) bits.push(`<span class="an">作答区 ${x.n_areas} 段</span>`);
      if (x.n_opts) bits.push(`<span class="hn">热区 ${x.n_opts}</span>`);
      if (x.regions > 1) bits.push(`<span class="warn">跨 ${x.regions} 段${x.pages.length?`（P${x.pages.join(',')}）`:''}</span>`);
      if (x.status && x.status !== 'auto') bits.push(esc(x.status));
      const lines = (x.subs || []).map(s =>
        `<div class="sline" style="top:${(s.y * 100).toFixed(2)}%"><i>${esc(s.label)}</i></div>`
      ).join('');
      // 蓝线 = **真实切点**（render_subsplit 的册内侧起点），不是 areas 的起点：
      // 首组从 answer_block 顶（册内题号行）开始，其余组从上一组作答区底部向上扩 2px。
      // 直接画 areas.y 会低一行到两行，跟切出来的子图对不上。
      let prevBottom = null;
      const aFirst = (x.areas || []).filter(a => a.first).map((a, i) => {
        let y = a.y;
        if (i === 0 && x.block_y != null) y = x.block_y;
        else if (prevBottom != null) y = Math.min(prevBottom, a.y);
        prevBottom = a.bottom;
        return {label: a.label, y: y};
      });
      const alines = aFirst.map(a =>
        `<div class="aline" style="top:${(a.y * 100).toFixed(2)}%"><i>${esc(a.label)}</i></div>`
      ).join('');
      // 选项热区：one box per A–D。标签挂在框**左外侧**（框内左上角正好压住选项字母），
      // 套别按出现顺序取色，同一题的两套 A–D 不会看混。
      const pc = new Map();
      (x.opts || []).forEach(o => {
        if (o.part && !pc.has(o.part)) pc.set(o.part, HOTPART[pc.size % HOTPART.length]);
      });
      const hots = (x.opts || []).map(o => {
        const c = o.part ? pc.get(o.part) : HOTPART[0];
        const box = `left:${(o.x*100).toFixed(2)}%;top:${(o.y*100).toFixed(2)}%;`
                  + `width:${(o.w*100).toFixed(2)}%;height:${(o.h*100).toFixed(2)}%;`
                  + `border-color:${c};background:${c}12`;
        return `<div class="hbox" style="${box}"><i style="background:${c}">`
             + `${o.part ? esc(o.part) + '·' : ''}${esc(o.label)}</i></div>`;
      }).join('');
      return `<article class="qcard" id="q${x.no}">
        <header><span class="qno">第 ${x.no} 题</span>
          <span class="qmeta">${bits.join(' · ')}</span></header>
        <div class="qimg"><div class="qwrap">${lines ? `<div class="subs">${lines}</div>` : ''}${alines ? `<div class="areas">${alines}</div>` : ''}${hots ? `<div class="hots">${hots}</div>` : ''}<img loading="lazy" decoding="async"
          src="/img?b=${S.b}&q=${S.q}&s=${S.s}&p=${encodeURIComponent(name)}&f=${x.file}&v=${x.ver || 0}"
          alt="第 ${x.no} 题"></div></div>
      </article>`;
    }).join('')}`;
  v.scrollTop = 0;
  bindPaper();
}

function bindPaper(){
  const v = $('#view');
  const cards = [...v.querySelectorAll('.qcard')];
  const dots = new Map([...v.querySelectorAll('.jd')].map(d => [+d.dataset.n, d]));
  const mine = S.cur;

  v.querySelectorAll('.jd').forEach(d => {
    d.onclick = () => {
      const el = document.getElementById('q' + d.dataset.n);
      if (el) el.scrollIntoView({block:'start'});
    };
  });

  function syncDot(){
    if (S.cur !== mine) return;
    const mid = v.scrollTop + 90;
    let cur = cards[0];
    for (const c of cards){
      if (c.offsetTop <= mid) cur = c; else break;
    }
    if (!cur) return;
    const no = +cur.id.slice(1);
    for (const [n, d] of dots) d.classList.toggle('on', n === no);
  }

  v.querySelectorAll('.qimg img').forEach(img => {
    img.onclick = () => { $('#lb img').src = img.src; $('#lb').classList.add('on'); };
    if (!img.complete) img.onload = syncDot;
  });

  let raf = 0;
  v.onscroll = () => {
    if (raf) return;
    raf = requestAnimationFrame(() => { raf = 0; syncDot(); });
  };
  syncDot();
}

$('#lb').onclick = () => $('#lb').classList.remove('on');

$('#subsbtn').onclick = e => {
  const off = $('#view').classList.toggle('nosubs');
  e.target.classList.toggle('on', !off);
};

$('#areasbtn').onclick = e => {
  const off = $('#view').classList.toggle('noareas');
  e.target.classList.toggle('on', !off);
};

$('#hotsbtn').onclick = e => {
  const off = $('#view').classList.toggle('nohots');
  e.target.classList.toggle('on', !off);
};

let zoom = 100;
function setZoom(v){
  zoom = Math.max(50, Math.min(300, Math.round(v / 5) * 5));
  $('#zval').textContent = zoom + '%';
  $('#view').style.setProperty('--zoom', zoom + '%');
  if ($('#view').onscroll) $('#view').dispatchEvent(new Event('scroll'));
}
$('#zin').onclick = () => setZoom(zoom + 5);
$('#zout').onclick = () => setZoom(zoom - 5);
$('#zval').onclick = () => setZoom(100);
$('#view').addEventListener('wheel', e => {
  if (!e.ctrlKey) return;
  e.preventDefault();
  setZoom(zoom + (e.deltaY < 0 ? 5 : -5));
}, {passive: false});

document.addEventListener('keydown', e => {
  if (e.key === 'Escape'){ $('#lb').classList.remove('on'); return; }
  if (!S.meta) return;
  if (e.target.tagName === 'INPUT') return;
  if (e.key === 'j' || e.key === 'J'){ step(1); e.preventDefault(); }
  if (e.key === 'k' || e.key === 'K'){ step(-1); e.preventDefault(); }
  if (e.key === '[' || e.key === ']'){
    const i = S.papers.findIndex(p => p.name === S.cur);
    const n = S.papers[i + (e.key === ']' ? 1 : -1)];
    if (n) openPaper(n.name);
    e.preventDefault();
  }
});

function step(dir){
  const v = $('#view');
  const cards = [...v.querySelectorAll('.qcard')];
  const y = v.scrollTop;
  if (dir > 0){
    const t = cards.find(c => c.offsetTop > y + 24);
    if (t) t.scrollIntoView({ block: 'start' });
  } else {
    const prev = cards.filter(c => c.offsetTop < y - 24);
    if (prev.length) prev[prev.length - 1].scrollIntoView({ block: 'start' });
  }
}

/* ---------------- 启动 ---------------- */
(async () => {
  try{
    const t = await api('/api/tree');
    Object.assign(TREE, t);
    renderBoard(); renderQual(); renderSubj(); renderCrumb();
    const h = decodeURIComponent(location.hash.replace(/^#\/?/, '')).split('/');
    if (h.length >= 3 && TREE[h[0]] && TREE[h[0]][h[1]] && TREE[h[0]][h[1]][h[2]] != null){
      S.b = h[0]; S.q = h[1]; S.s = h[2];
      renderBoard(); renderQual(); renderSubj(); renderCrumb();
      enter(h[3] || null);
    }
  }catch(e){
    $('#opt-board').innerHTML = '<span style="color:#c0392b">读取 data/ 失败：'
      + esc(e.message) + '</span>';
  }
})();
</script>
</body>
</html>
"""


# ----------------------------------------------------------------------------
# 启动
# ----------------------------------------------------------------------------

def pick_port():
    for i in range(PORT_TRIES):
        port = PORT_FIRST + i
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise SystemExit("8645-8654 全被占用，先关掉占用的进程")


def main():
    if not DATA.is_dir():
        raise SystemExit(f"找不到数据目录：{DATA}")

    tree = list_tree()
    if not tree:
        raise SystemExit(f"{DATA} 下没有可用的考试局目录")

    n_papers = 0
    for b, quals in tree.items():
        for q, subs in quals.items():
            for s in subs:
                try:
                    with os.scandir(DATA / b / q / s) as it:
                        n_papers += sum(
                            1 for e in it
                            if e.is_dir() and not e.name.startswith("_")
                            and parse_paper_name(e.name)
                        )
                except OSError:
                    pass

    port = pick_port()
    url = f"http://127.0.0.1:{port}"
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    srv.daemon_threads = True

    print(f"  数据  {DATA}")
    print(f"  范围  " + " · ".join(
        f"{b}/{q}/{'|'.join(s)}" for b, quals in tree.items() for q, s in quals.items()))
    print(f"  规模  {n_papers} 卷")
    print(f"  地址  {url}")
    print("  Ctrl+C 停止")
    if not os.environ.get("PREVIEW_NO_OPEN"):
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n  已停止")


if __name__ == "__main__":
    main()
