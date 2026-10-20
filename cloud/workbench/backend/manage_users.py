#!/usr/bin/env python3
"""账号管理 CLI（云端专用）。

    python manage_users.py init                 生成全量账号 + 初始密码 CSV
    python manage_users.py list                 列出账号（不显示哈希）
    python manage_users.py passwd <用户名>       重置密码（打印新密码）
    python manage_users.py disable <用户名>      停用（保留条目，登录立即失效）
    python manage_users.py enable <用户名>
    python manage_users.py add <用户名> --role tagger --subject physics
    python manage_users.py newkey <用户名>       重新签发 API Key（service 账号）
    python manage_users.py csv                  重新导出初始密码 CSV

初始密码只在生成时出现一次。CSV 分发完就删（脚本会在结尾提醒）。
"""
from __future__ import annotations

import argparse
import csv
import secrets
import string
import sys
from datetime import datetime
from pathlib import Path

import yaml

import auth

# 学科清单：token 必须与 data/<board>/<qual>/<subject>/ 的目录名一致
ROSTER: list[tuple[str, str, str, int]] = [
    ("physics", "物理", "phy", 10),
    ("chemistry", "化学", "che", 10),
    ("biology", "生物", "bio", 10),
    ("economics", "经济", "eco", 10),
    ("accounting", "会计", "acc", 10),
    ("business", "商务", "bus", 10),
    ("mathematics", "数学", "mat", 20),
]

CSV_PATH = auth.CONFIG_DIR / "users-initial.csv"

# 去掉容易看错的 0/O/1/l/I
_ALPHABET = "".join(c for c in string.ascii_letters + string.digits if c not in "0O1lI")

HEADER = """\
# 题库打标平台 · 账号表
# 密码只存 pbkdf2 哈希，这里看不到明文。要重置用 manage_users.py passwd <用户名>
# 角色：admin 管理员 / lead 学科负责人 / tagger 打标员 / viewer 只读 / service AI
"""


def gen_password(n: int = 10) -> str:
    """人念得出来的随机密码：字母+数字，避开易混字符，至少 8 位。"""
    return "".join(secrets.choice(_ALPHABET) for _ in range(n))


def _load_raw() -> dict:
    if auth.USERS_PATH.exists():
        data = yaml.safe_load(auth.USERS_PATH.read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict):
            data = {}
    else:
        data = {}
    data.setdefault("version", 1)
    data.setdefault("users", [])
    return data


