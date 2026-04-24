"""Tests for Feishu doc/drive tools."""

import importlib
import json
import os
from unittest.mock import patch

from model_tools import get_tool_definitions
from tools.registry import registry

feishu_doc_tool = importlib.import_module("tools.feishu_doc_tool")
importlib.import_module("tools.feishu_drive_tool")


class FakeResponse:
    def __init__(self, *, code=0, msg="ok", raw_content=None, data=None):
        self.code = code
        self.msg = msg
        self.raw = type("Raw", (), {"content": raw_content})() if raw_content is not None else None
        self.data = data


class FakeClient:
    def __init__(self, response):
        self._response = response

    def request(self, request):
        return self._response


class TestFeishuToolRegistration:
    EXPECTED_TOOLS = {
        "feishu_doc_read": "feishu_doc",
        "feishu_drive_list_comments": "feishu_drive",
        "feishu_drive_list_comment_replies": "feishu_drive",
        "feishu_drive_reply_comment": "feishu_drive",
        "feishu_drive_add_comment": "feishu_drive",
    }

    def test_all_tools_registered(self):
        for tool_name, toolset in self.EXPECTED_TOOLS.items():
            entry = registry.get_entry(tool_name)
            assert entry is not None, f"{tool_name} not registered"
            assert entry.toolset == toolset

    def test_doc_read_schema_supports_url_and_doc_token(self):
        entry = registry.get_entry("feishu_doc_read")
        props = entry.schema["parameters"].get("properties", {})
        assert "url" in props
        assert "doc_token" in props
        assert "anyOf" in entry.schema["parameters"]

    def test_drive_tools_require_file_token(self):
        for tool_name in self.EXPECTED_TOOLS:
            if tool_name == "feishu_doc_read":
                continue
            entry = registry.get_entry(tool_name)
            props = entry.schema["parameters"].get("properties", {})
            assert "file_token" in props
            assert "file_type" in props


class TestFeishuDocTool:
    def teardown_method(self):
        feishu_doc_tool.set_client(None)

    def test_generic_url_reader_resolves_wiki_and_fetches_docx(self):
        calls = []

        def fake_http_request(**kwargs):
            calls.append((kwargs["method"], kwargs["url"]))
            if kwargs["url"].endswith("/tenant_access_token/internal"):
                return {"tenant_access_token": "tenant-token", "expire": 7200}
            if kwargs["url"].endswith("/wiki/v2/spaces/get_node"):
                return {
                    "code": 0,
                    "data": {
                        "node": {
                            "obj_type": "docx",
                            "obj_token": "docx-token-1",
                            "title": "Wiki Title",
                        }
                    },
                }
            if kwargs["url"].endswith("/docx/v1/documents/docx-token-1/raw_content"):
                return {
                    "code": 0,
                    "data": {
                        "title": "Doc Title",
                        "content": "Hello from Feishu",
                    },
                }
            raise AssertionError(f"unexpected call: {kwargs}")

        with (
            patch.dict(
                os.environ,
                {
                    "FEISHU_APP_ID": "app-id",
                    "FEISHU_APP_SECRET": "app-secret",
                },
                clear=False,
            ),
            patch.object(feishu_doc_tool, "_TOKEN_CACHE", {}),
            patch.object(feishu_doc_tool, "_http_request", side_effect=fake_http_request),
        ):
            result = json.loads(
                feishu_doc_tool.feishu_doc_read_tool(
                    url="https://example.feishu.cn/wiki/wiki-token-1"
                )
            )

        assert result["success"] is True
        assert result["source_type"] == "wiki"
        assert result["resolved_type"] == "docx"
        assert result["token"] == "docx-token-1"
        assert result["content"] == "Hello from Feishu"
        assert any(url.endswith("/tenant_access_token/internal") for _, url in calls)
        assert any(url.endswith("/wiki/v2/spaces/get_node") for _, url in calls)

    def test_comment_context_can_read_by_doc_token_without_env_auth(self):
        raw_content = json.dumps(
            {
                "data": {
                    "title": "Comment Doc",
                    "content": "Nearby context from comment workflow",
                }
            }
        )
        feishu_doc_tool.set_client(FakeClient(FakeResponse(raw_content=raw_content)))

        with patch.dict(os.environ, {}, clear=True):
            result = json.loads(
                feishu_doc_tool.feishu_doc_read_tool(doc_token="docx-token-2")
            )

        assert result["success"] is True
        assert result["resolved_type"] == "docx"
        assert result["content"] == "Nearby context from comment workflow"

    def test_tool_exposed_in_web_toolset_when_available(self):
        with patch.object(feishu_doc_tool, "check_feishu_doc_requirements", return_value=True):
            entry = registry.get_entry("feishu_doc_read")
            original = entry.check_fn
            entry.check_fn = feishu_doc_tool.check_feishu_doc_requirements
            try:
                tools = get_tool_definitions(enabled_toolsets=["web"], quiet_mode=True)
            finally:
                entry.check_fn = original

        tool_names = [tool["function"]["name"] for tool in tools]
        assert "feishu_doc_read" in tool_names
