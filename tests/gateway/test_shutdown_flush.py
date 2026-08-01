"""Tests for gateway/shutdown_flush.py — pending message durability (#72680)."""

import json
import os
import stat
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from gateway.config import Platform
from gateway.platforms.base import MessageEvent
from gateway.session import SessionSource
from gateway.shutdown_flush import (
    _serialise_value,
    flush_pending_to_file,
    recover_pending_to_db,
)


def _make_flush_dir(tmp_path: Path) -> Path:
    """Create a temp flush dir and monkeypatch _get_flush_dir to use it."""
    flush_dir = tmp_path / "pending_messages"
    flush_dir.mkdir(parents=True, exist_ok=True)
    return flush_dir


def _make_real_event(text: str = "user message") -> MessageEvent:
    """A production-shaped MessageEvent (what adapters actually flush)."""
    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="123",
        chat_type="supergroup",
        user_id="456",
        user_name="Alice",
    )
    return MessageEvent(
        text=text,
        source=source,
        message_id="789",
        reply_to_message_id="700",
        reply_to_text="original message",
    )


def test_flush_writes_string_pending_to_file(tmp_path, monkeypatch):
    flush_dir = _make_flush_dir(tmp_path)
    monkeypatch.setattr(
        "gateway.shutdown_flush._get_flush_dir", lambda: flush_dir
    )
    pending = {"agent:main:telegram:supergroup:123": "hello world"}
    count = flush_pending_to_file(pending, reason="shutdown")
    assert count == 1
    files = list(flush_dir.glob("*.json"))
    assert len(files) == 1
    payload = json.loads(files[0].read_text(encoding="utf-8"))
    assert payload["session_key"] == "agent:main:telegram:supergroup:123"
    assert payload["reason"] == "shutdown"
    assert payload["data"]["text"] == "hello world"
    assert ":" not in files[0].name
    assert "telegram" not in files[0].name


def test_flush_writes_message_event_to_file(tmp_path, monkeypatch):
    """A REAL MessageEvent (not a mock) must serialise its actual fields."""
    flush_dir = _make_flush_dir(tmp_path)
    monkeypatch.setattr(
        "gateway.shutdown_flush._get_flush_dir", lambda: flush_dir
    )
    event = _make_real_event()

    count = flush_pending_to_file(
        {"session_key_1": event},
        reason="adapter_shutdown",
        resolve_session_id=lambda key: "20260728_120000_abc",
    )
    assert count == 1
    files = list(flush_dir.glob("*.json"))
    assert len(files) == 1
    payload = json.loads(files[0].read_text(encoding="utf-8"))
    assert payload["data"]["text"] == "user message"
    assert payload["data"]["session_id"] == "20260728_120000_abc"
    # Forensic fields come from the real MessageEvent shape.
    assert payload["data"]["platform"] == Platform.TELEGRAM.value
    assert payload["data"]["sender_id"] == "456"
    assert payload["data"]["sender_name"] == "Alice"
    assert payload["data"]["message_id"] == "789"
    assert payload["data"]["reply_to_text"] == "original message"


def test_flush_real_event_round_trips_into_db(tmp_path, monkeypatch):
    """End-to-end regression for #72680: a production MessageEvent flushed at
    shutdown MUST be recoverable into the DB on next startup.

    The original implementation serialised attributes MessageEvent does not
    have, so recovery never found a session_id and every flushed message was
    permanently unrecoverable.
    """
    flush_dir = _make_flush_dir(tmp_path)
    monkeypatch.setattr(
        "gateway.shutdown_flush._get_flush_dir", lambda: flush_dir
    )
    session_key = "agent:main:telegram:supergroup:123"
    event = _make_real_event("lost on shutdown")

    def resolver(key):
        return "20260731_090000_def" if key == session_key else None

    count = flush_pending_to_file(
        {session_key: event},
        reason="adapter_shutdown",
        resolve_session_id=resolver,
    )
    assert count == 1

    mock_db = MagicMock()
    recovered = recover_pending_to_db(mock_db)

    assert recovered == 1
    kwargs = mock_db.append_message.call_args.kwargs
    assert kwargs["session_id"] == "20260731_090000_def"
    assert kwargs["role"] == "user"
    assert kwargs["content"] == "lost on shutdown"
    # Recovered flush files must be deleted so they don't replay forever.
    assert list(flush_dir.glob("*.json")) == []


