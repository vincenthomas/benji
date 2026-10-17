"""`benji` command line. Milestone 0.1: init, config, version. Agents arrive in Milestone 2."""

from __future__ import annotations

from pathlib import Path

import typer

from benji import __version__
from benji.home import HomeError, init, read_watchlist, resolve

app = typer.Typer(help="Benji — an agentic investment desk on your laptop. Agents propose; you approve.")

HOME_OPTION = typer.Option(None, "--home", envvar="BENJI_HOME",
                           help="Benji data folder (default: remembered by `benji init`, else ~/Benji)")
ALLOW_IN_REPO = typer.Option(False, help="Permit a data folder inside a git working tree (not advised)")


@app.command()
def version() -> None:
    typer.echo(f"benji {__version__}")


@app.command("init")
def init_cmd(home: Path | None = HOME_OPTION, allow_in_repo: bool = ALLOW_IN_REPO) -> None:
    """Create the data folder (config, runs/, watchlist) and remember where it is."""
    target = resolve(home).path
    try:
        h = init(target, allow_in_repo=allow_in_repo)
    except HomeError as exc:
        typer.echo(f"refused: {exc}")
        raise typer.Exit(1) from None
    typer.echo(f"Benji home: {h.path}")
    typer.echo(f"  config    {h.config_file}")
    typer.echo(f"  watchlist {h.watchlist_file}")
    typer.echo(f"  runs      {h.runs_dir}/")
    typer.echo(f"  household {h.household_file}  (not created: copy your PortfolioPilot file here)")
    typer.echo("Nothing in this folder is ever sent anywhere. Keep it out of any repository and any sync.")


config_app = typer.Typer(help="Show or check the configuration.")
app.add_typer(config_app, name="config")


@config_app.command("show")
def config_show(home: Path | None = HOME_OPTION) -> None:
    h = resolve(home)
    if not h.exists():
        typer.echo(f"no Benji home at {h.path}; run `benji init`")
        raise typer.Exit(1)
    typer.echo(f"# {h.config_file}")
    typer.echo(h.config_file.read_text())


@config_app.command("check")
def config_check(home: Path | None = HOME_OPTION) -> None:
    """Validate config.yaml (tier rules, limits) and report the watchlist."""
    h = resolve(home)
    cfg = h.config  # raises on invalid
    wl = read_watchlist(h)
    all_local = all(t == "local" for t in cfg.models.nodes.values())
    typer.echo(f"config ok: {len(cfg.models.nodes)} nodes, all local={all_local}, "
               f"hosted_enabled={cfg.models.hosted_enabled}, shorts={cfg.limits.allow_shorts}")
    typer.echo(f"watchlist: {len(wl)} tickers")


egress_app = typer.Typer(help="Egress policy: what left the laptop, and to where.")
app.add_typer(egress_app, name="egress")


@egress_app.command("policy")
def egress_policy(home: Path | None = HOME_OPTION) -> None:
    """Print the effective egress rules for this configuration."""
    cfg = resolve(home).config
    typer.echo("Private data (positions, lots, basis, cash, tax profile, approvals, fills, memos):")
    typer.echo("  -> local models only (localhost). A hosted client refuses it in code.")
    typer.echo(f"Hosted tier enabled: {cfg.models.hosted_enabled}")
    typer.echo(f"Local endpoint: {cfg.models.local_endpoint}  model: {cfg.models.local_model}")
    typer.echo("Allow-listed hosts: localhost plus configured market-data vendors (run-time list).")
    typer.echo("Audit a run: benji egress audit <run-id>")


if __name__ == "__main__":
    app()


RUN_ID_ARG = typer.Argument(..., help="Run id as listed by `benji runs`")


