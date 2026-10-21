# -*- coding: utf-8 -*-
"""fetch_papers_lib.py — 试卷抓取的共用底座（限速 / 重试 / 文本层体检）

只做三件事，别的都交给调用方：
  1. `fetch(url)`   带 UA、绕系统代理、按站点限速的 GET（返回 bytes）
  2. `probe(url)`   只取前 64 字节，判断「文件在不在、是不是 PDF」
  3. `verdict(pdf)` 文本层质量：OK / PARTIAL / CORRUPT —— **只认 OK 为可交付**

`verdict` 与 `scripts/fetch_cie_chem_sources.py` 同一套判据（虚词 ≥30、控制字符 <2%），
那份脚本只换源，这份是批量下载，判据一致才能互相对得上。
"""
from __future__ import annotations

import re
import threading
import time
import urllib.error
import urllib.request

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

COMMON = set("the of and to in is are for that with which can be a an on by as at from "
             "this not was were has have it its".split())


class RateLimiter:
    """按 host 保底间隔 + 全局并发上限 + 全局限流冷却。

    pastpapers.co 的脾气：**突发请求就 403**，且 403 之后要静默一会儿才恢复。
    所以除了 `min_interval`，还有一条「一被封就全体停 `cooldown` 秒」的硬闸。
    """

    def __init__(self, min_interval=0.35, max_concurrency=3, cooldown=45):
        self.min_interval = min_interval
        self.cooldown = cooldown
        self._last = {}
        self._lock = threading.Lock()
        self._sem = threading.Semaphore(max_concurrency)
        self._pause_until = 0.0
        self.blocks = 0

    def pause(self, seconds=None):
        with self._lock:
            until = time.monotonic() + (seconds if seconds is not None else self.cooldown)
            if until > self._pause_until:
                self._pause_until = until
            self.blocks += 1

    def wait(self, host):
        self._sem.acquire()
        while True:
            with self._lock:
                left = self._pause_until - time.monotonic()
            if left <= 0:
                break
            time.sleep(min(left, 5.0))
        with self._lock:
            last = self._last.get(host, 0.0)
            gap = time.monotonic() - last
            if gap < self.min_interval:
                time.sleep(self.min_interval - gap)
            self._last[host] = time.monotonic()

    def release(self):
        self._sem.release()


_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _req(url, referer=None, rng=None, method="GET"):
    r = urllib.request.Request(url, method=method)
    r.add_header("User-Agent", UA)
    r.add_header("Accept", "*/*")
    r.add_header("Accept-Language", "en-US,en;q=0.9")
    if referer:
        r.add_header("Referer", referer)
    if rng:
        r.add_header("Range", rng)
    return r


def fetch(url, referer=None, timeout=90, retries=4, limiter=None, log=print):
    """返回 bytes；403/429 长退避，404 直接抛 FileNotFoundError。"""
    host = url.split("/")[2]
    last = None
    for i in range(retries):
        if limiter:
            limiter.wait(host)
        try:
            with _opener.open(_req(url, referer), timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            last = e
            if e.code == 404:
                raise FileNotFoundError(url)
            if e.code in (403, 429, 503):
                wait = 15 * (i + 1)
                log(f"    [{e.code}] {host} 冷却 {wait}s")
                if limiter:
                    limiter.pause(wait)
                else:
                    time.sleep(wait)
                continue
            if e.code >= 500:
                time.sleep(5 * (i + 1))
                continue
            raise
        except Exception as e:            # 超时 / 连接重置
            last = e
            time.sleep(4 * (i + 1))
        finally:
            if limiter:
                limiter.release()
    raise RuntimeError(f"fetch 失败 {retries} 次: {url} ({type(last).__name__}: {last})")


def probe(url, referer=None, timeout=30):
    """只读前 64 字节。返回 (status, content_type, total_len, head_bytes)。"""
    host = url.split("/")[2]
    wait = 0.0
    for i in range(3):
        if wait:
            time.sleep(wait)
        try:
            with _opener.open(_req(url, referer, "bytes=0-63"), timeout=timeout) as r:
                head = r.read(64)
                cr = r.headers.get("Content-Range") or ""
                total = int(cr.split("/")[-1]) if "/" in cr else int(r.headers.get("Content-Length") or 0)
                return r.status, r.headers.get("Content-Type"), total, head
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return 404, None, 0, b""
            if e.code in (403, 429, 503):
                wait = 25 * (i + 1)
                continue
            raise
        except Exception:
            wait = 3 * (i + 1)
    raise RuntimeError(f"probe 失败: {url}")


# ---------------------------------------------------------------- 文本层体检
_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_WORD_RE = re.compile(r"[A-Za-z]{3,}")


def verdict(pdf_bytes_or_path):
    """文本层质量。返回 dict(cls, chars, ctrl, common, pages, reason)。

    OK       : 虚词 ≥30 且 控制字符 <2%          → 可交付
    PARTIAL  : 有文本但选项行乱码 / 虚词偏少      → 可疑，不入交付
    CORRUPT  : 控制字符 >15% 或 虚词 <5 或 无文字  → 扫描件/乱码，弃
    """
    import pymupdf

    try:
        if isinstance(pdf_bytes_or_path, (bytes, bytearray)):
            doc = pymupdf.open(stream=bytes(pdf_bytes_or_path), filetype="pdf")
        else:
            doc = pymupdf.open(str(pdf_bytes_or_path))
        pages = doc.page_count
        txt = "".join(p.get_text() for p in doc)
        doc.close()
    except Exception as e:
        return dict(cls="ERROR", chars=0, ctrl=0, common=0, pages=0,
                    reason=f"{type(e).__name__}: {e}")

    total = len(txt)
    ctrl = len(_CTRL_RE.findall(txt))
    words = _WORD_RE.findall(txt)
    common = sum(1 for w in words if w.lower() in COMMON)
    ratio = ctrl / total if total else 0.0
    if total < 200:
        cls, why = "CORRUPT", f"文本仅 {total} 字符（扫描件？）"
    elif common >= 30 and ratio < 0.02:
        cls, why = "OK", ""
    elif ratio > 0.15 or common < 5:
        cls, why = "CORRUPT", f"控制字符 {ratio*100:.1f}% / 虚词 {common}"
    else:
        cls, why = "PARTIAL", f"控制字符 {ratio*100:.1f}% / 虚词 {common}"
    return dict(cls=cls, chars=total, ctrl=round(ratio, 4), common=common,
                pages=pages, reason=why)
