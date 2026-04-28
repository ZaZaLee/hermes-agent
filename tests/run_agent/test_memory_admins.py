"""Tests for gateway memory admin write restrictions."""

import json
from unittest.mock import MagicMock, patch

import run_agent
from run_agent import AIAgent


def _make_tool_defs(*names: str) -> list:
    return [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": f"{name} tool",
                "parameters": {"type": "object", "properties": {}},
            },
        }
        for name in names
    ]


def _make_agent(*, platform="feishu", user_id="ou_user", skip_memory=True) -> AIAgent:
    with (
        patch("run_agent.get_tool_definitions", return_value=_make_tool_defs("memory")),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        agent = AIAgent(
            api_key="test-key-1234567890",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=skip_memory,
            platform=platform,
            user_id=user_id,
        )
    agent.client = MagicMock()
    return agent


def test_memory_write_allowed_by_default_without_admin_envs(monkeypatch):
    monkeypatch.delenv("FEISHU_MEMORY_ADMIN_USERS", raising=False)
    monkeypatch.delenv("GATEWAY_MEMORY_ADMIN_USERS", raising=False)
    agent = _make_agent()
    assert agent._is_memory_write_authorized() is True


def test_non_admin_gateway_user_cannot_write_memory(monkeypatch):
    monkeypatch.setenv("FEISHU_MEMORY_ADMIN_USERS", "ou_admin")
    monkeypatch.delenv("GATEWAY_MEMORY_ADMIN_USERS", raising=False)
    agent = _make_agent(user_id="ou_guest")

    with patch("tools.memory_tool.memory_tool") as memory_tool:
        result = agent._invoke_tool(
            "memory",
            {"action": "add", "target": "user", "content": "Name: guest"},
            effective_task_id="task-1",
        )

    payload = json.loads(result)
    assert payload["success"] is False
    assert "restricted to configured admins" in payload["error"]
    memory_tool.assert_not_called()


def test_admin_gateway_user_can_write_memory(monkeypatch):
    monkeypatch.setenv("FEISHU_MEMORY_ADMIN_USERS", "ou_admin")
    monkeypatch.delenv("GATEWAY_MEMORY_ADMIN_USERS", raising=False)
    agent = _make_agent(user_id="ou_admin")

    with patch(
        "tools.memory_tool.memory_tool",
        return_value=json.dumps({"success": True, "target": "user"}),
    ) as memory_tool:
        result = agent._invoke_tool(
            "memory",
            {"action": "add", "target": "user", "content": "Name: admin"},
            effective_task_id="task-1",
        )

    payload = json.loads(result)
    assert payload["success"] is True
    memory_tool.assert_called_once()


def test_non_admin_gateway_user_cannot_call_external_memory_tools(monkeypatch):
    monkeypatch.setenv("FEISHU_MEMORY_ADMIN_USERS", "ou_admin")
    monkeypatch.delenv("GATEWAY_MEMORY_ADMIN_USERS", raising=False)
    agent = _make_agent(user_id="ou_guest")
    agent._memory_manager = MagicMock()
    agent._memory_manager.has_tool.return_value = True

    result = agent._invoke_tool(
        "honcho_profile",
        {"action": "read"},
        effective_task_id="task-1",
    )

    payload = json.loads(result)
    assert payload["success"] is False
    assert "restricted to configured admins" in payload["error"]
    agent._memory_manager.handle_tool_call.assert_not_called()


def test_non_admin_session_end_skips_provider_extraction(monkeypatch):
    monkeypatch.setenv("FEISHU_MEMORY_ADMIN_USERS", "ou_admin")
    monkeypatch.delenv("GATEWAY_MEMORY_ADMIN_USERS", raising=False)
    agent = _make_agent(user_id="ou_guest")
    agent._memory_manager = MagicMock()

    agent.commit_memory_session(messages=[{"role": "user", "content": "hello"}])
    agent.shutdown_memory_provider(messages=[{"role": "user", "content": "hello"}])

    agent._memory_manager.on_session_end.assert_not_called()
    agent._memory_manager.shutdown_all.assert_called_once()

