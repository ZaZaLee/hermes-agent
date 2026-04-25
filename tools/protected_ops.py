"""Helpers for protected gateway-triggered operations.

High-risk actions should not rely on prompt-only memory. This module keeps the
authorization policy, exact-trigger matching, and terminal bypass guard in one
place so the dedicated tool and generic terminal path enforce the same rules.
"""

from __future__ import annotations

import json
import logging
import os
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from gateway.session_context import get_session_env
from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)

_FEISHU_MENTION_HINT_RE = re.compile(
    r"^\[Mentioned: .*?\](?:\s*\n\s*\n|\s+)?",
    re.DOTALL,
)

_DEFAULT_TEST_ENV_UPGRADE_TRIGGER = "升级测试环境客户端"
_DEFAULT_TEST_ENV_UPGRADE_HOST = "47.76.186.165"
_DEFAULT_TEST_ENV_UPGRADE_SSH_USER = "root"
_DEFAULT_TEST_ENV_UPGRADE_PASSWORD_FILE = "/opt/data/loginTestEnv.conf"
_DEFAULT_TEST_ENV_UPGRADE_STEP1 = "/opt/sh/upgrade_frontend_step1.sh"
_DEFAULT_TEST_ENV_UPGRADE_STEP2 = "/opt/sh/upgrade_frontend_step2.sh"
_DEFAULT_TEST_ENV_UPGRADE_TIMEOUT = 900
_DEFAULT_TEST_ENV_UPGRADE_PLATFORMS = frozenset({"feishu"})


@dataclass(frozen=True)
class TestEnvUpgradeConfig:
    trigger: str
    allowed_users: frozenset[str]
    allowed_platforms: frozenset[str]
    host: str
    ssh_user: str
    password_file: str
    step1: str
    step2: str
    timeout_seconds: int
    strip_invisible: bool = True
    collapse_whitespace: bool = True
    strip_feishu_mention_hint: bool = True


def _parse_csv_env(name: str) -> frozenset[str]:
    raw = os.getenv(name, "")
    return frozenset(
        item.strip()
        for item in raw.split(",")
        if item and item.strip()
    )


def _parse_bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    return default


def _parse_positive_int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = int(raw.strip())
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def normalize_trigger_text(
    text: str,
    *,
    strip_invisible: bool,
    collapse_whitespace: bool,
    strip_feishu_mention_hint: bool,
) -> str:
    """Normalize inbound message text before exact trigger matching."""
    normalized = str(text or "")
    if strip_feishu_mention_hint:
        normalized = _FEISHU_MENTION_HINT_RE.sub("", normalized, count=1)
    if strip_invisible:
        filtered: list[str] = []
        for char in normalized:
            category = unicodedata.category(char)
            if category in {"Cf", "Co", "Cs"}:
                continue
            if category == "Cc" and char not in "\r\n\t":
                continue
            filtered.append(char)
        normalized = "".join(filtered)
    if collapse_whitespace:
        normalized = re.sub(r"\s+", "", normalized)
    return normalized.strip()


def load_test_env_upgrade_config() -> TestEnvUpgradeConfig:
    allowed_platforms = _parse_csv_env("TEST_ENV_UPGRADE_ALLOWED_PLATFORMS")
    return TestEnvUpgradeConfig(
        trigger=os.getenv("TEST_ENV_UPGRADE_TRIGGER", _DEFAULT_TEST_ENV_UPGRADE_TRIGGER).strip()
        or _DEFAULT_TEST_ENV_UPGRADE_TRIGGER,
        allowed_users=_parse_csv_env("TEST_ENV_UPGRADE_ALLOWED_USERS"),
        allowed_platforms=allowed_platforms or _DEFAULT_TEST_ENV_UPGRADE_PLATFORMS,
        host=os.getenv("TEST_ENV_UPGRADE_HOST", _DEFAULT_TEST_ENV_UPGRADE_HOST).strip()
        or _DEFAULT_TEST_ENV_UPGRADE_HOST,
        ssh_user=os.getenv("TEST_ENV_UPGRADE_SSH_USER", _DEFAULT_TEST_ENV_UPGRADE_SSH_USER).strip()
        or _DEFAULT_TEST_ENV_UPGRADE_SSH_USER,
        password_file=os.getenv(
            "TEST_ENV_UPGRADE_PASSWORD_FILE",
            _DEFAULT_TEST_ENV_UPGRADE_PASSWORD_FILE,
        ).strip() or _DEFAULT_TEST_ENV_UPGRADE_PASSWORD_FILE,
        step1=os.getenv("TEST_ENV_UPGRADE_STEP1", _DEFAULT_TEST_ENV_UPGRADE_STEP1).strip()
        or _DEFAULT_TEST_ENV_UPGRADE_STEP1,
        step2=os.getenv("TEST_ENV_UPGRADE_STEP2", _DEFAULT_TEST_ENV_UPGRADE_STEP2).strip()
        or _DEFAULT_TEST_ENV_UPGRADE_STEP2,
        timeout_seconds=_parse_positive_int_env(
            "TEST_ENV_UPGRADE_TIMEOUT_SECONDS",
            _DEFAULT_TEST_ENV_UPGRADE_TIMEOUT,
        ),
        strip_invisible=_parse_bool_env("TEST_ENV_UPGRADE_STRIP_INVISIBLE", True),
        collapse_whitespace=_parse_bool_env("TEST_ENV_UPGRADE_COLLAPSE_WHITESPACE", True),
        strip_feishu_mention_hint=_parse_bool_env(
            "TEST_ENV_UPGRADE_STRIP_FEISHU_MENTION_HINT",
            True,
        ),
    )


