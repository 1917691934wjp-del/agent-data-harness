"""Initialize a hashed administrator account and stable signing secret."""

from pathlib import Path
import secrets
from getpass import getpass

from dotenv import dotenv_values, set_key
from data_agent.config import CONFIG_DIR
from data_agent.ui.accounts import AccountStore
from data_agent.ui.history import history_dir


def main():
    path = Path(__file__).resolve().parents[1] / ".env"
    values = dotenv_values(path)
    if not values.get("CHAINLIT_AUTH_SECRET"):
        set_key(str(path), "CHAINLIT_AUTH_SECRET", secrets.token_urlsafe(48))
    import os

    if values.get("DATA_AGENT_HISTORY_DIR"):
        os.environ.setdefault(
            "DATA_AGENT_HISTORY_DIR", values["DATA_AGENT_HISTORY_DIR"]
        )
    store = AccountStore(history_dir())
    username = values.get("DATA_AGENT_LOGIN_USER") or "admin"
    if store.get(username):
        print(f"账号 {username} 已存在。使用账号管理页面修改密码和获取新恢复码。")
        return
    password = values.get("DATA_AGENT_LOGIN_PASSWORD")
    legacy = bool(password)
    if not password:
        password = getpass(f"为 {username} 设置密码（至少 12 位）：")
        if password != getpass("再次输入密码："):
            raise ValueError("两次密码不一致")
    recovery = store.register(
        username, password, [p.stem for p in CONFIG_DIR.glob("*.yaml")], legacy=legacy
    )
    print(
        f"账号 {username} 已创建，密码仅存储为哈希。请安全保存一次性恢复码：\n{recovery}"
    )
    if legacy:
        print(
            "已导入旧 .env 账号。确认能登录后，清空 DATA_AGENT_LOGIN_PASSWORD，并在 /account 修改密码。"
        )


if __name__ == "__main__":
    main()
