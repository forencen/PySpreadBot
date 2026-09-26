"""可重连的异步 WSS 传输：单读循环分发推送及请求响应，不自动重发订单。"""

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress

import aiohttp

log = logging.getLogger(__name__)


class Disconnected(ConnectionError):
    """连接中断或请求结果未知，调用方必须对账而非自动重试下单。"""


class WebSocketTransport:
    """一个交易所连接复用请求和行情；pending 按交易所响应 ID 关联 Future。"""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        url: str,
        handler: Callable,
        connected: Callable[[], Awaitable],
        invalidated: Callable,
        timeout: float,
        headers: dict | None = None,
        proxy: str | None = None,
    ):
        """注入解析回调和重连回调，传输层不理解交易所消息格式。"""
        self.session, self.url = session, url
        self.handler, self.connected, self.invalidated = handler, connected, invalidated
        self.timeout, self.headers = timeout, headers
        self.proxy = proxy
        self.pending: dict[str, asyncio.Future] = {}
        self.ws = None
        self.task = None
        self.ready = asyncio.Event()
        self.stopping = False

    async def start(self) -> None:
        """启动后台重连循环；初次连接必须在有限时间内完成，失败由上层退出。"""
        self.task = asyncio.create_task(self._run())
        await asyncio.wait_for(self.ready.wait(), self.timeout * 3)

    async def send(self, payload: dict) -> None:
        """仅发送一次消息；网络异常向上抛出，绝不透明重放金融操作。"""
        if self.ws is None or self.ws.closed:
            raise Disconnected("WebSocket unavailable")
        await self.ws.send_json(payload)

    async def request(self, request_id: str, payload: dict) -> dict:
        """注册等待者后发送，超时清理 Future；迟到响应不会匹配新订单。"""
        future = asyncio.get_running_loop().create_future()
        self.pending[str(request_id)] = future
        try:
            await self.send(payload)
            return await asyncio.wait_for(future, self.timeout)
        finally:
            self.pending.pop(str(request_id), None)

    def resolve(self, request_id: str, data: dict) -> None:
        """由交易所解析器调用，将最终响应交给对应请求；ACK 不应调用此方法。"""
        future = self.pending.get(str(request_id))
        if future is not None and not future.done():
            future.set_result(data)

    async def _read(self) -> None:
        """顺序处理收到的帧，行情队列不积压；解析失败会触发整条连接失效重建。"""
        async for message in self.ws:
            if message.type == aiohttp.WSMsgType.TEXT:
                await self.handler(json.loads(message.data))
            elif message.type in {aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSE}:
                break
        raise Disconnected("WebSocket closed")

    async def _run(self) -> None:
        """指数退避重连，每次断线立即清空行情及 pending，再重新登录和订阅。"""
        delay = 1
        while not self.stopping:
            reader = None
            try:
                async with self.session.ws_connect(
                    self.url,
                    headers=self.headers,
                    heartbeat=15,
                    max_msg_size=8 * 1024 * 1024,
                    proxy=self.proxy,
                ) as ws:
                    self.ws = ws
                    reader = asyncio.create_task(self._read())
                    await self.connected()
                    self.ready.set()
                    delay = 1
                    await reader
            except asyncio.CancelledError:
                raise
            except Exception as error:
                log.warning("WSS reconnect (%s)", type(error).__name__)
            finally:
                self.ready.clear()
                self.invalidated()
                self.ws = None
                for future in self.pending.values():
                    if not future.done():
                        future.set_exception(Disconnected("Connection lost; reconcile order"))
                if reader is not None:
                    reader.cancel()
                    with suppress(asyncio.CancelledError, Exception):
                        await reader
            if not self.stopping:
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30)

    async def close(self) -> None:
        """取消读循环和重连循环，等待清理完成，防止退出后残留连接。"""
        self.stopping = True
        if self.task:
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task
