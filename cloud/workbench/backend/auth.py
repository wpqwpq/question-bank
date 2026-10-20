"""认证 / 授权 / 审计（云端专用，本地版没有这个模块）。

三条设计原则，决定了下面所有取舍：

1. **无状态会话**。登录成功后签一个 HMAC 签名的 cookie，服务端不存 session。
   这样重启、崩溃都不影响在线用户，也不需要引入 session 表。
   代价是「立即踢人」做不到——但用户信息（角色/学科/启用状态）**每次请求都从
   users.yaml 现读**，所以改角色、删账号是立刻生效的，只有 cookie 里的过期时间动不了。

2. **API Key 走快哈希**。密码用 pbkdf2（低熵、必须慢），API Key 用 sha256
   （128 位随机串、必须快）。对 73 个账号做 O(1) 查找而不是逐个 pbkdf2，
   否则 AI 逐题调用每 100ms 一次会被认证自己拖死。

3. **学科隔离在「读路径」上做**，不做可见性隐藏。跨学科访问一律 404 而不是 403——
   403 等于告诉对方「这个东西存在，只是你不能看」，仍然泄露题库结构。

用户文件默认 `/opt/qbank/config/users.yaml`，可用 QBANK_USERS 覆盖。
密码只存 pbkdf2 哈希，明文只在生成时出现一次。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import yaml
from fastapi import HTTPException, Request
from starlette.responses import JSONResponse

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = REPO_ROOT / "config"
USERS_PATH = Path(os.environ.get("QBANK_USERS") or (CONFIG_DIR / "users.yaml"))
SECRET_PATH = Path(os.environ.get("QBANK_SECRET_FILE") or (CONFIG_DIR / "secret.key"))
AUDIT_DIR = Path(os.environ.get("QBANK_AUDIT_DIR") or (REPO_ROOT / "data" / "_audit"))

COOKIE_NAME = "qb_session"
SESSION_HOURS = 12
PBKDF2_ROUNDS = 260_000

# 角色能力矩阵（与方案第 5 节一致）
ROLES = {"admin", "lead", "tagger", "viewer", "service"}
WRITE_ROLES = {"admin", "lead", "tagger", "service"}   # 写标注 / 确认
# 改标签词表：tagger 也放开（2026-10-19 培琪定），但**只在自己学科内**——
# 学科边界由 require_subject 兜住，跨科一律 404。viewer 只读、service（AI）
# 不该有权改全局事实源，两者都排除在外。
TAX_ROLES = {"admin", "lead", "tagger"}
CONFIRM_ROLES = {"admin", "lead", "tagger", "service"}

# 登录失败限流：同一 (IP, 用户名) 10 分钟内最多 10 次
_MAX_FAILS = 10
_FAIL_WINDOW = 600


# ---------------------------------------------------------------------------
# users.yaml 读取（按 mtime+size 签名缓存，改文件立即生效，与 store/taxonomy 同一套路）
# ---------------------------------------------------------------------------

_users_lock = threading.RLock()
_users_cache: tuple[tuple[int, int], dict[str, "Ident"]] | None = None
_raw_cache: tuple[tuple[int, int], list[dict[str, Any]]] | None = None


def _users_sig() -> tuple[int, int]:
    try:
        st = USERS_PATH.stat()
    except OSError:
        return (0, 0)
    return (st.st_mtime_ns, st.st_size)


@dataclass
class Ident:
    """一个请求身份。subjects 里的 "*" 表示全部学科。"""

    username: str
    name: str
    role: str
    subjects: list[str] = field(default_factory=list)
    must_change_password: bool = False
    via: str = "cookie"  # cookie / key — 审计里区分人工与 AI

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    def can(self, subject: str | None) -> bool:
        """能否看到/操作该学科。subject=None 表示「不限学科的资源」（词表列表等）。"""
        if self.is_admin or "*" in self.subjects:
            return True
        if subject is None:
            return True
        return subject in self.subjects

    @property
    def can_write(self) -> bool:
        return self.role in WRITE_ROLES

    @property
    def can_taxonomy(self) -> bool:
        return self.role in TAX_ROLES

    @property
    def can_confirm(self) -> bool:
        return self.role in CONFIRM_ROLES

    def public(self) -> dict[str, Any]:
        return {
            "username": self.username,
            "name": self.name,
            "role": self.role,
            "subjects": self.subjects,
            "must_change_password": self.must_change_password,
        }


def load_users() -> dict[str, Ident]:
    """{username: Ident}。文件缺失返回空表——此时谁都登不进来，是安全的失败方向。"""
    global _users_cache
    sig = _users_sig()
    with _users_lock:
        if _users_cache is not None and _users_cache[0] == sig:
            return _users_cache[1]

    out: dict[str, Ident] = {}
    if USERS_PATH.exists():
        try:
            raw = yaml.safe_load(USERS_PATH.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:
            raw = {}
        for u in raw.get("users") or []:
            if not isinstance(u, dict) or not u.get("username"):
                continue
            if u.get("disabled"):
                continue
            role = str(u.get("role") or "tagger")
            if role not in ROLES:
                continue
            subs = u.get("subjects")
            if isinstance(subs, str):
                subs = [subs]
            out[str(u["username"])] = Ident(
                username=str(u["username"]),
                name=str(u.get("name") or u["username"]),
                role=role,
                subjects=[str(s) for s in (subs or [])],
                must_change_password=bool(u.get("must_change_password")),
            )
    with _users_lock:
        _users_cache = (sig, out)
    return out


def get_user(username: str) -> Ident | None:
    return load_users().get(str(username))


def _raw_all() -> list[dict[str, Any]]:
    """users.yaml 全部原始条目（含 password / key_hash 这些不对外暴露的字段）。

    ⚠️ 必须带签名缓存。旧实现里 _raw_user() 每次都整个重读 + yaml.safe_load，
    而 verify_api_key() 会为每个账号调一次 —— 83 个账号 = 83 次文件解析，
    每个带 API Key 的请求白等 3.5 秒（2026-10-18 实测 3.7s）。
    """
    global _raw_cache
    sig = _users_sig()
    with _users_lock:
        if _raw_cache is not None and _raw_cache[0] == sig:
            return _raw_cache[1]

    users: list[dict[str, Any]] = []
    if USERS_PATH.exists():
        try:
            raw = yaml.safe_load(USERS_PATH.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            raw = {}
        users = [u for u in (raw.get("users") or []) if isinstance(u, dict)]

    with _users_lock:
        _raw_cache = (sig, users)
    return users


def _raw_user(username: str) -> dict[str, Any] | None:
    """取 users.yaml 里的单个原始条目（需要 password / key_hash 这些不对外暴露的字段）。"""
    for u in _raw_all():
        if str(u.get("username")) == str(username):
            return u
    return None


# ---------------------------------------------------------------------------
# 哈希
# ---------------------------------------------------------------------------

def hash_password(password: str) -> str:
    """pbkdf2_sha256$轮数$盐$哈希（盐与哈希均为标准 base64，无 $ 字符）。"""
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ROUNDS)
    b64 = lambda b: base64.b64encode(b).decode("ascii")  # noqa: E731
    return f"pbkdf2_sha256${PBKDF2_ROUNDS}${b64(salt)}${b64(dk)}"


def verify_password(password: str, stored: str | None) -> bool:
    if not stored or not isinstance(stored, str):
        return False
    try:
        algo, rounds, salt_b64, hash_b64 = stored.split("$", 3)
        if algo != "pbkdf2_sha256":
            return False
        salt = base64.b64decode(salt_b64)
        expect = base64.b64decode(hash_b64)
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(rounds))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(dk, expect)


def hash_api_key(key: str) -> str:
    """API Key 是 128 位随机串，用快哈希；格式 sha256:<hex>，便于人工辨认。"""
    return "sha256:" + hashlib.sha256(key.encode("utf-8")).hexdigest()


def new_api_key() -> str:
    return "qbk_" + secrets.token_urlsafe(32)


# ---------------------------------------------------------------------------
# 会话 cookie：base64url(payload).hmac
# ---------------------------------------------------------------------------

def _secret() -> bytes:
    """签名密钥。首次启动自动生成 32 字节落盘，权限 0600。"""
    if os.environ.get("QBANK_SECRET"):
        return os.environ["QBANK_SECRET"].encode("utf-8")
    try:
        data = SECRET_PATH.read_bytes().strip()
        if len(data) >= 32:
            return data
    except OSError:
        pass
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    data = secrets.token_bytes(32)
    SECRET_PATH.write_bytes(data)
    try:
        os.chmod(SECRET_PATH, 0o600)
    except OSError:
        pass
    return data


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64d(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def make_session(username: str) -> str:
    exp = int(time.time()) + SESSION_HOURS * 3600
    payload = json.dumps({"u": username, "exp": exp}, separators=(",", ":")).encode("utf-8")
    body = _b64e(payload)
    mac = hmac.new(_secret(), body.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{body}.{mac}"


def read_session(cookie: str | None) -> str | None:
    if not cookie or "." not in cookie:
        return None
    body, _, mac = cookie.rpartition(".")
    expect = hmac.new(_secret(), body.encode("ascii"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(mac, expect):
        return None
    try:
        payload = json.loads(_b64d(body))
    except (ValueError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    if int(payload.get("exp") or 0) < int(time.time()):
        return None
    return str(payload.get("u") or "") or None


def verify_api_key(token: str) -> Ident | None:
    """token = qbk_xxx，users.yaml 里存 key_hash。按 key_hash 比对，O(1) 数据访问。

    原始条目与 Ident 都走带签名的缓存，整个过程只剩 83 次 sha256 比对（约 20µs）。
    """
    if not token.startswith("qbk_"):
        return None
    target = hash_api_key(token)
    idents = load_users()
    for u in _raw_all():
        if str(u.get("role")) != "service" or u.get("disabled"):
            continue
        if hmac.compare_digest(str(u.get("key_hash") or ""), target):
            ident = idents.get(str(u.get("username")))
            if ident is None:
                continue
            ident.via = "key"
            return ident
    return None


# ---------------------------------------------------------------------------
# 登录限流
# ---------------------------------------------------------------------------

_fails: dict[str, list[float]] = {}
_fails_lock = threading.Lock()


def _fail_key(ip: str, username: str) -> str:
    return f"{ip}|{username.lower()}"


def _too_many(ip: str, username: str) -> bool:
    now = time.time()
    with _fails_lock:
        hits = [t for t in _fails.get(_fail_key(ip, username), []) if now - t < _FAIL_WINDOW]
        _fails[_fail_key(ip, username)] = hits
        return len(hits) >= _MAX_FAILS


def _record_fail(ip: str, username: str) -> None:
    with _fails_lock:
        _fails.setdefault(_fail_key(ip, username), []).append(time.time())


def _clear_fails(ip: str, username: str) -> None:
    with _fails_lock:
        _fails.pop(_fail_key(ip, username), None)


def login(username: str, password: str, ip: str = "") -> Ident:
    if _too_many(ip, username):
        raise HTTPException(429, "登录失败次数过多，请 10 分钟后再试")
    raw = _raw_user(username)
    if not raw or raw.get("disabled"):
        _record_fail(ip, username)
        raise HTTPException(401, "用户名或密码不正确")
    if not verify_password(password, raw.get("password")):
        _record_fail(ip, username)
        raise HTTPException(401, "用户名或密码不正确")
    ident = get_user(username)
    if ident is None:
        raise HTTPException(401, "该账号未启用")
    _clear_fails(ip, username)
    return ident


def check_password_strength(pw: str) -> None:
    if len(pw) < 8:
        raise HTTPException(422, "新密码至少 8 位")
    if pw.isdigit():
        raise HTTPException(422, "新密码不能是纯数字")


def set_user_password(username: str, password: str) -> None:
    """改密码：读 users.yaml → 改一条 → 原子写回。

    保留原文件头注释与条目顺序；只动 password / must_change_password 两个键。
    这是「老师忘密码」的两条路之一（另一条是命令行 manage_users.py passwd）。
    """
    global _users_cache
    if not USERS_PATH.exists():
        raise HTTPException(500, "账号文件不存在")
    text = USERS_PATH.read_text(encoding="utf-8")
    # 把文件头注释原样留下，避免每次改密把说明洗掉
    head = "".join(l + "\n" for l in text.splitlines() if l.startswith("#"))
    data = yaml.safe_load(text) or {}
    hit = False
    for u in data.get("users") or []:
        if isinstance(u, dict) and str(u.get("username")) == str(username):
            u["password"] = hash_password(password)
            u["must_change_password"] = False
            hit = True
            break
    if not hit:
        raise HTTPException(404, f"账号不存在：{username}")
    payload = yaml.safe_dump(data, allow_unicode=True, sort_keys=False,
                             default_flow_style=False, width=200)
    tmp = USERS_PATH.with_suffix(".yaml.tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as f:
        f.write(head + payload)
    tmp.replace(USERS_PATH)
    try:
        USERS_PATH.chmod(0o600)
    except OSError:
        pass
    with _users_lock:
        _users_cache = None


# ---------------------------------------------------------------------------
# 每请求的身份解析
# ---------------------------------------------------------------------------

def resolve(request: Request) -> Ident | None:
    """先看 Bearer（AI），再看 cookie（浏览器）。两条通道汇到同一个身份对象。"""
    auth = request.headers.get("authorization") or ""
    if auth[:7].lower() == "bearer ":
        return verify_api_key(auth[7:].strip())
    return_user = read_session(request.cookies.get(COOKIE_NAME))
    if not return_user:
        return None
    ident = get_user(return_user)
    if ident is None:
        return None
    ident.via = "cookie"
    return ident


# 不需要登录的 API 路径（登录页自身要用）
OPEN_API = {"/api/login", "/api/auth/me", "/api/auth/password", "/api/logout"}

# 改密前仍可用的接口：只留「读自己是谁 + 登出 + 改密」，其余全部 428
PASSWORD_PENDING_OK = {"/api/auth/me", "/api/auth/password", "/api/logout"}


def _deny(status: int, detail: str) -> JSONResponse:
    return JSONResponse({"detail": detail}, status_code=status)


def _subject_of_file_path(path: str) -> str | None:
    """/files/<board>/<qual>/<subject>/<paper>/assets/<file> → subject，结构不对返回 None。

    只放行**题图目录**这一种形状。这样一刀就同时挡住了：
      /files/_review/*（审核记录 —— 2026-10-18 起已改存 var/state.db，本就不在 /files 范围，
       这里保留拦截是为了防「哪天有人把文件放回 data/_review」）、
      /files/taxonomy/...（词表文件）、
      /files/.../cut.json 与 paper.md、source.pdf（切割坐标与原卷）。
    这些走 API 都有接口，没必要也不应该能直链下载。
    """
    segs = [s for s in path.split("/") if s]
    # files / <board> / <qual> / <subject> / <paper> / assets / <file>
    if len(segs) < 7 or segs[0] != "files" or segs[5] != "assets":
        return None
    return segs[3]


class AuthMiddleware:
    """纯 ASGI 中间件。

    为什么不用 BaseHTTPMiddleware：它会把所有响应包一层流，静态题图（82KB×N）
    多一次拷贝不划算，而且它对 StaticFiles 的异常处理有历史坑。这里只需要读
    header/cookie，写在 request.scope["state"] 里就够了。
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        path = scope.get("path") or "/"
        method = (scope.get("method") or "GET").upper()

        # 静态资源 / SPA 外壳不设防（里面没有数据），只有 /api 与 /files 需要身份
        guarded = path.startswith("/api") or path == "/files" or path.startswith("/files/")
        if not guarded:
            return await self.app(scope, receive, send)

        request = Request(scope)

        is_open = path in OPEN_API or (path.startswith("/api/login"))
        ident = resolve(request)
        if ident is not None:
            scope.setdefault("state", {})["user"] = ident

        if ident is None and not is_open:
            return await _deny(401, "未登录或登录已过期")(scope, receive, send)

        if ident is not None and ident.must_change_password and path not in PASSWORD_PENDING_OK:
            return await _deny(428, "首次登录，请先修改初始密码")(scope, receive, send)

        # /files/ 的学科隔离：题图 URL 里带学科段，直接按段比对，
        # 不用去查 paper（这是每个题图请求都要跑的热路径）
        if path.startswith("/files/") and ident is not None:
            subject = _subject_of_file_path(path)
            if subject is None or not ident.can(subject):
                return await _deny(404, "not found")(scope, receive, send)

        return await self.app(scope, receive, send)


# ---------------------------------------------------------------------------
# 端点内的权限断言（中间件只管「有没有身份」，这里管「能不能做这件事」）
# ---------------------------------------------------------------------------

def current(request: Request) -> Ident:
    ident = getattr(request.state, "user", None)
    if ident is None:  # 理论上中间件已经拦掉，这是双保险
        raise HTTPException(401, "未登录或登录已过期")
    return ident


def require_subject(request: Request, subject: str | None) -> Ident:
    """学科不可见 → 404（不用 403，避免泄露「存在但你不能看」）。"""
    ident = current(request)
    if not ident.can(subject):
        raise HTTPException(404, "not found")
    return ident


def require_write(request: Request) -> Ident:
    ident = current(request)
    if not ident.can_write:
        raise HTTPException(403, f"当前角色（{ident.role}）为只读，不能修改标注")
    return ident


def require_taxonomy(request: Request) -> Ident:
    ident = current(request)
    if not ident.can_taxonomy:
        raise HTTPException(403, f"当前角色（{ident.role}）不能修改标签词表")
    return ident


# ---------------------------------------------------------------------------
# 审计：append-only JSONL，一行一个动作
# ---------------------------------------------------------------------------

_audit_lock = threading.Lock()


def audit(ident: Ident | None, action: str, target: str,
          detail: dict[str, Any] | None = None, request: Request | None = None) -> None:
    """写一行审计。**绝不影响主流程**：任何异常都吞掉。

    不记录密码、cookie、API Key；只记谁、什么时候、对什么做了什么。
    """
    try:
        AUDIT_DIR.mkdir(parents=True, exist_ok=True)
        now = datetime.now().astimezone()
        rec = {
            "ts": now.isoformat(timespec="seconds"),
            "user": ident.username if ident else None,
            "name": ident.name if ident else None,
            "role": ident.role if ident else None,
            "via": ident.via if ident else None,
            "action": action,
            "target": target,
            "ip": (request.client.host if request and request.client else None),
            "detail": detail or {},
        }
        line = json.dumps(rec, ensure_ascii=False) + "\n"
        path = AUDIT_DIR / f"{now.date().isoformat()}.jsonl"
        with _audit_lock:
            with path.open("a", encoding="utf-8", newline="") as f:
                f.write(line)
    except Exception:  # noqa: BLE001  审计失败不能拖垮业务
        pass


def audit_recent(limit: int = 200) -> list[dict[str, Any]]:
    """最近 N 条审计（新→旧），供管理员页查看。"""
    if not AUDIT_DIR.exists():
        return []
    files = sorted(AUDIT_DIR.glob("*.jsonl"), reverse=True)[:7]
    out: list[dict[str, Any]] = []
    for f in files:
        try:
            lines = f.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in reversed(lines):
            if not line.strip():
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
            if len(out) >= limit:
                return out
    return out
