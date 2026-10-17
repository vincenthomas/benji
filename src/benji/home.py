"""The Benji home folder: the one place the owner's data lives.

Resolution order for the home path:
  1. `--home` on the command line
  2. `BENJI_HOME` environment variable
  3. the pointer file `~/.config/benji/home` written by `benji init`
  4. `~/Benji`

The home folder must NOT sit inside a git working tree: a `.git` in any ancestor means a careless
`git add -A` could publish the book. `init` refuses such a path.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from benji.config import BenjiConfig

POINTER = Path("~/.config/benji/home").expanduser()
DEFAULT_HOME = Path("~/Benji").expanduser()

WATCHLIST_TEMPLATE = """# One ticker per line. Lines starting with # are ignored.
# These names get the full desk every day, alongside the screener's picks and your holdings.
"""


class HomeError(ValueError):
    pass


@dataclass(frozen=True)
class Home:
    path: Path

    @property
    def config_file(self) -> Path:
        return self.path / "config.yaml"

    @property
    def runs_dir(self) -> Path:
        return self.path / "runs"

    @property
    def watchlist_file(self) -> Path:
        return self.path / self.config.universe.watchlist_file

    @property
    def household_file(self) -> Path:
        return self.path / self.config.household_file

    @property
    def db_file(self) -> Path:
        return self.path / self.config.db_file

    @property
    def prices_db(self) -> Path:
        return self.path / Path(self.config.prices_db).expanduser()

    @property
    def config(self) -> BenjiConfig:
        return BenjiConfig.load(self.config_file) if self.config_file.exists() else BenjiConfig()

    def exists(self) -> bool:
        return self.config_file.exists()


def inside_git_tree(path: Path) -> Path | None:
    """Return the ancestor (or the path itself) that contains `.git`, or None."""
    for p in [path.resolve(), *path.resolve().parents]:
        if (p / ".git").exists():
            return p
    return None


def resolve(explicit: str | Path | None = None, env: dict[str, str] | None = None,
            pointer: Path = POINTER) -> Home:
    env = os.environ if env is None else env
    if explicit:
        return Home(Path(explicit).expanduser())
    if env.get("BENJI_HOME"):
        return Home(Path(env["BENJI_HOME"]).expanduser())
    if pointer.exists():
        return Home(Path(pointer.read_text().strip()).expanduser())
    return Home(DEFAULT_HOME)


def init(path: Path, pointer: Path = POINTER, allow_in_repo: bool = False) -> Home:
    """Create the folder layout, a default config, and remember the location. Idempotent."""
    path = path.expanduser()
    if not allow_in_repo:
        repo = inside_git_tree(path if path.exists() else path.parent)
        if repo:
            raise HomeError(f"{path} is inside the git working tree {repo}; your data must not "
                            "live in a repository (use --allow-in-repo only if you know why)")
    home = Home(path)
    home.runs_dir.mkdir(parents=True, exist_ok=True)
    if not home.config_file.exists():
        BenjiConfig().save(home.config_file)
    if not home.watchlist_file.exists():
        home.watchlist_file.write_text(WATCHLIST_TEMPLATE)
    pointer.parent.mkdir(parents=True, exist_ok=True)
    pointer.write_text(str(path))
    return home


def read_watchlist(home: Home) -> list[str]:
    if not home.watchlist_file.exists():
        return []
    out = []
    for line in home.watchlist_file.read_text().splitlines():
        t = line.split("#", 1)[0].strip().upper()
        if t:
            out.append(t)
    return out
