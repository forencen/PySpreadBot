"""单位、深度、精度和 PnL 的确定性测试。"""

from dataclasses import replace
from time import monotonic

import pytest
from pyspreadbot.config import Settings, load_settings
from pyspreadbot.exchange.base import ValidationError
from pyspreadbot.exchange.hyperliquid import HyperliquidExchange
from pyspreadbot.models import Book, D, Instrument, Level, Order, Position, Side
from pyspreadbot.normalization import normalize
from pyspreadbot.strategy import ArbitrageStrategy, common_step


def test_explicit_multiplier_and_numbered_asset():
    """千倍币映射和 1INCH 名称处理不能相互混淆。"""
    assert normalize("gate", "btc——usdt", "USDT", {}) == ("BTC", D(1))
    assert normalize("gate", "btcusdt", "USDT", {}) == ("BTC", D(1))
    assert normalize("gate", "1INCH_USDT", "USDT", {}) == ("1INCH", D(1))
    assert normalize("hyperliquid", "kPEPE", "USDC", {}) == ("PEPE", D(1000))
    assert normalize("gate", "1000PEPE_USDT", "USDT", {"gate:1000PEPE_USDT": ["PEPE", "1000"]}) == (
        "PEPE",
        D(1000),
    )


def test_contract_and_fx_roundtrip():
    """价格除千、数量乘千，并且张数乘数只影响数量。"""
    instrument = Instrument("gate", "1000PEPE_USDT", "PEPE", "USDT", unit=D(1000), contract_size=D("0.1"))
    assert instrument.quantity(D(20)) == D(2000)
    assert instrument.native_quantity(D(2000)) == D(20)
    assert instrument.price(D("0.02"), D("0.99")) == D("0.0000198")


def test_common_lot_is_lcm():
    """0.003 与 0.002 的可共同下单步长为 0.006，不能取 max。"""
    assert common_step(D("0.003"), D("0.002")) == D("0.006")


def test_depth_vwap_and_insufficient_liquidity():
    """深度需要逐档累计，不足时不外推最后一档价格。"""
    book = Book((Level(D(99), D(2)),), (Level(D(100), D(1)), Level(D(102), D(2))))
    assert book.sweep(Side.BUY, D(2)) == (D(202), D(102))
    assert book.sweep(Side.BUY, D(4)) is None
    assert not replace(book, received=monotonic() - 20).fresh(2)


def test_opportunity_fees_caps_and_stale(venues, settings):
    """信号要求共同数量、合规规模、往返费用和新鲜深度。"""
    left, right = venues.values()
    strategy = ArbitrageStrategy(settings)
    opportunity = strategy.opportunity("BTC", left, right)
    assert opportunity is not None
    assert opportunity.buy.quantity == opportunity.sell.quantity
    for order in (opportunity.buy, opportunity.sell):
        venues[order.exchange].validate_order(order)
        assert order.quantity * order.limit_price <= settings.max_notional
    expensive = replace(settings, gate_fee=D("0.1"))
    left.instruments["BTC"] = replace(left.instruments["BTC"], taker_fee=expensive.gate_fee)
    assert strategy.opportunity("BTC", left, right) is None
    right.books["BTC"] = replace(right.books["BTC"], received=monotonic() - 10)
    assert strategy.opportunity("BTC", left, right) is None


def test_hl_significant_figures(settings):
    """价格同时遵守有效数字、小数位数约束，并允许大整数。"""
    venue = HyperliquidExchange(settings)
    instrument = Instrument("hyperliquid", "BTC", "BTC", "USDC", size_decimals=5, tick=D("0.1"))
    assert venue.price_tick(instrument, D("12345.6")) == D(1)
    assert venue.price_tick(instrument, D("123456")) == D(1)
    assert venue.price_tick(instrument, D("1234.5")) == D("0.1")


def test_validation_rejects_bad_quantity(venues):
    """数量不符合步长时禁止发单。"""
    with pytest.raises(ValidationError):
        venues["left"].validate_order(Order("left", "BTC", Side.BUY, D("0.001"), D(100)))


def test_closing_pnl_uses_executable_sides(venues, settings):
    """平多看 bid、平空看 ask，不能使用中间价制造虚假利润。"""
    position = Position("BTC", "owner", "left", "right", D(1), D(1), D(3))
    pnl = ArbitrageStrategy(settings).closing_pnl(position, venues)
    assert pnl == D(3) + D(99) - D(104) - (D(99) + D(104)) * D("0.0005")


def test_config_rejects_invalid_fx(monkeypatch, tmp_path):
    """零折算率不能默默生成零价格或除零错误。"""
    monkeypatch.setenv("USDC_USD", "0")
    with pytest.raises(ValueError):
        load_settings(tmp_path / "absent.env")


def test_secrets_excluded_from_repr():
    """异常日志打印配置时不能泄露密钥。"""
    assert "supersecret" not in repr(Settings(gate_key="supersecret", hl_key="supersecret"))


def test_source_timestamp_stale_even_if_just_received():
    """刚收到的旧快照不能绕过新鲜度校验。"""
    from time import time

    book = Book((Level(D(99), D(1)),), (Level(D(100), D(1)),), exchange_time=time() - 60)
    assert not book.fresh(2)
