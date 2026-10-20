"""FastAPI 入口：认证中间件、登录接口、/files 静态题图、托管前端构建产物。

启动：
  PYTHONPATH=<部署根>/workbench/backend uvicorn main:app --host 127.0.0.1 --port 8642 --workers 1
或：
  python main.py

⚠️ 只能单进程（--workers 1）。store._lock / _meta_cache / state 的内存缓存 / auth 的
登录限流全是**进程内**状态，多 worker 会让 A 进程改的题 B 进程看不见，
两个进程同时写 state.db 还会互相覆盖。见方案第 4 节。
"""
from __future__ import annotations

import os
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import auth
import flags as flags_mod
import state
import store
from api import router

FRONTEND_DIST = Path(__file__).resolve().parents[1] / "frontend" / "dist"

# 后台指纹巡检间隔（秒）。标记少的时候 10 分钟足够；调小只会多花 IO，不会更准。
SWEEP_INTERVAL = int(os.environ.get("QBANK_SWEEP_INTERVAL") or 600)
SWEEP_BATCH = int(os.environ.get("QBANK_SWEEP_BATCH") or 200)


def _warm_index() -> None:
    """后台预热索引：把「首次请求才付」的全库扫描（≈2.7s）挪到进程启动阶段。

    不预热也不影响正确性——索引在首次用到时按需构建，只是那一次要等。
    线程是 daemon：预热未完成时直接退出进程也不会被卡住。
    """
    try:
        store.warm()
    except Exception as e:  # noqa: BLE001  预热失败只影响速度，按需重建兜底
        print(f"[warmup] 预热失败，改为按需构建：{e}", flush=True)


def _sweep_flags() -> None:
    """后台轮转校验「有误」标记的指纹。

    以前这件事在每个列表请求里做（2000 条标记 ≈ 40–100ms/请求，还会在读请求里写盘）。
    现在挪到这里，列表页完全不碰文件系统；只在有人打开某一题时验那一条（verify_one）。
    """
    time.sleep(30)  # 启动时别和 warmup 抢 IO
    while True:
        try:
            cleaned = flags_mod.sweep(SWEEP_BATCH)
            if cleaned:
                print(f"[flags] 巡检清理 {cleaned} 条失效标记", flush=True)
        except Exception as e:  # noqa: BLE001  巡检失败不影响服务
            print(f"[flags] 巡检失败：{e}", flush=True)
        time.sleep(SWEEP_INTERVAL)


@asynccontextmanager
async def lifespan(app: FastAPI):
    n = len(auth.load_users())
    print(f"[auth] 已加载 {n} 个账号（{auth.USERS_PATH}）", flush=True)
    if n == 0:
        print("[auth] ⚠️ 没有任何账号，登录会全部失败。先跑 manage_users.py init", flush=True)

    # state.db 是事实（确认状态/有误标记），打不开就**不要**带病启动：
    # 静默降级成空状态会让所有人以为"确认记录没了"，比启动失败更难查。
    state.init()
    st = state.stats()
    print(f"[state] {st['db']} 已加载：确认 {st['confirmed']} 条 / 标记 {st['flagged']} 条"
          f"（{st['db_bytes'] / 1024:.0f} KB）", flush=True)

    threading.Thread(target=_warm_index, name="index-warmup", daemon=True).start()
    threading.Thread(target=_sweep_flags, name="flags-sweep", daemon=True).start()
    yield
    state.close()


