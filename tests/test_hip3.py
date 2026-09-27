"""HIP-3 路由、资产编号、抵押币及统一执行流程的离线回归。"""

from dataclasses import replace
from time import monotonic
from unittest.mock import AsyncMock, Mock

import pytest

from cache import ContractCache
from exchange import create_exchange, exchange_pairs, resolve_exchanges
from exchange.base import ValidationError
from exchange.hyperliquid import HyperliquidExchange
from execution import ExecutionEngine
from models import Book, D, Level, Order, OrderStatus, Side
from strategy import Opportunity


def metadata_venue(settings, quote="USDC"):
    """模拟带空 DEX 槽位、已下架合约和非连续抵押币索引的官方元数据。"""
    venue = HyperliquidExchange(settings, dex="xyz")
    responses = {
        "meta": {
            "collateralToken": 42,
            "universe": [
                {"name": "xyz:OLD", "szDecimals": 2, "isDelisted": True},
                {"name": "xyz:BTC", "szDecimals": 3, "maxLeverage": 20, "marginMode": "noCross"},
            ],
        },
        "perpDexs": [None, None, {"name": "xyz"}],
        "spotMeta": {"tokens": [{"index": 0, "name": "OTHER"}, {"index": 42, "name": quote}]},
    }
    venue._http_info = AsyncMock(side_effect=lambda payload: responses[payload["type"]])
    return venue


async def test_metadata_namespace_asset_ids_and_collateral(settings):
    """编号保留所有原始槽位，未映射标的不自动与 Gate BTC 配对。"""
    venue = metadata_venue(settings)
    items, raw = await venue.fetch_instruments()
    assert not items[0].active
    btc = items[1]
    assert (btc.base, btc.native, btc.asset_id) == ("XYZ:BTC", "xyz:BTC", 120001)
    assert (btc.quote, btc.collateral_token, btc.max_leverage, btc.margin_mode) == ("USDC", 42, 20, "noCross")
    assert raw["dex_index"] == 2
    assert venue._http_info.call_args_list[0].args[0] == {"type": "meta", "dex": "xyz"}


async def test_explicit_alias_fx_and_dex_fee(settings):
    """显式别名和抵押币汇率使 HIP-3 进入现有统一数量/价格模型。"""
    settings = replace(
        settings,
        aliases={"hyperliquid:xyz:BTC": ["BTC", "1000"]},
        quote_usd_rates={"USDH": "0.99"},
        hl_dex_fees={"xyz": "0.0007"},
    )
    items, _ = await metadata_venue(settings, "USDH").fetch_instruments()
    item = items[1]
    assert item.active and item.base == "BTC"
    assert item.price(D(100), settings.fx(item.quote)) == D("0.099")
    assert item.quantity(D(2)) == 2000
    assert item.taker_fee == D("0.0007")


async def test_unknown_collateral_disabled_and_identity_change_rejected(settings):
    """缺少汇率只缓存不交易；刷新发现编号变化则清除旧行情并终止。"""
    venue = metadata_venue(settings, "USDH")
    items, _ = await venue.fetch_instruments()
    assert all(not item.active for item in items)
    venue.instruments = {items[1].base: replace(items[1], asset_id=110001)}
    venue.books[items[1].base] = object()
    with pytest.raises(RuntimeError, match="identity changed"):
        await venue.fetch_instruments()
    assert not venue.books


async def test_native_metadata_keeps_original_ids(settings):
    """原生永续保持无 dex 的接口、原始 asset ID 和 USDC 估值。"""
    venue = HyperliquidExchange(settings)
    venue._http_info = AsyncMock(return_value={"universe": [{"name": "BTC", "szDecimals": 3}]})
    items, _ = await venue.fetch_instruments()
    assert (items[0].base, items[0].asset_id, items[0].quote) == ("BTC", 0, "USDC")
    venue._http_info.assert_awaited_once_with({"type": "meta"})


