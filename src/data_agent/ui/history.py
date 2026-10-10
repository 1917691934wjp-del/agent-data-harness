"""Local durable chat history, protected attachments, and graph checkpoints."""

import asyncio
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
from uuid import uuid4

from chainlit.data.base import BaseDataLayer
from chainlit.data.utils import queue_until_user_message
from chainlit.types import PageInfo, PaginatedResponse
from chainlit.user import PersistedUser
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from data_agent.ui.account_routes import current_actor


def history_dir() -> Path:
    root = Path(__file__).resolve().parents[3]
    path = Path(os.getenv("DATA_AGENT_HISTORY_DIR", str(root / "local_history")))
    path.mkdir(parents=True, exist_ok=True)
    return path.resolve()


def open_checkpointer(root: Path | None = None):
    """Return an async context manager; keep it open for the chat's lifetime."""
    return AsyncSqliteSaver.from_conn_string(
        str((root or history_dir()) / "checkpoints.sqlite")
    )


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class LocalHistory(BaseDataLayer):
    """SQLite document storage for Chainlit's native history sidebar.

    Each operation runs off the event loop in a short transaction. Attachments
    are stored as BLOBs and served only after checking the conversation owner.
    """

    def __init__(self, root: Path | None = None):
        self.root = root or history_dir()
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "chats.sqlite"
        with self.connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS records (
                    kind TEXT NOT NULL, id TEXT NOT NULL, thread TEXT,
                    owner TEXT, data TEXT NOT NULL, updated TEXT NOT NULL,
                    PRIMARY KEY(kind, id)
                );
                CREATE INDEX IF NOT EXISTS records_thread ON records(thread, kind);
                CREATE INDEX IF NOT EXISTS records_owner ON records(owner, kind);
                CREATE TABLE IF NOT EXISTS files (
                    id TEXT PRIMARY KEY, thread TEXT NOT NULL,
                    mime TEXT NOT NULL, content BLOB NOT NULL
                );
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

    def _get(self, kind, record_id):
        with self.connection() as db:
            row = db.execute(
                "SELECT * FROM records WHERE kind=? AND id=?", (kind, record_id)
            ).fetchone()
            if row and kind != "user":
                self._authorize(db, row["thread"])
            return json.loads(row["data"]) if row else None

    def _authorize(self, db, thread_id):
        identifier = current_actor()
        if identifier:
            row = db.execute(
                "SELECT data FROM records WHERE kind='thread' AND id=?", (thread_id,)
            ).fetchone()
            if not row or json.loads(row[0]).get("userIdentifier") != identifier:
                raise PermissionError("Conversation belongs to another user")

    async def checkpoint_id(self, thread_id, identifier):
        if ":" in thread_id:
            raise PermissionError("Invalid conversation identifier")
        if await self.get_thread_author(thread_id) != identifier:
            raise PermissionError("Conversation belongs to another user")
        user = await self.get_user(identifier)
        target = f"{user.id}:{thread_id}"
        path = self.root / "checkpoints.sqlite"

        def migrate():
            if not path.exists():
                return
            with sqlite3.connect(path, timeout=15) as db:
                db.execute("BEGIN IMMEDIATE")
                for table in ("checkpoints", "writes"):
                    if db.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                        (table,),
                    ).fetchone():
                        db.execute(
                            f'UPDATE "{table}" SET thread_id=? WHERE thread_id=?',
                            (target, thread_id),
                        )

        await asyncio.to_thread(migrate)
        return target

    def _save(self, kind, record_id, data, thread=None, owner=None):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM records WHERE kind=? AND id=?", (kind, record_id)
            ).fetchone()
            previous = json.loads(row["data"]) if row else {}
            if kind != "user":
                if row:
                    self._authorize(db, row["thread"])
                    if thread and row["thread"] != thread:
                        raise PermissionError(
                            "Cannot move a record between conversations"
                        )
                if kind == "thread" and not row:
                    if (
                        current_actor()
                        and data.get("userIdentifier") != current_actor()
                    ):
                        raise PermissionError("Conversation owner required")
                else:
                    self._authorize(db, thread or (row["thread"] if row else None))
                if (
                    kind == "thread"
                    and previous.get("userId")
                    and owner
                    and previous["userId"] != owner
                ):
                    raise PermissionError("Cannot transfer conversation ownership")
            data = {key: value for key, value in data.items() if value is not None}
            if "metadata" in data:
                data["metadata"] = {**previous.get("metadata", {}), **data["metadata"]}
            merged = {**previous, **data}
            if kind == "user" and previous:
                merged.update(id=previous["id"], createdAt=previous["createdAt"])
            db.execute(
                "INSERT OR REPLACE INTO records VALUES (?,?,?,?,?,?)",
                (
                    kind,
                    record_id,
                    thread or (row["thread"] if row else None),
                    owner or (row["owner"] if row else None),
                    json.dumps(merged, ensure_ascii=False),
                    now(),
                ),
            )
            return merged

    async def get_user(self, identifier):
        data = await asyncio.to_thread(self._get, "user", identifier)
        return PersistedUser(**data) if data else None

    async def create_user(self, user):
        existing = await self.get_user(user.identifier)
        data = asdict(user)
        data.update(
            id=existing.id if existing else str(uuid4()),
            createdAt=existing.createdAt if existing else now(),
        )
        saved = await asyncio.to_thread(self._save, "user", user.identifier, data)
        return PersistedUser(**saved)

    async def update_thread(
        self, thread_id, name=None, user_id=None, metadata=None, tags=None
    ):
        existing = await asyncio.to_thread(self._get, "thread", thread_id)
        data = {
            "id": thread_id,
            "name": name,
            "userId": user_id,
            "metadata": metadata,
            "tags": tags,
        }
        if not existing:
            data.update(createdAt=now(), metadata=metadata or {}, tags=tags or [])
        if user_id:

            def find_user():
                with self.connection() as db:
                    for row in db.execute("SELECT data FROM records WHERE kind='user'"):
                        user = json.loads(row[0])
                        if user["id"] == user_id:
                            return user["identifier"]
                raise ValueError("Unknown history user")

            data["userIdentifier"] = await asyncio.to_thread(find_user)
        await asyncio.to_thread(
            self._save, "thread", thread_id, data, thread_id, user_id
        )

    async def get_thread_author(self, thread_id):
        data = await asyncio.to_thread(self._get, "thread", thread_id)
        return data.get("userIdentifier", "") if data else ""

    async def get_thread(self, thread_id):
        with self.connection() as db:
            self._authorize(db, thread_id)

        def read():
            data = self._get("thread", thread_id)
            if data is None:
                return None
            data.update(steps=[], elements=[])
            with self.connection() as db:
                records = db.execute(
                    "SELECT kind, data FROM records WHERE thread=? ORDER BY rowid",
                    (thread_id,),
                ).fetchall()
            feedbacks = {}
            for row in records:
                value = json.loads(row["data"])
                if row["kind"] == "step":
                    data["steps"].append(value)
                elif row["kind"] == "element":
                    data["elements"].append(value)
                elif row["kind"] == "feedback":
                    feedbacks[value["forId"]] = value
            for step in data["steps"]:
                if step["id"] in feedbacks:
                    step["feedback"] = feedbacks[step["id"]]
            data["steps"].sort(
                key=lambda step: step.get("createdAt") or step.get("start") or ""
            )
            return data

        return await asyncio.to_thread(read)

    async def list_threads(self, pagination, filters):
        if not filters.userId:
            return PaginatedResponse(PageInfo(False, None, None), [])
        if current_actor():
            user = await self.get_user(current_actor())
            if not user or filters.userId != user.id:
                raise PermissionError("Cannot list another user's conversations")

        def read():
            with self.connection() as db:
                return [
                    json.loads(row[0])
                    for row in db.execute(
                        "SELECT data FROM records WHERE kind='thread' AND owner=? ORDER BY updated DESC, id DESC",
                        (filters.userId,),
                    )
                ]

        threads = await asyncio.to_thread(read)
        matches = []
        for thread in threads:
            full = await self.get_thread(thread["id"])
            if filters.search:
                text = (
                    (thread.get("name") or "")
                    + " "
                    + " ".join(str(step.get("output", "")) for step in full["steps"])
                )
                if filters.search.casefold() not in text.casefold():
                    continue
            if filters.feedback is not None and not any(
                step.get("feedback", {}).get("value") == filters.feedback
                for step in full["steps"]
            ):
                continue
            matches.append(full)
        start = 0
        if pagination.cursor:
            start = next(
                (
                    i + 1
                    for i, item in enumerate(matches)
                    if item["id"] == pagination.cursor
                ),
                len(matches),
            )
        page = matches[start : start + pagination.first]
        return PaginatedResponse(
            PageInfo(
                start + len(page) < len(matches),
                page[0]["id"] if page else None,
                page[-1]["id"] if page else None,
            ),
            page,
        )

    @queue_until_user_message()
    async def create_step(self, step_dict):
        await asyncio.to_thread(
            self._save, "step", step_dict["id"], step_dict, step_dict["threadId"]
        )
        thread = await asyncio.to_thread(self._get, "thread", step_dict["threadId"])
        name = None
        if step_dict.get("type") == "user_message" and not (thread or {}).get("name"):
            name = str(step_dict.get("output") or "新对话")[:60]
        await self.update_thread(step_dict["threadId"], name=name)

    @queue_until_user_message()
    async def update_step(self, step_dict):
        await self.create_step.__wrapped__(self, step_dict)

    async def _delete_record(self, kind, record_id):
        def delete():
            with self.connection() as db:
                row = db.execute(
                    "SELECT thread FROM records WHERE kind=? AND id=?",
                    (kind, record_id),
                ).fetchone()
                if row:
                    self._authorize(db, row[0])
                db.execute(
                    "DELETE FROM records WHERE kind=? AND id=?", (kind, record_id)
                )

        await asyncio.to_thread(delete)

    @queue_until_user_message()
    async def delete_step(self, step_id):
        await self._delete_record("step", step_id)

    @queue_until_user_message()
    async def create_element(self, element):
        data = element.to_dict()
        with self.connection() as db:
            self._authorize(db, element.thread_id)
            previous = db.execute(
                "SELECT thread FROM files WHERE id=?", (element.id,)
            ).fetchone()
            if previous and previous[0] != element.thread_id:
                raise PermissionError(
                    "Cannot overwrite another conversation's attachment"
                )
        content = element.content
        if element.path:
            content = await asyncio.to_thread(Path(element.path).read_bytes)
        if content is not None:
            if isinstance(content, str):
                content = content.encode("utf-8")

            def save_file():
                with self.connection() as db:
                    db.execute(
                        "INSERT OR REPLACE INTO files VALUES (?,?,?,?)",
                        (
                            element.id,
                            element.thread_id,
                            element.mime or "application/octet-stream",
                            content,
                        ),
                    )

            await asyncio.to_thread(save_file)
            data["url"] = f"/history/files/{element.thread_id}/{element.id}"
        data["chainlitKey"] = None
        await asyncio.to_thread(
            self._save, "element", element.id, data, element.thread_id
        )

    async def get_element(self, thread_id, element_id):
        data = await asyncio.to_thread(self._get, "element", element_id)
        return data if data and data.get("threadId") == thread_id else None

    async def read_file(self, thread_id, element_id, identifier):
        if await self.get_thread_author(thread_id) != identifier:
            return None

        def read():
            with self.connection() as db:
                row = db.execute(
                    "SELECT mime, content FROM files WHERE thread=? AND id=?",
                    (thread_id, element_id),
                ).fetchone()
                return (row[0], row[1]) if row else None

        return await asyncio.to_thread(read)

    @queue_until_user_message()
    async def delete_element(self, element_id, thread_id=None):
        def delete():
            with self.connection() as db:
                row = db.execute(
                    "SELECT thread FROM records WHERE kind='element' AND id=?",
                    (element_id,),
                ).fetchone()
                if row:
                    self._authorize(db, row[0])
                    if thread_id and row[0] != thread_id:
                        raise PermissionError("Wrong conversation")
                db.execute("DELETE FROM files WHERE id=?", (element_id,))
                db.execute(
                    "DELETE FROM records WHERE kind='element' AND id=?", (element_id,)
                )

        await asyncio.to_thread(delete)

    async def upsert_feedback(self, feedback):
        data = asdict(feedback)
        data["id"] = data.get("id") or str(uuid4())
        step = await asyncio.to_thread(self._get, "step", feedback.forId)
        thread_id = feedback.threadId or (step or {}).get("threadId")
        if not step or thread_id != step.get("threadId"):
            raise PermissionError("Feedback requires a step in the same conversation")
        await asyncio.to_thread(self._save, "feedback", data["id"], data, thread_id)
        return data["id"]

    async def delete_feedback(self, feedback_id):
        await self._delete_record("feedback", feedback_id)
        return True

    async def delete_thread(self, thread_id):
        with self.connection() as db:
            self._authorize(db, thread_id)
        existing = await asyncio.to_thread(self._get, "thread", thread_id)
        checkpoint_id = thread_id
        if existing and existing.get("userIdentifier"):
            checkpoint_id = await self.checkpoint_id(
                thread_id, existing["userIdentifier"]
            )
        async with open_checkpointer(self.root) as saver:
            await saver.setup()
            await saver.adelete_thread(checkpoint_id)

        def delete():
            with self.connection() as db:
                db.execute("DELETE FROM files WHERE thread=?", (thread_id,))
                db.execute("DELETE FROM records WHERE thread=?", (thread_id,))

        await asyncio.to_thread(delete)

    async def build_debug_url(self):
        return ""

    async def close(self):
        pass


def register_file_route(layer: LocalHistory):
    """Add the authenticated attachment route ahead of Chainlit's SPA fallback."""
    from chainlit.auth import get_current_user
    from chainlit.server import app
    from fastapi import Depends, HTTPException
    from fastapi.responses import Response

    path = "/history/files/{thread_id}/{element_id}"
    if any(getattr(route, "path", None) == path for route in app.routes):
        return

    @app.get(path)
    async def history_file(
        thread_id: str, element_id: str, user=Depends(get_current_user)
    ):
        from chainlit.data import get_data_layer

        if not user:
            raise HTTPException(401)
        current_layer = get_data_layer()
        if not isinstance(current_layer, LocalHistory):
            raise HTTPException(404)
        result = await current_layer.read_file(thread_id, element_id, user.identifier)
        if result is None:
            raise HTTPException(404)
        mime, content = result
        return Response(
            content,
            media_type=mime,
            headers={
                "Cache-Control": "private, no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

    app.router.routes.insert(0, app.router.routes.pop())
