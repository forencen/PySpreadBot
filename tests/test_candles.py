"""15 分钟 K 线归一化、时间对齐、内存生命周期及入场检查。"""

import asyncio
from dataclasses import replace
from time import time
from unittest.mock import AsyncMock, Mock

import pytest

from config import load_settings
from engine import PairWorker
from exchange.gate import GateExchange
from exchange.hyperliquid import HyperliquidExchange
from execution import ExecutionEngine
from market_analysis import candle_spread_stats
from models import Candle, D, Instrument
from strategy import ArbitrageStrategy


def bar(stamp, close):
    """生成有效的平价 OHLC，输入价格保持 Decimal 精度。"""
    price = D(str(close))
    return Candle(stamp, price, price, price, price)


def history(venues, spreads, end):
    """两边同一收盘桶设置指定 bps 差，使用 USD/基础币规范价格。"""
    a, b = venues.values()
    a.candles["BTC"], b.candles["BTC"] = {}, {}
    for index, spread in enumerate(spreads):
        stamp = end - (len(spreads) - index) * 900
        a.candles["BTC"][stamp] = bar(stamp, 100)
        b.candles["BTC"][stamp] = bar(stamp, D(100) / (1 + D(spread) / 10000))


@pytest.mark.parametrize(
    "spreads,allowed", [([95, 100, 95, 100], False), ([85, 95, 85, 95], True), ([100] * 4, False)]
)
def test_aligned_candles_filter_spread_variation(venues, settings, spreads, allowed):
    """沿用绝对/相对变化门槛，但依据已收盘 K 线，不采集每秒价格。"""
    end = int(time()) // 900 * 900
    settings = replace(
        settings,
        candle_lookback_bars=4,
        candle_min_bars=4,
        spread_min_change_ratio=D("0.10"),
        spread_min_range_bps=D("5.01"),
    )
    history(venues, spreads, end)
    a, b = venues.values()
    strategy = ArbitrageStrategy(settings)
    assert (strategy.opportunity("BTC", a, b) is not None) is allowed
    stats = candle_spread_stats("BTC", a, b, settings)
    assert stats["samples"] == 4 and stats["last_at"] == end - 900


@pytest.mark.parametrize("problem", ["forming", "gap", "latest", "invalid", "misaligned"])
def test_missing_stale_and_forming_candles_block(venues, settings, problem):
    """只比较已收盘且连续对齐的 K 线，不能拿当前桶或旧桶补齐缺口。"""
    end = int(time()) // 900 * 900
    settings = replace(settings, candle_lookback_bars=8, candle_min_bars=4)
    history(venues, [85, 95, 85, 95], end)
    a, b = venues.values()
    if problem == "forming":
        a.candles["BTC"].pop(end - 900)
        a.candles["BTC"][end] = bar(end, 100)
        b.candles["BTC"][end] = bar(end, 200)
    elif problem == "gap":
        a.candles["BTC"].pop(end - 1800)
        a.candles["BTC"][end - 4500] = bar(end - 4500, 100)
        b.candles["BTC"][end - 4500] = bar(end - 4500, 100)
    elif problem == "latest":
        end += 900
    elif problem == "invalid":
        a.candles["BTC"][end - 900] = bar(end - 900, "NaN")
    else:
        a.candles["BTC"][end - 900] = bar(end - 899, 100)
    assert candle_spread_stats("BTC", a, b, settings, now=end + 10) is None


async def test_gate_history_proxy_unit_conversion(settings):
    """Gate 历史使用 HTTP 代理，USDT 汇率和千倍单位影响所有 OHLC。"""
    settings = replace(settings, http_proxy="http://localhost:7890", usdt_usd=D("0.99"))
    venue = GateExchange(settings)
    venue.instruments["PEPE"] = Instrument("gate", "1000PEPE_USDT", "PEPE", "USDT", unit=D(1000))
    response = Mock(
        raise_for_status=Mock(),
        json=AsyncMock(return_value=[{"t": 1800, "o": "1", "h": "2", "l": "0.5", "c": "1.5"}]),
    )
    context = AsyncMock()
    context.__aenter__.return_value = response
    venue.session = Mock(get=Mock(return_value=context))
    rows = await venue.fetch_candles("PEPE", 1800, 2700)
    assert rows[0].close == D("0.001485")
    assert venue.session.get.call_args.kwargs["params"] == {
        "contract": "1000PEPE_USDT",
        "interval": "15m",
        "from": 1800,
        "to": 2699,
    }
    assert venue.session.get.call_args.kwargs["proxy"] == settings.http_proxy


