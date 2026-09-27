"""从 .env 加载配置；环境变量优先，启动时拒绝不完整的实盘配置。"""

import json
import os
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    """运行、风控及连接配置；敏感字段不参与 repr，禁止把 Settings 整体记录到日志。"""

    mode: str = "paper"
    exchanges: tuple[str, ...] = ("gate", "hyperliquid")
    symbols: tuple[str, ...] = ("BTC", "ETH")
    redis_url: str = field(default="redis://localhost:6379/0", repr=False)
    namespace: str = "pyspreadbot"
    cache_dir: Path = Path("data/contracts")
    log_level: str = "INFO"
    max_notional: Decimal = Decimal("50")
    entry_bps: Decimal = Decimal("20")
    midline_bps: Decimal = Decimal("0")
    slippage_bps: Decimal = Decimal("5")
    exit_profit: Decimal = Decimal("0.1")
    stop_loss: Decimal = Decimal("2")
    take_fraction: Decimal = Decimal("0.2")
    max_age: float = 2.0
    request_timeout: float = 5.0
    http_proxy: str | None = field(default=None, repr=False)
    wss_proxy: str | None = field(default=None, repr=False)
    signal_seconds: float = 0.3
    reconcile_seconds: float = 5.0
    metadata_seconds: float = 3600.0
    max_positions: int = 3
    max_depth_subscriptions: int = 20
    orders_per_minute: int = 30
    usdt_usd: Decimal = Decimal("1")
    usdc_usd: Decimal = Decimal("1")
    gate_fee: Decimal = Decimal("0.0005")
    hl_fee: Decimal = Decimal("0.00045")
    hl_dexs: tuple[str, ...] = ("xyz",)
    hl_hip3_fee: Decimal = Decimal("0.001")
    hl_dex_fees: dict = field(default_factory=dict)
    quote_usd_rates: dict = field(default_factory=dict)
    gate_key: str = field(default="", repr=False)
    gate_secret: str = field(default="", repr=False)
    hl_key: str = field(default="", repr=False)
    hl_account: str = ""
    aliases: dict = field(default_factory=dict)

    def fx(self, quote: str) -> Decimal:
        """获取配置的结算币到 USD 折算率；未知币种直接报错，不默认当成美元。"""
        if quote in self.quote_usd_rates:
            return Decimal(str(self.quote_usd_rates[quote]))
        return {"USDT": self.usdt_usd, "USDC": self.usdc_usd}[quote]


def parse_proxy(raw: str, name: str) -> str | None:
    """把空值或大小写不敏感的 None 转为直连；校验代理 URL，报错不泄露认证信息。"""
    value = raw.strip()
    if not value or value.lower() == "none":
        return None
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError
        if parsed.port is not None and parsed.port <= 0:
            raise ValueError
    except ValueError:
        raise ValueError(f"{name} must be an http(s) proxy URL, empty or None") from None
    return value


def load_settings(env_file: str | Path = ".env", *, mode: str | None = None) -> Settings:
    """读取配置并验证；默认只对 BTC/ETH 模拟交易，SYMBOLS=* 才启用全部共同标的。"""
    load_dotenv(env_file, override=False)
    values = {}
    decimal_fields = {
        "max_notional",
        "entry_bps",
        "midline_bps",
        "slippage_bps",
        "exit_profit",
        "stop_loss",
        "take_fraction",
        "usdt_usd",
        "usdc_usd",
        "gate_fee",
        "hl_fee",
        "hl_hip3_fee",
    }
    float_fields = {"max_age", "request_timeout", "signal_seconds", "reconcile_seconds", "metadata_seconds"}
    int_fields = {"max_positions", "max_depth_subscriptions", "orders_per_minute"}
    for name in Settings.__dataclass_fields__:
        raw = os.getenv(name.upper())
        if raw is None:
            continue
        if name in {"http_proxy", "wss_proxy"}:
            values[name] = parse_proxy(raw, name.upper())
        elif name in decimal_fields:
            values[name] = Decimal(raw)
        elif name in float_fields:
            values[name] = float(raw)
        elif name in int_fields:
            values[name] = int(raw)
        elif name in {"symbols", "exchanges", "hl_dexs"}:
            values[name] = tuple(x.strip() for x in raw.split(",") if x.strip())
        elif name == "cache_dir":
            values[name] = Path(raw)
        elif name in {"aliases", "hl_dex_fees", "quote_usd_rates"}:
            values[name] = json.loads(raw)
        else:
            values[name] = raw
    if mode is not None:
        values["mode"] = mode
    settings = Settings(**values)
    if settings.mode not in {"paper", "live", "observe"}:
        raise ValueError("MODE must be paper, observe or live")
    for name in decimal_fields - {"midline_bps"}:
        value = getattr(settings, name)
        if not value.is_finite() or value < 0:
            raise ValueError(f"{name} must be finite and nonnegative")
    if not settings.midline_bps.is_finite():
        raise ValueError("MIDLINE_BPS must be finite")
    if settings.slippage_bps >= 10000 or max(settings.gate_fee, settings.hl_fee, settings.hl_hip3_fee) >= 1:
        raise ValueError("SLIPPAGE_BPS must be < 10000 and fee rates must be < 1")
    if not isinstance(settings.aliases, dict):
        raise ValueError("ALIASES must be a JSON object")
    for name in ("hl_dex_fees", "quote_usd_rates"):
        mapping = getattr(settings, name)
        if not isinstance(mapping, dict):
            raise ValueError(f"{name.upper()} must be a JSON object")
        for key, raw in mapping.items():
            value = Decimal(str(raw))
            if not value.is_finite() or (not 0 <= value < 1 if name == "hl_dex_fees" else value <= 0):
                raise ValueError(f"Invalid {name.upper()} value for {key}")
    if len(settings.hl_dexs) != len(set(settings.hl_dexs)) or (
        "*" in settings.hl_dexs and settings.hl_dexs != ("*",)
    ):
        raise ValueError("HL_DEXS must contain distinct dex names or a single *")
    for name in float_fields | int_fields:
        value = getattr(settings, name)
        if not 0 < value < float("inf"):
            raise ValueError(f"{name} must be finite and positive")
    if (
        not 0 < settings.take_fraction <= 1
        or min(settings.usdt_usd, settings.usdc_usd, settings.max_notional) <= 0
    ):
        raise ValueError("FX rates, notional and take fraction must be positive; fraction <= 1")
    minimum = 1 if settings.mode == "observe" else 2
    if len(set(settings.exchanges)) != len(settings.exchanges) or len(settings.exchanges) < minimum:
        raise ValueError(f"At least {minimum} distinct exchanges required")
    if not settings.symbols or settings.log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        raise ValueError("Invalid symbols or log level")
    if settings.mode == "live":
        if "gate" in settings.exchanges and not all((settings.gate_key, settings.gate_secret)):
            raise ValueError("Live Gate requires GATE_KEY and GATE_SECRET")
        if any(name.split(":", 1)[0] == "hyperliquid" for name in settings.exchanges) and not all(
            (settings.hl_key, settings.hl_account)
        ):
            raise ValueError("Live Hyperliquid requires HL_KEY and HL_ACCOUNT")
    return settings
