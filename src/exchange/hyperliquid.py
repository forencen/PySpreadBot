"""Hyperliquid 原生永续适配器；官方 SDK 仅用于签名，热路径网络全部使用 WSS。"""

from time import time

from ..models import ZERO, Book, D, Fill, Instrument, Level, OrderStatus, Quote
from ..normalization import normalize
from ..transport import WebSocketTransport
from .base import Exchange


class HyperliquidExchange(Exchange):
    """原生永续以 USDC 计价；HIP-3 builder dex 需要独立标的编号，暂不混入。"""

    name = "hyperliquid"
    ws_url = "wss://api.hyperliquid.xyz/ws"

    def __init__(self, settings, coordinator=None):
        """创建递增请求 ID；交易 nonce 由 Redis 按签名钱包跨进程分配。"""
        super().__init__(settings, coordinator)
        self.request_id = 0
        self.wallet = None

    async def fetch_instruments(self):
        """读取原生永续 universe，按列表位置保留 asset_id，过滤已下架资产。"""
        async with self.session.post(
            "https://api.hyperliquid.xyz/info", json={"type": "meta"}, proxy=self.settings.http_proxy
        ) as response:
            response.raise_for_status()
            raw = await response.json()
        instruments = []
        for index, item in enumerate(raw["universe"]):
            base, unit = normalize(self.name, item["name"], "USDC", self.settings.aliases)
            decimals = int(item["szDecimals"])
            instruments.append(
                Instrument(
                    self.name,
                    item["name"],
                    base,
                    "USDC",
                    unit=unit,
                    qty_step=D(10) ** -decimals,
                    min_qty=D(10) ** -decimals,
                    tick=D(10) ** -(6 - decimals),
                    min_notional=D("10"),
                    taker_fee=self.settings.hl_fee,
                    active=not item.get("isDelisted", False),
                    asset_id=index,
                    size_decimals=decimals,
                )
            )
        return instruments, raw

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
        await self.transport.send({"method": "subscribe", "subscription": {"type": "allMids"}})
        for base in sorted(self.depth):
            await self._depth_request(base, True)

    async def _depth_request(self, base, enabled):
        """订阅完整精度 l2Book；不设置 nSigFigs，避免价格合并损失精度。"""
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
        return response["payload"]

    async def handle_message(self, message):
        """分发 post 回复、全市场中间价和完整深度快照，不把中间价当作实际成交价。"""
        channel, data = message.get("channel"), message.get("data", {})
        if channel == "post":
            self.transport.resolve(str(data["id"]), data["response"])
        elif channel == "error":
            raise RuntimeError("Hyperliquid subscription rejected")
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
            if record["oid"] != oid or record.get("tid") in seen:
                continue
            seen.add(record.get("tid"))
            amount = instrument.quantity(D(record["sz"]))
            qty += amount
            value += amount * instrument.price(D(record["px"]), fx)
            if record.get("feeToken", "USDC") != "USDC":
                return Fill(order, OrderStatus.UNKNOWN, terminal=False, reason="Unsupported fee currency")
            fee += D(record["fee"]) * fx
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
        result = await self._post("info", {"type": "clearinghouseState", "user": self.settings.hl_account})
        native = self.instruments[base].native
        for item in result["assetPositions"]:
            if item["position"]["coin"] == native:
                return self.instruments[base].quantity(D(item["position"]["szi"]))
        return ZERO

    async def live_open_orders(self, base):
        """通过 WSS info 查询主账户当前挂单，禁止与已有委托混用同一标的。"""
        orders = await self._post("info", {"type": "openOrders", "user": self.settings.hl_account})
        native = self.instruments[base].native
        return any(item["coin"] == native for item in orders)
