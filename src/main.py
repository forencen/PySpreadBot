"""命令行入口：main.py 用于直接启动，包入口与 console script 复用同一函数。"""

import argparse
import asyncio
import json
import logging
import multiprocessing as mp
import signal
from dataclasses import replace

from config import load_settings
from coordination import Coordinator
from engine import PairWorker
from exchange import (
    create_exchange,
    exchange_pairs,
    resolve_exchanges,
    select_hyperliquid_routes,
    supports_exchange,
)


def worker_entry(settings, names):
    """spawn 子进程入口，子进程自己创建事件循环与网络连接，不共享父进程套接字。"""
    logging.basicConfig(
        level=settings.log_level, format="%(asctime)s %(processName)s %(levelname)s %(message)s"
    )
    try:
        asyncio.run(PairWorker(settings, names).run())
    except KeyboardInterrupt:
        pass


def run_processes(settings):
    """每两个交易所创建一个进程；任一异常退出停止整个运行组，避免无监管的部分运行。"""
    context = mp.get_context("spawn")
    workers = [
        context.Process(target=worker_entry, args=(settings, pair), name="-".join(pair))
        for pair in exchange_pairs(settings.exchanges)
    ]
    if not workers:
        raise ValueError("No cross-exchange pairs configured")
    try:
        for process in workers:
            process.start()
        while any(process.is_alive() for process in workers):
            for process in workers:
                process.join(timeout=0.2)
            if any(process.exitcode not in (None, 0) for process in workers):
                raise RuntimeError("Worker failed; inspect Redis status before restarting")
        if any(process.exitcode not in (None, 0) for process in workers):
            raise RuntimeError("Worker failed; inspect Redis status before restarting")
    finally:
        for process in workers:
            if process.is_alive():
                # SIGINT 使 asyncio.run 取消主任务并清理连接；不自动平仓。
                import os

                os.kill(process.pid, signal.SIGINT)
        for process in workers:
            if process.pid:
                process.join(timeout=5)
                if process.is_alive():
                    process.terminate()
                    process.join(timeout=2)


async def command(settings, name, symbol=None):
    """执行非交易命令：刷新规则、探测公共行情或读取状态，不创建任何真实订单。"""
    if name == "status":
        coordinator = Coordinator(settings.redis_url, settings.namespace, settings.mode)
        try:
            await coordinator.start()
            print(json.dumps(await coordinator.snapshot(), ensure_ascii=False, indent=2))
        finally:
            await coordinator.close()
    else:
        for exchange_name in settings.exchanges:
            venue = create_exchange(exchange_name, replace(settings, mode="observe"))
            try:
                await venue.initialize()
                print(f"{exchange_name}: {len(venue.instruments)} active contracts cached")
                if name == "probe":
                    if not venue.instruments:
                        print(
                            f"{exchange_name}: no enabled contracts; check collateral FX and listing status"
                        )
                        continue
                    base = symbol or next(
                        (s for s in settings.symbols if s in venue.instruments), next(iter(venue.instruments))
                    )
                    if base not in venue.instruments:
                        raise ValueError(f"{exchange_name}: unknown normalized symbol {base}")
                    await venue.connect({base})
                    await venue.subscribe_depth(base)
                    async with asyncio.timeout(30):
                        while True:
                            book = venue.books.get(base)
                            if base in venue.quotes and book and book.fresh(settings.max_age):
                                print(
                                    f"{exchange_name}: {base} fresh WSS book ({len(book.bids)} bids/{len(book.asks)} asks)"
                                )
                                break
                            await asyncio.sleep(0.05)
            finally:
                await venue.close()


def main():
    """解析命令并加载 .env；默认运行 paper，不隐式加载命令行中的私钥。"""
    parser = argparse.ArgumentParser(description="Async multi-exchange perpetual arbitrage")
    parser.add_argument("command", choices=["run", "cache", "probe", "status"], nargs="?", default="run")
    parser.add_argument("--env", default=".env", help="Configuration file path")
    parser.add_argument("--exchange", help="Single market for cache/probe, e.g. hyperliquid:xyz")
    parser.add_argument("--symbol", help="Normalized base symbol for probe, e.g. TSLA")
    args = parser.parse_args()
    settings = load_settings(args.env, mode="observe" if args.command in {"cache", "probe"} else None)
    if args.exchange:
        if args.command not in {"cache", "probe"}:
            parser.error("--exchange is only supported for cache/probe")
        settings = replace(settings, exchanges=(args.exchange,), hl_dexs=())
    unknown = {name for name in settings.exchanges if not supports_exchange(name)}
    if unknown:
        parser.error(f"Unknown exchanges: {sorted(unknown)}")
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(message)s")
    try:
        if args.command != "status":
            settings = asyncio.run(resolve_exchanges(settings))
        if args.command == "run":
            settings = asyncio.run(select_hyperliquid_routes(settings))
            run_processes(settings)
        else:
            asyncio.run(command(settings, args.command, args.symbol))
    except KeyboardInterrupt:
        logging.info("Stopped; open positions retain their Redis ownership")


if __name__ == "__main__":
    main()
