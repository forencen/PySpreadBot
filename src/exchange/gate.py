"""Gate USDT 线性永续适配器：WSS 行情、深度、登录、下单与查单。"""

import hashlib
import hmac
from time import monotonic, time
from uuid import uuid4

from ..models import Book, D, Fill, Instrument, Level, Order, OrderStatus, Quote
from ..normalization import normalize
from ..transport import WebSocketTransport
from .base import Exchange, ValidationError


class GateExchange(Exchange):
    """只接入 USDT direct 合约；反向及其他结算合约不会混入同一计算模型。"""

    name = "gate"
    http_url = "https://api.gateio.ws/api/v4"
    ws_url = "wss://fx-ws.gateio.ws/v4/ws/usdt"

    def __init__(self, settings, coordinator=None):
        """初始化增量深度序号、原始档位和标记价格缓存。"""
        super().__init__(settings, coordinator)
        self.raw_books = {}
        self.marks = {}

    async def fetch_instruments(self):
        """读取全部 USDT 合约；张数暂取整张，保留更严格但合法的下单步长。"""
        async with self.session.get(
            f"{self.http_url}/futures/usdt/contracts", proxy=self.settings.http_proxy
        ) as response:
            response.raise_for_status()
            raw = await response.json()
        instruments = []
        for item in raw:
            if item.get("type") != "direct":
                continue
            base, unit = normalize(self.name, item["name"], "USDT", self.settings.aliases)
            instruments.append(
                Instrument(
                    self.name,
                    item["name"],
                    base,
                    "USDT",
                    unit=unit,
                    contract_size=D(item["quanto_multiplier"]),
                    qty_step=D("1"),
                    min_qty=D(str(item["order_size_min"])),
                    max_qty=D(str(item["order_size_max"])),
                    tick=D(item["order_price_round"]),
                    taker_fee=self.settings.gate_fee,
                    active=not item.get("in_delisting", False) and item.get("status", "trading") == "trading",
                    price_deviation=D(item.get("order_price_deviate", "0")),
                )
            )
        return instruments, raw

    async def connect(self, symbols):
        """建立支持十进制张数响应的 WSS；私有登录仅在 live 模式执行。"""
        self.symbols = symbols
        self.transport = WebSocketTransport(
            self.session,
            self.ws_url,
            self.handle_message,
            self._connected,
            self.invalidate,
            self.settings.request_timeout,
            {"X-Gate-Size-Decimal": "1"},
            proxy=self.settings.wss_proxy,
        )
        await self.transport.start()

    async def _connected(self):
        """连接成功后先鉴权再恢复全部订阅；每次重连重新取得深度快照。"""
        if self.settings.mode == "live":
            timestamp = str(int(time()))
            signature = hmac.new(
                self.settings.gate_secret.encode(),
                f"api\nfutures.login\n\n{timestamp}".encode(),
                hashlib.sha512,
            ).hexdigest()
            await self._rpc(
                "futures.login",
                None,
                {
                    "api_key": self.settings.gate_key,
                    "signature": signature,
                    "timestamp": timestamp,
                },
            )
        natives = [self.instruments[base].native for base in sorted(self.symbols)]
        for offset in range(0, len(natives), 50):
            for channel in ("futures.book_ticker", "futures.tickers"):
                await self._subscription(channel, natives[offset : offset + 50])
        for base in sorted(self.depth):
            await self._subscription("futures.obu", [self._stream(base)])

    def invalidate(self):
        """在基类行情失效基础上清除增量序号及标记价，防止错接新旧连接。"""
        super().invalidate()
        self.raw_books.clear()
        self.marks.clear()

    def _stream(self, base):
        """构建 Gate V2 的 50 档 / 20ms 深度流，初始快照和后续增量都走 WSS。"""
        return f"ob.{self.instruments[base].native}.50"

    async def _subscription(self, channel, payload, enabled=True):
        """发送公共频道订阅/退订，交给读循环检查服务端错误。"""
        await self.transport.send(
            {
                "time": int(time()),
                "channel": channel,
                "event": "subscribe" if enabled else "unsubscribe",
                "payload": payload,
            }
        )

    async def subscribe_depth(self, base, enabled=True):
        """登记期望订阅状态；退订立即删除本地深度，释放活动订阅配额。"""
        if enabled:
            self.depth.add(base)
        else:
            self.depth.discard(base)
            self.books.pop(base, None)
            self.raw_books.pop(base, None)
        await self._subscription("futures.obu", [self._stream(base)], enabled)

    async def _rpc(self, channel, params, extra=None):
        """请求通过 req_id 对应最终响应，ACK 只表示收件，不表示成交。"""
        request_id = uuid4().hex
        payload = {"req_id": request_id, **(extra or {})}
        if params is not None:
            payload["req_param"] = params
        response = await self.transport.request(
            request_id, {"time": int(time()), "channel": channel, "event": "api", "payload": payload}
        )
        if int(response.get("header", {}).get("status", 500)) >= 400:
            raise RuntimeError("Gate rejected API request")
        return response.get("data", {}).get("result", {})

    async def handle_message(self, message):
        """分发请求响应、买卖一、标记价和 V2 深度；断序立刻废弃盘口并重新订阅。"""
        if "request_id" in message:
            if not message.get("ack", False):
                self.transport.resolve(message["request_id"], message)
            return
        if message.get("error"):
            raise RuntimeError("Gate subscription rejected")
        result, channel = message.get("result", {}), message.get("channel")
        if channel == "futures.tickers" and message.get("event") == "update":
            for item in result:
                instrument = self.by_native.get(item["contract"])
                if instrument and item.get("mark_price"):
                    self.marks[instrument.base] = (D(item["mark_price"]), monotonic())
        elif channel == "futures.book_ticker" and message.get("event") == "update":
            instrument = self.by_native.get(result["s"])
            if instrument and result.get("b") and result.get("a"):
                fx = self.settings.fx(instrument.quote)
                self.quotes[instrument.base] = Quote(
                    instrument.price(D(result["b"]), fx),
                    instrument.price(D(result["a"]), fx),
                    exchange_time=float(result.get("t", time() * 1000)) / 1000,
                )
                self.changed.set()
        elif channel == "futures.obu" and "u" in result:
            native = result["s"].split(".")[1]
            instrument = self.by_native.get(native)
            if instrument is None or instrument.base not in self.depth:
                return
            base = instrument.base
            if result.get("full"):
                self.raw_books[base] = {"id": result["u"], "b": {}, "a": {}}
            else:
                previous = self.raw_books.get(base)
                if previous and result["u"] <= previous["id"]:
                    return
                if previous is None or result.get("U") != previous["id"] + 1:
                    self.raw_books.pop(base, None)
                    self.books.pop(base, None)
                    await self._subscription("futures.obu", [self._stream(base)], False)
                    await self._subscription("futures.obu", [self._stream(base)], True)
                    return
            state = self.raw_books[base]
            state["id"] = result["u"]
            for side in ("b", "a"):
                for price, quantity in result.get(side, []):
                    price, quantity = D(price), D(str(quantity))
                    if quantity:
                        state[side][price] = quantity
                    else:
                        state[side].pop(price, None)
                # 只保留订阅深度内的档位，边界外旧档不能作为可靠成交流动性。
                state[side] = dict(sorted(state[side].items(), reverse=side == "b")[:50])
            fx = self.settings.fx(instrument.quote)
            levels = []
            for side in ("b", "a"):
                levels.append(
                    tuple(
                        Level(instrument.price(price, fx), instrument.quantity(quantity))
                        for price, quantity in sorted(state[side].items(), reverse=side == "b")
                    )
                )
            self.books[base] = Book(*levels, exchange_time=float(result.get("t", time() * 1000)) / 1000)
            self.changed.set()

    def validate_order(self, order):
        """通用校验后检查最新标记价允许的限价偏离，不能使用启动时的陈旧标记价。"""
        super().validate_order(order)
        instrument = self.instruments[order.base]
        if instrument.price_deviation:
            mark = self.marks.get(order.base)
            if mark is None or monotonic() - mark[1] > self.settings.max_age * 5:
                raise ValidationError("Missing fresh Gate mark price")
            if abs(self.native_price(order) - mark[0]) > mark[0] * instrument.price_deviation:
                raise ValidationError("Gate price deviation limit")

    def order_payload(self, order):
        """将统一基础币数量变为有符号张数；text 最大 28 字节，保留 UUID 的 26 位。"""
        instrument = self.instruments[order.base]
        qty = instrument.native_quantity(order.quantity)
        return {
            "contract": instrument.native,
            "size": str(qty if order.side == "buy" else -qty),
            "price": format(self.native_price(order), "f"),
            "tif": "ioc",
            "reduce_only": order.reduce_only,
            "text": "t-" + order.client_id[:26],
        }

    def parse_fill(self, order: Order, data: dict) -> Fill:
        """用 size-left 计算真实成交，只有 finished 才可确定 IOC 不会继续成交。"""
        instrument = self.instruments[order.base]
        terminal = data.get("status") == "finished"
        qty = instrument.quantity(abs(D(str(data["size"]))) - abs(D(str(data["left"]))))
        price = instrument.price(D(str(data.get("fill_price") or "0")), self.settings.fx(instrument.quote))
        if qty and price <= 0:
            return Fill(order, OrderStatus.UNKNOWN, terminal=False, reason="Missing execution price")
        status = (
            OrderStatus.FILLED
            if qty == order.quantity
            else OrderStatus.PARTIAL
            if qty
            else OrderStatus.CANCELED
        )
        return Fill(
            order,
            status if terminal else OrderStatus.UNKNOWN,
            qty,
            price,
            qty * price * instrument.taker_fee,
            str(data.get("id", "")),
            terminal,
        )

    async def place_live(self, order):
        """通过登录后的 WSS 发出 IOC 限价单，服务端非终态会交给统一查单逻辑。"""
        return self.parse_fill(order, await self._rpc("futures.order_place", self.order_payload(order)))

    async def query_order(self, order):
        """使用同一个 text ID 查最终结果；查询失败向上抛出，不能认定为零成交。"""
        data = await self._rpc("futures.order_status", {"order_id": "t-" + order.client_id[:26]})
        return self.parse_fill(order, data)

    async def live_position(self, base):
        """低频 REST 查询仓位；Gate WSS 未提供仓位查询请求接口，不能只依赖可能丢失的推送。"""
        path = f"/api/v4/futures/usdt/positions/{self.instruments[base].native}"
        timestamp = str(int(time()))
        digest = hashlib.sha512(b"").hexdigest()
        signature = hmac.new(
            self.settings.gate_secret.encode(),
            f"GET\n{path}\n\n{digest}\n{timestamp}".encode(),
            hashlib.sha512,
        ).hexdigest()
        headers = {
            "KEY": self.settings.gate_key,
            "Timestamp": timestamp,
            "SIGN": signature,
            "X-Gate-Size-Decimal": "1",
        }
        async with self.session.get(
            "https://api.gateio.ws" + path, headers=headers, proxy=self.settings.http_proxy
        ) as response:
            response.raise_for_status()
            data = await response.json()
        if data.get("mode", "single") != "single":
            raise ValidationError("Gate requires single-position mode")
        return self.instruments[base].quantity(D(str(data["size"])))

    async def live_open_orders(self, base):
        """通过 WSS 查询 open 委托；任意遗留挂单都会阻止该标的建立新双腿仓位。"""
        orders = await self._rpc(
            "futures.order_list",
            {
                "contract": self.instruments[base].native,
                "status": "open",
                "limit": 1,
            },
        )
        return bool(orders)
