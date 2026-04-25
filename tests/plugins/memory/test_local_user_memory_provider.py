"""Tests for the local per-user memory provider."""

from __future__ import annotations

from pathlib import Path

from hermes_state import SessionDB
from plugins.memory.local_user_memory import LocalUserMemoryProvider


def _seed_message(db_path: Path, *, session_id: str, user_id: str, content: str) -> None:
    db = SessionDB(db_path)
    db.create_session(session_id=session_id, source="feishu", user_id=user_id)
    db.add_message(session_id=session_id, role="user", content=content)


def test_sync_turn_stores_alias_and_prefetches_it(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "plugins.memory.local_user_memory.get_hermes_home",
        lambda: tmp_path,
    )

    provider = LocalUserMemoryProvider()
    provider.initialize(
        session_id="dm-session",
        user_id="ou_123",
        hermes_home=str(tmp_path),
        platform="feishu",
    )

    provider.sync_turn("记住，我的代号是大哥007", "好")

    context = provider.prefetch("我的代号是什么？")

    assert "大哥007" in context
    assert "代号" in context
    assert "ou_123" in context


def test_prefetch_reads_other_session_history_for_same_user(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "plugins.memory.local_user_memory.get_hermes_home",
        lambda: tmp_path,
    )

    state_db = tmp_path / "state.db"
    _seed_message(
        state_db,
        session_id="group-session",
        user_id="ou_same",
        content="记住，我的代号是大哥007",
    )
    _seed_message(
        state_db,
        session_id="other-user-session",
        user_id="ou_other",
        content="我的代号是路人甲",
    )

    provider = LocalUserMemoryProvider()
    provider.initialize(
        session_id="dm-session",
        user_id="ou_same",
        hermes_home=str(tmp_path),
        platform="feishu",
    )

    context = provider.prefetch("我的代号是什么？")

    assert "大哥007" in context
    assert "group-session" in context
    assert "路人甲" not in context


def test_cross_session_query_includes_recent_other_messages(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "plugins.memory.local_user_memory.get_hermes_home",
        lambda: tmp_path,
    )

    state_db = tmp_path / "state.db"
    _seed_message(
        state_db,
        session_id="group-session",
        user_id="ou_same",
        content="hello",
    )

    provider = LocalUserMemoryProvider()
    provider.initialize(
        session_id="dm-session",
        user_id="ou_same",
        hermes_home=str(tmp_path),
        platform="feishu",
    )

    context = provider.prefetch("你能看到我刚才在群里给你发的消息么")

    assert "hello" in context
    assert "Recent messages from this same user outside the current session" in context
