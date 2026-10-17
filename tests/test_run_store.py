from decimal import Decimal

import duckdb
import pytest

from benji.data.classes import Private, Public, build_prompt
from benji.egress.net import Net, Response
from benji.egress.policy import LocalClient
from benji.store.runs import FakeClient, RecordingClient, ReplayClient, ReplayMiss, RunStore, flush_net


class Quote(Public):
    ticker: str
    price: Decimal


class Position(Private):
    ticker: str
    qty: Decimal


@pytest.fixture
def store():
    return RunStore(duckdb.connect(":memory:"))


def desk(client, names=("AAPL", "MSFT")):
    """A tiny two-node 'graph' used to prove replay: analyst -> allocator per ticker."""
    out = []
    for t in names:
        a = client.complete(build_prompt("analyst", "analyze", f"ticker {t}", [Quote(ticker=t, price=Decimal(100))]))
        b = client.complete(build_prompt("allocator", "size", f"size {t} given {a.text}",
                                         [Position(ticker=t, qty=Decimal(5))]))
        out.append((t, a.text, b.text))
    return out


def test_record_then_replay_is_identical(store):
    fake = FakeClient({"analyze": '{"view":"bullish"}', "size": '{"shares": 3}'})
    run = store.start_run("test")
    first = desk(RecordingClient(fake, store, run))
    store.finish_run(run)

    second = desk(ReplayClient(store, run))
    assert second == first
    calls = store.calls(run)
    assert [c["node"] for c in calls] == ["analyst", "allocator"] * 2
    assert [c["data_class"] for c in calls] == ["public", "private"] * 2
    assert all(c["client"] == "fake" for c in calls)


def test_replay_refuses_a_prompt_the_run_never_saw(store):
    run = store.start_run("test")
    desk(RecordingClient(FakeClient(["x"]), store, run))
    with pytest.raises(ReplayMiss):
        desk(ReplayClient(store, run), names=("NVDA",))


def test_private_prompt_text_is_stored_only_in_the_store_file(store, tmp_path):
    s = RunStore.open(tmp_path / "benji.duckdb")
    run = s.start_run("test")
    RecordingClient(FakeClient(["ok"]), s, run).complete(
        build_prompt("allocator", "s", "u", [Position(ticker="AAPL", qty=Decimal(7))]))
    row = s.con.execute("SELECT prompt_json FROM calls").fetchone()[0]
    assert 'AAPL' in row and '7' in row  # it is there, in the owner's file ...
    s.con.close()  # flush the WAL into the file
    others = [p for p in tmp_path.iterdir() if p.is_file() and not p.name.startswith("benji.duckdb")]
    assert others == []  # ... and nowhere else


def test_net_log_and_egress_summary(store):
    class T:
        def __call__(self, method, url, headers, body, timeout):
            return Response(200, b'{"message":{"content":"ok"}}')

    net = Net(transport=T()).allow("data.example.com")
    LocalClient(net, "m").complete(build_prompt("allocator", "s", "u", [Position(ticker="A", qty=Decimal(1))]))
    net.request("GET", "https://data.example.com/bars", purpose="bars", data_class="public")
    run = store.start_run("test")
    assert flush_net(net, store, run) == 2 and net.records == []
    summary = store.egress_summary(run)
    assert summary["hosts"] == ["data.example.com", "localhost"]
    assert summary["non_local"] == ["data.example.com"] and summary["private_to_remote"] == 0


def test_fake_client_modes():
    seq = FakeClient(["a", "b"])
    p = build_prompt("n", "s", "u")
    assert [seq.complete(p).text for _ in range(3)] == ["a", "b", "b"]
    fn = FakeClient(lambda pr: pr.node.upper())
    assert fn.complete(p).text == "N"
    assert FakeClient().complete(p).json() == {}


def test_cli_audit_and_runs(tmp_path, monkeypatch):
    from datetime import UTC, datetime

    from typer.testing import CliRunner

    from benji.cli import app
    from benji.egress.net import NetRecord

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("BENJI_HOME", str(home))
    s = RunStore.open(home / "benji.duckdb")
    run = s.start_run("daily")
    s.record_net(run, [NetRecord(datetime.now(UTC), "localhost", "chat", "private", 10, 20, 200)])
    s.con.close()
    r = CliRunner().invoke(app, ["egress", "audit", run])
    assert r.exit_code == 0 and "no private data left localhost" in r.output
    assert run in CliRunner().invoke(app, ["runs"]).output

    s = RunStore.open(home / "benji.duckdb")
    s.record_net(run, [NetRecord(datetime.now(UTC), "api.example.com", "chat", "private", 10, 20, 200)])
    s.con.close()
    r = CliRunner().invoke(app, ["egress", "audit", run])
    assert r.exit_code == 1