def test_env_upgrade_is_configured(cfg: TestEnvUpgradeConfig | None = None) -> bool:
    cfg = cfg or load_test_env_upgrade_config()
    return bool(
        cfg.allowed_users
        and cfg.trigger
        and cfg.host
        and cfg.ssh_user
        and cfg.password_file
        and cfg.step1
        and cfg.step2
    )


def current_upgrade_request_is_authorized(
    cfg: TestEnvUpgradeConfig | None = None,
) -> tuple[bool, str]:
    cfg = cfg or load_test_env_upgrade_config()
    if not test_env_upgrade_is_configured(cfg):
        return (
            False,
            "Protected test-environment upgrade is not configured. "
            "Set TEST_ENV_UPGRADE_ALLOWED_USERS in ~/.hermes/.env.",
        )

    platform = get_session_env("HERMES_SESSION_PLATFORM", "").strip().lower()
    if not platform:
        return False, "This protected operation is only available from gateway sessions."
    if cfg.allowed_platforms and platform not in cfg.allowed_platforms:
        return (
            False,
            f"Platform '{platform}' is not allowed for the protected test-environment upgrade.",
        )

    user_id = get_session_env("HERMES_SESSION_USER_ID", "").strip()
    if not user_id:
        return False, "Protected operation denied: missing gateway user identity."
    if user_id not in cfg.allowed_users:
        return (
            False,
            f"Protected operation denied for user_id '{user_id}'.",
        )

    current_message = get_session_env("HERMES_SESSION_MESSAGE_TEXT", "")
    observed_text = normalize_trigger_text(
        current_message,
        strip_invisible=cfg.strip_invisible,
        collapse_whitespace=cfg.collapse_whitespace,
        strip_feishu_mention_hint=cfg.strip_feishu_mention_hint,
    )
    expected_text = normalize_trigger_text(
        cfg.trigger,
        strip_invisible=cfg.strip_invisible,
        collapse_whitespace=cfg.collapse_whitespace,
        strip_feishu_mention_hint=False,
    )
    if observed_text != expected_text:
        return (
            False,
            "Protected operation denied: inbound message did not exactly match "
            f"the configured trigger '{cfg.trigger}'.",
        )

    return True, ""


def protected_upgrade_terminal_block_message(
    command: str,
    cfg: TestEnvUpgradeConfig | None = None,
) -> str | None:
    """Reject raw terminal access to the protected upgrade path."""
    if not isinstance(command, str) or not command.strip():
        return None

    cfg = cfg or load_test_env_upgrade_config()
    if not test_env_upgrade_is_configured(cfg):
        return None
    normalized = unicodedata.normalize("NFKC", command).lower()
    protected_tokens = [
        cfg.host,
        cfg.step1,
        cfg.step2,
        cfg.password_file,
    ]
    for token in protected_tokens:
        token = (token or "").strip()
        if token and token.lower() in normalized:
            return (
                "This protected test-environment upgrade path cannot be run via "
                "the generic terminal tool. Use the dedicated "
                "`upgrade_test_env_client` tool instead."
            )
    return None


def append_protected_ops_audit(
    *,
    operation: str,
    status: str,
    details: dict,
) -> None:
    """Append a JSONL audit entry under HERMES_HOME for later traceability."""
    audit_path = get_hermes_home() / "protected_ops_audit.jsonl"
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "operation": operation,
        "status": status,
        "platform": get_session_env("HERMES_SESSION_PLATFORM", ""),
        "chat_id": get_session_env("HERMES_SESSION_CHAT_ID", ""),
        "thread_id": get_session_env("HERMES_SESSION_THREAD_ID", ""),
        "user_id": get_session_env("HERMES_SESSION_USER_ID", ""),
        "user_name": get_session_env("HERMES_SESSION_USER_NAME", ""),
        "message": get_session_env("HERMES_SESSION_MESSAGE_TEXT", ""),
        "details": details,
    }
    try:
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        with open(audit_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        logger.warning("Failed to write protected ops audit entry", exc_info=True)


def password_file_exists(cfg: TestEnvUpgradeConfig | None = None) -> tuple[bool, str]:
    cfg = cfg or load_test_env_upgrade_config()
    password_path = Path(cfg.password_file)
    if not password_path.exists():
        return False, f"Password file does not exist: {cfg.password_file}"
    if not password_path.is_file():
        return False, f"Password file path is not a regular file: {cfg.password_file}"
    return True, ""
