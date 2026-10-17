"""Market tools (build plan 1.1): bars, indicators and a snapshot, served from PortfolioPilot's
price cache. Everything here is **Public** — tickers and prices carry nothing about the owner.

Cache first, always. A tool touches a vendor only through an injected `fill` callable (wired
by the CLI to PortfolioPilot's `MarketDataService.sync`) and only for the part of the window
the cache does not cover. With no `fill`, the tools are strictly offline and report `gaps`.

Indicators are computed here (no stockstats): SMA, EMA, RSI(14), MACD(12,26,9), ATR(14),
Bollinger(20,2), 52-week range, and trailing returns.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from datetime import date, timedelta
from decimal import Decimal

from benji.data.classes import Public
from benji.egress.net import Net
from portfoliopilot.ledger.prices import PriceStore

Fill = Callable[[str, date, date], None]  # (ticker, start, end) -> fetch into the store

STALE_TOLERANCE = timedelta(days=4)  # a trailing gap this small is a weekend/holiday, not a miss


class BarRow(Public):
    ts: date
    open: float
    high: float
    low: float
    close: float
    adj_close: float | None
    volume: int


class BarSeries(Public):
    ticker: str
    start: date
    end: date
    bars: list[BarRow]
    gaps: list[tuple[date, date]]  # windows the cache could not serve (empty when fully covered)
    source: str  # "cache" or "cache+fill"

    def closes(self) -> list[float]:
        return [b.close for b in self.bars]


class Indicators(Public):
    ticker: str
    as_of: date
    close: float
    sma_20: float | None
    sma_50: float | None
    sma_200: float | None
    ema_12: float | None
    ema_26: float | None
    rsi_14: float | None
    macd: float | None
    macd_signal: float | None
    macd_hist: float | None
    atr_14: float | None
    bb_upper: float | None
    bb_lower: float | None
    high_52w: float | None
    low_52w: float | None
    ret_1m: float | None
    ret_3m: float | None
    ret_6m: float | None
    ret_12m: float | None
    bars_used: int


class Snapshot(Public):
    ticker: str
    as_of: date
    close: float
    prev_close: float | None
    change_pct: float | None
    volume: int
    avg_volume_20d: float | None
    high_52w: float | None
    low_52w: float | None
    pct_from_52w_high: float | None
    vs_sma_50_pct: float | None
    vs_sma_200_pct: float | None
    stale_days: int  # calendar days between as_of and `today`


# ---- pure math ---------------------------------------------------------------------------

def sma(xs: list[float], n: int) -> float | None:
    return sum(xs[-n:]) / n if len(xs) >= n else None


def ema_series(xs: list[float], n: int) -> list[float]:
    if len(xs) < n:
        return []
    k = 2 / (n + 1)
    out = [sum(xs[:n]) / n]
    for x in xs[n:]:
        out.append(x * k + out[-1] * (1 - k))
    return out


def rsi(xs: list[float], n: int = 14) -> float | None:
    if len(xs) < n + 1:
        return None
    gains, losses = [], []
    for a, b in zip(xs[-n - 1:-1], xs[-n:], strict=True):
        d = b - a
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    avg_gain, avg_loss = sum(gains) / n, sum(losses) / n
    if avg_loss == 0:
        return 100.0
    return 100 - 100 / (1 + avg_gain / avg_loss)


def macd(xs: list[float], fast: int = 12, slow: int = 26, signal: int = 9
         ) -> tuple[float | None, float | None, float | None]:
    if len(xs) < slow + signal:
        return None, None, None
    ef, es = ema_series(xs, fast), ema_series(xs, slow)
    line = [f - s for f, s in zip(ef[len(ef) - len(es):], es, strict=True)]
    sig = ema_series(line, signal)
    if not sig:
        return None, None, None
    return line[-1], sig[-1], line[-1] - sig[-1]


def atr(bars: list[BarRow], n: int = 14) -> float | None:
    if len(bars) < n + 1:
        return None
    trs = []
    for prev, cur in zip(bars[-n - 1:-1], bars[-n:], strict=True):
        trs.append(max(cur.high - cur.low, abs(cur.high - prev.close), abs(cur.low - prev.close)))
    return sum(trs) / n


def bollinger(xs: list[float], n: int = 20, k: float = 2.0) -> tuple[float | None, float | None]:
    if len(xs) < n:
        return None, None
    w = xs[-n:]
    m = sum(w) / n
    sd = math.sqrt(sum((x - m) ** 2 for x in w) / n)
    return m + k * sd, m - k * sd


def trailing_return(bars: list[BarRow], days: int) -> float | None:
    """Return from the last close on or before `as_of - days` to the latest close."""
    if not bars:
        return None
    cutoff = bars[-1].ts - timedelta(days=days)
    base = [b for b in bars if b.ts <= cutoff]
    if not base:
        return None
    prior = base[-1].adj_close or base[-1].close
    last = bars[-1].adj_close or bars[-1].close
    return last / prior - 1 if prior else None


# ---- the tools ---------------------------------------------------------------------------

class MarketTools:
    def __init__(self, store: PriceStore, net: Net | None = None, fill: Fill | None = None,
                 vendor_host: str = "query2.finance.yahoo.com", today: date | None = None):
        self.store = store
        self.net = net
        self.fill = fill
        self.vendor_host = vendor_host
        self.today = today or date.today()  # noqa: DTZ011 — calendar date, not an instant
        self.fills: list[tuple[str, date, date]] = []

    def _to_row(self, b) -> BarRow:
        return BarRow(ts=b.ts, open=float(b.open), high=float(b.high), low=float(b.low),
                      close=float(b.close), adj_close=float(b.adj_close) if b.adj_close is not None else None,
                      volume=int(b.volume))

    def _gaps(self, ticker: str, start: date, end: date) -> list[tuple[date, date]]:
        cov = self.store.coverage(ticker)
        gaps = []
        for lo, hi in cov.missing(start, end):
            if cov.last is not None and lo > cov.last and hi - cov.last <= STALE_TOLERANCE:
                continue  # weekend / holiday tail
            gaps.append((lo, hi))
        return gaps

    def get_bars(self, ticker: str, start: date, end: date) -> BarSeries:
        ticker = ticker.upper()
        gaps = self._gaps(ticker, start, end)
        source = "cache"
        if gaps and self.fill is not None:
            for lo, hi in gaps:
                if self.net is not None:
                    self.net.note(self.vendor_host, purpose=f"bars {ticker} {lo}..{hi}")
                self.fill(ticker, lo, hi)
                self.fills.append((ticker, lo, hi))
            gaps = self._gaps(ticker, start, end)
            source = "cache+fill"
        rows = [self._to_row(b) for b in self.store.bars(ticker, start, end)]
        return BarSeries(ticker=ticker, start=start, end=end, bars=rows, gaps=gaps, source=source)

    def get_indicators(self, ticker: str, as_of: date | None = None, lookback_days: int = 400
                       ) -> Indicators | None:
        as_of = as_of or self.today
        series = self.get_bars(ticker, as_of - timedelta(days=lookback_days), as_of)
        bars = series.bars
        if not bars:
            return None
        xs = series.closes()
        year = [b for b in bars if b.ts > as_of - timedelta(days=365)]
        m, sig, hist = macd(xs)
        up, lo = bollinger(xs)
        e12, e26 = ema_series(xs, 12), ema_series(xs, 26)
        return Indicators(
            ticker=series.ticker, as_of=bars[-1].ts, close=xs[-1],
            sma_20=sma(xs, 20), sma_50=sma(xs, 50), sma_200=sma(xs, 200),
            ema_12=e12[-1] if e12 else None, ema_26=e26[-1] if e26 else None,
            rsi_14=rsi(xs), macd=m, macd_signal=sig, macd_hist=hist, atr_14=atr(bars),
            bb_upper=up, bb_lower=lo,
            high_52w=max(b.high for b in year) if year else None,
            low_52w=min(b.low for b in year) if year else None,
            ret_1m=trailing_return(bars, 30), ret_3m=trailing_return(bars, 91),
            ret_6m=trailing_return(bars, 182), ret_12m=trailing_return(bars, 365),
            bars_used=len(bars),
        )

    def get_snapshot(self, ticker: str, as_of: date | None = None) -> Snapshot | None:
        as_of = as_of or self.today
        ind = self.get_indicators(ticker, as_of)
        if ind is None:
            return None
        bars = self.get_bars(ticker, as_of - timedelta(days=400), as_of).bars
        prev = bars[-2].close if len(bars) >= 2 else None
        vols = [b.volume for b in bars[-20:]]
        pct = lambda a, b: (a / b - 1) if (a is not None and b) else None  # noqa: E731
        return Snapshot(
            ticker=ind.ticker, as_of=ind.as_of, close=ind.close, prev_close=prev,
            change_pct=pct(ind.close, prev), volume=bars[-1].volume,
            avg_volume_20d=sum(vols) / len(vols) if vols else None,
            high_52w=ind.high_52w, low_52w=ind.low_52w,
            pct_from_52w_high=pct(ind.close, ind.high_52w),
            vs_sma_50_pct=pct(ind.close, ind.sma_50), vs_sma_200_pct=pct(ind.close, ind.sma_200),
            stale_days=(as_of - ind.as_of).days,
        )


def decimal_bar(ticker: str, ts: date, close: float, *, volume: int = 1_000, spread: float = 0.01):
    """Build a portfoliopilot Bar for tests and fixtures."""
    from portfoliopilot.market_data.base import Bar

    d = Decimal(str(round(close, 4)))
    return Bar(ticker=ticker, ts=ts, open=d, high=d * Decimal(str(1 + spread)),
               low=d * Decimal(str(1 - spread)), close=d, volume=volume, source="test", adj_close=d)
