import subprocess
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from benji.cli import app
from benji.config import BenjiConfig, ModelTiers
from benji.home import HomeError, init, inside_git_tree, read_watchlist, resolve


def test_default_config_is_local_only_and_shorts_off():
    c = BenjiConfig()
    assert not c.models.hosted_enabled
    assert all(t == "local" for t in c.models.nodes.values())
    assert not c.limits.allow_shorts


def test_private_nodes_cannot_be_hosted():
    with pytest.raises(ValueError, match="allocator"):
        ModelTiers(hosted_enabled=True, nodes={"allocator": "hosted-public"})
    with pytest.raises(ValueError, match="hosted_enabled is false"):
        ModelTiers(nodes={"bull_researcher": "hosted-public"})
    ok = ModelTiers(hosted_enabled=True, nodes={"bull_researcher": "hosted-public", "allocator": "local"})
    assert ok.nodes["bull_researcher"] == "hosted-public"


def test_config_round_trip(tmp_path):
    p = tmp_path / "config.yaml"
    c = BenjiConfig()
    c.limits.max_single_name_pct = 0.08
    c.save(p)
    assert BenjiConfig.load(p) == c
    assert "household" not in yaml.safe_load(p.read_text()).get("limits", {})  # config holds no book data


def test_init_creates_layout_and_pointer(tmp_path):
    pointer = tmp_path / "cfg" / "home"
    home = init(tmp_path / "benji-data", pointer=pointer)
    assert home.config_file.exists() and home.runs_dir.is_dir() and home.watchlist_file.exists()
    assert pointer.read_text() == str(tmp_path / "benji-data")
    # idempotent: a second init keeps an edited config
    home.config_file.write_text(home.config_file.read_text().replace("screener_top_n: 5", "screener_top_n: 7"))
    init(tmp_path / "benji-data", pointer=pointer)
    assert home.config.universe.screener_top_n == 7


def test_init_refuses_a_folder_inside_a_git_tree(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    assert inside_git_tree(repo / "sub" / "deeper") == repo.resolve()
    with pytest.raises(HomeError, match="inside the git working tree"):
        init(repo / "benji-data", pointer=tmp_path / "ptr")
    init(repo / "benji-data", pointer=tmp_path / "ptr", allow_in_repo=True)  # explicit override works


def test_resolution_order(tmp_path):
    pointer = tmp_path / "ptr"
    pointer.write_text(str(tmp_path / "from-pointer"))
    assert resolve(None, env={}, pointer=pointer).path == tmp_path / "from-pointer"
    assert resolve(None, env={"BENJI_HOME": str(tmp_path / "from-env")}, pointer=pointer).path == tmp_path / "from-env"
    assert resolve(tmp_path / "explicit", env={"BENJI_HOME": "x"}, pointer=pointer).path == tmp_path / "explicit"
    assert resolve(None, env={}, pointer=tmp_path / "missing").path.name == "Benji"


def test_watchlist_parsing(tmp_path):
    home = init(tmp_path / "d", pointer=tmp_path / "ptr")
    home.watchlist_file.write_text("# header\naapl\n MSFT  # note\n\nbrk.b\n")
    assert read_watchlist(home) == ["AAPL", "MSFT", "BRK.B"]


def test_cli_init_and_check(tmp_path, monkeypatch):
    monkeypatch.setattr("benji.home.POINTER", tmp_path / "ptr")
    r = CliRunner()
    out = r.invoke(app, ["init", "--home", str(tmp_path / "data")])
    assert out.exit_code == 0 and "Benji home" in out.output
    out = r.invoke(app, ["config", "check", "--home", str(tmp_path / "data")])
    assert out.exit_code == 0 and "all local=True" in out.output
    out = r.invoke(app, ["version"])
    assert out.output.startswith("benji 0.")


def test_cli_init_refuses_git_tree(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    out = CliRunner().invoke(app, ["init", "--home", str(tmp_path / "data")])
    assert out.exit_code == 1 and "refused" in out.output
    assert not (tmp_path / "data").exists()


def test_src_tree_matches_gitignore_backstop():
    """Data paths from the design are ignored even if someone inits inside the repo by force."""
    ignore = (Path(__file__).parent.parent / ".gitignore").read_text()
    for pat in ("*.duckdb", "runs/", "data/", ".env"):
        assert pat in ignore
