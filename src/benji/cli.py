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
