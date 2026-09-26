"""代理配置及 HTTP/WSS 分流测试，完全离线运行。"""

from dataclasses import replace
from unittest.mock import AsyncMock, Mock

import pytest
from pyspreadbot.config import Settings, load_settings, parse_proxy
from pyspreadbot.exchange.gate import GateExchange
from pyspreadbot.exchange.hyperliquid import HyperliquidExchange
from pyspreadbot.models import Instrument
from pyspreadbot.transport import WebSocketTransport


@pytest.mark.parametrize("value", ["", "None", "none", " NONE "])
def test_disabled_proxy(value):
    """空值和 None 必须转换为真正的 Python None。"""
    assert parse_proxy(value, "HTTP_PROXY") is None


def test_env_proxies_are_independent(monkeypatch, tmp_path):
    """HTTP 启用代理而 WSS 显式 None 时，WSS 必须直连。"""
    monkeypatch.delenv("HTTP_PROXY", raising=False)
    monkeypatch.delenv("WSS_PROXY", raising=False)
    path = tmp_path / "proxy.env"
    path.write_text("HTTP_PROXY=http://127.0.0.1:7890\nWSS_PROXY=None\n")
    settings = load_settings(path)
    assert settings.http_proxy == "http://127.0.0.1:7890"
    assert settings.wss_proxy is None
    # load_dotenv 修改进程环境，显式清理以免影响其他测试。
    monkeypatch.delenv("HTTP_PROXY", raising=False)
    monkeypatch.delenv("WSS_PROXY", raising=False)


@pytest.mark.parametrize("value", ["socks5://localhost:1080", "localhost:7890", "http://host:bad"])
def test_invalid_proxy(value):
    """不支持或无效的协议应在启动时失败。"""
    with pytest.raises(ValueError):
        parse_proxy(value, "WSS_PROXY")


def response_context(payload):
    """模拟 aiohttp 请求上下文，便于检查公共和私有 HTTP 的代理传递。"""
    response = Mock(raise_for_status=Mock(), json=AsyncMock(return_value=payload))
    context = AsyncMock()
    context.__aenter__.return_value = response
    return context


@pytest.mark.parametrize("proxy", [None, "http://127.0.0.1:7890"])
async def test_http_routes(proxy):
    """两个所公共请求及 Gate 私有仓位请求都应使用 HTTP_PROXY。"""
    settings = Settings(http_proxy=proxy, wss_proxy="http://127.0.0.1:7891")
    gate = GateExchange(settings)
    gate.session = Mock(get=Mock(return_value=response_context([])))
    await gate.fetch_instruments()
    assert gate.session.get.call_args.kwargs["proxy"] == proxy
    gate.instruments["BTC"] = Instrument("gate", "BTC_USDT", "BTC", "USDT")
    gate.session.get.return_value = response_context({"size": "0", "mode": "single"})
    await gate.live_position("BTC")
    assert gate.session.get.call_args.kwargs["proxy"] == proxy
    hl = HyperliquidExchange(settings)
    hl.session = Mock(post=Mock(return_value=response_context({"universe": []})))
    await hl.fetch_instruments()
    assert hl.session.post.call_args.kwargs["proxy"] == proxy


@pytest.mark.parametrize("implementation", [GateExchange, HyperliquidExchange])
async def test_adapter_wss_proxy(implementation, monkeypatch):
    """适配器必须把独立 WSS 配置传给共用传输层。"""
    monkeypatch.setattr(WebSocketTransport, "start", AsyncMock())
    settings = Settings(http_proxy="http://http-proxy:7890", wss_proxy="http://ws-proxy:7891")
    venue = implementation(settings)
    await venue.connect(set())
    assert venue.transport.proxy == settings.wss_proxy


@pytest.mark.parametrize("proxy", [None, "http://127.0.0.1:7891"])
async def test_ws_connect_receives_proxy(proxy):
    """连接循环每次建立 WSS 都明确传递 proxy，包括 None 直连。"""
    ws = AsyncMock()
    ws.__aiter__.return_value = iter([])
    context = AsyncMock()
    context.__aenter__.return_value = ws
    session = Mock(ws_connect=Mock(return_value=context))
    transport = WebSocketTransport(
        session, "wss://example.test/ws", AsyncMock(), AsyncMock(), Mock(), 1, proxy=proxy
    )

    async def connected():
        """限制测试只连接一次，不进入生产重连等待。"""
        transport.stopping = True

    transport.connected = connected
    await transport._run()
    assert session.ws_connect.call_args.kwargs["proxy"] == proxy


def test_proxy_credentials_not_in_repr():
    """代理 URL 可能包含密码，因此不得被配置 repr 打印。"""
    settings = replace(Settings(), http_proxy="http://user:secret@localhost:7890")
    assert "secret" not in repr(settings)
