# Benji

An agentic investment desk that runs on one laptop. Role-specific agents research candidates,
argue bull and bear, size ideas against your real book under hard risk and tax limits, and write
a **daily memo**. You are the portfolio manager: read the memo, approve or reject each line,
execute at your broker, report fills. Benji reconciles and grades its own agents.

**Your data never leaves the laptop.** Positions, lots, cash, tax profile, approvals, fills and
memos are typed `Private` and can only be seen by local models. That is enforced in code and
tested, not promised in a prompt. See [docs/DESIGN.md](docs/DESIGN.md) §3.

Benji is the front office. The deterministic back office — price cache, point-in-time S&P 500
membership, lot ledger, tax engine, wash-sale and concentration gates, broker import — is
[PortfolioPilot](https://github.com/vincenthomas/portfoliopilot), used as a library.

## Status

Design complete; implementation starts at Milestone 0 of [docs/BUILD_PLAN.md](docs/BUILD_PLAN.md).
Phase 0 runs **local-only** (Ollama). A hosted tier for public-data agents is designed in and
ships disabled.

## Lineage

The agent graph and several prompts are rewritten from
[TradingAgents](https://github.com/TauricResearch/TradingAgents) (Apache-2.0); see `NOTICE` and
[docs/DESIGN.md](docs/DESIGN.md) §10 for exactly what was kept, changed, and replaced.

## Disclaimer

Benji is software, not a licensed advisor. It never places orders. Review every recommendation
with your own judgment and, where it matters, a qualified professional.

## License

Apache-2.0
