"""标的映射使用显式别名，不盲目剥离数字（例如 1INCH 的 1 不是乘数）。"""

import re

from models import D

DEFAULT_ALIASES = {
    "hyperliquid:kPEPE": ("PEPE", "1000"),
    "hyperliquid:kBONK": ("BONK", "1000"),
    "hyperliquid:kSHIB": ("SHIB", "1000"),
    "hyperliquid:kFLOKI": ("FLOKI", "1000"),
    "hyperliquid:kLUNC": ("LUNC", "1000"),
    "hyperliquid:kDOGS": ("DOGS", "1000"),
}


def normalize(exchange: str, native: str, quote: str, aliases: dict) -> tuple[str, D]:
    """返回基础币名和单位倍数；用户 ALIASES 可按 exchange:native 覆盖内置映射。"""
    mapping = {**DEFAULT_ALIASES, **aliases}
    key = f"{exchange}:{native}"
    if key in mapping:
        base, unit = mapping[key]
        multiplier = D(str(unit))
        if not multiplier.is_finite() or multiplier <= 0:
            raise ValueError(f"Invalid multiplier for {key}")
        return str(base).upper(), multiplier
    if exchange == "hyperliquid" and ":" in native:
        # 下单身份仍保留完整 native；仅匹配名去掉部署方前缀，复用千倍币规则。
        return normalize(exchange, native.split(":", 1)[1], quote, aliases)
    compact = re.sub(r"[\s_\-/—–]+", "", native).upper()
    if exchange != "hyperliquid" and compact.endswith(quote):
        compact = compact[: -len(quote)]
    return compact, D("1")
