"""交易所注册表；新增实现后登记名称，运行器自动生成两两组合。"""

import re
from dataclasses import replace
from itertools import combinations

from exchange.gate import GateExchange
from exchange.hyperliquid import HyperliquidExchange

EXCHANGES = {"gate": GateExchange, "hyperliquid": HyperliquidExchange}


def supports_exchange(name):
    """接受已注册交易所或显式的 hyperliquid:dex 市场标识，拒绝无效路径字符。"""
    return name in EXCHANGES or bool(re.fullmatch(r"hyperliquid:[a-zA-Z0-9_-]+", name))


async def resolve_exchanges(settings):
    """把 HL_DEXS 展开成独立市场；* 通过公共 API 发现，固定名单不依赖额外请求。"""
    names = []
    for name in settings.exchanges:
        if not supports_exchange(name):
            raise ValueError(f"Unsupported exchange: {name}")
        names.append(name)
        if name == "hyperliquid":
            dexs = (
                await HyperliquidExchange.discover_dexs(settings)
                if settings.hl_dexs == ("*",)
                else settings.hl_dexs
            )
            for dex in dexs:
                venue = f"hyperliquid:{dex}"
                if not supports_exchange(venue):
                    raise ValueError("Invalid HL_DEXS name")
                names.append(venue)
    return replace(settings, exchanges=tuple(dict.fromkeys(names)))


def exchange_pairs(names):
    """按不同交易所两两组合；HIP-3 是 Hyperliquid 的市场，默认不启动同所市场之间套利。"""
    return tuple(
        (left, right)
        for left, right in combinations(names, 2)
        if left.split(":", 1)[0] != right.split(":", 1)[0]
    )


def create_exchange(name, settings, coordinator=None):
    """按配置创建适配器，未知名称在启动时明确失败，避免静默少运行交易所。"""
    if name.startswith("hyperliquid:") and supports_exchange(name):
        return HyperliquidExchange(settings, coordinator, dex=name.split(":", 1)[1])
    try:
        implementation = EXCHANGES[name]
    except KeyError as error:
        raise ValueError(f"Unsupported exchange: {name}") from error
    return implementation(settings, coordinator)


async def select_hyperliquid_routes(settings):
    """启动前按优先级固定每个基础币的市场，所有子进程共享同一份选择。

    原生市场优先保留；HIP-3 按 HL_DEXS 顺序选择有效合约。元数据获取失败直接
    阻止启动，不能把网络故障当成标的不存在。运行期间不切换已有仓位的 DEX。
    """
    names = [name for name in settings.exchanges if name.split(":", 1)[0] == "hyperliquid"]
    priority = ["hyperliquid", *(f"hyperliquid:{dex}" for dex in settings.hl_dexs)]
    names.sort(key=lambda name: priority.index(name) if name in priority else len(priority))
    routes = {}
    for name in names:
        venue = create_exchange(name, settings)
        try:
            await venue.initialize()
            for base in venue.instruments:
                routes.setdefault(base, name)
        finally:
            await venue.close()
    return replace(settings, hl_routes=routes)
