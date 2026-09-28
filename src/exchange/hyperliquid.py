"""Hyperliquid 原生及 HIP-3 永续适配器，共用 WSS 协议并保持各市场身份。"""

import asyncio
import logging
import re
from time import time

import aiohttp

from exchange.base import Exchange, ValidationError
from models import ZERO, Book, Candle, D, Fill, Funding, Instrument, Level, OrderStatus, Quote
from normalization import normalize
from transport import WebSocketTransport

log = logging.getLogger(__name__)


class HyperliquidExchange(Exchange):
    """每个实例对应一个 DEX；原生使用空 dex，HIP-3 使用 hyperliquid:dex 名称。"""

    name = "hyperliquid"
    ws_url = "wss://api.hyperliquid.xyz/ws"

    def __init__(self, settings, coordinator=None, *, dex=""):
        """设置市场身份；不同 DEX 的仓位隔离，但共享签名钱包 nonce 和订单限额。"""
        if dex and not re.fullmatch(r"[a-zA-Z0-9_-]+", dex):
            raise ValueError("Invalid Hyperliquid dex name")
        self.dex = dex
        self.name = f"hyperliquid:{dex}" if dex else "hyperliquid"
        super().__init__(settings, coordinator)
        self.request_id = 0
        self.wallet = None
        self.dex_index = 0

    @property
    def rate_limit_key(self):
        """所有 Hyperliquid DEX 共用一个账户级订单额度，不能通过添加 DEX 放大限频。"""
        return "hyperliquid"

    @staticmethod
    def dex_indices(raw):
        """保留 perpDexs 原始列表位置（含 null），不能过滤后重新编号。"""
        if not isinstance(raw, list):
            raise ValueError("Invalid perpDexs response")
        result = {}
        for index, item in enumerate(raw):
            if item is not None:
                name = item["name"]
                if not re.fullmatch(r"[a-zA-Z0-9_-]+", name) or name in result:
                    raise ValueError("Invalid or duplicate dex name")
                result[name] = index
        return result

    @classmethod
    async def discover_dexs(cls, settings):
        """在启动进程前读取公开 DEX 名单；使用 HTTP 代理，不要求钱包或 Redis。"""
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=settings.request_timeout), trust_env=False
        ) as session:
            async with session.post(
                "https://api.hyperliquid.xyz/info", json={"type": "perpDexs"}, proxy=settings.http_proxy
            ) as response:
                response.raise_for_status()
                return cls.dex_indices(await response.json())

    async def _http_info(self, payload):
        """低频元数据请求统一经过 HTTP_PROXY；订单和账户查询仍通过 WSS。"""
        async with self.session.post(
            "https://api.hyperliquid.xyz/info", json=payload, proxy=self.settings.http_proxy
        ) as response:
            response.raise_for_status()
            return await response.json()

    def _dex_payload(self, kind, **fields):
        """仅向支持 dex 的接口附加市场参数；原生请求保留兼容的默认格式。"""
        payload = {"type": kind, **fields}
        if self.dex:
            payload["dex"] = self.dex
        return payload

    async def fetch_instruments(self):
        """发现指定 DEX 的全部合约，读取真实抵押币并生成全球唯一的下单 asset ID。

        HIP-3 用 100000 + dex 索引 * 10000 + universe 原始索引；下架合约不能
        在编号前被过滤。匹配名去掉 DEX 前缀，原生名称和下单身份保持完整。
        """
        raw = await self._http_info(self._dex_payload("meta"))
        quote, collateral = "USDC", None
        if self.dex:
            dexs, spot = await asyncio.gather(
                self._http_info({"type": "perpDexs"}), self._http_info({"type": "spotMeta"})
            )
            indices = self.dex_indices(dexs)
            if self.dex not in indices:
                raise ValueError(f"Unknown HIP-3 dex: {self.dex}")
            self.dex_index = indices[self.dex]
            token_id = raw["collateralToken"]
            collateral = next((item for item in spot["tokens"] if item["index"] == token_id), None)
            if collateral is None:
                raise ValueError(f"Unknown collateral token {token_id} for {self.name}")
            quote = collateral["name"]
        try:
            self.settings.fx(quote)
            fx_available = True
        except KeyError:
            fx_available = False
            log.warning(
                "%s: configure QUOTE_USD_RATES for %s; contracts cached but disabled", self.name, quote
            )
        fee = (
            D(str(self.settings.hl_dex_fees.get(self.dex, self.settings.hl_hip3_fee)))
            if self.dex
            else self.settings.hl_fee
        )
        instruments = []
        for index, item in enumerate(raw["universe"]):
            native = item["name"]
            if self.dex and not native.startswith(self.dex + ":"):
                raise ValueError(f"Unexpected coin namespace on {self.name}")
            base, unit = normalize("hyperliquid", native, quote, self.settings.aliases)
            decimals = int(item["szDecimals"])
            instruments.append(
                Instrument(
                    self.name,
                    native,
                    base,
                    quote,
                    unit=unit,
                    qty_step=D(10) ** -decimals,
                    min_qty=D(10) ** -decimals,
                    tick=D(10) ** -(6 - decimals),
                    min_notional=D("10"),
                    taker_fee=fee,
                    active=not item.get("isDelisted", False) and fx_available,
                    asset_id=100000 + self.dex_index * 10000 + index if self.dex else index,
                    size_decimals=decimals,
                    dex=self.dex,
                    collateral_token=raw.get("collateralToken", 0),
                    max_leverage=item.get("maxLeverage"),
                    margin_mode=item.get("marginMode", "isolated" if item.get("onlyIsolated") else "cross"),
                )
            )
        # 规则变化时不能拿旧价格或旧编号继续交易。
        for item in instruments:
            old = self.instruments.get(item.base)
            if old and (old.native, old.asset_id, old.quote, old.unit) != (
                item.native,
                item.asset_id,
                item.quote,
                item.unit,
            ):
                self.invalidate()
                raise RuntimeError(f"Instrument identity changed on {self.name}; reconcile before restarting")
        return instruments, {
            "meta": raw,
            "dex": self.dex,
            "dex_index": self.dex_index,
            "collateral": collateral,
        }

    def validate_order(self, order):
        """复用统一校验；HIP-3 实盘必须显式配置该 DEX 的费率，不能套用原生费用。"""
        super().validate_order(order)
        routes = self.settings.hl_routes
        if not order.reduce_only and routes is not None and routes.get(order.base) != self.name:
            raise ValidationError("Ticker assigned to another Hyperliquid market")
        if self.dex and self.settings.mode == "live" and self.dex not in self.settings.hl_dex_fees:
            raise ValidationError(f"Configure HL_DEX_FEES for {self.dex} before live trading")

    async def fetch_candles(self, base, start, end):
        """通过 WSS candleSnapshot 补取 15m 历史；HIP-3 必须使用完整 dex:coin。"""
        instrument = self.instruments[base]
        rows = await self._post(
            "info",
            {
                "type": "candleSnapshot",
                "req": {
                    "coin": instrument.native,
                    "interval": "15m",
                    "startTime": start * 1000,
                    "endTime": end * 1000 - 1,
                },
            },
        )
        fx = self.settings.fx(instrument.quote)
        return [
            Candle(
                int(row["t"]) // 1000,
                *(instrument.price(D(str(row[key])), fx) for key in ("o", "h", "l", "c")),
            )
            for row in rows
            if row.get("s") == instrument.native and row.get("i") == "15m" and int(row["t"]) % 900000 == 0
        ]

    async def connect(self, symbols):
        """建立公共行情与交易共用 WSS，live 模式只在本地加载签名钱包。"""
        self.symbols = symbols
        if self.settings.mode == "live":
            from eth_account import Account

            self.wallet = Account.from_key(self.settings.hl_key)
        self.transport = WebSocketTransport(
            self.session,
            self.ws_url,
            self.handle_message,
            self._connected,
            self.invalidate,
            self.settings.request_timeout,
            proxy=self.settings.wss_proxy,
        )
        await self.transport.start()

    async def _connected(self):
        """订阅全市场中间价用于初筛，并恢复按需深度订阅。"""
        await self.transport.send({"method": "subscribe", "subscription": self._dex_payload("allMids")})
        for base in sorted(self.depth):
            await self._depth_request(base, True)

    async def _depth_request(self, base, enabled):
        """订阅完整精度 l2Book；不设置 nSigFigs，避免价格合并损失精度。"""
        await self.transport.send(
            {
                "method": "subscribe" if enabled else "unsubscribe",
                "subscription": {"type": "activeAssetCtx", "coin": self.instruments[base].native},
            }
        )
        if not enabled:
            self.funding.pop(base, None)
        await self.transport.send(
            {
                "method": "subscribe" if enabled else "unsubscribe",
                "subscription": {"type": "l2Book", "coin": self.instruments[base].native},
            }
        )

    async def subscribe_depth(self, base, enabled=True):
        """记录深度订阅状态并发送请求；收到新完整快照前不得使用旧盘口。"""
        if enabled:
            self.depth.add(base)
        else:
            self.depth.discard(base)
            self.books.pop(base, None)
        await self._depth_request(base, enabled)

    async def _post(self, kind, payload):
        """把 info/action 请求包装为 WebSocket post，返回服务端 payload。"""
        self.request_id += 1
        response = await self.transport.request(
            str(self.request_id),
            {
                "method": "post",
                "id": self.request_id,
                "request": {"type": kind, "payload": payload},
            },
        )
        if response["type"] == "error":
            raise RuntimeError("Hyperliquid post failed")
        result = response["payload"]
        # WSS info 比 HTTP 多一层 {type, data}；action 响应不使用这层包装。
        if (
            kind == "info"
            and isinstance(result, dict)
            and result.get("type") == payload.get("type")
            and "data" in result
        ):
            return result["data"]
        return result

    async def handle_message(self, message):
        """分发 post 回复、全市场中间价和完整深度快照，不把中间价当作实际成交价。"""
        channel, data = message.get("channel"), message.get("data", {})
        if channel == "post":
            self.transport.resolve(str(data["id"]), data["response"])
        elif channel == "error":
            raise RuntimeError("Hyperliquid subscription rejected")
        elif channel == "activeAssetCtx":
            instrument = self.by_native.get(data.get("coin"))
            ctx = data.get("ctx", {})
            if (
                instrument
                and instrument.base in self.depth
                and ctx.get("funding") is not None
                and ctx.get("markPx")
            ):
                now = time()
                self.funding[instrument.base] = Funding(
                    D(str(ctx["funding"])),
                    instrument.price(D(str(ctx["markPx"])), self.settings.fx(instrument.quote)),
                    3600,
                    (int(now) // 3600 + 1) * 3600,
                )
                self.changed.set()
        elif channel == "allMids":
            for native, raw in data["mids"].items():
                instrument = self.by_native.get(native)
                if instrument and instrument.base in self.symbols:
                    price = instrument.price(D(raw), self.settings.fx(instrument.quote))
                    self.quotes[instrument.base] = Quote(price, price)
            self.changed.set()
        elif channel == "l2Book":
            instrument = self.by_native.get(data["coin"])
            if instrument is None or instrument.base not in self.depth:
                return
            fx = self.settings.fx(instrument.quote)
            levels = [
                tuple(
                    Level(instrument.price(D(row["px"]), fx), instrument.quantity(D(row["sz"])))
                    for row in side
                    if D(row["sz"]) > 0
                )
                for side in data["levels"]
            ]
            self.books[instrument.base] = Book(
                tuple(sorted(levels[0], key=lambda x: x.price, reverse=True)),
                tuple(sorted(levels[1], key=lambda x: x.price)),
                exchange_time=float(data.get("time", time() * 1000)) / 1000,
            )
            self.changed.set()

    def price_tick(self, instrument, native_price):
        """非整数价格最多五位有效数字且最多 6-szDecimals 小数；整数价格总是合法。"""
        if native_price == native_price.to_integral_value():
            return D("1")
        return max(instrument.tick, D(10) ** (native_price.adjusted() - 4))

    def order_payload(self, order):
        """统一订单转换成 Hyperliquid wire 字段，全程 Decimal 转字符串，不绕经 float。"""
        instrument = self.instruments[order.base]
        return {
            "a": instrument.asset_id,
            "b": order.side == "buy",
            "p": format(self.native_price(order).normalize(), "f"),
            "s": format(instrument.native_quantity(order.quantity).normalize(), "f"),
            "r": order.reduce_only,
            "t": {"limit": {"tif": "Ioc"}},
            "c": "0x" + order.client_id,
        }

    async def place_live(self, order):
        """从共享 Redis 分配 nonce，用官方 SDK 签名，再通过 WSS 提交 IOC action。"""
        from hyperliquid.utils.signing import sign_l1_action

        action = {"type": "order", "orders": [self.order_payload(order)], "grouping": "na"}
        nonce = await self.coordinator.nonce(self.wallet.address)
        signature = sign_l1_action(self.wallet, action, None, nonce, None, True)
        result = await self._post(
            "action", {"action": action, "nonce": nonce, "signature": signature, "vaultAddress": None}
        )
        if result.get("status") != "ok":
            return Fill(order, OrderStatus.REJECTED, reason="Hyperliquid action rejected")
        status = result["response"]["data"]["statuses"][0]
        if "error" in status:
            return Fill(order, OrderStatus.REJECTED, reason=str(status["error"]))
        if "filled" not in status:
            return Fill(order, OrderStatus.UNKNOWN, terminal=False, reason="Nonterminal IOC response")
        data = status["filled"]
        instrument = self.instruments[order.base]
        qty = instrument.quantity(D(data["totalSz"]))
        price = instrument.price(D(data["avgPx"]), self.settings.fx(instrument.quote))
        return Fill(
            order,
            OrderStatus.FILLED if qty == order.quantity else OrderStatus.PARTIAL,
            qty,
            price,
            qty * price * instrument.taker_fee,
            str(data["oid"]),
        )

    async def query_order(self, order):
        """按 cloid 查订单终态，再读取成交列表获取真实均价与费用；未知状态保持冻结。"""
        result = await self._post(
            "info", {"type": "orderStatus", "user": self.settings.hl_account, "oid": "0x" + order.client_id}
        )
        if result.get("status") != "order":
            return Fill(order, OrderStatus.UNKNOWN, terminal=False)
        entry = result["order"]
        status, detail = entry["status"], entry["order"]
        if status in {"open", "triggered"}:
            return Fill(order, OrderStatus.UNKNOWN, terminal=False)
        terminal = status == "filled" or status.lower().endswith(("canceled", "rejected"))
        if not terminal:
            return Fill(order, OrderStatus.UNKNOWN, terminal=False)
        oid = detail["oid"]
        records = await self._post(
            "info",
            {
                "type": "userFillsByTime",
                "user": self.settings.hl_account,
                "startTime": max(0, int(detail["timestamp"]) - 1000),
            },
        )
        instrument = self.instruments[order.base]
        fx = self.settings.fx(instrument.quote)
        qty, value, fee = ZERO, ZERO, ZERO
        seen = set()
        for record in records:
            if (
                record["oid"] != oid
                or record.get("coin", instrument.native) != instrument.native
                or record.get("tid") in seen
            ):
                continue
            seen.add(record.get("tid"))
            amount = instrument.quantity(D(record["sz"]))
            qty += amount
            value += amount * instrument.price(D(record["px"]), fx)
            try:
                fee_fx = self.settings.fx(record.get("feeToken", instrument.quote).strip())
            except KeyError:
                return Fill(order, OrderStatus.UNKNOWN, terminal=False, reason="Unsupported fee currency")
            fee += D(record["fee"]) * fee_fx
        expected = (
            order.quantity
            if status == "filled"
            else instrument.quantity(D(detail["origSz"]) - D(detail["sz"]))
        )
        if qty != expected or (status == "filled" and qty != order.quantity):
            return Fill(order, OrderStatus.UNKNOWN, terminal=False, reason="Fill history not complete")
        final = (
            OrderStatus.FILLED
            if qty == order.quantity
            else OrderStatus.PARTIAL
            if qty
            else OrderStatus.CANCELED
        )
        return Fill(order, final, qty, value / qty if qty else ZERO, fee, str(oid))

    async def live_position(self, base):
        """通过 WSS info 查询清算账户仓位，返回真实基础币有符号数量。"""
        result = await self._post(
            "info", self._dex_payload("clearinghouseState", user=self.settings.hl_account)
        )
        native = self.instruments[base].native
        for item in result["assetPositions"]:
            if item["position"]["coin"] == native:
                return self.instruments[base].quantity(D(item["position"]["szi"]))
        return ZERO

    async def live_open_orders(self, base):
        """通过 WSS info 查询主账户当前挂单，禁止与已有委托混用同一标的。"""
        orders = await self._post("info", self._dex_payload("openOrders", user=self.settings.hl_account))
        native = self.instruments[base].native
        return any(item["coin"] == native for item in orders)
