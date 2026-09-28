"""资金费预算与历史价差过滤的确定性测试，不访问网络、不等待真实采样周期。"""

from dataclasses import replace
from time import monotonic, time

import pytest

from exchange.gate import GateExchange
from exchange.hyperliquid import HyperliquidExchange
from execution import ExecutionEngine
from market_analysis import funding_cost_per_unit
from models import D, Funding, Instrument
from strategy import ArbitrageStrategy


def set_funding(venues, now, left="0.001", right="0.0001"):
    """多头 8 小时一次、空头每小时一次，标记价与成交报价故意不同。"""
    a, b = venues.values()
    a.funding["BTC"] = Funding(D(left), D(100), 28800, now + 3600)
    b.funding["BTC"] = Funding(D(right), D(200), 3600, now + 3600)


def test_funding_counts_schedule_and_sign(venues, settings):
    """8 小时内多头付一次、空头收八次；反向持仓成本准确反号。"""
    now = time()
    set_funding(venues, now)
    settings = replace(settings, funding_horizon_hours=D(8))
    a, b = venues.values()
    assert funding_cost_per_unit("BTC", a, b, settings, now) == D("-0.06")
    assert funding_cost_per_unit("BTC", b, a, settings, now) == D("0.06")
    set_funding(venues, now, "-0.001", "-0.0001")
    assert funding_cost_per_unit("BTC", a, b, settings, now) == D("0.06")


def test_funding_boundary_and_fractional_horizon(venues, settings):
    """窗口外结算不计入，恰好位于窗口末尾的结算计入；旧边界不重复扣费。"""
    now = time()
    set_funding(venues, now)
    a, b = venues.values()
    assert funding_cost_per_unit("BTC", a, b, replace(settings, funding_horizon_hours=D("0.5")), now) == 0
    settings = replace(settings, funding_horizon_hours=D(1))
    assert funding_cost_per_unit("BTC", a, b, settings, now) == D("0.08")
    a.funding["BTC"] = replace(a.funding["BTC"], next_settlement=now)
    assert funding_cost_per_unit("BTC", a, b, settings, now) == D("-0.02")


@pytest.mark.parametrize("bad", ["missing", "stale", "nan", "schedule"])
def test_unknown_funding_blocks(venues, settings, bad):
    """未知费率、过期费率、非法数字和缺失结算周期不能当成零成本。"""
    now = time()
    set_funding(venues, now)
    a, b = venues.values()
    if bad == "missing":
        a.funding.clear()
    elif bad == "stale":
        a.funding["BTC"] = replace(a.funding["BTC"], received=monotonic() - 61)
    elif bad == "nan":
        a.funding["BTC"] = replace(a.funding["BTC"], rate=D("NaN"))
    else:
        a.funding["BTC"] = replace(a.funding["BTC"], interval_seconds=0)
    assert funding_cost_per_unit("BTC", a, b, replace(settings, funding_horizon_hours=D(8)), now) is None


def test_funding_rejects_raw_profitable_opportunity(venues, settings):
    """原始价差合格但预计资金费吞噬利润时拒绝；较低资金费可通过。"""
    a, b = venues.values()
    settings = replace(settings, funding_horizon_hours=D(8))
    set_funding(venues, time(), "0.1", "0")
    strategy = ArbitrageStrategy(settings)
    assert strategy.opportunity("BTC", a, b) is None
    set_funding(venues, time(), "0.001", "0")
    opportunity = strategy.opportunity("BTC", a, b)
    assert opportunity and opportunity.funding_cost_usd > 0


async def test_presubmit_rechecks_changed_funding(venues, settings, coordinator):
    """信号形成后资金费改变，下单前再算并拒绝，不能只检查深度价差。"""
    settings = replace(settings, funding_horizon_hours=D(8))
    a, b = venues.values()
    set_funding(venues, time(), "0.001", "0")
    strategy = ArbitrageStrategy(settings)
    opportunity = strategy.opportunity("BTC", a, b)
    assert opportunity
    set_funding(venues, time(), "0.1", "0")
    engine = ExecutionEngine(settings, venues, coordinator, "owner")
    assert await engine.open(opportunity) is None
    assert a.sent == b.sent == []
    assert not (await coordinator.snapshot())["owners"]


async def test_wss_funding_parsing_and_disconnect(settings):
    """Gate 接收单次费率与元数据日程；HIP-3 使用完整 coin，断线清空资金费。"""
    now = time()
    gate = GateExchange(settings)
    item = Instrument(
        "gate", "HOOD_USDT", "HOOD", "USDT", funding_interval=14400, funding_next_apply=now + 100
    )
    gate.by_native = {item.native: item}
    await gate.handle_message(
        {
            "channel": "futures.tickers",
            "event": "update",
            "result": [{"contract": "HOOD_USDT", "mark_price": "100", "funding_rate": "0", "t": now * 1000}],
        }
    )
    assert gate.funding["HOOD"].rate == 0 and gate.funding["HOOD"].interval_seconds == 14400
    hl = HyperliquidExchange(replace(settings, usdc_usd=D("0.99")), dex="xyz")
    item = Instrument(hl.name, "xyz:HOOD", "HOOD", "USDC")
    hl.by_native = {item.native: item}
    hl.depth = {"HOOD"}
    await hl.handle_message(
        {
            "channel": "activeAssetCtx",
            "data": {"coin": "xyz:HOOD", "ctx": {"markPx": "100", "funding": "-0.001"}},
        }
    )
    assert hl.funding["HOOD"].mark_price == 99 and hl.funding["HOOD"].interval_seconds == 3600
    await hl.handle_message(
        {"channel": "activeAssetCtx", "data": {"coin": "io:HOOD", "ctx": {"markPx": "1", "funding": "1"}}}
    )
    assert hl.funding["HOOD"].rate == D("-0.001")
    hl.invalidate()
    gate.invalidate()
    assert not hl.funding and not gate.funding


def test_stale_gate_schedule_blocks_even_with_fresh_rate(venues, settings):
    """WSS 费率再新也不能掩盖已过期的 Gate 结算日程。"""
    a, b = venues.values()
    set_funding(venues, time())
    a.name = "gate"
    a.metadata_at = monotonic() - 121
    assert funding_cost_per_unit("BTC", a, b, replace(settings, funding_horizon_hours=D(8))) is None


def test_relative_change_zero_crossing_and_denominator_floor():
    """正负价差不抵消分母，零附近不除零，常量价差变化率为零。"""
    from market_analysis import spread_change_ratio

    assert spread_change_ratio([D(85), D(95)]) == D(10) / D(90)
    assert spread_change_ratio([D(-10), D(10)]) == 2
    assert spread_change_ratio([D(0), D(0)]) == 0
    assert spread_change_ratio([D("-0.01"), D("0.01")]) == D("0.02")
