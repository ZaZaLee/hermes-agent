import json
import sys
import types
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from gateway.config import Platform
from gateway.session_context import clear_session_vars, set_session_vars


@pytest.fixture(autouse=True)
def _mock_dotenv(monkeypatch):
    fake = types.ModuleType("dotenv")
    fake.load_dotenv = lambda *a, **kw: None
    monkeypatch.setitem(sys.modules, "dotenv", fake)


def _make_runner():
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.session_store = MagicMock()
    return runner


@pytest.mark.asyncio
async def test_gateway_short_circuits_protected_upgrade_trigger(monkeypatch):
    from gateway.run import GatewayRunner

    runner = _make_runner()
    runner.session_store.append_to_transcript = MagicMock()

    source = SimpleNamespace(platform=Platform.FEISHU)
    session_entry = SimpleNamespace(session_id="sess-1")
    history = []

    monkeypatch.setenv("TEST_ENV_UPGRADE_ALLOWED_USERS", "ou_boss")
    tokens = set_session_vars(
        platform="feishu",
        user_id="ou_boss",
        message_text="升级测试环境客户端",
    )
    try:
        with patch(
            "gateway.run.upgrade_test_env_client",
            return_value=json.dumps(
                {
                    "success": True,
                    "summary": "Protected test-environment client upgrade completed successfully.",
                    "step1": {"returncode": 0, "stdout": "step1 ok", "stderr": ""},
                    "step2": {"returncode": 0, "stdout": "step2 ok", "stderr": ""},
                }
            ),
        ):
            result = await GatewayRunner._maybe_handle_protected_upgrade_trigger(
                runner,
                source=source,
                session_entry=session_entry,
                history=history,
                message_text="升级测试环境客户端",
            )
    finally:
        clear_session_vars(tokens)

    assert result is not None
    assert "completed successfully" in result["final_response"]
    assert result["api_calls"] == 0
    assert runner.session_store.append_to_transcript.call_count == 0


@pytest.mark.asyncio
async def test_gateway_skips_short_circuit_for_non_trigger(monkeypatch):
    from gateway.run import GatewayRunner

    runner = _make_runner()
    source = SimpleNamespace(platform=Platform.FEISHU)
    session_entry = SimpleNamespace(session_id="sess-2")

    monkeypatch.setenv("TEST_ENV_UPGRADE_ALLOWED_USERS", "ou_boss")
    tokens = set_session_vars(
        platform="feishu",
        user_id="ou_boss",
        message_text="帮我升级测试环境客户端",
    )
    try:
        result = await GatewayRunner._maybe_handle_protected_upgrade_trigger(
            runner,
            source=source,
            session_entry=session_entry,
            history=[],
            message_text="帮我升级测试环境客户端",
        )
    finally:
        clear_session_vars(tokens)

    assert result is None
