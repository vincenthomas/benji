"""Benji configuration: the owner's limits, universe, model tiers and budgets.

`config.yaml` lives in the Benji home folder and holds **no secrets** and **no portfolio data**;
it is safe to read aloud. Secrets go in the OS keychain; the book lives in `household.json`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, model_validator

Tier = Literal["local", "hosted-public"]

# Nodes that see Private data. They may never be assigned a hosted tier; the loader refuses.
PRIVATE_NODES: frozenset[str] = frozenset({"risk_reviewer", "allocator", "memo_writer"})


class Limits(BaseModel):
    """Hard gates. Enforced in code by the allocator; an agent cannot argue past them."""

    max_single_name_pct: float = Field(0.10, ge=0, le=1)
    max_sector_pct: float = Field(0.30, ge=0, le=1)
    min_cash_pct: float = Field(0.02, ge=0, le=1)
    max_daily_turnover_pct: float = Field(0.05, ge=0, le=1)
    drawdown_halt_pct: float = Field(0.15, ge=0, le=1, description="Pause new buys below this peak-to-trough")
    allow_shorts: bool = False


class Universe(BaseModel):
    index: str = "sp500"
    screener_top_n: int = Field(5, ge=0, le=50)
    watchlist_file: str = "watchlist.txt"


class ModelTiers(BaseModel):
    """Per-node tier assignment. Phase 0: everything local."""

    local_model: str = "qwen2.5:32b-instruct"
    local_endpoint: str = "http://localhost:11434"
    hosted_enabled: bool = False
    nodes: dict[str, Tier] = Field(default_factory=lambda: {
        "market_analyst": "local", "fundamentals_analyst": "local", "news_analyst": "local",
        "sentiment_analyst": "local", "bull_researcher": "local", "bear_researcher": "local",
        "research_manager": "local", "risk_reviewer": "local", "allocator": "local",
        "memo_writer": "local",
    })
    max_debate_rounds: int = Field(1, ge=1, le=5)

    @model_validator(mode="after")
    def private_nodes_stay_local(self) -> ModelTiers:
        bad = sorted(n for n, t in self.nodes.items() if n in PRIVATE_NODES and t != "local")
        if bad:
            raise ValueError(f"nodes {bad} see Private data and must be 'local'")
        if not self.hosted_enabled and any(t == "hosted-public" for t in self.nodes.values()):
            raise ValueError("hosted-public tier assigned but hosted_enabled is false")
        return self


class Budgets(BaseModel):
    """Hosted-tier spend caps in USD. Irrelevant while hosted_enabled is false."""

    per_run_usd: float = Field(2.0, ge=0)
    per_day_usd: float = Field(10.0, ge=0)
    per_month_usd: float = Field(100.0, ge=0)


class BenjiConfig(BaseModel):
    version: int = 1
    limits: Limits = Limits()
    universe: Universe = Universe()
    models: ModelTiers = ModelTiers()
    budgets: Budgets = Budgets()
    household_file: str = "household.json"
    db_file: str = "benji.duckdb"
    # PortfolioPilot price cache (public market data). Relative paths resolve inside the Benji home;
    # point it at an existing cache to share one history between the two tools.
    prices_db: str = "prices.duckdb"

    @classmethod
    def load(cls, path: Path) -> BenjiConfig:
        return cls.model_validate(yaml.safe_load(path.read_text()) or {})

    def save(self, path: Path) -> None:
        path.write_text(yaml.safe_dump(self.model_dump(mode="json"), sort_keys=False))
