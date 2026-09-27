"""纯计算策略：深度成交金额、共同数量步长、净溢价和可执行平仓 PnL。"""

from dataclasses import dataclass
from math import lcm
from time import monotonic, time

from models import BPS, ZERO, D, Order, Position, Side, floor_step


@dataclass(frozen=True)
class Opportunity:
    """通过双边逐档深度检查后形成的同基础币数量订单对。"""

    buy: Order
    sell: Order
    net_edge_bps: D


def common_step(left: D, right: D) -> D:
    """以十进制整数最小公倍数计算共同步长；取 max 会在非整倍数步长下产生裸露敞口。"""
    scale = 10 ** max(0, -left.as_tuple().exponent, -right.as_tuple().exponent)
    return D(lcm(int(left * scale), int(right * scale))) / D(scale)


class ArbitrageStrategy:
    """按可成交净价差选择方向，非零中枢按交易所组合固定顺序解释。"""

    def __init__(self, settings):
        """保存阈值；中枢不自动估计，用户应基于采集数据设置。"""
        self.settings = settings

    def candidate(self, base, left, right) -> bool:
        """轻量报价只触发深度订阅；中间价筛选结果永远不能直接触发下单。"""
        quotes = (left.quotes.get(base), right.quotes.get(base))
        if any(
            q is None
            or monotonic() - q.received > self.settings.max_age
            or not -1 <= time() - q.exchange_time <= self.settings.max_age
            for q in quotes
        ):
            return False
        a, b = quotes
        if min(a.bid, a.ask, b.bid, b.ask) <= 0:
            return False
        premium = ((a.bid + a.ask) / (b.bid + b.ask) - 1) * BPS
        return abs(premium - self.settings.midline_bps) >= self.settings.entry_bps / 2

    def opportunity(self, base, left, right) -> Opportunity | None:
        """尝试两方向，取扣除入场费、预留退出费及滑点后净溢价较大的一组。"""
        options = [
            self._direction(base, left, right, -self.settings.midline_bps),
            self._direction(base, right, left, self.settings.midline_bps),
        ]
        return max((o for o in options if o), key=lambda o: o.net_edge_bps, default=None)

    def _direction(self, base, buy, sell, baseline) -> Opportunity | None:
        """逐档匹配买卖盘，以两腿最差限价金额控制规模，保留完整往返费用预算。"""
        buy_book, sell_book = buy.books.get(base), sell.books.get(base)
        if (
            not buy_book
            or not sell_book
            or not buy_book.fresh(self.settings.max_age)
            or not sell_book.fresh(self.settings.max_age)
        ):
            return None
        a, b = buy.instruments[base], sell.instruments[base]
        fee_bps = (a.taker_fee + b.taker_fee) * BPS * 2
        hurdle = self.settings.entry_bps + baseline + fee_bps + self.settings.slippage_bps * 2
        # 只累计边际价差也合格的档位，防止用最优档利润补贴深处劣价。
        i = j = 0
        a_left, b_left = buy_book.asks[0].quantity, sell_book.bids[0].quantity
        quantity, max_price = ZERO, ZERO
        while i < len(buy_book.asks) and j < len(sell_book.bids):
            ask, bid = buy_book.asks[i], sell_book.bids[j]
            if (bid.price / ask.price - 1) * BPS < hurdle:
                break
            chunk = min(a_left, b_left) * self.settings.take_fraction
            max_price = max(max_price, ask.price, bid.price)
            cap = self.settings.max_notional / (max_price * (1 + self.settings.slippage_bps / BPS))
            quantity = min(quantity + chunk, cap)
            if quantity >= cap:
                break
            consumed = min(a_left, b_left)
            a_left -= consumed
            b_left -= consumed
            if not a_left:
                i += 1
                if i < len(buy_book.asks):
                    a_left = buy_book.asks[i].quantity
            if not b_left:
                j += 1
                if j < len(sell_book.bids):
                    b_left = sell_book.bids[j].quantity
        quantity = floor_step(quantity, common_step(a.base_step, b.base_step))
        if quantity <= 0:
            return None
        buy_value, buy_worst = buy_book.sweep(Side.BUY, quantity)
        sell_value, sell_worst = sell_book.sweep(Side.SELL, quantity)
        net = (sell_value / buy_value - 1) * BPS - fee_bps - self.settings.slippage_bps * 2
        if net < self.settings.entry_bps + baseline:
            return None
        buy_limit = buy.round_limit(base, Side.BUY, buy_worst * (1 + self.settings.slippage_bps / BPS))
        sell_limit = sell.round_limit(base, Side.SELL, sell_worst * (1 - self.settings.slippage_bps / BPS))
        return Opportunity(
            Order(buy.name, base, Side.BUY, quantity, buy_limit),
            Order(sell.name, base, Side.SELL, quantity, sell_limit),
            net,
        )

    def closing_pnl(self, position: Position, exchanges: dict) -> D | None:
        """按全部剩余持仓的反向盘口深度计算净平仓估值；深度不足或过期时返回 None。"""
        value = position.cash
        for name, qty, side in (
            (position.long_exchange, position.long_qty, Side.SELL),
            (position.short_exchange, position.short_qty, Side.BUY),
        ):
            if qty == 0:
                continue
            venue = exchanges[name]
            book = venue.books.get(position.base)
            if book is None or not book.fresh(self.settings.max_age):
                return None
            swept = book.sweep(side, qty)
            if swept is None:
                return None
            notional, _ = swept
            value += notional if side == Side.SELL else -notional
            value -= notional * venue.instruments[position.base].taker_fee
        return value
