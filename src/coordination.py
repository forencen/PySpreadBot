"""Redis 跨进程协调：永久标的归属、持仓日志、原子预算及钱包 nonce。"""

import json
from dataclasses import asdict
from time import time

from redis.asyncio import Redis


class Coordinator:
    """标的锁没有 TTL：进程崩溃不能把尚有仓位的标的自动交给另一个组合。"""

    def __init__(self, url: str, namespace: str, mode: str, client=None):
        """隔离模拟与真实交易状态；注入 client 可在测试中运行同一组 Lua 脚本。"""
        self.redis = client if client is not None else Redis.from_url(url, decode_responses=True)
        self.prefix = f"{namespace}:{mode}"
        self.nonce_prefix = f"{namespace}:wallet"  # 与交易模式无关，同钱包 nonce 不能复用

    def key(self, suffix: str) -> str:
        """返回统一命名空间下的键名；同一账户的所有进程必须使用相同 namespace。"""
        return f"{self.prefix}:{suffix}"

    async def start(self) -> None:
        """启动时验证 Redis 连接，协调器不可用时禁止退化为本地锁继续交易。"""
        await self.redis.ping()

    async def claim(self, base: str, owner: str, maximum: int) -> bool:
        """以单条 Lua 同时检查总持仓上限及标的归属，避免两进程同时开仓。"""
        script = """
        if redis.call('HEXISTS', KEYS[1], ARGV[1]) == 1 then return 0 end
        if redis.call('HLEN', KEYS[1]) >= tonumber(ARGV[3]) then return 0 end
        redis.call('HSET', KEYS[1], ARGV[1], ARGV[2]); return 1
        """
        return bool(await self.redis.eval(script, 1, self.key("owners"), base, owner, maximum))

    async def assert_owner(self, base: str, owner: str) -> None:
        """每轮下单前核对归属；Redis 断连或归属改变都立即停止发送。"""
        if await self.redis.hget(self.key("owners"), base) != owner:
            raise RuntimeError("Symbol ownership lost")

    async def release(self, base: str, owner: str) -> bool:
        """只允许原所有者释放；调用方必须已经查证双方仓位为零且订单均终态。"""
        script = """
        if redis.call('HGET', KEYS[1], ARGV[1]) ~= ARGV[2] then return 0 end
        redis.call('HDEL', KEYS[1], ARGV[1]); return 1
        """
        return bool(await self.redis.eval(script, 1, self.key("owners"), base, owner))

    async def save_position(self, position) -> None:
        """持久化生命周期状态；写失败向上抛出并保留所有权，不冒险继续交易。"""
        await self.redis.hset(self.key("positions"), position.base, json.dumps(asdict(position), default=str))

    async def journal(self, order, fill=None) -> None:
        """发送前写 intent，查清终态后写 fill；用于崩溃后识别未确定的金融操作。"""
        data = {"order": asdict(order), "fill": asdict(fill) if fill else None, "updated_at": time()}
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.hset(self.key("orders"), order.client_id, json.dumps(data, default=str))
            pipe.sadd(self.key(f"symbol_orders:{order.base}"), order.client_id)
            await pipe.execute()

    async def nonce(self, wallet: str) -> int:
        """为同一签名钱包提供毫秒级严格递增 nonce，处理多进程及同毫秒订单。"""
        script = """
        local old = tonumber(redis.call('GET', KEYS[1]) or '0')
        local n = math.max(old + 1, tonumber(ARGV[1]))
        redis.call('SET', KEYS[1], n); return n
        """
        return int(
            await self.redis.eval(script, 1, f"{self.nonce_prefix}:{wallet.lower()}", int(time() * 1000))
        )

    async def order_budget(self, exchange: str, maximum: int) -> bool:
        """共享一分钟滑动窗口订单预算，包含减仓请求；超预算拒绝而不排队追单。"""
        script = """
        redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', ARGV[1] - 60000)
        if redis.call('ZCARD', KEYS[1]) >= tonumber(ARGV[2]) then return 0 end
        local seq = redis.call('INCR', KEYS[2])
        redis.call('ZADD', KEYS[1], ARGV[1], tostring(seq))
        redis.call('PEXPIRE', KEYS[1], 61000); return 1
        """
        return bool(
            await self.redis.eval(
                script,
                2,
                self.key(f"budget:{exchange}"),
                self.key(f"budget_seq:{exchange}"),
                int(time() * 1000),
                maximum,
            )
        )

    async def heartbeat(self, owner: str) -> None:
        """心跳仅用于可观测性，不作为自动抢占持仓所有权的依据。"""
        await self.redis.set(self.key(f"heartbeat:{owner}"), str(time()), ex=15)

    async def publish_pnl(self, base: str, payload: dict) -> None:
        """同时保存最新平仓估值和发布通知，读者断线后仍能读取最后状态。"""
        encoded = json.dumps(payload, default=str)
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.hset(self.key("pnl"), base, encoded)
            pipe.publish(self.key("events"), encoded)
            await pipe.execute()

    async def snapshot(self) -> dict:
        """获取归属、持仓和最新 PnL，供 status CLI 读取而不连接交易所。"""
        return {
            "owners": await self.redis.hgetall(self.key("owners")),
            "positions": {
                k: json.loads(v) for k, v in (await self.redis.hgetall(self.key("positions"))).items()
            },
            "pnl": {k: json.loads(v) for k, v in (await self.redis.hgetall(self.key("pnl"))).items()},
        }

    async def close(self) -> None:
        """关闭 Redis 连接池，不删除任何未完成交易记录。"""
        await self.redis.aclose()
