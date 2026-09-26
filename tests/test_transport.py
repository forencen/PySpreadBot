"""WSS 请求关联与超时语义，使用内存发送器而非真实网络。"""

import asyncio
from unittest.mock import AsyncMock, Mock

import pytest
from pyspreadbot.transport import Disconnected, WebSocketTransport


def transport():
    """构造已连接的测试传输，保留生产 Future 关联机制。"""
    instance = WebSocketTransport(None, "wss://test", AsyncMock(), AsyncMock(), Mock(), 0.01)
    instance.ws = Mock(closed=False, send_json=AsyncMock())
    return instance


async def test_rpc_out_of_order():
    """响应乱序到达也只能完成自己的 Future。"""
    instance = transport()
    a = asyncio.create_task(instance.request("a", {"id": "a"}))
    b = asyncio.create_task(instance.request("b", {"id": "b"}))
    await asyncio.sleep(0)
    instance.resolve("b", {"result": "B"})
    instance.resolve("a", {"result": "A"})
    assert await a == {"result": "A"}
    assert await b == {"result": "B"}
    assert instance.pending == {}


async def test_timeout_no_replay_and_late_reply():
    """超时不自动重发，下次请求不会误收到之前迟到的结果。"""
    instance = transport()
    with pytest.raises(TimeoutError):
        await instance.request("a", {"order": 1})
    instance.resolve("a", {"filled": True})
    assert instance.pending == {}
    instance.ws.send_json.assert_awaited_once()


async def test_disconnected_no_pending_leak():
    """尚未连接时发送明确失败，并清除注册过的等待者。"""
    instance = transport()
    instance.ws = None
    with pytest.raises(Disconnected):
        await instance.request("a", {})
    assert instance.pending == {}
