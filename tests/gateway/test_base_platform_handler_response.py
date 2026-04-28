import asyncio
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
)
from gateway.session import SessionSource, build_session_key


class _StubAdapter(BasePlatformAdapter):
    async def connect(self) -> bool:
        return True

    async def disconnect(self) -> None:
        return None

    async def send(self, chat_id, content, reply_to=None, metadata=None) -> SendResult:
        return SendResult(success=True)

    async def get_chat_info(self, chat_id):
        return {}


def _make_adapter() -> _StubAdapter:
    adapter = _StubAdapter(PlatformConfig(enabled=True, token="t"), Platform.TELEGRAM)
    adapter._send_with_retry = AsyncMock(return_value=SendResult(success=True))
    return adapter


def _make_event(text: str = "hi", chat_id: str = "42") -> MessageEvent:
    return MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=SessionSource(platform=Platform.TELEGRAM, chat_id=chat_id, chat_type="dm"),
    )


def _session_key(chat_id: str = "42") -> str:
    return build_session_key(
        SessionSource(platform=Platform.TELEGRAM, chat_id=chat_id, chat_type="dm")
    )


async def _wait_until_idle(adapter: _StubAdapter, chat_id: str = "42") -> None:
    session_key = _session_key(chat_id)
    for _ in range(100):
        if session_key not in adapter._active_sessions and not adapter._background_tasks:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("adapter did not become idle")


@pytest.mark.asyncio
async def test_background_handler_dict_response_is_sent_as_text():
    adapter = _make_adapter()

    async def handler(_event):
        return {"final_response": "Protected upgrade finished"}

    adapter.set_message_handler(handler)

    await adapter.handle_message(_make_event())
    await _wait_until_idle(adapter)

    adapter._send_with_retry.assert_awaited_once()
    assert adapter._send_with_retry.await_args.kwargs["content"] == "Protected upgrade finished"


@pytest.mark.asyncio
async def test_background_handler_skips_dict_response_when_already_sent():
    adapter = _make_adapter()

    async def handler(_event):
        return {"final_response": "streamed already", "already_sent": True}

    adapter.set_message_handler(handler)

    await adapter.handle_message(_make_event())
    await _wait_until_idle(adapter)

    adapter._send_with_retry.assert_not_awaited()
