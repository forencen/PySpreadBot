"""入场前资金费预算和按固定时间采样的跨所价差历史。"""

from collections import deque
from math import floor, isfinite
from statistics import mean
from time import monotonic, time

from models import BPS, ZERO


def funding_cost_per_unit(base, buy, sell, settings, now=None):
    """估计持仓窗口内每单位基础币的净资金费支出，正值为成本、负值为收入。

    正费率由多头支付空头；按每所的标记价和结算日程计算，不把不同周期的
    费率直接相减。未来费率/标记价假定不变，这是入场预算，不是已实现资金费。
    缺失、非有限、陈旧数据返回 None；小时数为零表示显式关闭过滤。
    """
    if settings.funding_horizon_hours == 0:
        return ZERO
    now = time() if now is None else now
    end = now + float(settings.funding_horizon_hours * 3600)
    cost = ZERO
    for venue, sign in ((buy, 1), (sell, -1)):
        data = venue.funding.get(base)
        if data is None or not data.fresh(settings.funding_max_age):
            return None
        if venue.name == "gate" and monotonic() - venue.metadata_at > settings.funding_schedule_max_age:
            return None
        interval, next_at = data.interval_seconds, data.next_settlement
        if not isfinite(interval) or not isfinite(next_at) or interval <= 0 or next_at <= 0:
            return None
        # 已经过边界时沿最新已知周期向前滚动，不重复计算刚结束的结算。
        if next_at <= now:
            next_at += (floor((now - next_at) / interval) + 1) * interval
        count = max(0, floor((end - next_at) / interval) + 1) if next_at <= end else 0
        cost += sign * data.mark_price * data.rate * count
    return cost


class SpreadHistory:
    """每个有序交易所组合/基础币独立保存等时间权重样本，不按消息数量加权。"""

    def __init__(self, settings):
        """保存滑动窗口参数；历史只在当前进程有效，重启重新预热。"""
        self.settings = settings
        self.rows = {}
        self.pending = {}

    def observe(self, key, premium, now=None):
        """每个时间桶只采一份新鲜报价；进入后续桶才把上一桶纳入基准。

        不补齐断线期间的数据，也不把当前触发信号的样本用于自己的历史基准。
        返回新完成的样本统计，调用方按采样频率持久化，而不是逐行情写 Redis。
        """
        now = time() if now is None else now
        interval = self.settings.spread_sample_seconds
        bucket = floor(now / interval) * interval
        rows = self.rows.setdefault(key, deque())
        while rows and rows[0][0] < now - self.settings.spread_window_seconds:
            rows.popleft()
        previous = self.pending.get(key)
        record = None
        if previous is not None and previous[0] < bucket:
            if previous[0] >= now - self.settings.spread_window_seconds:
                rows.append(previous)
                values = [value for _, value in rows]
                record = {
                    "sample_at": previous[0],
                    "spread_bps": previous[1],
                    "mean_bps": mean(values),
                    "min_bps": min(values),
                    "max_bps": max(values),
                    "samples": len(values),
                    "timestamp": now,
                }
        if previous is None or previous[0] != bucket:
            self.pending[key] = (bucket, premium)
        return record

    def baseline(self, key, now=None):
        """返回足够且连续到近期的历史均值；样本不足或长时间断流时拒绝入场。"""
        now = time() if now is None else now
        rows = self.rows.get(key, ())
        values = [value for stamp, value in rows if stamp >= now - self.settings.spread_window_seconds]
        if len(values) < self.settings.spread_min_samples or not rows:
            return None
        if now - rows[-1][0] > self.settings.spread_sample_seconds * 3:
            return None
        return mean(values)


def reverse_premium(premium):
    """把左/右溢价准确转为右/左溢价，避免非零中枢直接取负造成误差。"""
    return -premium / (1 + premium / BPS)
