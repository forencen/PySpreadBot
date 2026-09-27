"""交易所注册表；新增实现后登记名称，运行器自动生成两两组合。"""

from exchange.gate import GateExchange
from exchange.hyperliquid import HyperliquidExchange

EXCHANGES = {"gate": GateExchange, "hyperliquid": HyperliquidExchange}


def create_exchange(name, settings, coordinator=None):
    """按配置创建适配器，未知名称在启动时明确失败，避免静默少运行交易所。"""
    try:
        implementation = EXCHANGES[name]
    except KeyError as error:
        raise ValueError(f"Unsupported exchange: {name}") from error
    return implementation(settings, coordinator)
