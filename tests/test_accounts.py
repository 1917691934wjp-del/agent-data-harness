"""Exercise security boundaries using real SQLite, JWTs and HTTP requests."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
import warnings

import pytest
from pydantic.warnings import PydanticDeprecatedSince20

with warnings.catch_warnings():
    warnings.simplefilter("ignore", PydanticDeprecatedSince20)
    from chainlit.user import User

from data_agent.ui.accounts import AccountStore
from data_agent.ui.account_routes import actor
from data_agent.ui.history import LocalHistory


def test_credentials_recovery_rotation_and_restart(tmp_path):
    store = AccountStore(tmp_path)
    recovery = store.register("alice", "long-first-password")
    user = store.verify("alice", "long-first-password")
    assert store.profiles(user) == frozenset()
    contents = tmp_path.joinpath("accounts.sqlite").read_bytes()
    assert b"long-first-password" not in contents and recovery.encode() not in contents
    reopened = AccountStore(tmp_path)
    assert reopened.verify("alice", "long-first-password")
    with pytest.raises(ValueError):
        store.register("alice", "another-long-password")
    with pytest.raises(ValueError):
        store.reset("alice", "wrong-code", "long-second-password")
    next_code = store.reset("alice", recovery, "long-second-password")
    assert next_code != recovery
    with pytest.raises(ValueError):
        store.validate(user)
    with pytest.raises(ValueError):
        store.reset("alice", recovery, "long-third-password")
    assert store.verify("alice", "long-first-password") is None
    new_user = store.verify("alice", "long-second-password")
    store.grant("alice", ["contoso"])
    with pytest.raises(ValueError):
        store.validate(new_user)
    assert store.profiles(store.verify("alice", "long-second-password")) == {"contoso"}


def test_atomic_password_change_and_lockout(tmp_path):
    store = AccountStore(tmp_path)
    code = store.register("alice", "long-first-password")
    user = store.verify("alice", "long-first-password")
    with pytest.raises(ValueError):
        store.change_password(user, "long-first-password", "short")
    # Invalid change leaves both the session and recovery code intact.
    store.validate(user)
    code2 = store.change_password(user, "long-first-password", "long-second-password")
    with pytest.raises(ValueError):
        store.reset("alice", code, "long-third-password")
    for _ in range(5):
        assert store.verify("alice", "wrong") is None
    assert store.verify("alice", "long-second-password") is None
    store.reset("alice", code2, "long-third-password")
    fresh = store.verify("alice", "long-third-password")
    assert fresh
    store.revoke("alice")
    with pytest.raises(ValueError):
        store.validate(fresh)
    forged = User(
        identifier="alice", metadata={"auth_version": 999, "account_id": "fake"}
    )
    with pytest.raises(ValueError):
        store.validate(forged)
    store.admin_reset("alice", "admin-reset-password")
    assert store.verify("alice", "admin-reset-password")


@pytest.mark.asyncio
async def test_socket_session_cannot_be_reused_by_another_account(
    tmp_path, monkeypatch
):
    from chainlit.auth import create_jwt
    from chainlit.server import sio
    from chainlit import socket
    from chainlit.session import WebsocketSession
    from data_agent.ui import app as ui

    store = AccountStore(tmp_path)
    store.register("alice", "long-first-password")
    store.register("bob", "long-second-password")
    monkeypatch.setattr(ui, "accounts", store)
    monkeypatch.setenv("CHAINLIT_AUTH_SECRET", "socket-test-secret" * 4)
    alice = store.verify("alice", "long-first-password")
    bob = store.verify("bob", "long-second-password")
    session = SimpleNamespace(user=alice, token=create_jwt(alice))
    monkeypatch.setattr(WebsocketSession, "get_by_id", lambda key: session)
    monkeypatch.setattr(socket, "_get_token", lambda env: create_jwt(bob))
    with pytest.raises(ConnectionRefusedError):
        socket.restore_existing_session("socket", "alice-session", None, None, {})
    monkeypatch.setattr(WebsocketSession, "get", lambda key: session)
    disconnected = AsyncMock()
    monkeypatch.setattr(sio, "disconnect", disconnected)
    store.revoke("alice")
    handler = next(
        handler
        for event, handler in sio.handlers["/"].items()
        if event not in {"connect", "disconnect"}
    )
    await handler("socket")
    disconnected.assert_awaited_once_with("socket")


@pytest.mark.asyncio
async def test_legacy_checkpoint_migration_preserves_owned_pending_plan(tmp_path):
    from data_agent.ui.history import open_checkpointer
    from test_history import durable_flow
    from test_collaboration import task
    from data_agent.models.collaboration import CollaborationPlan

    history = LocalHistory(tmp_path)
    user = await history.create_user(User(identifier="alice"))
    await history.update_thread("legacy", user_id=user.id)
    async with open_checkpointer(tmp_path) as saver:
        flow, _ = durable_flow(
            saver,
            [CollaborationPlan(tasks=[task("sales"), task("stock", deps=["sales"])])],
        )
        result = await flow.run("比较销售和库存", thread_id="legacy")
        pending = result["__interrupt__"][0].value
    graph_id = await history.checkpoint_id("legacy", "alice")
    assert graph_id == f"{user.id}:legacy"
    async with open_checkpointer(tmp_path) as saver:
        restored, _ = durable_flow(saver, [])
        assert await restored.get_pending_input(graph_id) == pending
        assert await saver.aget_tuple({"configurable": {"thread_id": "legacy"}}) is None
    await history.delete_thread("legacy")
    async with open_checkpointer(tmp_path) as saver:
        assert await saver.aget_tuple({"configurable": {"thread_id": graph_id}}) is None


@pytest.mark.asyncio
async def test_recovery_code_is_consumed_atomically(tmp_path):
    store = AccountStore(tmp_path)
    code = store.register("alice", "long-first-password")
    results = await asyncio.gather(
        asyncio.to_thread(store.reset, "alice", code, "long-second-password"),
        asyncio.to_thread(store.reset, "alice", code, "long-third-password"),
        return_exceptions=True,
    )
    assert sum(isinstance(value, str) for value in results) == 1
    assert sum(isinstance(value, ValueError) for value in results) == 1


@pytest.mark.asyncio
async def test_history_rejects_foreign_reads_writes_and_identifier_collisions(tmp_path):
    history = LocalHistory(tmp_path)
    alice = await history.create_user(User(identifier="alice"))
    bob = await history.create_user(User(identifier="bob"))
    await history.update_thread("a", user_id=alice.id)
    await history.update_thread("b", user_id=bob.id)
    await history.create_step.__wrapped__(
        history, {"id": "bob-answer", "threadId": "b", "output": "private"}
    )
    context = actor.set("alice")
    try:
        for operation in (
            history.get_thread("b"),
            history.update_thread("b", name="overwrite"),
            history.delete_thread("b"),
            history._delete_record("step", "bob-answer"),
            history.create_step.__wrapped__(
                history, {"id": "bob-answer", "threadId": "a", "output": "overwrite"}
            ),
            history.update_thread("a", user_id=bob.id),
            history.checkpoint_id("b", "alice"),
            history.checkpoint_id(f"{bob.id}:b", "alice"),
        ):
            with pytest.raises(PermissionError):
                await operation
        assert (await history.get_thread("a"))["userId"] == alice.id
    finally:
        actor.reset(context)
    assert (await history.get_thread("b"))["steps"][0]["output"] == "private"


@pytest.mark.asyncio
async def test_http_registration_recovery_and_revoked_sessions(tmp_path, monkeypatch):
    import chainlit.data as data_module
    from chainlit.server import app
    from httpx import ASGITransport, AsyncClient
    from data_agent.ui import app as ui

    store = AccountStore(tmp_path)
    monkeypatch.setattr(ui, "accounts", store)
    monkeypatch.setattr(data_module, "_data_layer", LocalHistory(tmp_path))
    monkeypatch.setattr(data_module, "_data_layer_initialized", True)
    monkeypatch.setenv("CHAINLIT_AUTH_SECRET", "offline-test-secret" * 4)
    monkeypatch.setenv("DATA_AGENT_ALLOW_REGISTRATION", "true")
    transport = ASGITransport(app=app, client=("auth-test-client", 123))
    async with AsyncClient(transport=transport, base_url="http://localhost") as client:
        page = await client.get("/account")
        assert page.status_code == 200 and "注册" in page.text
        assert page.headers["cache-control"] == "no-store"
        payload = {"username": "alice", "password": "long-first-password"}
        assert (
            await client.post(
                "/accounts/register",
                json=payload,
                headers={"Origin": "https://evil.test"},
            )
        ).status_code == 403
        registered = await client.post("/accounts/register", json=payload)
        assert registered.status_code == 200
        code = registered.json()["recovery"]
        assert (
            await client.post("/accounts/register", json=payload)
        ).status_code == 400
        assert (await client.post("/login", data=payload)).status_code == 200
        assert (await client.get("/accounts/me")).json()["profiles"] == []
        assert (
            await client.post(
                "/accounts/rotate",
                json={"password": payload["password"], "new_password": "short"},
            )
        ).status_code == 400
        assert (await client.get("/accounts/me")).status_code == 200
        reset = await client.post(
            "/accounts/recover",
            json={
                "username": "alice",
                "recovery": code,
                "password": "long-second-password",
            },
        )
        assert reset.status_code == 200
        assert (await client.get("/accounts/me")).status_code == 401
        # An expired cookie must not prevent opening the login/account screens.
        assert (await client.get("/auth/config")).status_code == 200
        assert (await client.get("/account")).status_code == 200
        assert (await client.get("/accounts/page.js")).status_code == 200
        assert (await client.get("/project/thread/missing")).status_code == 401
        assert (
            await client.post(
                "/login", data={"username": "alice", "password": "long-second-password"}
            )
        ).status_code == 200
        assert (await client.post("/accounts/revoke", json={})).status_code == 200
        assert (await client.get("/accounts/me")).status_code == 401
        monkeypatch.setenv("DATA_AGENT_ALLOW_REGISTRATION", "false")
        assert (
            await client.post(
                "/accounts/register",
                json={"username": "other", "password": "long-other-password"},
            )
        ).status_code == 403


@pytest.mark.asyncio
async def test_unauthorized_config_and_resume_fail_before_connection(
    tmp_path, monkeypatch
):
    from data_agent.ui import app as ui

    store = AccountStore(tmp_path)
    store.register("alice", "long-first-password", ["contoso"])
    values = {
        "user": store.verify("alice", "long-first-password"),
        "chat_profile": "金融交易",
    }
    monkeypatch.setattr(ui, "accounts", store)
    monkeypatch.setattr(ui, "history", LocalHistory(tmp_path))
    monkeypatch.setattr(
        ui.cl,
        "user_session",
        SimpleNamespace(get=values.get, set=lambda k, v: values.__setitem__(k, v)),
    )
    messages = []
    monkeypatch.setattr(
        ui.cl,
        "Message",
        lambda **kw: (
            messages.append(kw["content"]) or SimpleNamespace(send=AsyncMock())
        ),
    )
    constructor = AsyncMock()
    monkeypatch.setattr(ui, "DataAgentFlow", constructor)
    await ui.initialize_chat()
    assert "无权" in messages[-1]
    constructor.assert_not_called()
    bob = await ui.history.create_user(User(identifier="bob"))
    await ui.history.update_thread("bob-thread", user_id=bob.id)
    with pytest.raises(PermissionError):
        await ui.initialize_chat({"id": "bob-thread", "metadata": {}})
    constructor.assert_not_called()
    assert [p.name for p in await ui.chat_profiles(values["user"])] == [
        "全部数据助手",
        "零售与库存",
    ]
