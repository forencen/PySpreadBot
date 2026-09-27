"""执行与并发失败场景测试，不连接真实交易所。"""

import asyncio
from dataclasses import replace

from execution import ExecutionEngine
from models import D, Order, Side
from strategy import Opportunity


def opportunity():
    """固定同量 IOC 双腿，减少测试对策略参数的依赖。"""
    return Opportunity(
        Order("left", "BTC", Side.BUY, D(1), D(100)), Order("right", "BTC", Side.SELL, D(1), D(103)), D(100)
    )


async def test_atomic_claim_limit_and_wrong_owner(coordinator):
    """多组合竞争同一基础币时只有一个成功，其他 owner 无法释放。"""
    results = await asyncio.gather(*(coordinator.claim("BTC", str(i), 3) for i in range(20)))
    assert sum(results) == 1
    owner = (await coordinator.snapshot())["owners"]["BTC"]
    assert not await coordinator.release("BTC", "impostor")
    assert await coordinator.release("BTC", owner)
    assert await coordinator.claim("ETH", "a", 1)
    assert not await coordinator.claim("BTC", "b", 1)


async def test_shared_nonce_and_order_budget(coordinator):
    """共享 nonce 不重复，全局订单预算不会被多个进程各自重复使用。"""
    nonces = await asyncio.gather(*(coordinator.nonce("wallet") for _ in range(20)))
    assert len(set(nonces)) == 20
    accepted = await asyncio.gather(*(coordinator.order_budget("gate", 3) for _ in range(10)))
    assert sum(accepted) == 3


async def test_open_close_and_release(venues, settings, coordinator):
    """全生命周期：开仓后锁保留，双腿平仓并对账后释放。"""
    engine = ExecutionEngine(settings, venues, coordinator, "owner")
    position = await engine.open(opportunity())
    assert position.state == "open"
    assert position.long_qty == position.short_qty == 1
    assert not await coordinator.claim("BTC", "other", 3)
    await engine.close_position(position)
    assert position.state == "closed"
    assert (await coordinator.snapshot())["owners"] == {}
    assert all(order.reduce_only for venue in venues.values() for order in venue.sent[1:])


async def test_validation_failure_sends_neither_leg(venues, settings, coordinator):
    """任一所预校验失败时，两边都不发送。"""
    venues["right"].instruments["BTC"] = replace(venues["right"].instruments["BTC"], min_qty=D(2))
    await ExecutionEngine(settings, venues, coordinator, "owner").open(opportunity())
    assert venues["left"].sent == venues["right"].sent == []
    assert not (await coordinator.snapshot())["owners"]


async def test_partial_fills_reduce_excess(venues, settings, coordinator):
    """一边半成交，另一边以 reduce-only 减少差额后维持等量双腿。"""
    venues["right"].script = [D("0.5")]
    position = await ExecutionEngine(settings, venues, coordinator, "owner").open(opportunity())
    assert position.state == "open"
    assert position.long_qty == position.short_qty == D("0.5")
    compensation = venues["left"].sent[1]
    assert compensation.reduce_only and compensation.side == Side.SELL
    assert compensation.quantity == D("0.5")


async def test_one_leg_rejection_unwinds_other(venues, settings, coordinator):
    """一边明确拒单时，已成交的一边应全部平掉，最终确认空仓后释放锁。"""
    venues["right"].script = [D(0)]
    position = await ExecutionEngine(settings, venues, coordinator, "owner").open(opportunity())
    assert position.state == "closed"
    assert venues["left"].paper_positions["BTC"] == 0
    assert not (await coordinator.snapshot())["owners"]


async def test_unknown_is_not_retried_or_unlocked(venues, settings, coordinator):
    """订单超时无法确认时冻结标的，禁止补单、禁止锁超时后给其他组合操作。"""
    venues["right"].unknown = True
    position = await ExecutionEngine(settings, venues, coordinator, "owner").open(opportunity())
    assert position.state == "blocked"
    assert len(venues["right"].sent) == len(venues["left"].sent) == 1
    assert not await coordinator.claim("BTC", "other", 3)


async def test_existing_account_exposure_blocks(venues, settings, coordinator):
    """启动前已有真实仓位不能被本策略误认作自己的成交。"""
    venues["left"].paper_positions["BTC"] = D(1)
    position = await ExecutionEngine(settings, venues, coordinator, "owner").open(opportunity())
    assert position.state == "blocked"
    assert not venues["left"].sent


async def test_price_moved_during_account_preflight(venues, settings, coordinator):
    """账户核对后价格已变差时，应重新检查并阻止双方下单。"""
    from unittest.mock import AsyncMock

    from models import Book, Level

    original = venues["left"].position

    async def shifted(base):
        """模拟低频账户查询等待期间买入盘口上移。"""
        venues["left"].books[base] = Book((Level(D(109), D(10)),), (Level(D(110), D(10)),))
        return await original(base)

    venues["left"].position = AsyncMock(side_effect=shifted)
    await ExecutionEngine(settings, venues, coordinator, "owner").open(opportunity())
    assert not venues["left"].sent and not venues["right"].sent
    assert not (await coordinator.snapshot())["owners"]


async def test_pending_external_order_prevents_entry(venues, settings, coordinator):
    """净仓位为零也不能忽略遗留挂单。"""
    from unittest.mock import AsyncMock

    venues["right"].has_open_orders = AsyncMock(return_value=True)
    position = await ExecutionEngine(settings, venues, coordinator, "owner").open(opportunity())
    assert position.state == "blocked"
    assert not venues["left"].sent and not venues["right"].sent
