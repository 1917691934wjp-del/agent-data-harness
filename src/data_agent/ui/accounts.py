"""Durable credentials and revocable login sessions; no plaintext secrets."""

from contextlib import contextmanager
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import time
from uuid import uuid4

from chainlit.user import User


def digest(value: str, salt: str) -> str:
    return hashlib.scrypt(
        value.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1, dklen=32
    ).hex()


def password_valid(password: str):
    if not 12 <= len(password) <= 128:
        raise ValueError("密码长度须为 12–128 个字符。")


class AccountStore:
    def __init__(self, root: Path):
        self.path = root / "accounts.sqlite"
        root.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS accounts (
                  username TEXT PRIMARY KEY, id TEXT UNIQUE NOT NULL,
                  salt TEXT NOT NULL, password TEXT NOT NULL, recovery TEXT NOT NULL,
                  version INTEGER NOT NULL DEFAULT 1, profiles TEXT NOT NULL,
                  disabled INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS attempts (
                  username TEXT PRIMARY KEY, count INTEGER NOT NULL, until REAL NOT NULL);
            """)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def get(self, username):
        with self.connection() as db:
            row = db.execute(
                "SELECT * FROM accounts WHERE username=?", (username,)
            ).fetchone()
            return dict(row) if row else None

    def register(self, username, password, profiles=(), *, legacy=False):
        if not re.fullmatch(r"[A-Za-z0-9_\-]{3,64}", username):
            raise ValueError("用户名须为 3–64 位字母、数字、下划线或短横线。")
        if not legacy:
            password_valid(password)
        elif not password or len(password) > 128:
            raise ValueError("初始密码无效。")
        salt = secrets.token_hex(16)
        recovery = secrets.token_urlsafe(32)
        with self.connection() as db:
            try:
                db.execute(
                    "INSERT INTO accounts VALUES (?,?,?,?,?,1,?,0)",
                    (
                        username,
                        str(uuid4()),
                        salt,
                        digest(password, salt),
                        hashlib.sha256(recovery.encode()).hexdigest(),
                        json.dumps(list(profiles)),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError("用户名不可用。") from exc
        return recovery

    def bootstrap(self, profiles):
        username = os.getenv("DATA_AGENT_LOGIN_USER", "")
        password = os.getenv("DATA_AGENT_LOGIN_PASSWORD", "")
        if username and password and not self.get(username):
            try:
                self.register(username, password, profiles, legacy=True)
            except ValueError:
                if not self.get(username):
                    raise

    def verify(self, username, password):
        if len(username) > 64 or len(password) > 128:
            return None
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM attempts WHERE until < ?", (time.time(),))
            attempt = db.execute(
                "SELECT * FROM attempts WHERE username=?", (username,)
            ).fetchone()
            if attempt and attempt["count"] >= 5:
                return None
            account = db.execute(
                "SELECT * FROM accounts WHERE username=?", (username,)
            ).fetchone()
            salt = account["salt"] if account else "00" * 16
            valid = hmac.compare_digest(
                digest(password, salt), account["password"] if account else "00" * 32
            )
            if not account or not valid or account["disabled"]:
                # Bound unknown-name records while still doing the same password work.
                if account:
                    db.execute(
                        "INSERT INTO attempts VALUES (?,1,?) ON CONFLICT(username) DO UPDATE SET count=count+1",
                        (username, time.time() + 300),
                    )
                return None
            db.execute("DELETE FROM attempts WHERE username=?", (username,))
            return User(
                identifier=username,
                display_name=username,
                metadata={
                    "account_id": account["id"],
                    "auth_version": account["version"],
                },
            )

    def validate(self, user):
        account = self.get(user.identifier) if user else None
        if (
            not account
            or account["disabled"]
            or user.metadata.get("auth_version") != account["version"]
            or user.metadata.get("account_id") != account["id"]
        ):
            raise ValueError("登录已失效，请重新登录。")
        return account

    def reset(self, username, recovery, password):
        password_valid(password)
        if len(recovery) > 128:
            raise ValueError("账号或恢复码无效。")
        next_recovery = secrets.token_urlsafe(32)
        salt = secrets.token_hex(16)
        with self.connection() as db:
            changed = db.execute(
                """UPDATE accounts SET salt=?, password=?, recovery=?, version=version+1
                WHERE username=? AND recovery=? AND disabled=0""",
                (
                    salt,
                    digest(password, salt),
                    hashlib.sha256(next_recovery.encode()).hexdigest(),
                    username,
                    hashlib.sha256(recovery.encode()).hexdigest(),
                ),
            ).rowcount
            if not changed:
                raise ValueError("账号或恢复码无效。")
            db.execute("DELETE FROM attempts WHERE username=?", (username,))
        return next_recovery

    def revoke(self, username):
        with self.connection() as db:
            db.execute(
                "UPDATE accounts SET version=version+1 WHERE username=?", (username,)
            )

    def change_password(self, user, password, new_password):
        password_valid(new_password)
        if not self.verify(user.identifier, password):
            raise ValueError("当前密码无效。")
        account = self.validate(user)
        salt = secrets.token_hex(16)
        code = secrets.token_urlsafe(32)
        with self.connection() as db:
            if not db.execute(
                """UPDATE accounts SET salt=?, password=?, recovery=?, version=version+1
                WHERE username=? AND version=? AND disabled=0""",
                (
                    salt,
                    digest(new_password, salt),
                    hashlib.sha256(code.encode()).hexdigest(),
                    user.identifier,
                    account["version"],
                ),
            ).rowcount:
                raise ValueError("登录已失效，请重新登录。")
        return code

    def grant(self, username, profiles):
        with self.connection() as db:
            if not db.execute(
                "UPDATE accounts SET profiles=?, version=version+1 WHERE username=?",
                (json.dumps(list(profiles)), username),
            ).rowcount:
                raise ValueError("账号不存在。")

    def admin_reset(self, username, password):
        """Trusted host administrator recovery; never exposed as an HTTP route."""
        password_valid(password)
        salt = secrets.token_hex(16)
        code = secrets.token_urlsafe(32)
        with self.connection() as db:
            if not db.execute(
                """UPDATE accounts SET salt=?, password=?, recovery=?, version=version+1
                WHERE username=?""",
                (
                    salt,
                    digest(password, salt),
                    hashlib.sha256(code.encode()).hexdigest(),
                    username,
                ),
            ).rowcount:
                raise ValueError("账号不存在。")
            db.execute("DELETE FROM attempts WHERE username=?", (username,))
        return code

    def profiles(self, user):
        return frozenset(json.loads(self.validate(user)["profiles"]))
