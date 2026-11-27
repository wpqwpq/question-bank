#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
作业布置平台 - 本地服务器
1) 静态托管 web/ 目录下的选题网页
2) /api/image?path=<远程路径> 图片代理：服务端携带题库会话 cookie 拉取截图，
   解决浏览器直连远程图片 401（HttpOnly cookie + 跨域）的问题
3) /api/bank/* 提供本地题库快照 JSON

只读约束：本服务器对题库服务器仅发起 GET，不发起任何写操作。
"""
import json, os, sys, urllib.request, urllib.parse, mimetypes, re, datetime
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
import export_engine

ROOT = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(ROOT, "web")
DATA = os.path.join(ROOT, "data")
TEXTBANK = os.path.join(ROOT, "textbank")   # 题目文字库（整题粒度 JSONL，只读）
DRAFTS = os.path.join(ROOT, ".drafts")          # 本地临时草稿文件夹（未发布时自动保存）
DRAFT_FILE = os.path.join(DRAFTS, "draft.json")
REMOTE = "http://140.143.229.85"
# 部署到题库服务器本机后，可直接只读读取题图文件，无需会话 cookie。
QDATA = "/opt/qbank/data"
# 图片代理优先使用可跨科目读取的会话；化学账号作为回退。
COOKIE_ADMIN = os.path.join(ROOT, ".session_cookie_admin")
COOKIE_FILE = os.path.join(ROOT, ".session_cookie")

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8080


def load_cookie():
    for fp in (COOKIE_ADMIN, COOKIE_FILE):
        try:
            v = open(fp, encoding="utf-8").read().strip()
            if v:
                return v
        except Exception:
            pass
    return ""


def local_image_path(remote_path):
    """把 /files/board/qual/subject/... 映射到题库服务器上的只读文件路径。
    仅做路径规范化与前缀校验，绝不写入。"""
    if not remote_path.startswith("/files/"):
        return None
    rel = remote_path[len("/files/"):]
    parts = rel.split("/")
    if not rel or any(p in ("", ".", "..") for p in parts):
        return None
    fp = os.path.normpath(os.path.join(QDATA, *parts))
    if fp != QDATA and not fp.startswith(QDATA + os.sep):
        return None
    return fp


# ---- 关键词检索：题目文字库（textbank/<subject>__<board>__<qual>.jsonl） ----
SESSION_ORDER = {'jan': 1, 'feb': 2, 'mar': 3, 'apr': 4, 'may': 5, 'june': 6,
                 'july': 7, 'aug': 8, 'sep': 9, 'oct': 10, 'nov': 11, 'dec': 12}


def unit_sort_key(unit):
    """单元排序：提取数字，U1<U2<U10；无数字的按文字排。"""
    m = re.search(r'(\d+)', str(unit or ''))
    if m:
        return (0, int(m.group(1)), str(unit))
    return (1, 0, str(unit or ''))


def search_sort_key(r):
    """先按单元（卷），再按年月（年+月序号），同卷内按题号。"""
    ym = SESSION_ORDER.get(str(r.get('session', '')).lower(), 99)
    qn = re.search(r'(\d+)', str(r.get('question_number', '')))
    return (unit_sort_key(r.get('unit')), int(r.get('year') or 0), ym,
            str(r.get('paper_code', '')), int(qn.group(1)) if qn else 0, str(r.get('qid', '')))


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=WEB, **kw)

    def log_message(self, fmt, *args):
        pass  # 静默，避免刷屏

    def end_headers(self):
        """静态网页资源禁用缓存，避免改了 JS/CSS 后浏览器仍用旧副本。
        API 图片/JSON 各自已设置缓存策略，此处只处理静态文件。"""
        if getattr(self, "_static", False):
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
        super().end_headers()

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _stream_init(self):
        """初始化 NDJSON 分块流：无 Content-Length，每条命中即刻写出并 flush。"""
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")   # 关闭 nginx 缓冲，保证逐条到达浏览器
        self.end_headers()

    def _stream_line(self, obj):
        body = (json.dumps(obj, ensure_ascii=False, separators=(',', ':')) + '\n').encode("utf-8")
        self.wfile.write(body)
        self.wfile.flush()

    def _draft_read(self):
        try:
            with open(DRAFT_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None

    def _draft_write(self, obj):
        os.makedirs(DRAFTS, exist_ok=True)
        tmp = DRAFT_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False)
        os.replace(tmp, DRAFT_FILE)

    def do_DELETE(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/draft":
            try:
                if os.path.exists(DRAFT_FILE):
                    os.remove(DRAFT_FILE)
                return self._json({"ok": True})
            except Exception as e:
                return self._json({"error": str(e)[:120]}, 500)
        return self._json({"error": "not found"}, 404)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/draft":
            try:
                length = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                self._draft_write(payload)
                return self._json({"ok": True})
            except Exception as e:
                return self._json({"error": str(e)[:120]}, 500)
        if parsed.path == "/api/export":
            try:
                length = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                fmt = payload.get("format", "pdf")
                items = payload.get("items", [])
                if not items:
                    return self._json({"error": "没有可导出的题目"}, 400)
                pages = export_engine.build_pages(items)
                if fmt == "docx":
                    data = export_engine.to_docx(pages)
                    ctype = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
                    ext = "docx"
                else:
                    data = export_engine.to_pdf(pages)
                    ctype = "application/pdf"
                    ext = "pdf"
                fname = "export-" + datetime.datetime.now().strftime("%Y%m%d-%H%M%S") + "." + ext
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Disposition", 'attachment; filename="' + fname + '"')
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except Exception as e:
                self._json({"error": str(e)[:160]}, 500)
            return
        return self._json({"error": "not found"}, 404)

    def do_GET(self):
        self._static = False
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)

        if path == "/api/image":
            # 图片代理
            remote_path = qs.get("path", [""])[0]
            if not remote_path.startswith("/files/"):
                return self._json({"error": "bad path"}, 400)
            # 部署在题库服务器本机时直接读文件（只读），避免 cookie 过期导致题图失效
            fp = local_image_path(remote_path)
            if fp and os.path.isfile(fp):
                with open(fp, "rb") as f:
                    data = f.read()
                ctype = mimetypes.guess_type(fp)[0] or "image/webp"
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            ck = load_cookie()
            if not ck:
                return self._json({"error": "no session cookie"}, 503)
            try:
                req = urllib.request.Request(
                    REMOTE + remote_path,
                    headers={"Cookie": f"qb_session={ck}", "User-Agent": "Mozilla/5.0"},
                )
                for attempt in range(6):
                    try:
                        with urllib.request.urlopen(req, timeout=25) as r:
                            data = r.read()
                            ctype = r.headers.get("Content-Type", "image/webp")
                            self.send_response(200)
                            self.send_header("Content-Type", ctype)
                            self.send_header("Cache-Control", "public, max-age=86400")
                            self.send_header("Content-Length", str(len(data)))
                            self.end_headers()
                            self.wfile.write(data)
                            return
                    except Exception:
                        if attempt == 5:
                            raise
            except Exception as e:
                return self._json({"error": str(e)[:80]}, 502)
            return

        if path == "/api/layout":
            # 题目布局 JSON 代理（只读）：与 /api/image 同一套路径校验，
            # 供前端按小问边界做安全切分/裁剪，绝不写入题库。
            remote_path = qs.get("path", [""])[0]
            if not remote_path.startswith("/files/") or not remote_path.endswith(".json"):
                return self._json({"error": "bad path"}, 400)
            fp = local_image_path(remote_path)
            if fp and os.path.isfile(fp):
                with open(fp, "rb") as f:
                    data = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            ck = load_cookie()
            if not ck:
                return self._json({"error": "no session cookie"}, 503)
            try:
                req = urllib.request.Request(
                    REMOTE + remote_path,
                    headers={"Cookie": f"qb_session={ck}", "User-Agent": "Mozilla/5.0"},
                )
                with urllib.request.urlopen(req, timeout=25) as r:
                    data = r.read()
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "public, max-age=86400")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except Exception as e:
                return self._json({"error": str(e)[:80]}, 502)
            return

        if path == "/api/draft":
            d = self._draft_read()
            return self._json(d if d is not None else {})

        if path == "/api/health":
            return self._json({"ok": True, "service": "assignments",
                               "local_images": os.path.isdir(QDATA)})

        if path == "/api/search":
            # 关键词检索：按当前 科目/考试局/阶段 选中对应的题目文字库，逐行扫描、命中即流式返回。
            term = qs.get("q", [""])[0].strip().lower()
            subj = qs.get("subject", [""])[0]
            board = qs.get("board", [""])[0]
            qual = qs.get("level", [""])[0]
            if not term or not subj or not board or not qual:
                return self._json({"error": "缺少查询参数"}, 400)
            if not re.fullmatch(r"[a-z0-9-]+", subj) or not re.fullmatch(r"[a-z]+", board) \
                    or not re.fullmatch(r"[a-z-]+", qual):
                return self._json({"error": "非法参数"}, 400)
            words = [w for w in re.split(r"\s+", term) if w]
            fp = os.path.join(TEXTBANK, f"{subj}__{board}__{qual}.jsonl")
            if not os.path.isfile(fp):
                # 该组合无文字库，返回空流（前端显示空结果）
                self._stream_init()
                return self._stream_line({"__done": True, "total": 0})
            hits = []
            try:
                with open(fp, encoding="utf-8") as f:
                    for line in f:
                        if not line.strip():
                            continue
                        try:
                            r = json.loads(line)
                        except Exception:
                            continue
                        text = (r.get('text') or '').lower()
                        if text and all(w in text for w in words):
                            hits.append(r)
            except Exception as e:
                return self._json({"error": str(e)[:120]}, 500)
            hits.sort(key=search_sort_key)
            self._stream_init()
            for r in hits:
                out = {k: r.get(k) for k in (
                    'qid', 'board', 'qualification', 'subject', 'year', 'session', 'paper_code',
                    'unit', 'question_number', 'total_marks', 'question_type', 'difficulty',
                    'image_path', 'primary_tag')}
                self._stream_line(out)
            return self._stream_line({"__done": True, "total": len(hits)})

        if path.startswith("/api/bank/"):
            name = path[len("/api/bank/"):]
            # 目录类数据全量提供；题目类数据按科目按需提供（subjects/<科目>.json.gz）
            catalog = ("catalog.json", "papers.json", "questions.json", "prefixes.json",
                       "taxonomy.json", "taxonomy_v2.json", "meta.json", "subtags.json",
                       "difficulty.json", "question_text.json.gz", "submeta.json.gz")
            scope_bundle = re.fullmatch(r"subjects/[a-z0-9-]+/diff/[A-Za-z0-9_-]+\.json\.gz", name)
            tree_bundle = re.fullmatch(r"subjects/[a-z0-9-]+/[a-z]+-[a-z-]+/tree\.json\.gz", name)
            if name not in catalog and not re.fullmatch(r"subjects/[a-z0-9-]+\.json\.gz", name) \
                    and not scope_bundle and not tree_bundle:
                return self._json({"error": "not found"}, 404)
            fp = os.path.join(DATA, name)
            if not os.path.exists(fp):
                return self._json({"error": "data not ready"}, 404)
            with open(fp, "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            if name.endswith(".gz"):
                # 浏览器 fetch 会按 Content-Encoding 自动解压，JS 直接 r.json() 即可
                self.send_header("Content-Encoding", "gzip")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return

        # 静态文件
        self._static = True
        return super().do_GET()


if __name__ == "__main__":
    os.makedirs(WEB, exist_ok=True)
    host = os.environ.get("ASSIGNMENTS_HOST", "127.0.0.1")
    srv = ThreadingHTTPServer((host, PORT), Handler)
    print(f"作业布置平台服务已启动: http://{host}:{PORT}", flush=True)
    print(f"  网页目录: {WEB}", flush=True)
    print(f"  数据目录: {DATA}", flush=True)
    print(f"  草稿目录: {DRAFTS}", flush=True)
    srv.serve_forever()
