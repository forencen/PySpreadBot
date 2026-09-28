"""交易所无关的数据契约；所有金额使用 Decimal，数量统一为基础币数量。"""

from dataclasses import dataclass, field
from decimal import ROUND_DOWN, Decimal
from enum import StrEnum
from time import monotonic, time
from uuid import uuid4

D = Decimal
ZERO = D("0")
BPS = D("10000")


class Side(StrEnum):
    """统一方向；BUY 增加基础币敞口，SELL 减少基础币敞口。"""

    BUY = "buy"
    SELL = "sell"

    @property
    def opposite(self) -> "Side":
        """返回反向交易方向，用于仅减仓补偿和平仓。"""
        return Side.SELL if self == Side.BUY else Side.BUY


class OrderStatus(StrEnum):
    """UNKNOWN 不能视为失败，必须查单后才能判断是否可以重新交易。"""

    FILLED = "filled"
    PARTIAL = "partial"
    CANCELED = "canceled"
    REJECTED = "rejected"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Instrument:
    """合约规格：unit 为一个交易所价格单位对应币数，contract_size 为一张对应单位数。"""

    exchange: str
    native: str
    base: str
    quote: str
    unit: Decimal = D("1")
    contract_size: Decimal = D("1")
    qty_step: Decimal = D("1")  # 交易所原始数量步长（Gate 张数、HL 币数）
    min_qty: Decimal = D("1")
    max_qty: Decimal = D("1e18")
    tick: Decimal = D("0.01")
    min_notional: Decimal = ZERO  # 原始结算币计价
    taker_fee: Decimal = D("0.0005")
    active: bool = True
    asset_id: int = 0
    size_decimals: int = 0
    price_deviation: Decimal = ZERO
    dex: str = ""
    collateral_token: int | None = None
    max_leverage: int | None = None
    margin_mode: str = ""
    funding_interval: int = 0
    funding_next_apply: float = 0

    @property
    def symbol(self) -> str:
        """返回规范名称，保留实际报价币；跨报价币配对使用 base。"""
        return f"{self.base}-{self.quote}"

    @property
    def base_per_qty(self) -> Decimal:
        """返回一个原始下单数量对应的基础币数量，避免千倍币和张数混淆。"""
        return self.unit * self.contract_size

    @property
    def base_step(self) -> Decimal:
        """返回基础币计量的最小数量步长，供双腿共同步长计算使用。"""
        return self.base_per_qty * self.qty_step

    def price(self, native_price: Decimal, fx: Decimal) -> Decimal:
        """把交易所原始价格换算成每个基础币的统一 USD 估值。"""
        return native_price * fx / self.unit

    def quantity(self, native_qty: Decimal) -> Decimal:
        """将原始数量换算成真实基础币数量。"""
        return native_qty * self.base_per_qty

    def native_quantity(self, base_qty: Decimal) -> Decimal:
        """转换数量，不自动取整；不合规数量由下单校验拒绝。"""
        return base_qty / self.base_per_qty


@dataclass(frozen=True)
class Level:
    """单档价格及基础币数量；price 已折算到统一 USD 估值。"""

    price: Decimal
    quantity: Decimal


@dataclass(frozen=True)
class Candle:
    """统一 USD/基础币 OHLC；start 是 UTC 15 分钟桶起点的 Unix 秒。"""

    start: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal

    def valid(self) -> bool:
        """拒绝错位时间、非有限/非正价格和内部不一致的 OHLC。"""
        return (
            self.start >= 0
            and self.start % 900 == 0
            and all(v.is_finite() and v > 0 for v in (self.open, self.high, self.low, self.close))
            and self.low <= min(self.open, self.close) <= max(self.open, self.close) <= self.high
        )


