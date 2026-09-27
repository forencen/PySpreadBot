"""Gate 单进程调试入口；复用 .env 和适配器，只读取公共数据，不连接 Redis。"""

import argparse
import asyncio
import logging
from dataclasses import replace
from pathlib import Path

if __package__ in {None, ""}:
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from src.config import load_settings
    from src.exchange.gate import GateExchange
else:
    from .config import load_settings
    from .exchange.gate import GateExchange

log = logging.getLogger(__name__)


async def debug_gate(settings, symbol="BTC", *, once=False, contracts_only=False, exchange_type=GateExchange):
    """先初始化 HTTP 会话和合约缓存，再订阅单个标的；任何退出路径都关闭网络连接。

    默认持续接收行情，可在适配器的 handle_message 中断点调试。
    once 等待首个新鲜盘口后退出；contracts_only 只获取全部合约规则。
    无论 .env 的 MODE 如何设置，此入口都强制 observe，不登录、不下单。
    """
    venue = exchange_type(replace(settings, mode="observe"))
    try:
        await venue.initialize()
        log.info("Gate: %d active contracts cached", len(venue.instruments))
        if contracts_only:
            return
        if symbol not in venue.instruments:
            raise ValueError(f"Gate has no active base symbol {symbol}")
        await venue.connect({symbol})
        await venue.subscribe_depth(symbol)
        async with asyncio.timeout(30):
            while symbol not in venue.books or not venue.books[symbol].fresh(settings.max_age):
                await asyncio.sleep(0.1)
        while True:
            book = venue.books.get(symbol)
            if book and book.fresh(settings.max_age):
                log.info("%s bid=%s ask=%s (USD estimate)", symbol, book.bids[0].price, book.asks[0].price)
            else:
                log.info("%s waiting for fresh depth", symbol)
            if once:
                return
            await asyncio.sleep(1)
    finally:
        await venue.close()


def main(*, exchange_type=GateExchange):
    """解析调试选项；默认定位项目根目录 .env，避免 PyCharm 工作目录改变配置来源。"""
    parser = argparse.ArgumentParser(description="Debug Gate public market data in one process")
    parser.add_argument("--env", default=str(Path(__file__).resolve().parent.parent / ".env"))
    parser.add_argument("--symbol", default="BTC", help="Normalized base symbol, e.g. BTC or PEPE")
    parser.add_argument("--once", action="store_true", help="Exit after the first fresh book")
    parser.add_argument("--contracts-only", action="store_true", help="Fetch contract rules without WSS")
    args = parser.parse_args()
    settings = load_settings(args.env, mode="observe")
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(message)s")
    try:
        asyncio.run(
            debug_gate(
                settings,
                args.symbol.upper(),
                once=args.once,
                contracts_only=args.contracts_only,
                exchange_type=exchange_type,
            )
        )
    except KeyboardInterrupt:
        log.info("Gate debugging stopped")


if __name__ == "__main__":
    main()