async def ready_venue(settings):
    """构造已映射 BTC 的 HIP-3 实例，供 WSS 和统一执行测试使用。"""
    venue = metadata_venue(replace(settings, aliases={"hyperliquid:xyz:BTC": ["BTC", "1"]}))
    items, _ = await venue.fetch_instruments()
    item = items[1]
    venue.instruments = {"BTC": item}
    venue.by_native = {item.native: item}
    venue.symbols = venue.depth = {"BTC"}
    venue.metadata_at = monotonic()
    venue.books["BTC"] = Book((Level(D(103), D(10)),), (Level(D(104), D(10)),))
    venue.transport = Mock(send=AsyncMock())
    return venue


async def test_wss_routing_and_namespace_isolation(settings):
    """轻行情带 dex，深度带完整 coin；其他 DEX 的同名币不能更新本地状态。"""
    venue = await ready_venue(settings)
    await venue._connected()
    subscriptions = [call.args[0]["subscription"] for call in venue.transport.send.call_args_list]
    assert subscriptions == [{"type": "allMids", "dex": "xyz"}, {"type": "l2Book", "coin": "xyz:BTC"}]
    await venue.handle_message({"channel": "allMids", "data": {"mids": {"BTC": "1", "io:BTC": "2"}}})
    assert not venue.quotes
    await venue.handle_message({"channel": "allMids", "data": {"mids": {"xyz:BTC": "103"}}})
    assert venue.quotes["BTC"].bid == 103
    await venue.handle_message({"channel": "l2Book", "data": {"coin": "io:BTC", "levels": [[], []]}})
    assert venue.books["BTC"].bids[0].price == 103


async def test_position_and_open_orders_scoped_to_dex(settings):
    """账户查询明确指定 DEX，避免查原生账户后误报 HIP-3 空仓。"""
    venue = await ready_venue(settings)
    venue._post = AsyncMock(
        side_effect=[
            {"assetPositions": [{"position": {"coin": "xyz:BTC", "szi": "-2"}}]},
            [{"coin": "xyz:BTC"}],
        ]
    )
    assert await venue.live_position("BTC") == -2
    assert await venue.live_open_orders("BTC")
    assert [c.args[1] for c in venue._post.call_args_list] == [
        {"type": "clearinghouseState", "user": settings.hl_account, "dex": "xyz"},
        {"type": "openOrders", "user": settings.hl_account, "dex": "xyz"},
    ]


async def test_live_fee_validation_and_wire_asset(settings):
    """缺少专属费率拒绝实盘，配置后下单使用 HIP-3 asset ID。"""
    venue = await ready_venue(replace(settings, mode="live"))
    order = Order(venue.name, "BTC", Side.SELL, D(1), D(103))
    with pytest.raises(ValidationError, match="HL_DEX_FEES"):
        venue.validate_order(order)
    venue.settings = replace(venue.settings, hl_dex_fees={"xyz": "0.001"})
    venue.validate_order(order)
    assert venue.order_payload(order)["a"] == 120001
    assert venue.rate_limit_key == HyperliquidExchange(settings).rate_limit_key


@pytest.mark.parametrize("fee_token, expected", [("USDH", D("0.049")), ("UNKNOWN", None)])
async def test_fill_history_fee_currency(settings, fee_token, expected):
    """成交费用按 feeToken 折算；未配置币种不能伪造已完成对账。"""
    venue = await ready_venue(replace(settings, quote_usd_rates={"USDH": "0.98"}))
    venue._post = AsyncMock(
        side_effect=[
            {"status": "order", "order": {"status": "filled", "order": {"oid": 9, "timestamp": 1000}}},
            [
                {
                    "coin": "xyz:BTC",
                    "oid": 9,
                    "tid": 1,
                    "sz": "1",
                    "px": "103",
                    "fee": "0.05",
                    "feeToken": fee_token,
                }
            ],
        ]
    )
    result = await venue.query_order(Order(venue.name, "BTC", Side.SELL, D(1), D(103)))
    if expected is None:
        assert result.status == OrderStatus.UNKNOWN and not result.terminal
    else:
        assert result.fee == expected and result.terminal
    assert all("dex" not in c.args[1] for c in venue._post.call_args_list)


