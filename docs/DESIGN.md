# Benji — System Design

Benji is an agentic investment desk that runs on one laptop. A team of role-specific agents
researches candidates, argues bull and bear, sizes ideas against the owner's real book under
hard risk and tax limits, and writes a **daily memo**. The owner is the portfolio manager: they
read the memo, approve or reject each line, execute at their broker, and report fills. Benji
reconciles and grades its own agents against what actually happened.

Benji is the **front office**. The deterministic **back office** — price cache, point-in-time
universe, lot ledger, tax engine, wash-sale and concentration gates, broker import — is
[PortfolioPilot](https://github.com/vincenthomas/portfoliopilot), consumed as a library.
Benji is the only component that ever talks to a language model.

## 1. Non-negotiables

1. **The owner's data never leaves the laptop.** Positions, lots, cost basis, cash, tax profile,
   account names, approvals, fills, memos — all of it is *private* and is never placed in a
   payload to any remote service, never written to any log that syncs, never sent as telemetry.
   This is enforced in code (§3), tested, and auditable, not left to prompt discipline.
2. **Agents propose; the owner disposes.** No order is ever placed by Benji. Output is a memo.
3. **Hard limits are code, not conversation.** Position, sector, drawdown, wash-sale, tax-budget
   and restricted-ticker rules are deterministic gates an agent must call and cannot override.
4. **Shorting is off.** Long-biased equity only. (A config flag exists; it defaults to false and
   the risk gate rejects short tickets while it is false.)
5. **Every change is tested before anything is built on it.** Each piece rewritten from
   TradingAgents is ported, given its own tests, run, and only then used as a dependency.
6. **Cache everything that does not change.** Repeated online calls for stable data are a bug.

## 2. Investment flavor

**Long-biased discretionary equity with a systematic screen in front.**

- A deterministic **screener** over the S&P 500 (plus the owner's **watchlist**) produces a
  short list of candidates each day. It runs locally, costs nothing, and is backtestable.
- Candidates, and every **existing holding**, go through the agent desk, which produces a
  thesis: `buy` / `add` / `hold` / `trim` / `sell`, with conviction, horizon and size.
- The allocation step sizes approved theses against the real book under the hard limits and the
  tax engine, and writes the memo.

Why this and not the alternatives: pure quant needs no agents; pure discretionary L/S needs
margin and asymmetric risk a personal book should not carry; multi-strategy pods are a later
extension (the screener becomes one pod) once the single desk is measured and trusted.

## 3. Security model

### 3.1 Data classes

| Class | Examples | May be sent to |
| --- | --- | --- |
| `Public` | ticker, prices, bars, filings, news, screener scores, indicator values | local models; **later** hosted models |
| `Private` | positions, lots, basis, cash, NAV, tax profile, account ids, approvals, fills, memos, scoreboard | **local models only** |

Data class is a type, not a convention: every payload an agent builds is a Pydantic model
tagged `Public` or `Private`. A `Private` model cannot be coerced into a `Public` one; a
prompt that embeds a `Private` value produces a `PrivatePrompt`.

### 3.2 Egress policy

One module, `benji/egress/policy.py`, sits between every agent and every model client.

- The **local client** (Ollama over `localhost`) accepts `PublicPrompt` and `PrivatePrompt`.
- A **hosted client** (not enabled in Phase 0; see §4) accepts `PublicPrompt` only and raises
  `EgressViolation` on anything else. There is no override flag.
- Every outbound HTTP request in the process goes through one `net` module that records
  `(host, purpose, bytes, data_class)`; `benji egress audit <run-id>` prints the list. Any host
  not on the allow-list (`localhost`, configured market-data vendors, and — when enabled — the
  hosted model API) fails the run.
- Tests: a fuzz test builds prompts from every `Private` model and asserts the hosted client
  rejects each; a network test asserts a local-only run makes zero non-localhost calls.

### 3.3 Where data lives

```
<Benji home>/                 owner-chosen folder (`benji init --home PATH`; default ~/Benji),
                              OUTSIDE any git working tree — init refuses otherwise — never synced by Benji
  config.yaml                 limits, universe, model tiers, budgets (no secrets)
  household.json              lots, accounts, tax profile  (PortfolioPilot format)
  benji.duckdb                prices, universe, filings, news archive, runs, memos, scoreboard
  runs/<date>/<run-id>/       every prompt, response, tool call, and the memo for that run
```

Secrets (broker tokens, future API keys) live in the macOS Keychain via `keyring`. Benji has no
account, no cloud, no telemetry, no auto-update.

## 4. Model tiers

| Tier | Where | Used for | Phase |
| --- | --- | --- | --- |
| **local** | Ollama on the laptop; default a ~30B-class model (64 GB RAM allows it) | everything | 0 (now) |
| **hosted-public** | Claude API (Console key, pay per token) | analysts and bull/bear debate on *public* data only, when quality justifies cost | 1+ (opt-in) |

Phase 0 runs **local-only**. The hosted tier is designed in from the start (egress policy, cost
ledger, caching) but ships disabled; enabling it is a config change plus an API key, and the
egress tests must still pass.

**Cost controls (hosted tier):** a budget ledger in DuckDB records tokens and dollars per call
(price table in config); caps per run, per day, per month; when a cap would be exceeded the
call is refused and the agent falls back to the local tier (recorded in the memo). Hosted outputs
are cached by `(agent, prompt-hash)` so a re-run is free. The memo prints spend-to-date.

## 5. Architecture

```
                owner (portfolio manager)
                   ▲ memo        │ approvals, fills
                   │             ▼
 ┌──────────────── Benji front office ────────────────────────────┐
 │  screener ─▶ desk (agent graph) ─▶ allocator ─▶ memo writer     │
 │                   ▲   │                  ▲                       │
 │             tools │   │ theses           │ hard gates (code)     │
 │                   │   ▼                  │                       │
 │  egress policy ── model clients (local / hosted-public)         │
 │  cost ledger ── run store ── scoreboard                         │
 └──────────────────────┬──────────────────────────────────────────┘
                        │ library calls
 ┌──────────────── PortfolioPilot back office ────────────────────┐
 │  price cache · PIT universe · dividends/splits · ledger & lots  │
 │  tax engine · wash-sale / restricted / concentration gates      │
 │  broker import · realized gains                                 │
 └─────────────────────────────────────────────────────────────────┘
```

## 6. The desk: agent roster

Rewritten from TradingAgents' graph (analysts → bull/bear → research manager → trader → risk →
portfolio manager), with these changes: the trader becomes a *thesis writer* (no execution), the
risk debate is replaced by **deterministic gates plus one risk reviewer**, the PM becomes an
**allocator** that sees the book, and the owner replaces the final decision.

| Agent | Role | Inputs (data class) | Tools | Tier (Phase 0 → later) |
| --- | --- | --- | --- | --- |
| Market analyst | Price action, trend, indicators | bars, indicators (Public) | `get_bars`, `get_indicators`, `get_snapshot` | local → hosted-public |
| Fundamentals analyst | Quality, growth, valuation from filings | fundamentals, filings (Public) | `get_fundamentals`, `get_filing_sections` | local → hosted-public |
| News analyst | Company and macro news in the window | news archive (Public) | `get_news`, `get_macro` | local → hosted-public |
| Sentiment analyst | Crowd positioning | social/sentiment snapshot (Public) | `get_sentiment` | local → hosted-public; optional |
| Bull researcher | Strongest case to own it | analyst reports (Public) | none | local → hosted-public |
| Bear researcher | Strongest case not to | analyst reports (Public) | none | local → hosted-public |
| Research manager | Adjudicates the debate; writes the thesis | debate (Public) | none | local → hosted-public |
| Risk reviewer | Challenges the thesis against the *book's* exposures; may downgrade | thesis + book exposures (**Private**) | `get_exposures`, `check_gates` | **local only** |
| Allocator | Sizes approved theses against the book under gates and tax; produces tickets | theses + book + tax state (**Private**) | `size_position`, `estimate_tax`, `check_gates`, `select_lots` | **local only** |
| Memo writer | Renders the daily memo | everything (**Private**) | none | **local only** |

Debate depth (`max_debate_rounds`) is config; default 1 round each in Phase 0 to keep local
runs under ~10 minutes for five names.

### 6.1 Run modes

- **`benji pitch TICKER`** — full desk on one name; prints the thesis. Public data only until the
  risk reviewer, so this is the mode that will use the hosted tier first.
- **`benji daily`** — screener + watchlist + holdings → desk on each → allocator → memo. The
  production loop.
- **`benji backtest --from --to`** — replays `daily` over past dates using the point-in-time
  universe and prices; **price-only agents** until a news/filings archive exists for the window
  (otherwise look-ahead). Labeled as such in results.

### 6.2 Deterministic gates (code, PortfolioPilot)

Max single-name %, max sector %, min cash, max turnover per day, drawdown halt, restricted
tickers → scheduled-plan queue, wash-sale check across all accounts, annual LT-gain budget,
shorting disabled. A ticket that fails a gate is dropped with the reason printed in the memo.

## 7. Screener and universes

- **Universe sources:** S&P 500 (point-in-time membership, from PortfolioPilot), the owner's
  watchlist (a file), and current holdings. All three feed the desk; the memo labels the source.
- **Screener:** deterministic factor scores from cached bars and fundamentals — momentum,
  quality, value, low-volatility — combined by configurable weights; top-N per day plus any
  holding whose score fell below a threshold (a trim candidate). Entirely local and testable.
- Candidate count is the main cost lever: default N=5 new names/day.

## 8. The daily memo

Written to `runs/<date>/memo.md` and printed. Sections, in order:

1. **Book** — NAV, cash, top exposures by name and sector, change since last memo.
2. **Recommendations** — one line per ticket: action, ticker, size (shares and % NAV), lots
   for sells (specific-ID), est. tax, conviction, horizon, one-sentence rationale, the bear case
   that lost, gate results. Ranked by after-tax risk-adjusted merit.
3. **Holdings review** — hold/trim/add verdict per position, in one line each.
4. **Dropped** — ideas that failed a gate or lost the debate, with the reason.
5. **Scoreboard** — rolling hit rate and calibration per agent and per source; open theses.
6. **Operations** — data freshness, models used, run time, hosted spend-to-date (when enabled).

Owner marks each recommendation `approve` / `reject` / `modify` (`benji review`), executes at
the broker, and reports fills (`benji fills`); the ledger reconciles and the thesis is marked
`live`.

## 9. Scoreboard

Every thesis is stored with ticker, direction, conviction, horizon, entry price, agents that
authored it, and the owner's decision. On each run it is marked to market; at horizon it is
scored (hit, excess return vs SPY, calibration bucket). Aggregates per agent, per role, per
source (screener / watchlist / holdings) are what the owner uses to decide which agents to trust
— and what a future "trust-weighted" allocator would consume.

## 10. What is taken from TradingAgents, and what is not

| TradingAgents piece | Benji |
| --- | --- |
| LangGraph state machine, `AgentState`, debate routing (`conditional_logic.py`) | **Rewritten** with the same shape; state carries data-class-tagged models instead of strings |
| Analyst / researcher / research-manager prompts | **Ported** with light edits; each prompt file carries its origin and a test that the agent produces the structured schema |
| Trader, three-way risk debate, portfolio manager | **Replaced** by thesis writer, risk reviewer, allocator (§6) |
| `dataflows` vendors (Yahoo, Alpha Vantage, SEC EDGAR, FRED, Reddit, StockTwits) | **Replaced** by PortfolioPilot's cache for prices/universe; EDGAR and news fetchers ported behind the cache and the egress `net` module |
| `llm_clients` factory (many providers) | **Reduced** to two: local (Ollama) and hosted-public (Anthropic); both behind the egress policy |
| Memory log + reflection + settlement | **Replaced** by the scoreboard (numbers, not prose) — reflection text is kept as an input to the research manager |
| Checkpointer (SQLite) | **Ported**; stored under `~/Benji/runs/` |
| CLI (`typer`) and reports | **Rewritten** around the memo |

## 11. Storage

One DuckDB file (`benji.duckdb`) with PortfolioPilot's tables plus: `filings`, `fundamentals`,
`news` (archived daily snapshots with fetch date — this is what makes future backtests honest),
`runs`, `prompts`, `theses`, `tickets`, `decisions`, `fills`, `scores`, `spend`.

## 12. Testing discipline

- Unit tests for every module; **no test calls a model**: agents are tested against a
  recorded-response fake client, and prompts are tested for schema validity.
- Egress tests (§3.2) run in CI and must stay green for any change touching a client.
- Gate tests use hand-computed golden cases (as in PortfolioPilot).
- A nightly **smoke run** on the sample household with the local model, checked for a complete
  memo, is the only test that touches a model, and it is not in CI.
- Order of work: port one piece → write its tests → run them → merge → next piece.

## 13. Open questions

- Which local model: candidates in the 30B class to evaluate on a fixed pitch for quality,
  speed, and structured-output reliability before committing.
- News source for the archive: Yahoo news endpoint vs. an RSS set vs. EDGAR 8-Ks only.
- Whether the sentiment analyst earns its keep at all; off by default until the scoreboard says
  otherwise.
