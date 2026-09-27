"""低频合约规则缓存，采用原子替换，避免多进程写入产生半个 JSON 文件。"""

import asyncio
import json
import os
import tempfile
from dataclasses import asdict
from pathlib import Path
from time import time

from models import Instrument


class ContractCache:
    """保存规范化规则及交易所原始响应，便于排查单位、限额与下架状态。"""

    def __init__(self, directory: Path):
        """记录缓存目录；延迟创建目录，单纯导入模块不会写磁盘。"""
        self.directory = directory

    async def save(self, exchange: str, instruments: list[Instrument], raw: object) -> None:
        """把文件 IO 移出事件循环；仅成功获取的新规则可覆盖缓存，不使用过期缓存交易。"""
        payload = {
            "version": 1,
            "updated_at": time(),
            "instruments": [asdict(i) for i in instruments],
            "raw": raw,
        }
        await asyncio.to_thread(self._write, exchange, payload)

    def _write(self, exchange: str, payload: dict) -> None:
        """在同目录创建临时文件并原子重命名；finally 清除失败写入的临时文件。"""
        self.directory.mkdir(parents=True, exist_ok=True)
        exchange = exchange.replace(":", "__")
        fd, name = tempfile.mkstemp(dir=self.directory, prefix=f".{exchange}-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump(payload, stream, default=str, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.directory / f"{exchange}.json")
        finally:
            if os.path.exists(name):
                os.unlink(name)
