"""一个交易所组合的 asyncio 事件循环；行情在内存，跨进程归属和执行日志在 Redis。"""

import asyncio
import logging
from time import monotonic, time
from uuid import uuid4

from coordination import Coordinator
from exchange import create_exchange
from exchange.base import ValidationError
from execution import ExecutionEngine
from strategy import ArbitrageStrategy

log = logging.getLogger(__name__)


class PairWorker:
    """独立进程管理两个交易所；每个 worker 自有 WSS，所有 worker 共用标的归属。"""

    def __init__(self, settings, names):
        """生成唯一进程 owner；重启不会继承旧 owner，以免误接管未知仓位。"""
        from dataclasses import replace

        # 中枢方向按当前组合定义，不能沿用其他组合的第一个交易所。
        settings = replace(settings, exchanges=tuple(names))
        self.settings, self.names = settings, names
        self.owner = ":".join(names) + ":" + uuid4().hex
        self.coordinator = Coordinator(settings.redis_url, settings.namespace, settings.mode)
        self.exchanges = {name: create_exchange(name, settings, self.coordinator) for name in names}
        self.strategy = ArbitrageStrategy(settings)
        self.candle_tasks = {}
        self.candle_attempts = {}
        self.execution = ExecutionEngine(
            settings, self.exchanges, self.coordinator, self.owner, self.strategy
        )
        self.armed = {}
        self.depth_seen = {}
        self.last_maintenance = 0.0

    async def run(self) -> None:
        """初始化最新规则后订阅共同标的，退出时保留未平仓状态和锁供人工恢复。"""
        try:
            await self.coordinator.start()
            for venue in self.exchanges.values():
                await venue.initialize()
            left, right = self.exchanges.values()
            common = set(left.instruments) & set(right.instruments)
            if self.settings.hl_routes is not None:
                for name in self.names:
                    if name.split(":", 1)[0] == "hyperliquid":
                        common &= {
                            base for base, selected in self.settings.hl_routes.items() if selected == name
                        }
            if self.settings.symbols != ("*",):
                common &= set(self.settings.symbols)
            if not common:
                log.info(
                    "Pair %s skipped: no common configured instruments; check SYMBOLS/ALIASES", self.names
                )
                return
            for venue in self.exchanges.values():
                await venue.connect(common)
            log.info("Pair %s: %d common instruments; mode=%s", self.names, len(common), self.settings.mode)
            while True:
                await self._wait_for_market()
                await self._maintenance()
                await self._manage_positions()
                for base in sorted(common):
                    if base not in left.instruments or base not in right.instruments:
                        continue
                    if base in self.execution.positions:
                        continue
                    if self.strategy.price_candidate(base, left, right):
                        self._ensure_candles(base)
                    candidate = self.strategy.candidate(base, left, right)
                    if not candidate:
                        self.armed.pop(base, None)
                        if base in self.depth_seen and monotonic() - self.depth_seen[base] > 30:
                            await self._depth(base, False)
                        continue
                    if base not in left.depth:
                        if len(left.depth) >= self.settings.max_depth_subscriptions:
                            continue
                        await self._depth(base, True)
                    self.depth_seen[base] = monotonic()
                    opportunity = self.strategy.opportunity(base, left, right)
                    if opportunity is None:
                        self.armed.pop(base, None)
                        continue
                    direction = opportunity.buy.exchange
                    previous = self.armed.get(base)
                    if previous is None or previous[0] != direction:
                        self.armed[base] = (direction, monotonic())
                        continue
                    if monotonic() - previous[1] < self.settings.signal_seconds:
                        continue
                    if self.settings.mode == "observe":
                        log.info(
                            "%s opportunity net=%s bps baseline=%s bps funding_cost=%s USD",
                            base,
                            opportunity.net_edge_bps,
                            opportunity.baseline_bps,
                            opportunity.funding_cost_usd,
                        )
                        self.armed[base] = (direction, monotonic())
                    else:
                        await self.execution.open(opportunity)
                        self.armed.pop(base, None)
        finally:
            for task in self.candle_tasks.values():
                task.cancel()
            await asyncio.gather(*self.candle_tasks.values(), return_exceptions=True)
            for venue in self.exchanges.values():
                await venue.close()
            await self.coordinator.close()

    def _ensure_candles(self, base):
        """按需启动历史补取，每个进程最多四个标的并发；新收盘桶或断线后重补。

        网络请求不阻塞入场循环。失败后按配置退避；未具备有效 K 线的标的不入场。
        """
        if not self.settings.candle_lookback_bars:
            return
        for key, task in list(self.candle_tasks.items()):
            if task.done():
                del self.candle_tasks[key]
        latest = int(time()) // 900 * 900 - 900
        if all(latest in venue.candles.get(base, {}) for venue in self.exchanges.values()):
            return
        if base in self.candle_tasks or len(self.candle_tasks) >= 4:
            return
        if monotonic() - self.candle_attempts.get(base, float("-inf")) < self.settings.candle_retry_seconds:
            return
        self.candle_attempts[base] = monotonic()
        self.candle_tasks[base] = asyncio.create_task(self._load_candles(base))

    async def _load_candles(self, base):
        """低频并发获取两边 K 线；失败仅跳过该标的，下一次候选检查可重试。"""
        results = await asyncio.gather(
            *(venue.load_candles(base) for venue in self.exchanges.values()), return_exceptions=True
        )
        for result in results:
            if isinstance(result, Exception):
                log.warning("%s candle history unavailable (%s)", base, type(result).__name__)

    async def _wait_for_market(self) -> None:
        """任一交易所行情更新即唤醒；定时唤醒只为对账及心跳，不轮询 REST 行情。"""
        tasks = [asyncio.create_task(venue.changed.wait()) for venue in self.exchanges.values()]
        try:
            await asyncio.wait(tasks, timeout=0.25, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            for venue in self.exchanges.values():
                venue.changed.clear()

    async def _depth(self, base, enabled) -> None:
        """同时管理两边深度订阅；任何连接错误由 worker 退出并保留所有权。"""
        for venue in self.exchanges.values():
            await venue.subscribe_depth(base, enabled)
        if not enabled:
            self.depth_seen.pop(base, None)

    async def _maintenance(self) -> None:
        """低频写心跳并刷新规则；刷新不改变已订阅标的集合，新上市标的下次启动纳入。"""
        now = monotonic()
        if now - self.last_maintenance < 5:
            return
        await self.coordinator.heartbeat(self.owner)
        self.last_maintenance = now
        for venue in self.exchanges.values():
            refresh_seconds = self.settings.metadata_seconds
            if venue.name == "gate" and self.settings.funding_horizon_hours:
                refresh_seconds = min(refresh_seconds, self.settings.funding_schedule_max_age / 2)
            if now - venue.metadata_at >= refresh_seconds:
                await venue.refresh_instruments()

    async def _manage_positions(self) -> None:
        """维护已开仓标的的平仓 PnL、止盈止损及周期账户对账，冻结标的不自动操作。"""
        for base, position in list(self.execution.positions.items()):
            if position.state == "blocked":
                continue
            if any(base not in venue.instruments for venue in self.exchanges.values()):
                await self.execution.block(position, "Instrument removed or delisted")
                continue
            pnl = self.strategy.closing_pnl(position, self.exchanges)
            await self.coordinator.publish_pnl(
                base,
                {
                    "base": base,
                    "owner": self.owner,
                    "close_pnl_usd": pnl,
                    "estimate": True,
                    "funding_included": False,
                    "usdt_usd": self.settings.usdt_usd,
                    "usdc_usd": self.settings.usdc_usd,
                    "timestamp": time(),
                },
            )
            try:
                if position.state == "closing" or (
                    pnl is not None and (pnl >= self.settings.exit_profit or pnl <= -self.settings.stop_loss)
                ):
                    await self.execution.close_position(position)
                elif (
                    monotonic() - self.execution.last_reconcile.get(base, 0) > self.settings.reconcile_seconds
                ):
                    await self.execution.verify(position)
            except ValidationError as error:
                await self.execution.block(position, str(error))
