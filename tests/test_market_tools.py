import math
from datetime import date, timedelta

import duckdb
import pytest

from benji.data.classes import classify
from benji.egress.net import EgressViolation, Net
from benji.tools.market import (
    MarketTools,
    bollinger,
    decimal_bar,
    ema_series,
    macd,
    rsi,
    sma,
    trailing_return,
)
from portfoliopilot.ledger.prices import PriceStore

TODAY = date(2026, 10, 16)  # a Friday


def weekdays(end: date, n: int) -> list[date]:
    out, d = [], end
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d -= timedelta(days=1)
    return list(reversed(out))


@pytest.fixture
def store():
    s = PriceStore(duckdb.connect(":memory:"))
    days = weekdays(TODAY, 300)
    # a gentle uptrend with a wobble, so indicators are non-degenerate
    s.upsert([decimal_bar("AAPL", d, 100 + i * 0.5 + 3 * math.sin(i / 7), volume=1000 + i)
              for i, d in enumerate(days)])
    return s




def test_bars_come_from_cache_with_zero_network(store):
    net = Net().allow("query2.finance.yahoo.com")
    calls = []
    tools = MarketTools(store, net=net, fill=lambda t, a, b: calls.append((t, a, b)), today=TODAY)
    s = tools.get_bars("aapl", TODAY - timedelta(days=200), TODAY)
    assert s.ticker == "AAPL" and s.gaps == [] and s.source == "cache"
    assert calls == [] and net.records == []  # exit gate: cache covered the window, no fetch
    assert classify(s) == "public"


def test_gap_triggers_fill_and_is_audited(store):
    net = Net().allow("query2.finance.yahoo.com")
    first = weekdays(TODAY, 300)[0]

    def fill(t, lo, hi):
        store.upsert([decimal_bar(t, d, 50.0) for d in weekdays(first - timedelta(days=1), 10) if d >= lo])

    tools = MarketTools(store, net=net, fill=fill, today=TODAY)
    s = tools.get_bars("AAPL", first - timedelta(days=14), TODAY)
    assert s.source == "cache+fill" and tools.fills and s.gaps == []
    assert [r.host for r in net.records] == ["query2.finance.yahoo.com"]
    assert net.records[0].data_class == "public"


def test_offline_tools_report_gaps_instead_of_fetching(store):
    tools = MarketTools(store, today=TODAY)  # no fill, no net
    s = tools.get_bars("AAPL", date(2020, 1, 1), TODAY)
    assert s.source == "cache" and len(s.gaps) == 1 and s.gaps[0][0] == date(2020, 1, 1)
    assert tools.get_bars("ZZZZ", TODAY - timedelta(days=30), TODAY).bars == []


def test_weekend_tail_is_not_a_gap(store):
    tools = MarketTools(store, today=TODAY + timedelta(days=2))  # Sunday
    assert tools.get_bars("AAPL", TODAY - timedelta(days=30), TODAY + timedelta(days=2)).gaps == []


def test_note_obeys_allow_list_and_data_class():
    net = Net()
    with pytest.raises(EgressViolation):
        net.note("query2.finance.yahoo.com", purpose="bars")
    net.allow("query2.finance.yahoo.com")
    with pytest.raises(EgressViolation):
        net.note("query2.finance.yahoo.com", purpose="bars", data_class="private")
    net.note("query2.finance.yahoo.com", purpose="bars")
    assert net.non_local_hosts() == {"query2.finance.yahoo.com"}


def test_indicator_math_against_hand_values():
    xs = [float(i) for i in range(1, 31)]  # 1..30
    assert sma(xs, 10) == 25.5
    assert sma(xs, 31) is None
    assert ema_series(xs, 3)[0] == 2.0 and abs(ema_series(xs, 3)[-1] - 29.0) < 1e-9
    assert rsi(xs) == 100.0  # monotone up
    assert rsi(list(reversed(xs))) == 0.0
    assert rsi([1.0] * 5) is None
    up, lo = bollinger([10.0] * 20)
    assert up == 10.0 and lo == 10.0
    m, s, h = macd([float(i) for i in range(1, 61)])
    assert m is not None and s is not None and abs(h - (m - s)) < 1e-12
    assert macd(xs) == (None, None, None)  # 30 points < 26 + 9


def test_indicators_and_snapshot(store):
    tools = MarketTools(store, today=TODAY)
    ind = tools.get_indicators("AAPL")
    assert ind is not None and ind.as_of == TODAY and ind.bars_used > 250
    assert ind.sma_200 is not None and ind.sma_200 < ind.sma_50 < ind.close + 10  # uptrend
    assert 0 <= ind.rsi_14 <= 100 and ind.atr_14 > 0
    assert ind.low_52w <= ind.close <= ind.high_52w * 1.0001
    assert ind.ret_12m is not None and ind.ret_12m > 0.5
    assert classify(ind) == "public"

    snap = tools.get_snapshot("AAPL")
    assert snap is not None and snap.stale_days == 0 and snap.prev_close is not None
    assert abs(snap.change_pct - (snap.close / snap.prev_close - 1)) < 1e-12
    assert snap.pct_from_52w_high <= 0 and snap.avg_volume_20d > 1000
    assert tools.get_snapshot("NOPE") is None


def test_trailing_return_uses_adjusted_close():
    bars = [decimal_bar("X", d, 100.0) for d in weekdays(TODAY, 40)]
    from benji.tools.market import BarRow
    rows = [BarRow(ts=b.ts, open=100, high=101, low=99, close=100, adj_close=float(b.adj_close) * (1.1 if i == len(bars) - 1 else 1), volume=1)
            for i, b in enumerate(bars)]
    assert abs(trailing_return(rows, 30) - 0.1) < 1e-9
    assert trailing_return(rows, 365) is None


def test_cli_snapshot(tmp_path, monkeypatch, store):
    from typer.testing import CliRunner

    from benji.cli import app

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("BENJI_HOME", str(home))
    disk = PriceStore.open(home / "prices.duckdb")
    disk.upsert(store.bars("AAPL", date(2020, 1, 1), TODAY))
    disk.con.close()
    r = CliRunner().invoke(app, ["market", "snapshot", "aapl", "--as-of", TODAY.isoformat()])
    assert r.exit_code == 0, r.output
    assert '"ticker": "AAPL"' in r.output and "egress" not in r.output
    r = CliRunner().invoke(app, ["market", "indicators", "aapl", "--as-of", TODAY.isoformat()])
    assert r.exit_code == 0 and '"rsi_14"' in r.output