def test_flush_real_event_without_resolver_preserves_text(tmp_path, monkeypatch):
    """No resolver (legacy callers): the file is still written with the text.

    Recovery can't insert it (no session_id) but must preserve the file on
    disk instead of deleting or crashing — the text is the only copy.
    """
    flush_dir = _make_flush_dir(tmp_path)
    monkeypatch.setattr(
        "gateway.shutdown_flush._get_flush_dir", lambda: flush_dir
    )
    event = _make_real_event("still preserved")

    count = flush_pending_to_file({"session_key_1": event}, reason="shutdown")
    assert count == 1
    files = list(flush_dir.glob("*.json"))
    assert len(files) == 1
    payload = json.loads(files[0].read_text(encoding="utf-8"))
    assert payload["data"]["text"] == "still preserved"
    assert "session_id" not in payload["data"]

    mock_db = MagicMock()
    assert recover_pending_to_db(mock_db) == 0
    mock_db.append_message.assert_not_called()
    assert files[0].exists()


def test_flush_resolver_failure_still_writes_payload(tmp_path, monkeypatch):
    """A raising resolver must never lose the message: text is still flushed."""
    flush_dir = _make_flush_dir(tmp_path)
    monkeypatch.setattr(
        "gateway.shutdown_flush._get_flush_dir", lambda: flush_dir
    )

    def boom(key):
        raise RuntimeError("routing index unavailable")

    count = flush_pending_to_file(
        {"session_key_1": _make_real_event("survives resolver crash")},
        reason="shutdown",
        resolve_session_id=boom,
    )
    assert count == 1
    files = list(flush_dir.glob("*.json"))
    assert len(files) == 1
    payload = json.loads(files[0].read_text(encoding="utf-8"))
    assert payload["data"]["text"] == "survives resolver crash"
    assert "session_id" not in payload["data"]


def test_flush_dict_value_keeps_existing_session_id(tmp_path, monkeypatch):
    """The resolver must not override a session_id already in the payload."""
    flush_dir = _make_flush_dir(tmp_path)
    monkeypatch.setattr(
        "gateway.shutdown_flush._get_flush_dir", lambda: flush_dir
    )
    pending = {"k": {"text": "dict message", "session_id": "sid_original"}}
    count = flush_pending_to_file(
        pending,
        reason="shutdown",
        resolve_session_id=lambda key: "sid_resolved",
    )
    assert count == 1
    payload = json.loads(
        next(iter(flush_dir.glob("*.json"))).read_text(encoding="utf-8")
    )
    assert payload["data"]["session_id"] == "sid_original"


def test_recover_inserts_via_append_message_and_deletes_file(tmp_path, monkeypatch):
    flush_dir = _make_flush_dir(tmp_path)
    monkeypatch.setattr(
        "gateway.shutdown_flush._get_flush_dir", lambda: flush_dir
    )
    ts = int(time.time())
    # Write a flush file with session_id
    payload = {
        "session_key": "agent:main:telegram:supergroup:123",
        "reason": "shutdown",
        "ts": ts,
        "data": {
            "text": "lost message",
            "session_id": "20260728_120000_abc",
        },
    }
    flush_file = flush_dir / "test_session_123.json"
    flush_file.write_text(json.dumps(payload), encoding="utf-8")

    mock_db = MagicMock()
    count = recover_pending_to_db(mock_db)

    assert count == 1
    mock_db.append_message.assert_called_once_with(
        session_id="20260728_120000_abc",
        role="user",
        content="lost message",
        timestamp=ts,
    )
    assert not flush_file.exists()


def test_serialise_real_message_event_reads_real_fields():
    result = _serialise_value(_make_real_event("msg"))
    assert result is not None
    assert result["text"] == "msg"
    # Platform/sender live on the nested SessionSource, not the event.
    assert result["platform"] == Platform.TELEGRAM.value
    assert result["sender_id"] == "456"
    assert result["sender_name"] == "Alice"
    assert result["message_id"] == "789"
    assert result["reply_to_message_id"] == "700"
    # MessageEvent has no session_id — it is injected at flush time by the
    # resolver, never invented during serialisation.
    assert "session_id" not in result
    json.dumps(result)  # payload must be JSON-clean


def test_serialise_bare_object_with_text_preserves_text():
    class Bare:
        text = "msg"

    result = _serialise_value(Bare())
    assert result is not None
    assert result["text"] == "msg"


def test_get_flush_dir_uses_get_hermes_home(tmp_path, monkeypatch):
    """Flush dir must use get_hermes_home(), not hardcoded Path.home()."""
    import gateway.shutdown_flush as mod

    captured = {}

    def fake_get_hermes_home():
        from pathlib import Path
        captured["called"] = True
        return tmp_path

    monkeypatch.setattr(
        "hermes_constants.get_hermes_home", fake_get_hermes_home
    )
    result = mod._get_flush_dir()
    assert captured.get("called") is True
    assert result == tmp_path / "pending_messages"