@egress_app.command("audit")
def egress_audit(run_id: str = RUN_ID_ARG, home: Path | None = HOME_OPTION) -> None:
    """Every host a stored run contacted, from the persisted net log. Exit 1 if Private data went remote."""
    from benji.store.runs import RunStore

    h = resolve(home)
    if not h.db_file.exists():
        typer.echo(f"no store at {h.db_file}", err=True)
        raise typer.Exit(2)
    store = RunStore.open(h.db_file)
    s = store.egress_summary(run_id)
    typer.echo(f"run {run_id}: {s['calls']} network calls, hosts: {', '.join(s['hosts']) or '(none)'}")
    for ts, host, purpose, dc, out, inn, status in store.net_log(run_id):
        typer.echo(f"  {ts:%H:%M:%S} {host:30} {dc:7} {purpose:20} out={out} in={inn} status={status}")
    if s["private_to_remote"]:
        typer.echo(f"VIOLATION: {s['private_to_remote']} private payload(s) sent to a non-local host",
                   err=True)
        raise typer.Exit(1)
    typer.echo("non-local hosts: " + (", ".join(s["non_local"]) or "none")
               + " — no private data left localhost")


@app.command("runs")
def runs_cmd(home: Path | None = HOME_OPTION, limit: int = 20) -> None:
    """List stored runs, newest first."""
    from benji.store.runs import RunStore

    h = resolve(home)
    if not h.db_file.exists():
        typer.echo("no runs yet")
        return
    for run_id, started, mode, status, _ in RunStore.open(h.db_file).runs(limit):
        typer.echo(f"{run_id:40} {started:%Y-%m-%d %H:%M} {mode:8} {status}")


market_app = typer.Typer(help="Market tools (Public data) served from the price cache.")
app.add_typer(market_app, name="market")

TICKER_ARG = typer.Argument(..., help="Ticker symbol")
AS_OF_OPTION = typer.Option(None, "--as-of", help="YYYY-MM-DD (default today)")
FILL_OPTION = typer.Option(False, "--fill", help="Fetch missing bars from the vendor via PortfolioPilot")


def _market(home: Path | None, fill: bool):
    from benji.egress.net import Net
    from benji.tools.market import MarketTools
    from portfoliopilot.ledger.prices import PriceStore

    h = resolve(home)
    if not h.prices_db.exists() and not fill:
        typer.echo(f"no price cache at {h.prices_db}; run with --fill or set prices_db in config", err=True)
        raise typer.Exit(2)
    store = PriceStore.open(h.prices_db)
    net = Net().allow("query1.finance.yahoo.com", "query2.finance.yahoo.com")
    filler = None
    if fill:
        from portfoliopilot.market_data.adapters.yfinance_adapter import YFinanceAdapter
        from portfoliopilot.market_data.service import MarketDataService

        svc = MarketDataService(YFinanceAdapter(), store)
        filler = lambda t, lo, hi: svc.sync(t, lo, hi)  # noqa: E731
    return MarketTools(store, net=net, fill=filler), net


@market_app.command("snapshot")
def market_snapshot(ticker: str = TICKER_ARG, as_of: str | None = AS_OF_OPTION,
                    fill: bool = FILL_OPTION, home: Path | None = HOME_OPTION) -> None:
    """Latest close, change, 52-week range and trend context for one ticker."""
    from datetime import date

    tools, net = _market(home, fill)
    snap = tools.get_snapshot(ticker, date.fromisoformat(as_of) if as_of else None)
    if snap is None:
        typer.echo(f"no bars for {ticker.upper()} in the cache", err=True)
        raise typer.Exit(1)
    typer.echo(snap.model_dump_json(indent=2))
    if net.records:
        typer.echo(f"egress: {sorted(net.hosts_contacted())}", err=True)


@market_app.command("indicators")
def market_indicators(ticker: str = TICKER_ARG, as_of: str | None = AS_OF_OPTION,
                      fill: bool = FILL_OPTION, home: Path | None = HOME_OPTION) -> None:
    """Moving averages, RSI, MACD, ATR, Bollinger bands, trailing returns."""
    from datetime import date

    tools, _ = _market(home, fill)
    ind = tools.get_indicators(ticker, date.fromisoformat(as_of) if as_of else None)
    if ind is None:
        typer.echo(f"no bars for {ticker.upper()} in the cache", err=True)
        raise typer.Exit(1)
    typer.echo(ind.model_dump_json(indent=2))
