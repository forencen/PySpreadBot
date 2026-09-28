"""统一交易所行为；新交易所只需实现协议方法，不修改策略或执行器。"""

import asyncio
from abc import ABC, abstractmethod
from dataclasses import replace
from decimal import ROUND_CEILING, ROUND_FLOOR
from time import monotonic, time

import aiohttp

from cache import ContractCache
from config import Settings
from models import ZERO, Book, D, Fill, Instrument, Order, OrderStatus, Quote, Side


class ValidationError(ValueError):
    """订单未发送前的校验失败，可以安全取消整组交易。"""


class Exchange(ABC):
    """价格、数量均向上层呈现统一单位；协议签名及原始单位转换封装于子类。"""

    name: str

    @property
    def rate_limit_key(self) -> str:
        """返回共享订单限额桶；同一交易所的多个市场可覆盖此属性共用额度。"""
        return self.name

    def __init__(self, settings: Settings, coordinator=None):
        """创建内存状态；每个工作进程独立连接，Redis 协调跨进程状态。"""
        self.settings, self.coordinator = settings, coordinator
        self.instruments: dict[str, Instrument] = {}
        self.by_native: dict[str, Instrument] = {}
        self.quotes: dict[str, Quote] = {}
        self.funding = {}
        self.candles = {}
        self.candle_generation = 0
        self.books: dict[str, Book] = {}
        self.depth: set[str] = set()
        self.symbols: set[str] = set()
        self.changed = asyncio.Event()
        self.session = None
        self.transport = None
        self.metadata_at = 0.0
        self.cache = ContractCache(settings.cache_dir)
        self.paper_positions: dict[str, D] = {}
        self.paper_fills: dict[str, Fill] = {}

    async def initialize(self) -> None:
        """建立异步 HTTP 会话并获取最新规则；元数据可用前不能建立可交易状态。"""
        self.session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=self.settings.request_timeout), trust_env=False
        )
        await self.refresh_instruments()

    async def refresh_instruments(self) -> None:
        """刷新规则并写本地缓存；同所同基础币出现歧义时拒绝任意选择。"""
        instruments, raw = await self.fetch_instruments()
        active = [item for item in instruments if item.active]
        bases = [item.base for item in active]
        if len(bases) != len(set(bases)):
            raise ValueError(f"Ambiguous base symbols on {self.name}; configure ALIASES")
        await self.cache.save(self.name, instruments, raw)
        self.instruments = {item.base: item for item in active}
        self.by_native = {item.native: item for item in active}
        self.metadata_at = monotonic()

    @abstractmethod
    async def fetch_instruments(self) -> tuple[list[Instrument], object]:
        """通过低频元数据 API 获取全部受支持合约，包括最小数量、精度及限制。"""
        raise NotImplementedError

    async def fetch_candles(self, base: str, start: int, end: int):
        """返回 [start, end) 内 15 分钟 K 线；由交易所实现历史接口，默认不支持。"""
        raise NotImplementedError("15m candles unavailable on this exchange")

    async def load_candles(self, base: str) -> None:
        """低频补取最近已收盘 K 线；替换有界内存缓存，不写文件或数据库。

        时间边界在请求前固定，因此跨越下一根 K 线时不会误把未完成数据计入。
        断线或规则身份变化期间的迟到请求不会恢复已失效缓存。
        """
        end = int(time()) // 900 * 900
        start = end - self.settings.candle_lookback_bars * 900
        generation, instrument = self.candle_generation, self.instruments[base]
        rows = await self.fetch_candles(base, start, end)
        if generation != self.candle_generation or self.instruments.get(base) != instrument:
            return
        self.candles[base] = {
            row.start: row for row in rows if row.valid() and start <= row.start and row.start + 900 <= end
        }
        self.changed.set()

    @abstractmethod
    async def connect(self, symbols: set[str]) -> None:
        """通过 WSS 订阅轻量行情，并准备交易请求通道。"""
        raise NotImplementedError

    @abstractmethod
    async def subscribe_depth(self, base: str, enabled: bool = True) -> None:
        """按需订阅或退订深度，断线后必须重新取得快照。"""
        raise NotImplementedError

    @abstractmethod
    async def place_live(self, order: Order) -> Fill:
        """通过 WSS 发送已校验 IOC 订单；不确定响应返回 UNKNOWN。"""
        raise NotImplementedError

    @abstractmethod
    async def query_order(self, order: Order) -> Fill:
        """按固定客户端 ID 通过 WSS 查单，禁止将查不到等同于未成交。"""
        raise NotImplementedError

    @abstractmethod
    async def live_position(self, base: str) -> D:
        """查询实际有符号仓位；低频对账可在交易所不支持时使用 REST。"""
        raise NotImplementedError

    @abstractmethod
    async def live_open_orders(self, base: str) -> bool:
        """通过 WSS 检查未终态委托，避免空仓但有遗留挂单的账户被误认为可开仓。"""
        raise NotImplementedError

    async def has_open_orders(self, base: str) -> bool:
        """模拟 IOC 不保留挂单；实盘需确认指定标的没有其他来源的活动订单。"""
        return await self.live_open_orders(base) if self.settings.mode == "live" else False

    def invalidate(self) -> None:
        """断线立即使全部本地行情失效，阻止使用重连前的残留盘口。"""
        self.candle_generation += 1
        self.candles.clear()
        self.funding.clear()
        self.books.clear()
        self.quotes.clear()
        self.changed.set()

    def native_price(self, order: Order) -> D:
        """把 USD/基础币限价转回原始报价币/合约单位。"""
        instrument = self.instruments[order.base]
        return order.limit_price * instrument.unit / self.settings.fx(instrument.quote)

    def price_tick(self, instrument: Instrument, native_price: D) -> D:
        """返回原生价格步长；动态有效位限制由 Hyperliquid 覆盖。"""
        return instrument.tick

    def round_limit(self, base: str, side: Side, price: D) -> D:
        """向价格保护范围内取整：买价向下、卖价向上，不能扩大容许滑点。"""
        instrument = self.instruments[base]
        fx = self.settings.fx(instrument.quote)
        native = price * instrument.unit / fx
        step = self.price_tick(instrument, native)
        rounding = ROUND_FLOOR if side == Side.BUY else ROUND_CEILING
        return instrument.price((native / step).to_integral_value(rounding=rounding) * step, fx)

    def validate_order(self, order: Order) -> None:
        """校验单位、精度、数量、最小金额及规则新鲜度；失败在发送前抛出。"""
        instrument = self.instruments.get(order.base)
        if instrument is None or not instrument.active or order.exchange != self.name:
            raise ValidationError("Unknown, inactive or mismatched instrument")
        if monotonic() - self.metadata_at > self.settings.metadata_seconds * 2:
            raise ValidationError("Stale instrument rules")
        if not all(value.is_finite() and value > 0 for value in (order.quantity, order.limit_price)):
            raise ValidationError("Quantity and price must be finite and positive")
        book = self.books.get(order.base)
        if book is None or not book.fresh(self.settings.max_age):
            raise ValidationError("No fresh executable book")
        qty, price = instrument.native_quantity(order.quantity), self.native_price(order)
        if qty % instrument.qty_step or not instrument.min_qty <= qty <= instrument.max_qty:
            raise ValidationError("Quantity violates min/max/step")
        if price % self.price_tick(instrument, price):
            raise ValidationError("Price violates exchange precision")
        if (
            not order.reduce_only
            and order.quantity * order.limit_price / self.settings.fx(instrument.quote)
            < instrument.min_notional
        ):
            raise ValidationError("Below exchange minimum notional")
        if not order.reduce_only and order.quantity * order.limit_price > self.settings.max_notional:
            raise ValidationError("Above configured per-leg notional cap")

    async def submit(self, order: Order) -> Fill:
        """统一下单入口：强制校验后才调用交易所，paper 不执行任何私有网络请求。"""
        self.validate_order(order)
        if self.settings.mode == "observe":
            raise ValidationError("Observe mode never submits orders")
        if self.settings.mode == "paper":
            return self.paper_fill(order)
        if self.coordinator is None:
            raise ValidationError("Live orders require global coordination")
        if not await self.coordinator.order_budget(self.rate_limit_key, self.settings.orders_per_minute):
            return Fill(order, OrderStatus.REJECTED, reason="Global rate budget exhausted")
        try:
            return await self.place_live(order)
        except Exception as error:
            return Fill(order, OrderStatus.UNKNOWN, terminal=False, reason=type(error).__name__)

    def paper_fill(self, order: Order) -> Fill:
        """按可见真实深度模拟 IOC；受限价和 reduce-only 约束，不能模拟超过盘口的成交。"""
        if order.client_id in self.paper_fills:
            return self.paper_fills[order.client_id]
        book = self.books.get(order.base)
        if book is None or not book.fresh(self.settings.max_age):
            return Fill(order, OrderStatus.REJECTED, reason="No fresh book")
        remaining, value, filled = order.quantity, ZERO, ZERO
        position = self.paper_positions.get(order.base, ZERO)
        if order.reduce_only:
            reducible = max(position if order.side == Side.SELL else -position, ZERO)
            remaining = min(remaining, reducible)
        for level in book.asks if order.side == Side.BUY else book.bids:
            if (order.side == Side.BUY and level.price > order.limit_price) or (
                order.side == Side.SELL and level.price < order.limit_price
            ):
                break
            take = min(remaining, level.quantity)
            filled += take
            value += take * level.price
            remaining -= take
            if remaining == 0:
                break
        status = (
            OrderStatus.FILLED
            if filled == order.quantity
            else OrderStatus.PARTIAL
            if filled
            else OrderStatus.CANCELED
        )
        self.paper_positions[order.base] = position + (filled if order.side == Side.BUY else -filled)
        result = Fill(
            order,
            status,
            filled,
            value / filled if filled else ZERO,
            value * self.instruments[order.base].taker_fee,
        )
        self.paper_fills[order.client_id] = result
        return result

    async def resolve(self, fill: Fill) -> Fill:
        """查证非终态订单；三次有限查单后仍不明确则保留 UNKNOWN，由执行器冻结标的。"""
        if fill.terminal and fill.status != OrderStatus.UNKNOWN:
            return fill
        for _ in range(3):
            try:
                result = await self.query_order(fill.order)
                if result.terminal and result.status != OrderStatus.UNKNOWN:
                    return result
            except Exception:
                pass
            await asyncio.sleep(0.2)
        return replace(fill, status=OrderStatus.UNKNOWN, terminal=False)

    async def position(self, base: str) -> D:
        """统一查询实际仓位，模拟状态与真实账户严格隔离。"""
        if self.settings.mode != "live":
            return self.paper_positions.get(base, ZERO)
        return await self.live_position(base)

    async def close(self) -> None:
        """释放进程拥有的网络资源，不在清理函数中擅自平仓或释放持仓锁。"""
        if self.transport:
            await self.transport.close()
        if self.session:
            await self.session.close()