async def test_market_expansion_and_cross_exchange_pairs(settings, monkeypatch):
    """固定/自动发现市场均去重，原生与 HIP-3 不创建同所套利进程。"""
    settings = replace(settings, hl_dexs=("xyz", "io"), exchanges=("gate", "hyperliquid", "hyperliquid:xyz"))
    expanded = await resolve_exchanges(settings)
    assert expanded.exchanges == ("gate", "hyperliquid", "hyperliquid:xyz", "hyperliquid:io")
    assert exchange_pairs(expanded.exchanges) == tuple(("gate", name) for name in expanded.exchanges[1:])
    monkeypatch.setattr(HyperliquidExchange, "discover_dexs", AsyncMock(return_value={"xyz": 1, "io": 10}))
    assert (await resolve_exchanges(replace(settings, hl_dexs=("*",)))).exchanges == expanded.exchanges
    assert create_exchange("hyperliquid:io", settings).dex == "io"
    with pytest.raises(ValueError):
        await resolve_exchanges(replace(settings, hl_dexs=("../bad",)))


async def test_cache_keeps_each_market_separate(tmp_path, settings):
    """原生与部署方缓存互不覆盖，并持久化原生 coin 和资产编号。"""
    venue = await ready_venue(settings)
    cache = ContractCache(tmp_path)
    await cache.save("hyperliquid", [], {})
    await cache.save(venue.name, list(venue.instruments.values()), {})
    assert {p.name for p in tmp_path.iterdir()} == {"hyperliquid.json", "hyperliquid__xyz.json"}
    assert "xyz:BTC" in (tmp_path / "hyperliquid__xyz.json").read_text()


async def test_hip3_paper_execution_ownership_and_close(settings, venues, coordinator):
    """真实 HIP-3 适配器在统一执行器模拟开平仓，基础币归属阻止其他组合重复操作。"""
    hip = await ready_venue(settings)
    left = venues["left"]
    engine = ExecutionEngine(settings, {"left": left, hip.name: hip}, coordinator, "left-xyz")
    opportunity = Opportunity(
        Order("left", "BTC", Side.BUY, D(1), D(100)), Order(hip.name, "BTC", Side.SELL, D(1), D(103)), D(100)
    )
    position = await engine.open(opportunity)
    assert position.state == "open" and position.short_exchange == "hyperliquid:xyz"
    assert not await coordinator.claim("BTC", "gate-io", 3)
    await engine.close_position(position)
    assert position.state == "closed" and hip.paper_positions["BTC"] == 0
    assert not (await coordinator.snapshot())["owners"]


async def test_no_common_market_closes_cleanly(settings, monkeypatch):
    """没有显式别名的 HIP-3 组合跳过，退出时仍关闭连接。"""
    from engine import PairWorker

    worker = PairWorker(settings, ("gate", "hyperliquid:xyz"))
    worker.coordinator = Mock(start=AsyncMock(), close=AsyncMock())
    for name, venue in worker.exchanges.items():
        venue.instruments = {"BTC" if name == "gate" else "XYZ:BTC": object()}
        venue.initialize = AsyncMock()
        venue.connect = AsyncMock()
        venue.close = AsyncMock()
    await worker.run()
    assert worker.settings.exchanges == ("gate", "hyperliquid:xyz")
    for venue in worker.exchanges.values():
        venue.connect.assert_not_awaited()
        venue.close.assert_awaited_once()
    worker.coordinator.close.assert_awaited_once()


def test_hip3_configuration_loading(tmp_path, monkeypatch):
    """新增 JSON 配置按 Decimal 使用；空 DEX 列表明确关闭扩展市场。"""
    from config import load_settings

    monkeypatch.setenv("MODE", "observe")
    monkeypatch.setenv("HL_DEXS", "xyz,io")
    monkeypatch.setenv("HL_DEX_FEES", '{"xyz":"0.0009"}')
    monkeypatch.setenv("QUOTE_USD_RATES", '{"USDH":"0.999"}')
    settings = load_settings(tmp_path / "missing.env")
    assert settings.hl_dexs == ("xyz", "io")
    assert settings.fx("USDH") == D("0.999")
    monkeypatch.setenv("HL_DEXS", "")
    assert load_settings(tmp_path / "missing.env").hl_dexs == ()
    monkeypatch.setenv("QUOTE_USD_RATES", '{"USDH":"0"}')
    with pytest.raises(ValueError, match="QUOTE_USD_RATES"):
        load_settings(tmp_path / "missing.env")