async def test_hip3_history_wss_namespace_and_milliseconds(settings):
    """HIP-3 历史 WSS 使用完整 coin，拒绝其他 DEX 和其他周期返回。"""
    venue = HyperliquidExchange(settings, dex="xyz")
    venue.instruments["HOOD"] = Instrument(venue.name, "xyz:HOOD", "HOOD", "USDC")
    row = {"s": "xyz:HOOD", "i": "15m", "t": 1800000, "o": "100", "h": "101", "l": "99", "c": "100"}
    venue._post = AsyncMock(return_value=[row, {**row, "s": "io:HOOD"}, {**row, "i": "1m"}])
    result = await venue.fetch_candles("HOOD", 1800, 2700)
    assert len(result) == 1 and result[0].start == 1800
    assert venue._post.call_args.args[1]["req"] == {
        "coin": "xyz:HOOD",
        "interval": "15m",
        "startTime": 1800000,
        "endTime": 2699999,
    }


async def test_cache_bounds_and_late_response_after_disconnect(settings):
    """缓存只保留已收盘有效区间；断线期间返回的旧请求不能恢复缓存。"""
    venue = HyperliquidExchange(replace(settings, candle_lookback_bars=4))
    venue.instruments["BTC"] = Instrument(venue.name, "BTC", "BTC", "USDC")
    end = int(time()) // 900 * 900
    venue.fetch_candles = AsyncMock(return_value=[bar(end - 900, 100), bar(end, 101), bar(end - 4500, 90)])
    await venue.load_candles("BTC")
    assert list(venue.candles["BTC"]) == [end - 900]

    async def disconnected(*args):
        """模拟请求在途时发生 WSS 断线。"""
        venue.invalidate()
        return [bar(end - 900, 100)]

    venue.fetch_candles = AsyncMock(side_effect=disconnected)
    await venue.load_candles("BTC")
    assert not venue.candles


async def test_presubmit_rechecks_candle_filter(venues, settings, coordinator):
    """账户核对之后 K 线不足或变成固定价差，两腿均不能发送。"""
    settings = replace(settings, exchanges=("left", "right"), candle_lookback_bars=4, candle_min_bars=4)
    end = int(time()) // 900 * 900
    history(venues, [85, 95, 85, 95], end)
    strategy = ArbitrageStrategy(settings)
    a, b = venues.values()
    opportunity = strategy.opportunity("BTC", a, b)
    assert opportunity
    history(venues, [100] * 4, end)
    assert await ExecutionEngine(settings, venues, coordinator, "owner", strategy).open(opportunity) is None
    assert not a.sent and not b.sent


async def test_background_history_load_deduplicates_and_retries(settings):
    """候选标的后台补历史不阻塞循环，同标的只建一项任务，错误会退避。"""
    worker = PairWorker(replace(settings, candle_lookback_bars=4, candle_min_bars=4), ("gate", "hyperliquid"))
    entered = asyncio.Event()
    release = asyncio.Event()

    async def waiting(base):
        """模拟在途请求直到测试允许完成。"""
        entered.set()
        await release.wait()

    for venue in worker.exchanges.values():
        venue.load_candles = AsyncMock(side_effect=waiting)
    worker._ensure_candles("BTC")
    await entered.wait()
    task = worker.candle_tasks["BTC"]
    worker._ensure_candles("BTC")
    assert worker.candle_tasks["BTC"] is task
    release.set()
    await task
    worker._ensure_candles("BTC")
    assert not worker.candle_tasks
    await worker.coordinator.close()


def test_candle_config_validation(monkeypatch, tmp_path):
    """历史窗口能容纳最小根数，0 可显式关闭过滤。"""
    monkeypatch.setenv("CANDLE_LOOKBACK_BARS", "8")
    monkeypatch.setenv("CANDLE_MIN_BARS", "4")
    assert load_settings(tmp_path / "none").candle_lookback_bars == 8
    monkeypatch.setenv("CANDLE_MIN_BARS", "9")
    with pytest.raises(ValueError, match="CANDLE_MIN_BARS"):
        load_settings(tmp_path / "none")
    monkeypatch.setenv("CANDLE_LOOKBACK_BARS", "0")
    assert load_settings(tmp_path / "none").candle_lookback_bars == 0


@pytest.mark.parametrize(
    "kind,payload,result",
    [
        ("info", {"type": "candleSnapshot"}, [{"t": 1800000}]),
        ("info", {"type": "clearinghouseState"}, {"assetPositions": []}),
        ("info", {"type": "openOrders"}, []),
    ],
)
async def test_hyperliquid_wss_info_envelope(settings, kind, payload, result):
    """官方 WSS info 多一层 type/data，K 线和已有账户查询都需解包。"""
    venue = HyperliquidExchange(settings)
    venue.transport = Mock(
        request=AsyncMock(return_value={"type": "info", "payload": {"type": payload["type"], "data": result}})
    )
    assert await venue._post(kind, payload) == result


async def test_hyperliquid_action_response_not_unwrapped(settings):
    """下单 action 的 status/response 结构原样返回，不被 info 解包误伤。"""
    venue = HyperliquidExchange(settings)
    result = {"status": "ok", "response": {"type": "order", "data": {"statuses": []}}}
    venue.transport = Mock(request=AsyncMock(return_value={"type": "action", "payload": result}))
    assert await venue._post("action", {"action": {"type": "order"}}) == result