app = FastAPI(title="题库审核打标台（云端）", version="1.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
# 认证中间件最后加 = 最外层：任何业务代码执行前先判身份。
app.add_middleware(auth.AuthMiddleware)

app.include_router(router)


# ---------------------------------------------------------------------------
# 登录 / 登出 / 我是谁 / 改密 / 审计查询
# ---------------------------------------------------------------------------

class LoginPayload(BaseModel):
    username: str
    password: str


class ChangePasswordPayload(BaseModel):
    old_password: str
    new_password: str


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else ""


@app.post("/api/login")
def login(payload: LoginPayload, request: Request, response: Response):
    """用户名 + 密码换 12 小时有效的签名 cookie。

    失败一律回「用户名或密码不正确」，不区分「没这个账号」和「密码错了」——
    否则等于给暴力破解提供了用户名枚举接口。
    """
    ident = auth.login(payload.username.strip(), payload.password, _client_ip(request))
    response.set_cookie(
        auth.COOKIE_NAME,
        auth.make_session(ident.username),
        max_age=auth.SESSION_HOURS * 3600,
        httponly=True,     # JS 读不到，挡 XSS 偷 cookie
        samesite="lax",    # 挡跨站 CSRF
        path="/",
        # secure=False：当前只有 HTTP（无域名无证书）。上了 HTTPS 之后改成 True。
    )
    auth.audit(ident, "login", ident.username, {"via": "password"}, request)
    return ident.public()


@app.post("/api/logout")
def logout(request: Request, response: Response):
    ident = getattr(request.state, "user", None)
    response.delete_cookie(auth.COOKIE_NAME, path="/")
    if ident is not None:
        auth.audit(ident, "logout", ident.username, {}, request)
    return {"ok": True}


@app.get("/api/auth/me")
def whoami(request: Request):
    """前端启动时先问这一句：401 = 未登录（弹登录页），200 = 带着身份继续。"""
    return auth.current(request).public()


@app.post("/api/auth/password")
def change_password(payload: ChangePasswordPayload, request: Request, response: Response):
    ident = auth.current(request)
    raw = auth._raw_user(ident.username)
    if not raw or not auth.verify_password(payload.old_password, raw.get("password")):
        raise HTTPException(401, "原密码不正确")
    auth.check_password_strength(payload.new_password)
    if payload.old_password == payload.new_password:
        raise HTTPException(422, "新密码不能与原密码相同")
    auth.set_user_password(ident.username, payload.new_password)
    # 改完重签 cookie。签名密钥没变，旧 cookie 本来也有效，重签发只是让
    # must_change_password 标记从下一个请求起就消失。
    response.set_cookie(
        auth.COOKIE_NAME, auth.make_session(ident.username),
        max_age=auth.SESSION_HOURS * 3600, httponly=True, samesite="lax", path="/",
    )
    auth.audit(ident, "password.change", ident.username, {}, request)
    return {"ok": True}


@app.get("/api/auth/audit")
def audit_log(request: Request, limit: int = 200):
    """最近的操作记录（管理员专享）。谁在什么时候改了哪道题，从这儿查。"""
    ident = auth.current(request)
    if not ident.is_admin:
        raise HTTPException(403, "只有管理员可以查看操作日志")
    return auth.audit_recent(max(1, min(int(limit), 2000)))


# ---------------------------------------------------------------------------
# 静态资源
# ---------------------------------------------------------------------------

app.mount("/files", StaticFiles(directory=store.DATA_DIR), name="files")

if FRONTEND_DIST.exists():
    app.mount("/assets", StaticFiles(directory=FRONTEND_DIST / "assets"), name="spa-assets")

    _DIST_ROOT = FRONTEND_DIST.resolve()

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa(full_path: str):
        # ⚠️ 安全（2026-10-18 体检发现并修复）：full_path 直接来自 URL，绝不能直接拼到
        #    FRONTEND_DIST 上——`Path("/a/b") / "/c"` 等于 `/c`，于是
        #        curl --path-as-is 'http://<host>//opt/qbank/config/users.yaml'
        #    会把账号表（含全部密码哈希）原样吐出去，`//opt/qbank/config/secret.key`
        #    则泄露会话签名密钥（可伪造 admin 的 cookie）。这条路由不需要登录，
        #    等于对公网开放任意文件读。实测已复现（nginx 不拦 `//`，只拦 `../`）。
        #    两道保险：去掉前导斜杠归一成相对路径；resolve 后必须仍在 dist 之内。
        candidate = (FRONTEND_DIST / full_path.lstrip("/")).resolve()
        if full_path and _DIST_ROOT in candidate.parents and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(FRONTEND_DIST / "index.html")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host=os.environ.get("QBANK_HOST", "127.0.0.1"),
        port=int(os.environ.get("QBANK_PORT", "8642")),
        workers=1,
    )