@dataclass(frozen=True)
class Funding:
    """单次结算费率、USD/基础币标记价和结算日程；正费率代表多头支付。"""

    rate: Decimal
    mark_price: Decimal
    interval_seconds: int
    next_settlement: float
    received: float = field(default_factory=monotonic)
    exchange_time: float = field(default_factory=time)

    def fresh(self, max_age: float) -> bool:
        """拒绝非有限费率/价格及迟到快照，零费率仍是有效数据。"""
        return (
            self.rate.is_finite()
            and self.mark_price.is_finite()
            and self.mark_price > 0
            and 0 <= monotonic() - self.received <= max_age
            and -1 <= time() - self.exchange_time <= max_age
        )


@dataclass(frozen=True)
class Quote:
    """轻量价格快照；bid/ask 可相同（中间价），仅用于候选筛选。"""

    bid: Decimal
    ask: Decimal
    received: float = field(default_factory=monotonic)
    exchange_time: float = field(default_factory=time)


@dataclass(frozen=True)
class Book:
    """可成交深度快照；买盘降序、卖盘升序，时间使用本机单调时钟。"""

    bids: tuple[Level, ...]
    asks: tuple[Level, ...]
    received: float = field(default_factory=monotonic)
    exchange_time: float = field(default_factory=time)

    def fresh(self, max_age: float) -> bool:
        """检查数据年龄及双边盘口，禁止陈旧、空盘或交叉盘口参与决策。"""
        return (
            0 <= monotonic() - self.received <= max_age
            and -1 <= time() - self.exchange_time <= max_age
            and bool(self.bids and self.asks)
            and self.bids[0].price < self.asks[0].price
        )

    def sweep(self, side: Side, quantity: Decimal) -> tuple[Decimal, Decimal] | None:
        """逐档吃深度，返回总金额和最差成交价；深度不足返回 None，不外推成交。"""
        remaining, value, worst = quantity, ZERO, ZERO
        for level in self.asks if side == Side.BUY else self.bids:
            take = min(remaining, level.quantity)
            value += take * level.price
            remaining -= take
            worst = level.price
            if remaining == 0:
                return value, worst
        return None


@dataclass(frozen=True)
class Order:
    """流转于策略、执行器、交易所的统一 IOC 限价订单，不含交易所专有参数。"""

    exchange: str
    base: str
    side: Side
    quantity: Decimal
    limit_price: Decimal  # 每个基础币 USD 估值
    reduce_only: bool = False
    client_id: str = field(default_factory=lambda: uuid4().hex)


@dataclass(frozen=True)
class Fill:
    """最终 IOC 成交结果；只有 terminal=True 才能据此管理敞口。"""

    order: Order
    status: OrderStatus
    quantity: Decimal = ZERO
    average_price: Decimal = ZERO
    fee: Decimal = ZERO
    exchange_id: str = ""
    terminal: bool = True
    reason: str = ""


@dataclass
class Position:
    """一组双腿持仓及生命周期现金流；cash 含成交及费用，平仓后即估算净 PnL。"""

    base: str
    owner: str
    long_exchange: str
    short_exchange: str
    long_qty: Decimal = ZERO
    short_qty: Decimal = ZERO
    cash: Decimal = ZERO
    state: str = "opening"
    created: float = field(default_factory=time)
    order_ids: list[str] = field(default_factory=list)

    def apply(self, fill: Fill) -> None:
        """计入一次最终成交；执行器保证每个订单结果仅调用一次，禁止 UNKNOWN 入账。"""
        if not fill.terminal or fill.status == OrderStatus.UNKNOWN:
            raise ValueError("Cannot account for an unknown fill")
        direction = D("1") if fill.order.side == Side.BUY else D("-1")
        self.cash -= direction * fill.quantity * fill.average_price + fill.fee
        if fill.order.exchange == self.long_exchange:
            self.long_qty += direction * fill.quantity
        else:
            self.short_qty -= direction * fill.quantity
        self.order_ids.append(fill.order.client_id)


def floor_step(value: Decimal, step: Decimal) -> Decimal:
    """向下截断到数量/价格步长，不使用浮点数或银行家舍入。"""
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step
