"""基于官方消息形状的适配器回归测试。"""

from unittest.mock import AsyncMock, Mock

from pyspreadbot.exchange.gate import GateExchange
from pyspreadbot.exchange.hyperliquid import HyperliquidExchange
from pyspreadbot.models import D, Instrument, Order, OrderStatus, Side


def gate(settings):
    """创建 Gate 规则、模拟发送器及可订阅标的，避免真实鉴权。"""
    venue = GateExchange(settings)
    item = Instrument("gate", "BTC_USDT", "BTC", "USDT", contract_size=D("0.001"))
    venue.instruments = {"BTC": item}
    venue.by_native = {"BTC_USDT": item}
    venue.depth = {"BTC"}
    venue.transport = Mock(send=AsyncMock(), resolve=Mock())
    return venue


async def test_gate_snapshot_delta_and_gap(settings):
    """完整快照后按序更新；旧包忽略，跳号立即使盘口失效并重订阅。"""
    venue = gate(settings)
    await venue.handle_message(
        {
            "channel": "futures.obu",
            "result": {
                "s": "ob.BTC_USDT.400",
                "u": 10,
                "full": True,
                "b": [["99", "2000"]],
                "a": [["100", "1000"]],
            },
        }
    )
    assert venue.books["BTC"].bids[0].quantity == D(2)
    await venue.handle_message(
        {
            "channel": "futures.obu",
            "result": {"s": "ob.BTC_USDT.400", "U": 11, "u": 12, "b": [["99", "0"], ["98", "1000"]]},
        }
    )
    assert venue.books["BTC"].bids[0].price == D(98)
    await venue.handle_message(
        {"channel": "futures.obu", "result": {"s": "ob.BTC_USDT.400", "U": 15, "u": 16}}
    )
    assert "BTC" not in venue.books
    assert venue.transport.send.await_count == 2


async def test_gate_ack_is_not_a_fill(settings):
    """Gate ACK 不能提前完成下单 Future。"""
    venue = gate(settings)
    await venue.handle_message({"request_id": "one", "ack": True})
    venue.transport.resolve.assert_not_called()
    await venue.handle_message({"request_id": "one", "ack": False, "data": {}})
    venue.transport.resolve.assert_called_once()


def test_gate_units_payload_and_partial_fill(settings):
    """张数、原始价格、客户端 ID 和部分成交数量转换准确。"""
    venue = gate(settings)
    order = Order("gate", "BTC", Side.SELL, D("0.01"), D(100))
    payload = venue.order_payload(order)
    assert payload["size"] == "-1E+1" or D(payload["size"]) == -10
    assert len(payload["text"]) == 28
    fill = venue.parse_fill(order, {"status": "finished", "size": "-10", "left": "-4", "fill_price": "101"})
    assert fill.quantity == D("0.006") and fill.status == OrderStatus.PARTIAL


async def test_hyperliquid_fx_and_multiplier(settings):
    """千倍币 USDC 报价统一换算到基础币 USD，wire 反向转换不丢精度。"""
    from dataclasses import replace

    venue = HyperliquidExchange(replace(settings, usdc_usd=D("0.98")))
    item = Instrument("hyperliquid", "kPEPE", "PEPE", "USDC", unit=D(1000), size_decimals=0)
    venue.instruments = {"PEPE": item}
    venue.by_native = {"kPEPE": item}
    venue.depth = {"PEPE"}
    await venue.handle_message(
        {
            "channel": "l2Book",
            "data": {"coin": "kPEPE", "levels": [[{"px": "0.01", "sz": "2"}], [{"px": "0.02", "sz": "3"}]]},
        }
    )
    assert venue.books["PEPE"].bids[0].quantity == 2000
    assert venue.books["PEPE"].bids[0].price == D("0.0000098")
    order = Order("hyperliquid", "PEPE", Side.BUY, D(3000), D("0.0000196"))
    payload = venue.order_payload(order)
    assert payload["p"] == "0.02" and payload["s"] == "3"
    assert payload["t"] == {"limit": {"tif": "Ioc"}}


async def test_hl_signature_matches_wire_payload(settings, coordinator):
    """使用公开的测试私钥离线恢复签名地址，确保签名与实际发送 action 完全一致。"""
    from eth_account import Account
    from hyperliquid.utils.signing import recover_agent_or_user_from_l1_action

    venue = HyperliquidExchange(settings, coordinator)
    venue.wallet = Account.from_key("0x" + "0" * 63 + "1")
    item = Instrument("hyperliquid", "BTC", "BTC", "USDC", qty_step=D("0.001"), min_qty=D("0.001"))
    venue.instruments = {"BTC": item}
    venue._post = AsyncMock(
        return_value={
            "status": "ok",
            "response": {"data": {"statuses": [{"filled": {"totalSz": "0.01", "avgPx": "100", "oid": 1}}]}},
        }
    )
    order = Order("hyperliquid", "BTC", Side.BUY, D("0.01"), D("100"))
    fill = await venue.place_live(order)
    kind, payload = venue._post.call_args.args
    assert kind == "action"
    recovered = recover_agent_or_user_from_l1_action(
        payload["action"], payload["signature"], None, payload["nonce"], None, True
    )
    assert recovered == venue.wallet.address
    assert fill.quantity == order.quantity and fill.terminal


async def test_hl_filled_query_uses_fill_history(settings):
    """订单终态里 sz 不一定为零，filled 数量必须与完整成交记录及请求量一致。"""
    venue = HyperliquidExchange(settings)
    venue.instruments = {"BTC": Instrument("hyperliquid", "BTC", "BTC", "USDC")}
    venue._post = AsyncMock(
        side_effect=[
            {
                "status": "order",
                "order": {
                    "status": "filled",
                    "order": {"oid": 9, "timestamp": 1000, "origSz": "1", "sz": "1"},
                },
            },
            [{"oid": 9, "tid": 1, "sz": "1", "px": "100", "fee": "0.05", "feeToken": "USDC"}],
        ]
    )
    result = await venue.query_order(Order("hyperliquid", "BTC", Side.BUY, D(1), D(100)))
    assert result.quantity == 1 and result.fee == D("0.05") and result.terminal
