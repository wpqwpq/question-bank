"""FastAPI 入口：挂 API 路由、/files 静态题图、托管前端构建产物。

启动（在 workbench/backend 目录下）：
  PYTHONPATH=<venv>/Lib/site-packages python -m uvicorn main:app --host 127.0.0.1 --port 8642
"""
from __future__ import annotations

import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

import store
from api import router

FRONTEND_DIST = Path(__file__).resolve().parents[1] / "frontend" / "dist"


def _warm_index() -> None:
    """后台预热索引：把「首次请求才付」的全库扫描（≈2.7s）挪到进程启动阶段。

    不预热也不影响正确性——索引在首次用到时按需构建，只是那一次要等。
    线程是 daemon：预热未完成时直接退出进程也不会被卡住。
    """
    try:
        store.warm()
    except Exception as e:  # noqa: BLE001  预热失败只影响速度，按需重建兜底
        print(f"[warmup] 预热失败，改为按需构建：{e}", flush=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    threading.Thread(target=_warm_index, name="index-warmup", daemon=True).start()
    yield


app = FastAPI(title="题库审核打标台", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(router)
app.mount("/files", StaticFiles(directory=store.DATA_DIR), name="files")

if FRONTEND_DIST.exists():
    app.mount("/assets", StaticFiles(directory=FRONTEND_DIST / "assets"), name="spa-assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa(full_path: str):
        candidate = FRONTEND_DIST / full_path
        if full_path and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(FRONTEND_DIST / "index.html")
