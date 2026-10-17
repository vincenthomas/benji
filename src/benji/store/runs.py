"""Run store: every model call and every network record, persisted in the Benji DuckDB file.

Tables
  runs   (run_id, started, mode, status, finished)
  calls  (run_id, seq, node, client, model, data_class, prompt_hash, prompt_json, response_text,
          prompt_tokens, completion_tokens, ms)
  net_log(run_id, seq, ts, host, purpose, data_class, bytes_out, bytes_in, status)

`RecordingClient` wraps any ModelClient and writes each call. `ReplayClient` answers from the
store by prompt hash and raises if a prompt was never seen, so a replayed run is provably the
same run. `FakeClient` scripts responses for tests. None of the three contacts a model.

Privacy: the prompt text of a PrivatePrompt is stored here, in the owner's DuckDB file in the
Benji home, and nowhere else. The store is Private data.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb

from benji.data.classes import PrivatePrompt, PublicPrompt
from benji.egress.net import Net, NetRecord
from benji.egress.policy import Completion, ModelClient

Prompt = PublicPrompt | PrivatePrompt

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY, started TIMESTAMP NOT NULL, mode TEXT NOT NULL,
    status TEXT NOT NULL, finished TIMESTAMP
);
CREATE TABLE IF NOT EXISTS calls (
    run_id TEXT NOT NULL, seq INTEGER NOT NULL, ts TIMESTAMP NOT NULL, node TEXT NOT NULL,
    client TEXT NOT NULL, model TEXT NOT NULL, data_class TEXT NOT NULL,
    prompt_hash TEXT NOT NULL, prompt_json TEXT NOT NULL, response_text TEXT NOT NULL,
    prompt_tokens INTEGER, completion_tokens INTEGER, ms INTEGER,
    PRIMARY KEY (run_id, seq)
);
CREATE TABLE IF NOT EXISTS net_log (
    run_id TEXT NOT NULL, seq INTEGER NOT NULL, ts TIMESTAMP NOT NULL, host TEXT NOT NULL,
    purpose TEXT NOT NULL, data_class TEXT NOT NULL, bytes_out INTEGER, bytes_in INTEGER, status INTEGER,
    PRIMARY KEY (run_id, seq)
);
"""


def new_run_id(mode: str) -> str:
    return f"{datetime.now(UTC):%Y%m%d-%H%M%S}-{mode}-{uuid.uuid4().hex[:6]}"


class RunStore:
    def __init__(self, con: duckdb.DuckDBPyConnection):
        self.con = con
        self.con.execute(SCHEMA)

    @classmethod
    def open(cls, path: str | Path) -> RunStore:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        return cls(duckdb.connect(str(path)))

    # ---- runs -----------------------------------------------------------

    def start_run(self, mode: str, run_id: str | None = None) -> str:
        run_id = run_id or new_run_id(mode)
        self.con.execute("INSERT INTO runs VALUES (?, ?, ?, 'running', NULL)",
                         [run_id, datetime.now(UTC), mode])
        return run_id

    def finish_run(self, run_id: str, status: str = "done") -> None:
        self.con.execute("UPDATE runs SET status = ?, finished = ? WHERE run_id = ?",
                         [status, datetime.now(UTC), run_id])

    def runs(self, limit: int = 20) -> list[tuple]:
        return self.con.execute(
            "SELECT run_id, started, mode, status, finished FROM runs ORDER BY started DESC LIMIT ?",
            [limit]).fetchall()

    # ---- calls ----------------------------------------------------------

    def _next_seq(self, table: str, run_id: str) -> int:
        assert table in {"calls", "net_log"}
        row = self.con.execute(f"SELECT coalesce(max(seq), 0) + 1 FROM {table} WHERE run_id = ?",  # noqa: S608
                               [run_id]).fetchone()
        return int(row[0]) if row else 1

    def record_call(self, run_id: str, prompt: Prompt, completion: Completion, ms: int) -> int:
        seq = self._next_seq("calls", run_id)
        self.con.execute(
            "INSERT INTO calls VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [run_id, seq, datetime.now(UTC), prompt.node, completion.client, completion.model,
             prompt.data_class, prompt.hash,
             json.dumps({"schema": prompt.schema_name,
                         "messages": [m.model_dump() for m in prompt.messages]}),
             completion.text, completion.prompt_tokens, completion.completion_tokens, ms])
        return seq

    def calls(self, run_id: str) -> list[dict[str, Any]]:
        cols = ["seq", "ts", "node", "client", "model", "data_class", "prompt_hash", "response_text",
                "prompt_tokens", "completion_tokens", "ms"]
        rows = self.con.execute(
            f"SELECT {', '.join(cols)} FROM calls WHERE run_id = ? ORDER BY seq",  # noqa: S608
            [run_id]).fetchall()
        return [dict(zip(cols, r, strict=True)) for r in rows]

    def lookup(self, prompt_hash: str, run_id: str | None = None) -> Completion | None:
        q = ("SELECT response_text, model, client, prompt_tokens, completion_tokens "
             "FROM calls WHERE prompt_hash = ?")
        args: list[Any] = [prompt_hash]
        if run_id:
            q += " AND run_id = ?"
            args.append(run_id)
        row = self.con.execute(q + " ORDER BY ts DESC LIMIT 1", args).fetchone()
        if not row:
            return None
        return Completion(text=row[0], model=row[1], client=f"replay:{row[2]}",
                          prompt_tokens=row[3] or 0, completion_tokens=row[4] or 0)

    # ---- net log --------------------------------------------------------

    def record_net(self, run_id: str, records: Iterable[NetRecord]) -> int:
        n = 0
        seq = self._next_seq("net_log", run_id)
        for r in records:
            self.con.execute("INSERT INTO net_log VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                             [run_id, seq, r.ts, r.host, r.purpose, r.data_class,
                              r.bytes_out, r.bytes_in, r.status])
            seq += 1
            n += 1
        return n

    def net_log(self, run_id: str) -> list[tuple]:
        return self.con.execute(
            "SELECT ts, host, purpose, data_class, bytes_out, bytes_in, status FROM net_log "
            "WHERE run_id = ? ORDER BY seq", [run_id]).fetchall()

    def egress_summary(self, run_id: str) -> dict[str, Any]:
        rows = self.net_log(run_id)
        hosts = sorted({r[1] for r in rows})
        non_local = [h for h in hosts if h not in {"localhost", "127.0.0.1", "::1"}]
        private_remote = [r for r in rows if r[3] == "private" and r[1] in non_local]
        return {"hosts": hosts, "non_local": non_local, "calls": len(rows),
                "private_to_remote": len(private_remote)}


