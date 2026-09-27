"""测试夹具：与生产统一的 Exchange 接口，完全离线模拟交易所返回。"""

from dataclasses import replace
from time import monotonic

import fakeredis.aioredis
import pytest

from config import Settings
from coordination import Coordinator
from exchange.base import Exchange
from models import ZERO, Book, D, Fill, Instrument, Level, OrderStatus, Quote


class FakeExchange(Exchange):
    """脚本化成交结果，复用真实校验、模拟成交及持仓核对代码。"""

    def __init__(self, name, settings, bid="99", ask="100"):
        """建立一份可交易 BTC 规则和盘口，script 可注入超时或部分成交。"""
        self.name = name
        super().__init__(settings)
        self.instruments = {
            "BTC": Instrument(
                name, "BTC", "BTC", "USDT", qty_step=D("0.01"), min_qty=D("0.01"), tick=D("0.01")
            )
        }
        self.metadata_at = monotonic()
        self.books["BTC"] = Book((Level(D(bid), D("10")),), (Level(D(ask), D("10")),))
        self.quotes["BTC"] = Quote(D(bid), D(ask))
        self.script = []
        self.sent = []
        self.unknown = False

    async def fetch_instruments(self):
        """返回内存规则，测试不会访问网络。"""
        return list(self.instruments.values()), []

    async def connect(self, symbols):
        """测试已经提供就绪盘口，无需连接。"""
        self.symbols = symbols

    async def subscribe_depth(self, base, enabled=True):
        """测试仅维护订阅意图。"""
        if enabled:
            self.depth.add(base)
        else:
            self.depth.discard(base)

    async def submit(self, order):
        """支持模拟一条腿少成交、拒绝及未知结果；记录实际发送次数。"""
        self.validate_order(order)
        self.sent.append(order)
        if self.unknown:
            return Fill(order, OrderStatus.UNKNOWN, terminal=False)
        fraction = self.script.pop(0) if self.script else D("1")
        fill = (
            self.paper_fill(replace(order, quantity=order.quantity * fraction))
            if fraction
            else Fill(order, OrderStatus.REJECTED)
        )
        return replace(fill, order=order)

    async def place_live(self, order):
        """离线适配器禁止真实下单。"""
        raise AssertionError("No live network in tests")

    async def query_order(self, order):
        """故障注入时始终返回 UNKNOWN，验证执行器不能重复发单。"""
        return Fill(order, OrderStatus.UNKNOWN, terminal=False)

    async def live_position(self, base):
        """返回测试账户当前基础币净仓位。"""
        return self.paper_positions.get(base, ZERO)

    async def live_open_orders(self, base):
        """离线模拟中 IOC 均即时终止，没有遗留挂单。"""
        return False


@pytest.fixture
def settings():
    """返回更宽的测试规模上限，避免测试盘口单价触及示例默认上限。"""
    return Settings(
        max_notional=D("1000"),
        take_fraction=D("0.5"),
        funding_horizon_hours=D(0),
        spread_min_change_ratio=D(0),
        spread_window_seconds=0,
    )


@pytest.fixture
def venues(settings):
    """提供买低卖高的两交易所，报价差足以覆盖配置的所有费用。"""
    return {"left": FakeExchange("left", settings), "right": FakeExchange("right", settings, "103", "104")}


@pytest.fixture
async def coordinator():
    """使用支持 Lua 的 FakeRedis 执行与生产相同的原子协调脚本。"""
    instance = Coordinator("", "test", "paper", fakeredis.aioredis.FakeRedis(decode_responses=True))
    await instance.start()
    yield instance
    await instance.close()
