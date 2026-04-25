"""Local per-user memory provider with zero external dependencies.

Pure local SQLite memory that scopes recall by the runtime gateway ``user_id``.
It combines two sources:

1. Explicit user facts extracted from strong patterns like "我的代号是 X"
2. Cross-session message recall by reading Hermes' existing ``state.db``

This is intentionally lightweight: no external services, no extra LLM keys,
and no daemon process. It is designed for messaging platforms where the same
user talks to Hermes across multiple groups and DMs.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List

from agent.memory_provider import MemoryProvider
from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)

_DB_NAME = "local_user_memory.db"
_STATE_DB_NAME = "state.db"
_MAX_FACTS = 4
_MAX_HISTORY = 4
_MAX_RECENT = 3
_MAX_SNIPPET_CHARS = 180

_IDENTITY_HINTS = (
    "代号",
    "名字",
    "昵称",
    "称呼",
    "英文名",
    "花名",
    "我是谁",
    "还记得我",
    "认识我",
)
_CROSS_SESSION_HINTS = (
    "群",
    "私聊",
    "dm",
    "消息",
    "刚才",
    "刚刚",
    "之前",
    "上次",
    "另一个",
    "会话",
    "session",
)
_QUERY_TERMS = (
    "代号",
    "名字",
    "昵称",
    "称呼",
    "英文名",
    "花名",
    "身份",
    "消息",
    "群",
    "私聊",
)
_STOP_TOKENS = {
    "我的",
    "我是",
    "什么",
    "现在",
    "刚才",
    "刚刚",
    "一下",
    "一个",
    "这个",
    "那个",
    "你能",
    "看到",
    "记住",
    "可以",
    "怎么",
}

_SUBJECT_ALIASES = {
    "代号": "代号",
    "花名": "代号",
    "名字": "名字",
    "昵称": "昵称",
    "称呼": "称呼",
    "英文名": "英文名",
}

_EXPLICIT_PATTERNS = (
    re.compile(
        r"(?:记住[\s,，:：]*)?(?:我(?:的)?)(?P<subject>代号|花名|名字|昵称|称呼|英文名)\s*是\s*(?P<value>[^\n，。！？!?；;]+)"
    ),
    re.compile(
        r"(?:记住[\s,，:：]*)?叫我\s*(?P<value>[^\n，。！？!?；;]{1,40})"
    ),
)


def _clip(text: str, limit: int = _MAX_SNIPPET_CHARS) -> str:
    text = re.sub(r"\s+", " ", (text or "").strip())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _normalize_subject(subject: str) -> str:
    return _SUBJECT_ALIASES.get((subject or "").strip(), (subject or "").strip())


def _clean_value(value: str) -> str:
    cleaned = (value or "").strip()
    cleaned = cleaned.strip("`'\"“”‘’ ")
    cleaned = re.sub(r"\s+", " ", cleaned)
    cleaned = re.sub(r"[。！？!?；;，,]+$", "", cleaned)
    return cleaned[:80].strip()


def _extract_keywords(query: str) -> List[str]:
    text = (query or "").strip().lower()
    if not text:
        return []

    keywords: list[str] = []
    seen: set[str] = set()

    for term in _QUERY_TERMS:
        if term in query and term not in seen:
            keywords.append(term)
            seen.add(term)

    for token in re.findall(r"[A-Za-z0-9_]{2,}", text):
        if token not in seen:
            keywords.append(token)
            seen.add(token)

    for chunk in re.findall(r"[\u4e00-\u9fff]{2,8}", query):
        if chunk in _STOP_TOKENS or chunk in seen:
            continue
        if len(chunk) <= 4:
            keywords.append(chunk)
            seen.add(chunk)

    return keywords[:6]


def _looks_identity_query(query: str) -> bool:
    text = (query or "").strip()
    return any(term in text for term in _IDENTITY_HINTS)


def _looks_cross_session_query(query: str) -> bool:
    text = (query or "").strip().lower()
    return any(term in text for term in _CROSS_SESSION_HINTS)


def _is_trivial_query(query: str) -> bool:
    text = (query or "").strip().lower()
    if not text:
        return True
    return text in {"ok", "okay", "yes", "no", "收到", "好的", "嗯", "hello", "hi"}


class LocalUserMemoryProvider(MemoryProvider):
    """Local cross-session recall keyed by the current gateway user_id."""

    def __init__(self) -> None:
        self._user_id = ""
        self._session_id = ""
        self._db_path = get_hermes_home() / _DB_NAME
        self._state_db_path = get_hermes_home() / _STATE_DB_NAME

    @property
    def name(self) -> str:
        return "local_user_memory"

    def is_available(self) -> bool:
        return True

    def initialize(self, session_id: str, **kwargs) -> None:
        self._session_id = str(session_id or "").strip()
        self._user_id = str(kwargs.get("user_id") or "").strip()
        hermes_home = Path(str(kwargs.get("hermes_home") or get_hermes_home()))
        self._db_path = hermes_home / _DB_NAME
        self._state_db_path = hermes_home / _STATE_DB_NAME
        self._init_db()

    def system_prompt_block(self) -> str:
        return (
            "# Local User Memory\n"
            "Active. For gateway sessions with a runtime user_id, relevant facts and "
            "cross-session message excerpts for that same user are auto-injected. "
            "Use that context to answer questions about who the user is, what they "
            "asked you to remember, and what they recently said in other chats."
        )

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        if session_id:
            self._session_id = str(session_id).strip()
        if not self._user_id or _is_trivial_query(query):
            return ""

        facts = self._load_relevant_facts(query)
        related_messages = self._search_related_messages(query)
        recent_cross_session = []
        if _looks_cross_session_query(query):
            recent_cross_session = self._recent_cross_session_messages()

        parts: list[str] = []
        if facts:
            lines = [f"- {row['subject']}: {row['value']}" for row in facts]
            parts.append("Known user facts:\n" + "\n".join(lines))
        if related_messages:
            lines = [
                f"- [session {row['session_id']}] {row['content']}"
                for row in related_messages
            ]
            parts.append("Relevant messages from this same user in other sessions:\n" + "\n".join(lines))
        if recent_cross_session:
            lines = [
                f"- [session {row['session_id']}] {row['content']}"
                for row in recent_cross_session
            ]
            parts.append("Recent messages from this same user outside the current session:\n" + "\n".join(lines))

        if not parts:
            return ""

        return (
            f"# Local User Memory (user_id={self._user_id})\n"
            + "\n\n".join(parts)
        )

    def sync_turn(self, user_content: str, assistant_content: str, *, session_id: str = "") -> None:
        if session_id:
            self._session_id = str(session_id).strip()
        if not self._user_id or not user_content:
            return

        extracted = self._extract_explicit_facts(user_content)
        if not extracted:
            return

        now = time.time()
        with sqlite3.connect(str(self._db_path), timeout=5.0) as conn:
            conn.execute("PRAGMA foreign_keys=ON")
            for item in extracted:
                conn.execute(
                    """
                    INSERT INTO user_memories (
                        user_id, subject, value, source_session_id, source_message,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(user_id, subject) DO UPDATE SET
                        value = excluded.value,
                        source_session_id = excluded.source_session_id,
                        source_message = excluded.source_message,
                        updated_at = excluded.updated_at
                    """,
                    (
                        self._user_id,
                        item["subject"],
                        item["value"],
                        self._session_id or None,
                        _clip(user_content, 400),
                        now,
                        now,
                    ),
                )

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return []

    def shutdown(self) -> None:
        pass

    def _init_db(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(str(self._db_path), timeout=5.0) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS user_memories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    value TEXT NOT NULL,
                    source_session_id TEXT,
                    source_message TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    UNIQUE(user_id, subject)
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_user_memories_user_updated ON user_memories(user_id, updated_at DESC)"
            )

    def _extract_explicit_facts(self, text: str) -> List[Dict[str, str]]:
        cleaned = (text or "").strip()
        results: list[dict[str, str]] = []
        seen: set[tuple[str, str]] = set()

        for pattern in _EXPLICIT_PATTERNS:
            for match in pattern.finditer(cleaned):
                raw_subject = match.groupdict().get("subject") or "称呼"
                subject = _normalize_subject(raw_subject)
                value = _clean_value(match.groupdict().get("value") or "")
                if not value:
                    continue
                key = (subject, value)
                if key in seen:
                    continue
                seen.add(key)
                results.append({"subject": subject, "value": value})
        return results

    def _load_relevant_facts(self, query: str) -> List[Dict[str, str]]:
        if not self._user_id:
            return []

        clauses = ["user_id = ?"]
        params: list[Any] = [self._user_id]
        query_text = (query or "").strip()
        matched_subjects: list[str] = []
        seen_subjects: set[str] = set()
        for raw_subject, normalized in _SUBJECT_ALIASES.items():
            if raw_subject in query_text and normalized not in seen_subjects:
                matched_subjects.append(normalized)
                seen_subjects.add(normalized)
        if matched_subjects:
            placeholders = ",".join("?" for _ in matched_subjects)
            clauses.append(f"subject IN ({placeholders})")
            params.extend(matched_subjects)

        sql = (
            "SELECT subject, value, updated_at FROM user_memories "
            f"WHERE {' AND '.join(clauses)} "
            "ORDER BY updated_at DESC LIMIT ?"
        )
        params.append(_MAX_FACTS)

        with sqlite3.connect(str(self._db_path), timeout=5.0) as conn:
            conn.row_factory = sqlite3.Row
            rows = [dict(row) for row in conn.execute(sql, params).fetchall()]

        if rows or not _looks_identity_query(query_text):
            return rows

        with sqlite3.connect(str(self._db_path), timeout=5.0) as conn:
            conn.row_factory = sqlite3.Row
            return [
                dict(row)
                for row in conn.execute(
                    "SELECT subject, value, updated_at FROM user_memories "
                    "WHERE user_id = ? ORDER BY updated_at DESC LIMIT ?",
                    (self._user_id, _MAX_FACTS),
                ).fetchall()
            ]

    def _search_related_messages(self, query: str) -> List[Dict[str, str]]:
        if not self._user_id or not self._state_db_path.exists():
            return []

        keywords = _extract_keywords(query)
        if not keywords:
            return []

        where = [
            "s.user_id = ?",
            "m.role = 'user'",
        ]
        params: list[Any] = [self._user_id]
        if self._session_id:
            where.append("m.session_id != ?")
            params.append(self._session_id)

        like_terms = []
        for keyword in keywords:
            like_terms.append("m.content LIKE ?")
            params.append(f"%{keyword}%")
        where.append("(" + " OR ".join(like_terms) + ")")
        params.append(_MAX_HISTORY)

        sql = (
            "SELECT m.session_id, m.content, m.timestamp "
            "FROM messages m JOIN sessions s ON s.id = m.session_id "
            f"WHERE {' AND '.join(where)} "
            "ORDER BY m.timestamp DESC LIMIT ?"
        )

        with sqlite3.connect(str(self._state_db_path), timeout=5.0) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(sql, params).fetchall()
        return self._dedupe_rows(rows)

    def _recent_cross_session_messages(self) -> List[Dict[str, str]]:
        if not self._user_id or not self._state_db_path.exists():
            return []

        where = [
            "s.user_id = ?",
            "m.role = 'user'",
            "m.content IS NOT NULL",
            "TRIM(m.content) != ''",
        ]
        params: list[Any] = [self._user_id]
        if self._session_id:
            where.append("m.session_id != ?")
            params.append(self._session_id)
        params.append(_MAX_RECENT)

        sql = (
            "SELECT m.session_id, m.content, m.timestamp "
            "FROM messages m JOIN sessions s ON s.id = m.session_id "
            f"WHERE {' AND '.join(where)} "
            "ORDER BY m.timestamp DESC LIMIT ?"
        )

        with sqlite3.connect(str(self._state_db_path), timeout=5.0) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(sql, params).fetchall()
        return self._dedupe_rows(rows)

    @staticmethod
    def _dedupe_rows(rows: Iterable[sqlite3.Row]) -> List[Dict[str, str]]:
        seen: set[tuple[str, str]] = set()
        result: list[dict[str, str]] = []
        for row in rows:
            session_id = str(row["session_id"])
            content = _clip(str(row["content"] or ""))
            if not content:
                continue
            key = (session_id, content)
            if key in seen:
                continue
            seen.add(key)
            result.append({"session_id": session_id, "content": content})
        return result


def register(ctx) -> None:
    """Register LocalUserMemoryProvider as a memory provider plugin."""
    ctx.register_memory_provider(LocalUserMemoryProvider())
