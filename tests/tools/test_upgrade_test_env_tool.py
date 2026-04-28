import json
from types import SimpleNamespace
from unittest.mock import MagicMock

from gateway.session_context import clear_session_vars, set_session_vars
from tools.protected_ops import (
    current_message_requests_test_env_upgrade,
    current_upgrade_request_is_authorized,
    protected_upgrade_terminal_block_message,
)
from tools import terminal_tool as terminal_tool_module
from tools.upgrade_test_env_tool import upgrade_test_env_client


def test_authorization_accepts_exact_trigger_after_invisible_char_normalization(monkeypatch):
    monkeypatch.setenv("TEST_ENV_UPGRADE_ALLOWED_USERS", "ou_allowed")

    tokens = set_session_vars(
        platform="feishu",
        user_id="ou_allowed",
        message_text="升\ue000级\ue000测\ue000试\ue000环\ue000境\ue000客\ue000户\ue000端",
    )
    try:
        authorized, reason = current_upgrade_request_is_authorized()
    finally:
        clear_session_vars(tokens)

    assert authorized is True
    assert reason == ""


def test_trigger_match_detects_exact_trigger_after_mention_hint(monkeypatch):
    monkeypatch.setenv("TEST_ENV_UPGRADE_ALLOWED_USERS", "ou_allowed")

    tokens = set_session_vars(
        platform="feishu",
        user_id="ou_allowed",
        message_text="[Mentioned: 佳爷飞书分爷]\n\n升级测试环境客户端",
    )
    try:
        matched = current_message_requests_test_env_upgrade()
    finally:
        clear_session_vars(tokens)

    assert matched is True


def test_authorization_rejects_unauthorized_user(monkeypatch):
    monkeypatch.setenv("TEST_ENV_UPGRADE_ALLOWED_USERS", "ou_boss")

    tokens = set_session_vars(
        platform="feishu",
        user_id="ou_other",
        message_text="升级测试环境客户端",
    )
    try:
        authorized, reason = current_upgrade_request_is_authorized()
    finally:
        clear_session_vars(tokens)

    assert authorized is False
    assert "ou_other" in reason


def test_protected_terminal_block_message_matches_sensitive_upgrade_path(monkeypatch):
    monkeypatch.setenv("TEST_ENV_UPGRADE_ALLOWED_USERS", "ou_boss")

    block = protected_upgrade_terminal_block_message(
        "ssh root@47.76.186.165 'bash -lc /opt/sh/upgrade_frontend_step1.sh'"
    )

    assert block is not None
    assert "upgrade_test_env_client" in block


def test_terminal_tool_blocks_direct_protected_upgrade_command(monkeypatch):
    monkeypatch.setenv("TEST_ENV_UPGRADE_ALLOWED_USERS", "ou_boss")
    monkeypatch.setattr(
        "tools.terminal_tool._get_env_config",
        lambda: {
            "env_type": "local",
            "timeout": 180,
            "cwd": "/tmp",
            "host_cwd": None,
            "modal_mode": "auto",
            "docker_image": "",
            "singularity_image": "",
            "modal_image": "",
            "daytona_image": "",
        },
    )
    monkeypatch.setattr("tools.terminal_tool._start_cleanup_thread", lambda: None)
    monkeypatch.setattr(
        "tools.terminal_tool._active_environments",
        {"default": MagicMock()},
    )
    monkeypatch.setattr("tools.terminal_tool._last_activity", {"default": 0})

    result = json.loads(
        terminal_tool_module.terminal_tool(
            "ssh root@47.76.186.165 'bash -lc /opt/sh/upgrade_frontend_step1.sh'"
        )
    )

    assert result["status"] == "blocked"
    assert "upgrade_test_env_client" in result["error"]


def test_upgrade_tool_denies_unauthorized_user(monkeypatch, tmp_path):
    password_file = tmp_path / "loginTestEnv.conf"
    password_file.write_text("secret\n", encoding="utf-8")

    monkeypatch.setenv("TEST_ENV_UPGRADE_ALLOWED_USERS", "ou_boss")
    monkeypatch.setenv("TEST_ENV_UPGRADE_PASSWORD_FILE", str(password_file))

    tokens = set_session_vars(
        platform="feishu",
        user_id="ou_other",
        message_text="升级测试环境客户端",
    )
    try:
        result = json.loads(upgrade_test_env_client())
    finally:
        clear_session_vars(tokens)

    assert result["success"] is False
    assert result["stage"] == "validation"
    assert "ou_other" in result["error"]


def test_upgrade_tool_runs_step1_then_step2_and_audits(monkeypatch, tmp_path):
    password_file = tmp_path / "loginTestEnv.conf"
    password_file.write_text("secret\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    monkeypatch.setenv("TEST_ENV_UPGRADE_ALLOWED_USERS", "ou_boss,ou_ops")
    monkeypatch.setenv("TEST_ENV_UPGRADE_PASSWORD_FILE", str(password_file))

    calls = []

    def fake_run(argv, capture_output, text, timeout, check):
        del capture_output, text, timeout, check
        calls.append(argv)
        remote_command = argv[-1]
        if "upgrade_frontend_step1.sh" in remote_command:
            return SimpleNamespace(returncode=0, stdout="step1 ok\n", stderr="")
        if "upgrade_frontend_step2.sh" in remote_command:
            return SimpleNamespace(returncode=0, stdout="step2 ok\n", stderr="")
        raise AssertionError(f"unexpected remote command: {remote_command}")

    monkeypatch.setattr("tools.upgrade_test_env_tool.subprocess.run", fake_run)
    monkeypatch.setattr(
        "tools.upgrade_test_env_tool.shutil.which",
        lambda name: f"/usr/bin/{name}",
    )

    tokens = set_session_vars(
        platform="feishu",
        user_id="ou_ops",
        user_name="ops",
        chat_id="oc_chat",
        thread_id="omt-1",
        message_text="升级测试环境客户端",
    )
    try:
        result = json.loads(upgrade_test_env_client())
    finally:
        clear_session_vars(tokens)

    assert result["success"] is True
    assert result["stage"] == "completed"
    assert len(calls) == 2
    assert result["step1"]["returncode"] == 0
    assert result["step2"]["returncode"] == 0

    audit_path = tmp_path / ".hermes" / "protected_ops_audit.jsonl"
    assert audit_path.exists()
    audit_lines = audit_path.read_text(encoding="utf-8").strip().splitlines()
    assert audit_lines
    last_entry = json.loads(audit_lines[-1])
    assert last_entry["status"] == "completed"
    assert last_entry["user_id"] == "ou_ops"
