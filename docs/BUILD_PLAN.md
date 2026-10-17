# Build Plan — piece by piece

Rule from the design: **port one piece, test it, confirm the tests pass, then build on it.** One
branch and PR per item; CI green before merge. No test calls a model. Phase 0 is local-only.

Dependency order:

```
egress & clients → run store → tools (back-office bridge) → analysts → researchers →
research manager → risk reviewer → allocator → memo → daily loop → screener → scoreboard →
backtest → hosted tier
```

## Milestone 0 — Skeleton and security floor

| # | Item | Scope | Exit gate |
| --- | --- | --- | --- |
| 0.1 ✅ | Repo skeleton | `pyproject` (depends on `portfoliopilot`), CI, `benji` CLI stub, `~/Benji` folder layout, config loader | `benji init` creates the folder and a config; CI green |
| 0.2 ✅ | Data classes | `Public` / `Private` Pydantic bases; `PublicPrompt` / `PrivatePrompt`; render helpers | A `Private` field inside a `Public` model fails validation; prompt type follows content |
| 0.3 ✅ | Egress policy + `net` | `LocalClient` (Ollama), `HostedClient` (disabled), `EgressViolation`, allow-listed `net` module, `benji egress audit` | Fuzz test: hosted client rejects every `Private` model; local-only run makes zero non-localhost calls |
| 0.4 ✅ | Fake client + run store | Recorded-response client for tests; `runs`/`prompts` tables; every call persisted | A run can be replayed from the store with identical outputs |

## Milestone 1 — Tools (back-office bridge)

| # | Item | Scope | Exit gate |
| --- | --- | --- | --- |
| 1.1 | Market tools | `get_bars`, `get_indicators` (stockstats or own), `get_snapshot` from PortfolioPilot cache; **Public** | Tools never hit the network when the cache covers the window |
| 1.2 | Fundamentals + filings | EDGAR fetcher ported behind cache (`filings`, `fundamentals` tables); `get_fundamentals`, `get_filing_sections`; **Public** | Second call for the same filing is served from DuckDB |
| 1.3 | News archive | Daily fetch into `news` with fetch date; `get_news`, `get_macro`; **Public** | Archive rows are immutable; a query for a past window returns only rows fetched on or before it |
| 1.4 | Book tools | `get_exposures`, `check_gates`, `estimate_tax`, `select_lots`, `size_position`; **Private** | Golden cases from PortfolioPilot pass through the tool layer unchanged |

## Milestone 2 — The desk

| # | Item | Scope | Exit gate |
| --- | --- | --- | --- |
| 2.1 | Graph state + routing | `AgentState` with tagged models; debate routing rewritten from `conditional_logic.py` | Routing unit tests for every branch; state round-trips through checkpointer |
| 2.2 | Market analyst | Prompt ported; structured report schema | Against the fake client, produces a valid `MarketReport`; prompt contains no `Private` field |
| 2.3 | Fundamentals analyst | same | same |
| 2.4 | News analyst | same | same |
| 2.5 | Sentiment analyst (off by default) | same | same |
| 2.6 | Bull + bear researchers | Prompts ported; N-round debate | Debate terminates at `max_debate_rounds`; transcript stored |
| 2.7 | Research manager → thesis | Adjudication prompt; `Thesis` schema (action, conviction, horizon, bear case) | Valid `Thesis` from fake client; schema rejects shorts when disabled |
| 2.8 | Risk reviewer | **Private**; sees exposures; may downgrade; local-only client enforced | Attempting a hosted client for this node raises `EgressViolation` |
| 2.9 | Allocator | **Private**; sizes theses under gates and tax; emits `Ticket[]` | On the sample household: no ticket breaches any gate; sells carry specific lots and est. tax |
| 2.10 | `benji pitch` | End-to-end on one ticker with the local model (manual smoke) | Memo-style thesis printed; run stored; `egress audit` shows localhost + data vendors only |

## Milestone 3 — Daily loop

| # | Item | Scope | Exit gate |
| --- | --- | --- | --- |
| 3.1 | Watchlist + holdings feed | Watchlist file; holdings from household; source labels | Sources appear on each candidate |
| 3.2 | Memo writer | Memo format (§8) rendered from structured results; **Private**, local-only | Golden memo for the sample household matches byte-for-byte |
| 3.3 | `benji daily` | Orchestrates feeds → desk → allocator → memo; checkpoint/resume | Interrupted run resumes without re-calling finished nodes |
| 3.4 | `benji review` + `benji fills` | Approve/reject/modify per line; fills reconcile through the ledger; thesis → `live` | A reported fill changes realized gains and the thesis status |

## Milestone 4 — Screener and scoreboard

| # | Item | Scope | Exit gate |
| --- | --- | --- | --- |
| 4.1 | Factor scores | Momentum, quality, value, low-vol from cached data; configurable weights | Scores reproduce a hand-computed example; no network |
| 4.2 | Screener | Top-N over PIT universe + trim candidates from holdings | Deterministic for a fixed date; N is config |
| 4.3 | Scoreboard | Theses marked to market; scored at horizon; per-agent / per-source aggregates | Hit rate and calibration computed on a synthetic thesis set with known answers |
| 4.4 | Memo integration | Scoreboard section; dropped-ideas section | Memo shows both from stored data |

## Milestone 5 — Backtest and hosted tier

| # | Item | Scope | Exit gate |
| --- | --- | --- | --- |
| 5.1 | Price-only backtest | `benji backtest` over PIT universe with price-only agents; results labeled | Snapshot-vs-PIT comparison shows survivorship gap; look-ahead test passes |
| 5.2 | Cost ledger | `spend` table, price table, caps per run/day/month, degrade-to-local | Cap breach refuses the call and the memo records the fallback |
| 5.3 | Hosted-public client | Anthropic client behind egress policy; prompt cache by hash | All egress tests still green; a `Private` prompt is refused |
| 5.4 | Tiering config | Per-agent tier assignment; risk reviewer / allocator / memo pinned local | Config that pins a Private node to hosted is rejected at load |

## Working agreement

- Branch `m<milestone>/<item>-<slug>`; squash-merge; CI green on 3.11–3.13.
- Every ported prompt file names its TradingAgents origin in a header comment.
- No real household data in the repo; `examples/` is synthetic (PortfolioPilot's sample).
- A change to anything under `benji/egress/` or any client requires the egress tests in the PR.
