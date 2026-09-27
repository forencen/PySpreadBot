"""双腿执行状态机：先双边验证，再并发下单，最终对账后才释放标的归属。"""

import asyncio
import logging
from time import monotonic

from exchange.base import ValidationError
from market_analysis import funding_cost_per_unit, reverse_premium
from models import BPS, Fill, Order, OrderStatus, Position, Side

log = logging.getLogger(__name__)


class ExecutionEngine:
    """每个组合进程只运行一个执行器；每个基础币持仓由全局 Redis owner 唯一拥有。"""

    def __init__(self, settings, exchanges, coordinator, owner, strategy=None):
        """保存交易所注册表及协调器，本地只追踪当前进程创建的持仓。"""
        self.settings, self.exchanges = settings, exchanges
        self.coordinator, self.owner = coordinator, owner
        self.positions: dict[str, Position] = {}
        self.last_reconcile: dict[str, float] = {}
        self.strategy = strategy

    async def open(self, opportunity) -> Position | None:
        """获取全局所有权后检查原有账户仓位；两条腿都校验通过才记录 intent 并发送。"""
        base = opportunity.buy.base
        if not await self.coordinator.claim(base, self.owner, self.settings.max_positions):
            return None
        position = Position(base, self.owner, opportunity.buy.exchange, opportunity.sell.exchange)
        self.positions[base] = position
        try:
            await self.coordinator.save_position(position)
            actual = await asyncio.gather(
                *(
                    self.exchanges[name].position(base)
                    for name in (position.long_exchange, position.short_exchange)
                )
            )
            pending = await asyncio.gather(
                *(
                    self.exchanges[name].has_open_orders(base)
                    for name in (position.long_exchange, position.short_exchange)
                )
            )
            if any(actual) or any(pending):
                await self.block(position, "Existing account exposure or orders require reconciliation")
                return position
            orders = (opportunity.buy, opportunity.sell)
            try:
                for order in orders:
                    self.exchanges[order.exchange].validate_order(order)
                self._recheck_edge(opportunity)
            except ValidationError as error:
                log.info("%s validation rejected: %s", base, error)
                await self._finish_flat(position)
                return None
            await self._execute(position, orders)
            if position.state != "blocked":
                await self.rebalance(position)
                await self.verify(position)
            return position
        except Exception as error:
            await self.block(position, type(error).__name__)
            raise

    def _recheck_edge(self, opportunity) -> None:
        """账户检查和 Redis IO 后用最新深度重算价差，防止等待期间信号消失仍发送旧订单。"""
        buy, sell = opportunity.buy, opportunity.sell
        values = []
        fees = 0
        for order in (buy, sell):
            venue = self.exchanges[order.exchange]
            swept = venue.books[order.base].sweep(order.side, order.quantity)
            if swept is None:
                raise ValidationError("Depth disappeared before submission")
            value, worst = swept
            if (order.side == Side.BUY and worst > order.limit_price) or (
                order.side == Side.SELL and worst < order.limit_price
            ):
                raise ValidationError("Market moved outside order price protection")
            values.append(value)
            fees += venue.instruments[order.base].taker_fee
        if self.settings.spread_window_seconds:
            if self.strategy is None:
                raise ValidationError("Spread history unavailable before submission")
            left, right = (self.exchanges[name] for name in self.settings.exchanges)
            center = self.strategy.baseline(buy.base, left, right)
        else:
            center = self.settings.midline_bps
        if center is None or center <= -BPS:
            raise ValidationError("Spread history not ready before submission")
        baseline = center if sell.exchange == self.settings.exchanges[0] else reverse_premium(center)
        funding = funding_cost_per_unit(
            buy.base, self.exchanges[buy.exchange], self.exchanges[sell.exchange], self.settings
        )
        if funding is None:
            raise ValidationError("Funding unavailable before submission")
        gross = (values[1] / values[0] - 1) * BPS
        net = (
            gross - funding * buy.quantity / values[0] * BPS - fees * BPS * 2 - self.settings.slippage_bps * 2
        )
        if gross - baseline < self.settings.entry_bps or net - baseline < self.settings.entry_bps:
            raise ValidationError("Net edge disappeared before submission")

    async def _execute(self, position, orders) -> None:
        """所有 intent 在发送前持久化；并发发送完成后统一查证结果，绝不盲目重发。"""
        await self.coordinator.assert_owner(position.base, self.owner)
        for order in orders:
            self.exchanges[order.exchange].validate_order(order)
        for order in orders:
            await self.coordinator.journal(order)
        results = await asyncio.gather(
            *(self.exchanges[o.exchange].submit(o) for o in orders), return_exceptions=True
        )
        fills = []
        for order, result in zip(orders, results, strict=True):
            if isinstance(result, BaseException):
                result = Fill(order, OrderStatus.UNKNOWN, terminal=False, reason=type(result).__name__)
            fills.append(result)
        resolved = await asyncio.gather(*(self.exchanges[f.order.exchange].resolve(f) for f in fills))
        unknown = False
        for fill in resolved:
            await self.coordinator.journal(fill.order, fill)
            if not fill.terminal or fill.status == OrderStatus.UNKNOWN:
                unknown = True
            else:
                if fill.quantity < 0 or fill.quantity > fill.order.quantity:
                    raise RuntimeError("Invalid fill quantity")
                position.apply(fill)
        await self.coordinator.save_position(position)
        if unknown:
            await self.block(position, "Order result unknown; ownership retained")

    async def rebalance(self, position) -> None:
        """削减成交较多的一腿到两腿较小值；不以加仓追价掩盖失败，最多尝试三次。"""
        for _ in range(3):
            difference = position.long_qty - position.short_qty
            if difference == 0 or position.state == "blocked":
                return
            name = position.long_exchange if difference > 0 else position.short_exchange
            side = Side.SELL if difference > 0 else Side.BUY
            order = self._close_order(position.base, name, side, abs(difference))
            if order is None:
                await self.block(position, "Cannot safely reduce unmatched exposure")
                return
            try:
                await self._execute(position, (order,))
            except ValidationError:
                await self.block(position, "Residual exposure below exchange lot/minimum")
                return
        if position.long_qty != position.short_qty:
            await self.block(position, "Residual exposure after compensation attempts")

    def _close_order(self, base, name, side, quantity) -> Order | None:
        """使用新鲜反向深度构造 reduce-only IOC，不足以覆盖数量时停止自动修复。"""
        venue = self.exchanges[name]
        book = venue.books.get(base)
        if book is None or not book.fresh(self.settings.max_age):
            return None
        swept = book.sweep(side, quantity)
        if swept is None:
            return None
        _, worst = swept
        factor = (
            1 + self.settings.slippage_bps / BPS if side == Side.BUY else 1 - self.settings.slippage_bps / BPS
        )
        return Order(name, base, side, quantity, venue.round_limit(base, side, worst * factor), True)

    async def close_position(self, position) -> None:
        """并发平双腿，按实际成交更新剩余数量；未平完保留 closing 状态供后续行情重试。"""
        if position.state == "blocked":
            return
        orders = []
        for name, side, qty in (
            (position.long_exchange, Side.SELL, position.long_qty),
            (position.short_exchange, Side.BUY, position.short_qty),
        ):
            if qty:
                order = self._close_order(position.base, name, side, qty)
                if order is None:
                    return
                orders.append(order)
        position.state = "closing"
        await self.coordinator.save_position(position)
        if orders:
            await self._execute(position, orders)
        if position.state != "blocked":
            await self.rebalance(position)
            await self.verify(position, closing=True)

    async def verify(self, position, closing=False) -> None:
        """对照交易所实际有符号仓位；不一致立即冻结，不能把本地推测作为最终事实。"""
        if position.state == "blocked":
            return
        actual = await asyncio.gather(
            self.exchanges[position.long_exchange].position(position.base),
            self.exchanges[position.short_exchange].position(position.base),
        )
        self.last_reconcile[position.base] = monotonic()
        if actual != [position.long_qty, -position.short_qty]:
            await self.block(position, "Account positions do not match execution ledger")
        elif position.long_qty == position.short_qty == 0:
            await self._finish_flat(position)
        else:
            position.state = "closing" if closing else "open"
            await self.coordinator.save_position(position)

    async def _finish_flat(self, position) -> None:
        """已确认无未决订单且空仓后保存 closed 并 CAS 释放；绝不依靠 TTL 释放。"""
        position.state = "closed"
        await self.coordinator.save_position(position)
        await self.coordinator.release(position.base, self.owner)
        self.positions.pop(position.base, None)
        log.info("%s closed, estimated cash PnL=%s", position.base, position.cash)

    async def block(self, position, reason) -> None:
        """标记需要恢复检查并保留全局所有权；日志不记录密钥或原始签名请求。"""
        position.state = "blocked"
        log.error("%s BLOCKED: %s", position.base, reason)
        await self.coordinator.save_position(position)