# ---- clients that never call a model --------------------------------------------

class RecordingClient:
    """Wrap a real client; persist every call to the store under `run_id`."""

    def __init__(self, inner: ModelClient, store: RunStore, run_id: str):
        self.inner, self.store, self.run_id = inner, store, run_id
        self.name, self.model = inner.name, inner.model

    def complete(self, prompt: Prompt, *, json_schema: dict[str, Any] | None = None,
                 temperature: float = 0.2) -> Completion:
        t0 = time.monotonic()
        out = self.inner.complete(prompt, json_schema=json_schema, temperature=temperature)
        self.store.record_call(self.run_id, prompt, out, int((time.monotonic() - t0) * 1000))
        return out


class ReplayMiss(LookupError):
    pass


class ReplayClient:
    """Answer every prompt from a stored run. A prompt the run never saw is an error, which is
    what makes a replay a proof: same prompts in, same text out."""

    name = "replay"

    def __init__(self, store: RunStore, run_id: str):
        self.store, self.run_id = store, run_id
        self.model = "replay"

    def complete(self, prompt: Prompt, *, json_schema: dict[str, Any] | None = None,
                 temperature: float = 0.2) -> Completion:
        hit = self.store.lookup(prompt.hash, self.run_id)
        if hit is None:
            raise ReplayMiss(f"run {self.run_id} has no call for node={prompt.node} hash={prompt.hash}")
        return hit


class FakeClient:
    """Scripted responses for tests: a list consumed in order, or a dict of regex -> response
    matched against the prompt text. Records what it was asked."""

    name = "fake"

    def __init__(self, responses: list[str] | dict[str, str] | Callable[[Prompt], str] | None = None,
                 model: str = "fake-model"):
        self.model = model
        self.responses = responses if responses is not None else ["{}"]
        self.prompts: list[Prompt] = []

    def complete(self, prompt: Prompt, *, json_schema: dict[str, Any] | None = None,
                 temperature: float = 0.2) -> Completion:
        self.prompts.append(prompt)
        r = self.responses
        if callable(r):
            text = r(prompt)
        elif isinstance(r, dict):
            text = next((v for pat, v in r.items() if re.search(pat, prompt.text())), "{}")
        else:
            text = r[min(len(self.prompts) - 1, len(r) - 1)]
        return Completion(text=text, model=self.model, client=self.name,
                          prompt_tokens=len(prompt.text()) // 4, completion_tokens=len(text) // 4)


def flush_net(net: Net, store: RunStore, run_id: str) -> int:
    """Persist a run's network records and clear them from the in-memory Net."""
    n = store.record_net(run_id, net.records)
    net.records.clear()
    return n
