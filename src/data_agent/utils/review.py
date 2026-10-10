"""Translate explicit text decisions at interactive entry points."""

from typing import Any


def review_reply(payload: dict[str, Any], text: str) -> dict[str, Any]:
    value = text.strip()
    action = (
        "approve"
        if value.lower() in {"approve", "确认"}
        else "cancel"
        if value.lower() in {"cancel", "取消"}
        else "revise"
    )
    return {
        "action": action,
        "plan_version": payload["plan_version"],
        "feedback": value,
    }


def human_reply(payload: dict[str, Any], text: str) -> dict[str, Any]:
    """Bind explicit approval to the displayed version; other text revises it."""
    if payload.get("type") == "plan_review":
        return review_reply(payload, text)
    if text.strip().lower() in {"cancel", "取消"}:
        return {"action": "cancel"}
    return {"question": text}
