"""SQLite 持久化价差采样；多进程共用 WAL 数据库，磁盘写入移出事件循环。"""

import asyncio
import json
import sqlite3


class SpreadStore:
    """保存逐时间桶原始价差及窗口统计，独立于 Redis 有界状态缓存。"""

    def __init__(self, settings):
        """记录数据库路径与运行命名空间，构造时不打开连接或进行磁盘 IO。"""
        self.path = settings.spread_db_path
        self.namespace = settings.namespace
        self.mode = settings.mode
        self.interval = settings.spread_sample_seconds

    async def save(self, records):
        """同一轮所有标的合成一个事务，在工作线程写入；失败传递给运行器。"""
        if records:
            await asyncio.to_thread(self._save, records)

    def _save(self, records):
        """每次批量写入使用独立连接；唯一键去重，完整市场身份避免 DEX 相互覆盖。

        WAL 允许并发读取；busy_timeout 等待其他组合的短写事务。历史不自动删除，
        便于离线统计；启动交易仍重新预热，不直接信任过去运行的历史数据。
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=30)
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("""CREATE TABLE IF NOT EXISTS spread_samples (
                namespace TEXT NOT NULL, mode TEXT NOT NULL,
                left_exchange TEXT NOT NULL, right_exchange TEXT NOT NULL, base TEXT NOT NULL,
                sample_seconds REAL NOT NULL, sample_at REAL NOT NULL,
                spread_bps TEXT NOT NULL, mean_bps TEXT NOT NULL, min_bps TEXT NOT NULL,
                max_bps TEXT NOT NULL, range_bps TEXT NOT NULL, samples INTEGER NOT NULL,
                recorded_at REAL NOT NULL, payload TEXT NOT NULL,
                PRIMARY KEY(namespace, mode, left_exchange, right_exchange, base, sample_seconds, sample_at)
            )""")
            with connection:
                connection.executemany(
                    """INSERT INTO spread_samples VALUES
                    (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT DO NOTHING""",
                    [
                        (
                            self.namespace,
                            self.mode,
                            row["left"],
                            row["right"],
                            row["base"],
                            self.interval,
                            row["sample_at"],
                            str(row["spread_bps"]),
                            str(row["mean_bps"]),
                            str(row["min_bps"]),
                            str(row["max_bps"]),
                            str(row["range_bps"]),
                            row["samples"],
                            row["timestamp"],
                            json.dumps(row, default=str),
                        )
                        for row in records
                    ],
                )
        finally:
            connection.close()
