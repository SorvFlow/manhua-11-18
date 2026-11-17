"""Interactive account administration. Passwords never enter shell arguments."""

from __future__ import annotations

import argparse
import getpass
from dataclasses import replace
from pathlib import Path

from web.settings import Settings
from web.store import Problem, Store


def configure_parser(parser: argparse.ArgumentParser) -> None:
    actions = parser.add_subparsers(dest="account_action", required=True)
    for name in ("create-admin", "reset-password", "list"):
        action = actions.add_parser(name)
        action.add_argument("--state-dir", type=Path, default=None)
        if name != "list":
            action.add_argument("--username", required=True)


def run(args: argparse.Namespace) -> int:
    settings = Settings.from_env()
    if args.state_dir:
        settings = replace(settings, state_dir=args.state_dir.expanduser().resolve())
    store = Store(settings)
    try:
        if args.account_action == "list":
            for user in store.users():
                print(f"{user['username']}\t{user['role']}\t{'启用' if user['active'] else '停用'}")
            return 0
        password = getpass.getpass("请输入新密码（至少 10 个字符，不会回显）：")
        if password != getpass.getpass("再次输入密码："):
            raise Problem(400, "两次密码不一致。")
        if args.account_action == "create-admin":
            store.create_user(args.username, password, role="admin", must_change=False)
        else:
            user = next((u for u in store.users() if u["username"] == args.username.lower()), None)
            if user is None:
                raise Problem(404, "账号不存在。")
            store.reset_password(user["id"], password)
        print(f"账号 {args.username} 已更新。数据库：{store.path}")
        return 0
    except Problem as exc:
        print(exc.message)
        return 1


def main() -> None:
    from core.dotenv import load_dotenv

    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__)
    configure_parser(parser)
    raise SystemExit(run(parser.parse_args()))


if __name__ == "__main__":
    main()