def _save_raw(data: dict) -> None:
    """原子写。顺序：写临时文件 → replace，半截文件永远不会被读到。"""
    auth.USERS_PATH.parent.mkdir(parents=True, exist_ok=True)
    data["updated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    text = HEADER + yaml.safe_dump(
        data, allow_unicode=True, sort_keys=False, default_flow_style=False, width=200
    )
    tmp = auth.USERS_PATH.with_suffix(".yaml.tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    tmp.replace(auth.USERS_PATH)
    try:
        auth.USERS_PATH.chmod(0o600)
    except OSError:
        pass


def _find(data: dict, username: str) -> dict | None:
    for u in data["users"]:
        if str(u.get("username")) == username:
            return u
    return None


def _new_entry(username: str, name: str, role: str, subjects: list[str]) -> tuple[dict, str | None]:
    entry: dict = {
        "username": username,
        "name": name,
        "role": role,
        "subjects": subjects,
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    if role == "service":
        key = auth.new_api_key()
        entry["key_hash"] = auth.hash_api_key(key)
        entry["must_change_password"] = False
        return entry, key
    pw = gen_password()
    entry["password"] = auth.hash_password(pw)
    entry["must_change_password"] = True
    return entry, pw


def cmd_init(args: argparse.Namespace) -> int:
    if auth.USERS_PATH.exists() and not args.force:
        print(f"已存在 {auth.USERS_PATH}，要重建请加 --force（会覆盖全部账号）", file=sys.stderr)
        return 2

    users: list[dict] = []
    rows: list[list[str]] = []

    # 管理员
    for uname, disp in (("admin", "管理员"), ("admin2", "管理员备用")):
        e, pw = _new_entry(uname, disp, "admin", ["*"])
        users.append(e)
        rows.append([uname, "admin", "全部", disp, pw or ""])

    # 各学科：01 号是学科负责人（lead），其余是打标员
    for subject, zh, prefix, count in ROSTER:
        for i in range(1, count + 1):
            uname = f"{prefix}{i:02d}"
            role = "lead" if i == 1 else "tagger"
            disp = f"{zh}{i:02d}"
            e, pw = _new_entry(uname, disp, role, [subject])
            users.append(e)
            rows.append([uname, role, zh, disp, pw or ""])

    # AI 服务账号
    e, key = _new_entry("ai-tagger", "AI 打标", "service", ["*"])
    users.append(e)

    data = {"version": 1, "users": users}
    _save_raw(data)

    # CSV 单独落盘（不与 yaml 同目录暴露给 git）
    CSV_PATH.parent.mkdir(parents=True, exist_ok=True)
    with CSV_PATH.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["用户名", "角色", "学科", "姓名", "初始密码"])
        w.writerows(rows)
        w.writerow(["ai-tagger", "service", "全部", "AI 打标", key or ""])
    try:
        CSV_PATH.chmod(0o600)
    except OSError:
        pass

    print(f"已生成 {len(users)} 个账号 → {auth.USERS_PATH}")
    print(f"初始密码 CSV → {CSV_PATH}")
    print(f"AI 账号 ai-tagger 的 API Key（仅此一次）：{key}")
    print("⚠️ 分发完密码后立刻删除 CSV：rm -f " + str(CSV_PATH))
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    data = _load_raw()
    rows = [u for u in data["users"]]
    print(f"{'用户名':<12}{'角色':<9}{'学科':<26}{'状态':<6}姓名")
    for u in rows:
        subs = ",".join(u.get("subjects") or []) or "-"
        state = "停用" if u.get("disabled") else ("待改密" if u.get("must_change_password") else "正常")
        print(f"{str(u.get('username')):<12}{str(u.get('role')):<9}{subs:<26}{state:<6}{u.get('name') or ''}")
    print(f"\n共 {len(rows)} 条")
    return 0


def cmd_passwd(args: argparse.Namespace) -> int:
    data = _load_raw()
    u = _find(data, args.username)
    if u is None:
        print(f"没有这个账号：{args.username}", file=sys.stderr)
        return 1
    pw = gen_password()
    u["password"] = auth.hash_password(pw)
    u["must_change_password"] = True
    _save_raw(data)
    print(f"{args.username} 的新密码：{pw}（首次登录会被要求改密）")
    return 0


def cmd_disable(args: argparse.Namespace, on: bool) -> int:
    data = _load_raw()
    u = _find(data, args.username)
    if u is None:
        print(f"没有这个账号：{args.username}", file=sys.stderr)
        return 1
    u["disabled"] = not on
    _save_raw(data)
    print(f"{args.username} → {'启用' if on else '停用'}（下一次请求立即生效）")
    return 0


def cmd_add(args: argparse.Namespace) -> int:
    data = _load_raw()
    if _find(data, args.username) is not None:
        print(f"账号已存在：{args.username}", file=sys.stderr)
        return 1
    subs = ["*"] if args.role in ("admin", "service") else list(args.subject or [])
    if args.role not in ("admin", "service") and not subs:
        print("非管理员/服务账号必须指定 --subject", file=sys.stderr)
        return 1
    e, secret = _new_entry(args.username, args.name or args.username, args.role, subs)
    data["users"].append(e)
    _save_raw(data)
    print(f"已新增 {args.username}（{args.role}）")
    if args.role == "service":
        print(f"API Key（仅此一次）：{secret}")
    else:
        print(f"初始密码：{secret}（首次登录会被要求改密）")
    return 0


def cmd_newkey(args: argparse.Namespace) -> int:
    data = _load_raw()
    u = _find(data, args.username)
    if u is None or u.get("role") != "service":
        print(f"{args.username} 不是 service 账号", file=sys.stderr)
        return 1
    key = auth.new_api_key()
    u["key_hash"] = auth.hash_api_key(key)
    _save_raw(data)
    print(f"{args.username} 的新 API Key（仅此一次）：{key}")
    print("旧 Key 已立即失效。")
    return 0


def cmd_csv(args: argparse.Namespace) -> int:
    """账号已存在、只是把 CSV 丢了/删早了——重新导出一份（密码列留空，需逐个重置）。"""
    data = _load_raw()
    with CSV_PATH.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["用户名", "角色", "学科", "姓名", "初始密码"])
        for u in data["users"]:
            if u.get("role") == "service":
                continue
            w.writerow([u.get("username"), u.get("role"), ",".join(u.get("subjects") or []),
                        u.get("name"), ""])
    print(f"已导出 {CSV_PATH}（密码列需用 passwd 逐个重置）")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="题库打标平台账号管理")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("init", help="生成全量账号")
    sp.add_argument("--force", action="store_true")
    sp.set_defaults(func=cmd_init)

    sub.add_parser("list", help="列出账号").set_defaults(func=cmd_list)

    sp = sub.add_parser("passwd", help="重置密码")
    sp.add_argument("username")
    sp.set_defaults(func=cmd_passwd)

    sp = sub.add_parser("disable", help="停用账号")
    sp.add_argument("username")
    sp.set_defaults(func=lambda a: cmd_disable(a, False))

    sp = sub.add_parser("enable", help="启用账号")
    sp.add_argument("username")
    sp.set_defaults(func=lambda a: cmd_disable(a, True))

    sp = sub.add_parser("add", help="新增账号")
    sp.add_argument("username")
    sp.add_argument("--role", default="tagger", choices=sorted(auth.ROLES))
    sp.add_argument("--subject", action="append")
    sp.add_argument("--name")
    sp.set_defaults(func=cmd_add)

    sp = sub.add_parser("newkey", help="重签 API Key")
    sp.add_argument("username")
    sp.set_defaults(func=cmd_newkey)

    sub.add_parser("csv", help="重新导出 CSV").set_defaults(func=cmd_csv)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
