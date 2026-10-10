"""Durable history tests: restart, isolation, attachments and paused plans."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
import warnings

from pydantic.warnings import PydanticDeprecatedSince20

# Chainlit imports traceloop, whose models still use Pydantic's legacy Config.
with warnings.catch_warnings():
    warnings.simplefilter("ignore", PydanticDeprecatedSince20)
    from chainlit.types import Feedback, Pagination, ThreadFilter
    from chainlit.user import User
import pytest
from test_collaboration import coordinator, flow_for, task, worker

from data_agent.models.collaboration import CollaborationPlan, CollaborationSettings
from data_agent.ui.history import LocalHistory, open_checkpointer
from data_agent.utils.review import human_reply


@pytest.mark.asyncio
async def test_history_reopen_search_pagination_and_ownership(tmp_path):
    store = LocalHistory(tmp_path)
    alice = await store.create_user(User(identifier="alice"))
    bob = await store.create_user(User(identifier="bob"))
    for number in range(3):
        tid = f"a{number}"
        await store.update_thread(
            tid, user_id=alice.id, metadata={"chat_profile": "零售与库存"}
        )
        await store.create_step.__wrapped__(
            store,
            {
                "id": f"s{number}",
                "threadId": tid,
                "type": "user_message",
                "name": "用户",
                "output": f"仓库库存 {number}",
                "createdAt": f"2026-10-01T10:00:0{number}Z",
            },
        )
    await store.update_thread("b", name="私有对话", user_id=bob.id)
    # New object and new connections simulate a server restart.
    store = LocalHistory(tmp_path)
    assert (await store.get_user("alice")).id == alice.id
    page = await store.list_threads(Pagination(first=2), ThreadFilter(userId=alice.id))
    assert len(page.data) == 2 and page.pageInfo.hasNextPage
    tail = await store.list_threads(
        Pagination(first=2, cursor=page.pageInfo.endCursor),
        ThreadFilter(userId=alice.id),
    )
    assert len(tail.data) == 1
    assert len({t["id"] for t in page.data + tail.data}) == 3
    found = await store.list_threads(
        Pagination(first=10), ThreadFilter(userId=alice.id, search="库存 1")
    )
    assert [t["id"] for t in found.data] == ["a1"]
    assert found.data[0]["metadata"]["chat_profile"] == "零售与库存"
    assert (await store.list_threads(Pagination(first=10), ThreadFilter())).data == []
    await store.update_thread("a1", name="重命名", metadata={"extra": True})
    assert (await store.get_thread("a1"))["metadata"]["chat_profile"] == "零售与库存"
    feedback = Feedback(forId="s1", value=1)
    feedback_id = await store.upsert_feedback(feedback)
    assert (await store.get_thread("a1"))["steps"][0]["feedback"]["value"] == 1
    await store.delete_feedback(feedback_id)
    assert "feedback" not in (await store.get_thread("a1"))["steps"][0]


@pytest.mark.asyncio
async def test_attachments_and_deletion(tmp_path):
    store = LocalHistory(tmp_path)
    user = await store.create_user(User(identifier="owner"))
    await store.update_thread("t", user_id=user.id)
    element = SimpleNamespace(
        id="chart",
        thread_id="t",
        content=b"image bytes",
        path=None,
        mime="image/png",
        to_dict=lambda: {
            "id": "chart",
            "threadId": "t",
            "type": "image",
            "name": "图表",
            "forId": "s",
        },
    )
    await store.create_element.__wrapped__(store, element)
    restored = LocalHistory(tmp_path)
    assert await restored.read_file("t", "chart", "owner") == (
        "image/png",
        b"image bytes",
    )
    assert await restored.read_file("t", "chart", "intruder") is None
    assert await restored.get_element("different-thread", "chart") is None
    assert (await restored.get_thread("t"))["elements"][0][
        "url"
    ] == "/history/files/t/chart"
    await restored.delete_thread("t")
    assert await restored.get_thread("t") is None
    assert await restored.read_file("t", "chart", "owner") is None


def durable_flow(saver, plans):
    coord = coordinator(
        {"sales": worker(), "stock": worker()},
        plans=plans,
        settings=CollaborationSettings(review_policy="cross_source"),
    )
    flow = flow_for(coord)
    flow._checkpointer = saver
    flow._graph = flow._build_workflow()
    return flow, coord


@pytest.mark.asyncio
async def test_restart_restores_pending_plan_and_followup_context(tmp_path):
    plan = CollaborationPlan(tasks=[task("sales"), task("stock", deps=["sales"])])
    async with open_checkpointer(tmp_path) as saver:
        first, coord = durable_flow(saver, [plan])
        result = await first.run("比较销售和库存", thread_id="durable")
        pending = result["__interrupt__"][0].value
        coord.agents["sales"].ainvoke.assert_not_awaited()
    async with open_checkpointer(tmp_path) as saver:
        restored, coord = durable_flow(
            saver, [CollaborationPlan(tasks=[task("sales")])]
        )
        assert await restored.get_pending_input("durable") == pending
        done = await restored.run(
            thread_id="durable", resume_value=human_reply(pending, "确认")
        )
        assert len(done["agent_results"]) == 2
        coord.planner.ainvoke.assert_not_awaited()
        await restored.run("刚才的销售结果再解释一下", thread_id="durable")
        assert "比较销售和库存" in str(coord.planner.ainvoke.call_args)
        blank = await restored._graph.aget_state({
            "configurable": {"thread_id": "other"}
        })
        assert not blank.values
    # Deleting a UI conversation also removes its graph state.
    await LocalHistory(tmp_path).delete_thread("durable")
    async with open_checkpointer(tmp_path) as saver:
        assert (
            await saver.aget_tuple({"configurable": {"thread_id": "durable"}}) is None
        )


@pytest.mark.asyncio
async def test_ui_resume_uses_saved_id_profile_and_pending_input(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "offline-test-key")
    from data_agent.ui import app as ui
    from data_agent.ui.accounts import AccountStore

    accounts = AccountStore(tmp_path)
    accounts.register("owner", "test-password-long", ["amex"])
    monkeypatch.setattr(ui, "accounts", accounts)
    values = {"user": accounts.verify("owner", "test-password-long")}
    monkeypatch.setattr(
        ui.cl,
        "user_session",
        SimpleNamespace(
            get=values.get, set=lambda key, value: values.__setitem__(key, value)
        ),
    )
    monkeypatch.setattr(ui, "history", LocalHistory(tmp_path))
    owner = await ui.history.create_user(values["user"])
    await ui.history.update_thread("original", user_id=owner.id)
    monkeypatch.setattr(
        ui, "get_azure_credentials", lambda: ("endpoint", "key", "model")
    )
    pending = {"type": "plan_review", "message": "请确认", "plan_version": "v1"}
    flow = SimpleNamespace(
        connect=AsyncMock(),
        disconnect=AsyncMock(),
        get_pending_input=AsyncMock(return_value=pending),
    )
    monkeypatch.setattr(ui, "DataAgentFlow", lambda **kwargs: flow)
    monkeypatch.setattr(
        ui.cl, "Message", lambda **kwargs: SimpleNamespace(send=AsyncMock())
    )
    await ui.initialize_chat({
        "id": "original",
        "metadata": {"chat_profile": "金融交易"},
    })
    assert values["thread_id"] == "original"
    assert values["config_name"] == "amex"
    assert values["pending_input"] == pending
    flow.get_pending_input.assert_awaited_once_with(f"{owner.id}:original")
    await ui.on_chat_end()


@pytest.mark.asyncio
async def test_login_fails_closed(tmp_path, monkeypatch):
    from data_agent.ui import app as ui
    from data_agent.ui.accounts import AccountStore

    accounts = AccountStore(tmp_path)
    accounts.register("admin", "test-password-long")
    monkeypatch.setattr(ui, "accounts", accounts)
    assert await ui.authenticate("admin", "wrong") is None
    assert await ui.authenticate("stranger", "test-password-long") is None
    assert (await ui.authenticate("admin", "test-password-long")).identifier == "admin"
    assert await ui.authenticate("admin", "") is None


@pytest.mark.asyncio
async def test_real_http_login_sidebar_restore_and_attachment_access(
    tmp_path, monkeypatch
):
    import chainlit.data as data_module
    from chainlit.server import app
    from httpx import ASGITransport, AsyncClient
    from data_agent.ui import app as ui
    from data_agent.ui.accounts import AccountStore

    store = LocalHistory(tmp_path)
    accounts = AccountStore(tmp_path)
    accounts.register("owner", "test-login-password", ["contoso"])
    monkeypatch.setattr(ui, "accounts", accounts)
    monkeypatch.setattr(data_module, "_data_layer", store)
    monkeypatch.setattr(data_module, "_data_layer_initialized", True)
    monkeypatch.setenv("DATA_AGENT_LOGIN_USER", "owner")
    monkeypatch.setenv("DATA_AGENT_LOGIN_PASSWORD", "test-login-password")
    monkeypatch.setenv("CHAINLIT_AUTH_SECRET", "test-secret-" * 8)
    owner = await store.create_user(User(identifier="owner"))
    other = await store.create_user(User(identifier="other"))
    await store.update_thread(
        "saved",
        name="我的库存查询",
        user_id=owner.id,
        metadata={"chat_profile": "零售与库存"},
    )
    await store.update_thread("private", name="其他人的会话", user_id=other.id)
    await store.create_step.__wrapped__(
        store,
        {
            "id": "answer",
            "threadId": "saved",
            "type": "assistant_message",
            "name": "助手",
            "output": "库存为 125",
            "createdAt": "2026-10-01T10:00:00Z",
        },
    )
    element = SimpleNamespace(
        id="image",
        thread_id="saved",
        content=b"picture",
        path=None,
        mime="image/png",
        to_dict=lambda: {
            "id": "image",
            "threadId": "saved",
            "type": "image",
            "name": "图表",
        },
    )
    await store.create_element.__wrapped__(store, element)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://localhost"
    ) as client:
        assert (await client.get("/auth/config")).json()["passwordAuth"]
        assert (await client.get("/history/files/saved/image")).status_code == 401
        bad = await client.post("/login", data={"username": "owner", "password": "bad"})
        assert bad.status_code == 401
        login = await client.post(
            "/login", data={"username": "owner", "password": "test-login-password"}
        )
        assert login.status_code == 200
        listing = await client.post(
            "/project/threads", json={"pagination": {"first": 10}, "filter": {}}
        )
        assert listing.status_code == 200
        assert [t["id"] for t in listing.json()["data"]] == ["saved"]
        restored = await client.get("/project/thread/saved")
        assert restored.status_code == 200
        assert "库存为 125" in restored.text
        assert (await client.get("/project/thread/private")).status_code in {401, 403}
        image = await client.get("/history/files/saved/image")
        assert image.status_code == 200 and image.content == b"picture"
        await client.post("/logout")
        assert (await client.get("/history/files/saved/image")).status_code == 401
