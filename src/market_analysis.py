"""入场前资金费预算和对齐的 15 分钟 K 线价差分析。"""

from math import floor, isfinite
from statistics import mean
from time import monotonic, time

from models import BPS, ZERO, D


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


def spread_change_ratio(values):
    """价差相对变化幅度 = 极差 / 平均绝对价差，返回比率而非百分数。

    使用绝对值均值避免正负价差相互抵消；分母至少为 1 bps，防止接近零时
    微小噪声被放大。它衡量窗口波动幅度，不代表单位时间速度或收敛概率。
    """
    return (max(values) - min(values)) / max(mean(abs(value) for value in values), D(1))


def candle_spread_stats(base, left, right, settings, now=None):
    """按 UTC 桶对齐两所已收盘 K 线，用收盘价计算历史价差，不拼接异步高低点。

    只取最近配置根数内的交集，要求最新完整桶及至少 min_bars 根连续数据。
    缺口不填充、未收盘不参与；否则不能证明两所对应的是同一历史区间。
    """
    now = time() if now is None else now
    end = int(now) // 900 * 900
    cutoff = end - settings.candle_lookback_bars * 900
    a, b = left.candles.get(base, {}), right.candles.get(base, {})
    stamps = sorted(
        stamp
        for stamp in a.keys() & b.keys()
        if cutoff <= stamp < end and a[stamp].valid() and b[stamp].valid()
    )
    if len(stamps) < settings.candle_min_bars or not stamps or stamps[-1] != end - 900:
        return None
    recent = stamps[-settings.candle_min_bars :]
    if any(y - x != 900 for x, y in zip(recent, recent[1:], strict=False)):
        return None
    values = [(a[stamp].close / b[stamp].close - 1) * BPS for stamp in stamps]
    return {
        "interval": "15m",
        "left": left.name,
        "right": right.name,
        "base": base,
        "first_at": stamps[0],
        "last_at": stamps[-1],
        "samples": len(values),
        "mean_bps": mean(values),
        "min_bps": min(values),
        "max_bps": max(values),
        "range_bps": max(values) - min(values),
        "change_ratio": spread_change_ratio(values),
    }


def reverse_premium(premium):
    """把左/右溢价准确转为右/左溢价，避免非零中枢直接取负造成误差。"""
    return -premium / (1 + premium / BPS)
