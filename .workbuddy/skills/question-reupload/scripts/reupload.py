#!/usr/bin/env python3
"""单题重传：把本地某题的文件推到云端，并清理该题的审核 / 打标 / 有误状态。

用法（在项目根 D:/Tasks/questionBank 下跑）：
    python .workbuddy/skills/question-reupload/scripts/reupload.py 2020-jan-wph11 14 --dry-run
    python .workbuddy/skills/question-reupload/scripts/reupload.py 2020-jan-wph11 14 --mode image
    python .workbuddy/skills/question-reupload/scripts/reupload.py 2020-jan-wph11 14 --mode reset
    python .workbuddy/skills/question-reupload/scripts/reupload.py 2020-jan-wph11 14 --mode reset --drop-subs

模式：
    --mode image   只传 assets（图 + layout），不碰 qNN.md → 标签/难度原样保留
    --mode reset   连 qNN.md 一起传，并剥掉 tags / difficulty / primary_tag（默认）

可选：
    --drop-subs    删掉云端该题的 qNN-*.md 与子图（本地重切后不再分小问时用）
    --no-backup    跳过备份（不建议）
    --yes          跳过交互确认

铁律（脚本已内建）：
    1. 只碰这一题的文件，绝不整卷 rsync，绝不带 --delete；
    2. 云端该题现有文件先拉到 _prev/reupload/ 备份；
    3. 剥标签在临时副本上做，不改你的本地源文件；
    4. confirmed.json 手动撤（没有自动失效机制），flagged.json 靠指纹自动失效。
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[4]
DATA = PROJECT / "data"
PREV = PROJECT / "_prev" / "reupload"

HOST = "ubuntu@140.143.229.85"
REMOTE_ROOT = "/opt/qbank/data"
REMOTE_PY = "/opt/qbank/venv/bin/python"
REMOTE_BACKEND = "/opt/qbank/workbench/backend"

FM_RE = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*\r?\n", re.DOTALL)


def _key() -> str:
    k = os.path.expanduser("~/.ssh/wpq.pem")
    if not Path(k).exists():
        sys.exit(f"[x] 找不到 SSH 密钥：{k}")
    return k


def _bin(name: str) -> str:
    p = shutil.which(name)
    if not p:
        sys.exit(f"[x] 本机找不到 {name}（Git Bash 里应当自带）")
    return p


SSH = None
SCP = None
KEY = None


def ssh(cmd: str, timeout: int = 90) -> tuple[int, str, str]:
    r = subprocess.run(
        [SSH, "-i", KEY, "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new", HOST, cmd],
        capture_output=True, text=True, timeout=timeout,
    )
    return r.returncode, r.stdout, r.stderr


def scp_down(remote_rel: str, local_abs: Path) -> None:
    local_abs.parent.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(
        [SCP, "-i", KEY, "-o", "BatchMode=yes", f"{HOST}:{REMOTE_ROOT}/{remote_rel}", str(local_abs)],
        capture_output=True, text=True, timeout=120,
    )
    if r.returncode != 0:
        raise RuntimeError(f"scp 下载失败 {remote_rel}: {r.stderr.strip()}")


def scp_up(local_abs: Path, remote_rel: str) -> None:
    r = subprocess.run(
        [SCP, "-i", KEY, "-o", "BatchMode=yes", str(local_abs), f"{HOST}:{REMOTE_ROOT}/{remote_rel}"],
        capture_output=True, text=True, timeout=120,
    )
    if r.returncode != 0:
        raise RuntimeError(f"scp 上传失败 {remote_rel}: {r.stderr.strip()}")


def rmtree_quiet(path: Path) -> None:
    """删临时目录。Windows 下走 rd /s /q —— shutil.rmtree 在沙箱里会逐条进回收站。"""
    if os.name == "nt":
        subprocess.run(["cmd", "/c", "rd", "/s", "/q", str(path)],
                       capture_output=True, text=True, timeout=60)
    else:
        shutil.rmtree(path, ignore_errors=True)


def strip_annotation(text: str) -> tuple[str, bool]:
    """剥掉 front-matter 里的 tags / difficulty / primary_tag。

    刻意**保留** split: 与 subquestions: —— 那两个是切分结构，不是标注。
    store._strip_annotation_lines 会连它们一起剥（因为它随后会重建），这里不能照抄。
    """
    m = FM_RE.match(text)
    if not m:
        return text, False
    out: list[str] = []
    in_block = False
    changed = False
    for line in m.group(1).splitlines():
        if in_block:
            if re.match(r"^[ \t]+-[ \t]", line):
                changed = True
                continue
            in_block = False
        if re.match(r"^tags:[ \t]*$", line):
            in_block = True
            changed = True
            continue
        if re.match(r"^tags:[ \t]*\[", line):
            changed = True
            continue
        if re.match(r"^difficulty:", line) or re.match(r"^primary_tag:", line):
            changed = True
            continue
        out.append(line)
    if not changed:
        return text, False
    result = "---\n" + "\n".join(out) + "\n---\n" + text[m.end():]
    # 统一成 LF：上传目标是 Linux，混行的 front-matter 没必要留着
    return result.replace("\r\n", "\n"), True


def read_id(md: Path) -> str | None:
    """从 md 的 front-matter 读真实 qid。这是唯一真源——不要用目录名拼。

    资格 token 有历史分裂（数据里 `alevel`、词表/前端 `a-level`），拼出来的 qid
    可能对不上，导致服务器找不到题。
    """
    try:
        raw = md.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    m = FM_RE.match(raw)
    if not m:
        return None
    for line in m.group(1).splitlines():
        if line.startswith("id:"):
            return line.split(":", 1)[1].strip().strip("'\"")
    return None


def find_local_paper(paper: str) -> Path:
    hits = sorted(DATA.glob(f"*/*/*/{paper}"))
    hits = [h for h in hits if h.is_dir()]
    if not hits:
        sys.exit(f"[x] 本地找不到卷目录：data/*/*/*/{paper}")
    if len(hits) > 1:
        sys.exit("[x] 本地有多份同名卷，请手工指定：" + ", ".join(str(h) for h in hits))
    return hits[0]


def remote_question_files(rel_dir: str, q: str, timeout: int = 60) -> list[str]:
    """列出云端该题相关的全部文件（主题 + 子题 + assets），按相对路径返回。"""
    code, out, err = ssh(f"test -d {REMOTE_ROOT}/{rel_dir} && echo OK", timeout=timeout)
    if "OK" not in out:
        sys.exit(f"[x] 云端目录不存在：{REMOTE_ROOT}/{rel_dir}")
    cmd = f"cd {REMOTE_ROOT}/{rel_dir} && ls -1 {q}.md {q}-*.md assets/{q}* 2>/dev/null"
    _, out, _ = ssh(cmd, timeout=timeout)
    return sorted({ln.strip() for ln in out.splitlines() if ln.strip()})


def clear_state(rel_dir: str, qid: str, clear_flag: bool) -> dict:
    """在服务器上撤该题所有确认单元（+ 可选清 flagged）。返回结构化结果。"""
    py = f'''
import sys, json
sys.path.insert(0, {REMOTE_BACKEND!r})
import store, review, flags

qid = {qid!r}
found = store.find_question(qid)
if found is None:
    print(json.dumps({{"ok": False, "error": "qid not found on server"}}, ensure_ascii=False))
    raise SystemExit(0)
paper, qpath = found
subs = paper.sub_files(qpath)
units = [qid]
for s in subs:
    m = store.read_meta(s)
    if m.get("id"):
        units.append(str(m["id"]))

removed = []
for u in units:
    if review.is_confirmed(u):
        review.set_confirmed(u, False)
        removed.append(u)

flag_state = "none"
if {clear_flag!r}:
    if flags.is_flagged(qid):
        flags.set_flag(qid, False)
        flag_state = "cleared"
    else:
        flag_state = "absent"

print(json.dumps({{"ok": True, "units": units, "removed": removed,
                  "flag": flag_state, "subs": [s.name for s in subs]}}, ensure_ascii=False))
'''
    code, out, err = ssh(f"{REMOTE_PY} - <<'PYEOF'\n{py}\nPYEOF", timeout=120)
    if code != 0:
        raise RuntimeError(f"状态清理失败：{err.strip() or out.strip()}")
    import json as _json
    for line in reversed(out.strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return _json.loads(line)
            except _json.JSONDecodeError:
                continue
    raise RuntimeError(f"状态清理返回无法解析：{out.strip()[:300]}")


def main() -> None:
    global SSH, SCP, KEY
    ap = argparse.ArgumentParser()
    ap.add_argument("paper", help="卷目录名，如 2020-jan-wph11")
    ap.add_argument("qnum", help="题号，如 14")
    ap.add_argument("--mode", choices=["image", "reset"], default="reset")
    ap.add_argument("--drop-subs", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-backup", action="store_true")
    ap.add_argument("--yes", action="store_true")
    args = ap.parse_args()

    SSH, SCP, KEY = _bin("ssh"), _bin("scp"), _key()

    local_paper = find_local_paper(args.paper)
    rel_dir = local_paper.relative_to(DATA).as_posix()
    parts = rel_dir.split("/")
    if len(parts) != 4:
        sys.exit(f"[x] 目录层级不是 <board>/<qual>/<subject>/<paper>：{rel_dir}")
    board, qual, subject, paper = parts

    q = f"q{int(args.qnum):02d}"

    # 本地该题文件
    main_md = local_paper / f"{q}.md"
    if not main_md.exists():
        sys.exit(f"[x] 本地没有 {main_md.relative_to(PROJECT)}")

    # qid 从 front-matter 读，不靠目录名拼
    qid = read_id(main_md) or f"{board}-{qual}-{subject}-{paper}-{q}"

    assets = local_paper / "assets"
    local_subs = sorted(local_paper.glob(f"{q}-*.md"))

    if args.mode == "image":
        # 只换主题图：精确三个候选，绝不带上子题图
        upload = [assets / n for n in (f"{q}.webp", f"{q}.png", f"{q}.layout.json")]
        upload = [p for p in upload if p.exists()]
    else:
        # reset：主题 md + 主题全部 assets + 本地存在的子题 md 与其 assets
        upload = [main_md]
        upload += sorted(p for p in assets.glob(f"{q}*") if p.is_file())
        for s in local_subs:
            upload.append(s)
            upload += sorted(p for p in assets.glob(f"{s.stem}*") if p.is_file())

    seen: set[Path] = set()
    upload = [p for p in upload if not (p in seen or seen.add(p))]

    remote_files = remote_question_files(rel_dir, q)
    remote_subs = [f for f in remote_files if re.match(rf"^{q}-[a-z0-9]+\.md$", f)]
    remote_sub_assets = [f for f in remote_files if re.match(rf"^assets/{q}-[a-z0-9]+\.", f)]

    print(f"卷：{paper}   题：{args.qnum}   模式：{args.mode}")
    print(f"本地：{local_paper.relative_to(PROJECT).as_posix()}")
    print(f"云端：{HOST}:{REMOTE_ROOT}/{rel_dir}")
    print()
    print("云端现有该题文件：")
    for f in remote_files:
        print(f"  {f}")
    if not remote_files:
        print("  （无）")
    print()
    print("将上传：")
    for p in upload:
        note = ""
        if p.suffix == ".md" and args.mode == "reset":
            note = "  （剥 tags / difficulty / primary_tag）"
        print(f"  {p.relative_to(local_paper).as_posix()}{note}")
    print()

    will_delete = []
    if args.drop_subs:
        will_delete = remote_subs + remote_sub_assets
        print("将删除（--drop-subs）：" + ("、".join(will_delete) if will_delete else "无"))
    else:
        orphan = remote_subs + remote_sub_assets
        if orphan:
            print("⚠ 云端还有子题文件，本次不动：" + "、".join(orphan))
            print("   如果本地已经不再分小问，加 --drop-subs 一起清掉。")
    print()

    if args.dry_run:
        print("-- dry-run：什么都没动。去掉 --dry-run 才会执行。")
        return

    if not args.yes:
        ans = input("确认执行？输入 yes 继续：").strip().lower()
        if ans != "yes":
            print("已取消。")
            return

    # ---- 备份 ----
    backup_dir = None
    if not args.no_backup and remote_files:
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_dir = PREV / f"{paper}-{q}-{ts}"
        backup_dir.mkdir(parents=True, exist_ok=True)
        for f in remote_files:
            scp_down(f"{rel_dir}/{f}", backup_dir / f)
        print(f"[1/4] 已备份云端 {len(remote_files)} 个文件 → {backup_dir.relative_to(PROJECT).as_posix()}")
    else:
        print("[1/4] 跳过备份")

    # ---- 准备待传文件（剥标签在临时副本上做）----
    tmpdir = Path(tempfile.mkdtemp(prefix="reupload-"))
    staged: list[tuple[Path, str]] = []
    stripped = False
    try:
        for p in upload:
            dst = tmpdir / p.name
            shutil.copy2(p, dst)
            if p.suffix == ".md" and args.mode == "reset":
                raw = dst.read_bytes()
                new, changed = strip_annotation(raw.decode("utf-8"))
                if changed:
                    dst.write_bytes(new.encode("utf-8"))
                    stripped = True
            parent = p.relative_to(local_paper).parent.as_posix()
            remote_rel = f"{rel_dir}/{parent}/{p.name}" if parent != "." else f"{rel_dir}/{p.name}"
            staged.append((dst, remote_rel))

        print(f"[2/4] 上传 {len(staged)} 个文件" + ("（已剥标注）" if stripped else ""))
        for local, remote_rel in staged:
            scp_up(local, remote_rel)
            print(f"      ✓ {remote_rel.split(rel_dir + '/', 1)[-1]}")
    finally:
        rmtree_quiet(tmpdir)

    # ---- 删子题（在传完之后，避免中间态）----
    if will_delete:
        cmds = " && ".join(f"rm -f {REMOTE_ROOT}/{rel_dir}/{f}" for f in will_delete)
        code, out, err = ssh(f"cd {REMOTE_ROOT}/{rel_dir} && {cmds}")
        if code != 0:
            print(f"[3/4] ⚠ 删除子题失败：{err.strip()}")
        else:
            print(f"[3/4] 已删除 {len(will_delete)} 个子题文件")
    else:
        print("[3/4] 无需删除")

    # ---- 清状态 ----
    try:
        res = clear_state(rel_dir, qid, clear_flag=True)
    except RuntimeError as e:
        print(f"[4/4] ⚠ 状态清理失败：{e}")
        print("      文件已上传成功，请手工检查 confirmed.json / flagged.json")
        return

    if not res.get("ok"):
        print(f"[4/4] ⚠ 状态清理：{res.get('error')}（qid={qid}，确认本地 md 的 id 与云端一致）")
    else:
        units = res.get("units") or []
        removed = res.get("removed") or []
        print(f"[4/4] 状态清理：确认单元 {len(units)} 个，撤掉 {len(removed)} 个")
        for u in removed:
            print(f"      - {u}")
        flag = res.get("flag")
        flag_cn = {"cleared": "已清除", "absent": "本无标记", "none": "未处理"}.get(flag, str(flag))
        print(f"      flagged：{flag_cn}")

    print()
    print("完成。打标台刷新即可看到新题（无需重启；题图响应头是 no-cache）。")
    if backup_dir:
        print(f"回滚：把 {backup_dir.relative_to(PROJECT).as_posix()} 里的文件 scp 回云端同一目录。")


if __name__ == "__main__":
    main()
