"""资金费预算与历史价差过滤的确定性测试，不访问网络、不等待真实采样周期。"""

from dataclasses import replace
from time import monotonic, time

import pytest

from config import Settings, load_settings
from exchange.gate import GateExchange
from exchange.hyperliquid import HyperliquidExchange
from execution import ExecutionEngine
from market_analysis import SpreadHistory, funding_cost_per_unit, reverse_premium
from models import Book, D, Funding, Instrument, Level, Quote
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


def test_time_weighted_samples_warmup_expiry_and_no_lookahead():
    """每桶一条、预热后生效；当前异常价不污染自身基准，长时间断流失效。"""
    history = SpreadHistory(
        Settings(spread_sample_seconds=10, spread_window_seconds=60, spread_min_samples=3)
    )
    key = ("gate", "hyperliquid:xyz", "HOOD")
    history.observe(key, D(100), 100)
    for _ in range(100):
        history.observe(key, D(999), 101)
    history.observe(key, D(100), 110)
    history.observe(key, D(100), 120)
    assert history.baseline(key, 120) is None
    record = history.observe(key, D(500), 130)
    assert record["samples"] == 3 and history.baseline(key, 130) == 100
    assert history.baseline(("gate", "hyperliquid:io", "HOOD"), 130) is None
    assert history.baseline(key, 170) is None
    history.observe(key, D(500), 300)
    assert history.baseline(key, 300) is None


def test_stationary_spread_rejected_and_deviation_admitted(venues, settings, monkeypatch):
    """长期固定价差即使高于 ENTRY_BPS 也不交易；偏离历史中枢足够大才通过。"""
    now = time()
    monkeypatch.setattr("market_analysis.time", lambda: now)
    settings = replace(settings, spread_sample_seconds=1, spread_window_seconds=60, spread_min_samples=3)
    strategy = ArbitrageStrategy(settings)
    a, b = venues.values()
    premium = strategy.premium("BTC", a, b)
    for stamp in (now - 4, now - 3, now - 2, now - 1):
        strategy.history.observe((a.name, b.name, "BTC"), premium, stamp)
    assert abs(premium) > settings.entry_bps
    assert not strategy.candidate("BTC", a, b)
    assert strategy.opportunity("BTC", a, b) is None
    b.books["BTC"] = Book((Level(D(108), D(10)),), (Level(D(109), D(10)),))
    b.quotes["BTC"] = Quote(D(108), D(109))
    assert strategy.candidate("BTC", a, b)
    assert strategy.opportunity("BTC", a, b) is not None
    assert reverse_premium(D(1000)) == -D(1000) / D("1.1")


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


async def test_spread_statistics_persist_bounded_and_separate(coordinator):
    """不同 DEX 的统计独立保存；窗口历史有界且 status 可读最新值。"""
    for value in range(4):
        await coordinator.save_spread("gate", "hyperliquid:xyz", "HOOD", {"spread_bps": value}, 2)
    await coordinator.save_spread("gate", "hyperliquid:io", "HOOD", {"spread_bps": 9}, 2)
    assert len((await coordinator.snapshot())["spreads"]) == 2
    assert await coordinator.redis.llen(coordinator.key("spread_history:gate|hyperliquid:xyz|HOOD")) == 2


def test_new_config_validation(monkeypatch, tmp_path):
    """资金费允许显式关闭，历史窗口必须容纳所需样本。"""
    monkeypatch.setenv("FUNDING_HORIZON_HOURS", "0.5")
    monkeypatch.setenv("SPREAD_WINDOW_SECONDS", "30")
    monkeypatch.setenv("SPREAD_SAMPLE_SECONDS", "10")
    monkeypatch.setenv("SPREAD_MIN_SAMPLES", "3")
    assert load_settings(tmp_path / "none").funding_horizon_hours == D("0.5")
    monkeypatch.setenv("SPREAD_MIN_SAMPLES", "4")
    with pytest.raises(ValueError, match="Spread window"):
        load_settings(tmp_path / "none")


def test_stale_gate_schedule_blocks_even_with_fresh_rate(venues, settings):
    """WSS 费率再新也不能掩盖已过期的 Gate 结算日程。"""
    a, b = venues.values()
    set_funding(venues, time())
    a.name = "gate"
    a.metadata_at = monotonic() - 121
    assert funding_cost_per_unit("BTC", a, b, replace(settings, funding_horizon_hours=D(8))) is None


async def test_presubmit_uses_same_history_and_cannot_bypass(venues, settings, coordinator, monkeypatch):
    """执行器必须使用已预热的同一策略历史；基准更新后原机会失效则两腿均不发送。"""
    now = time()
    monkeypatch.setattr("market_analysis.time", lambda: now)
    settings = replace(
        settings,
        exchanges=("left", "right"),
        spread_sample_seconds=1,
        spread_window_seconds=60,
        spread_min_samples=3,
    )
    a, b = venues.values()
    strategy = ArbitrageStrategy(settings)
    for stamp in (now - 4, now - 3, now - 2, now - 1):
        strategy.history.observe((a.name, b.name, "BTC"), D(0), stamp)
    opportunity = strategy.opportunity("BTC", a, b)
    assert opportunity is not None
    assert await ExecutionEngine(settings, venues, coordinator, "no-history").open(opportunity) is None
    premium = strategy.premium("BTC", a, b)
    strategy.history.rows.clear()
    strategy.history.pending.clear()
    for stamp in (now - 4, now - 3, now - 2, now - 1):
        strategy.history.observe((a.name, b.name, "BTC"), premium, stamp)
    assert (
        await ExecutionEngine(settings, venues, coordinator, "changed-history", strategy).open(opportunity)
        is None
    )
    assert not a.sent and not b.sent
