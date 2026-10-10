"""Local administrator operations; run only on the trusted application host."""

import argparse
from getpass import getpass

from dotenv import load_dotenv

from data_agent.config import CONFIG_DIR
from data_agent.ui.accounts import AccountStore
from data_agent.ui.history import history_dir


def main():
    load_dotenv()
    parser = argparse.ArgumentParser(description="管理数据助手账号及数据源权限")
    parser.add_argument(
        "action", choices=["create", "grant", "revoke", "disable", "enable", "reset"]
    )
    parser.add_argument("username")
    parser.add_argument("--profiles", nargs="*", default=[])
    args = parser.parse_args()
    available = {path.stem for path in CONFIG_DIR.glob("*.yaml")}
    if set(args.profiles) - available:
        parser.error(f"可用配置：{', '.join(sorted(available))}")
    store = AccountStore(history_dir())
    if args.action in {"create", "reset"}:
        password = getpass("新密码（至少 12 位）：")
        if password != getpass("再次输入密码："):
            parser.error("两次密码不一致")
        recovery = (
            store.register(args.username, password, args.profiles)
            if args.action == "create"
            else store.admin_reset(args.username, password)
        )
        print(f"账号密码已设置。恢复码仅显示一次，请安全保存：\n{recovery}")
    elif args.action == "grant":
        store.grant(args.username, args.profiles)
        print("权限已更新，旧登录已撤销。")
    elif args.action == "revoke":
        store.revoke(args.username)
        print("所有登录已撤销。")
    else:
        with store.connection() as db:
            if not db.execute(
                "UPDATE accounts SET disabled=?, version=version+1 WHERE username=?",
                (int(args.action == "disable"), args.username),
            ).rowcount:
                parser.error("账号不存在")
        print("账号状态已更新，旧登录已撤销。")


if __name__ == "__main__":
    main()
