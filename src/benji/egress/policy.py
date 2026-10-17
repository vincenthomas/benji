"""Model clients behind the egress policy.

- `LocalClient` (Ollama on localhost) accepts PublicPrompt and PrivatePrompt.
- `HostedClient` accepts PublicPrompt ONLY and raises EgressViolation on a PrivatePrompt before
  anything is built, logged, or sent. It is also disabled in Phase 0: constructing it with
  `enabled=False` makes every call raise HostedTierDisabled. There is no override flag.

Both clients return a `Completion`; the run store (0.4) records every call.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol

from benji.data.classes import PrivatePrompt, PublicPrompt
from benji.egress.net import EgressViolation, Net

Prompt = PublicPrompt | PrivatePrompt


class HostedTierDisabled(RuntimeError):
    pass


@dataclass(frozen=True)
class Completion:
    text: str
    model: str
    client: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    raw: Any = None

    def json(self) -> Any:
        """Parse the completion as JSON (structured output). Tolerates a fenced block."""
        t = self.text.strip()
        if t.startswith("```"):
            t = t.strip("`")
            t = t[t.find("{"):t.rfind("}") + 1] if "{" in t else t
        return json.loads(t)


class ModelClient(Protocol):
    name: str
    model: str

    def complete(self, prompt: Prompt, *, json_schema: dict[str, Any] | None = None,
                 temperature: float = 0.2) -> Completion: ...


class LocalClient:
    """Ollama chat API over localhost. Public and Private prompts both allowed."""

    name = "local"

    def __init__(self, net: Net, model: str, endpoint: str = "http://localhost:11434"):
        self.net = net
        self.model = model
        self.endpoint = endpoint.rstrip("/")
        self.net.check(f"{self.endpoint}/api/chat", "private")  # must be a local host

    def complete(self, prompt: Prompt, *, json_schema: dict[str, Any] | None = None,
                 temperature: float = 0.2) -> Completion:
        body: dict[str, Any] = {
            "model": self.model, "stream": False,
            "messages": [m.model_dump() for m in prompt.messages],
            "options": {"temperature": temperature},
        }
        if json_schema is not None:
            body["format"] = json_schema
        resp = self.net.request("POST", f"{self.endpoint}/api/chat", purpose=f"llm:{prompt.node}",
                                data_class=prompt.data_class, json_body=body, timeout=600)
        if resp.status != 200:
            raise RuntimeError(f"ollama returned {resp.status}: {resp.body[:200]!r}")
        data = resp.json()
        return Completion(text=data["message"]["content"], model=self.model, client=self.name,
                          prompt_tokens=int(data.get("prompt_eval_count", 0)),
                          completion_tokens=int(data.get("eval_count", 0)), raw=data)


class HostedClient:
    """A remote model. PublicPrompt only; disabled until Phase 1 enables it in config."""

    name = "hosted-public"

    def __init__(self, net: Net, model: str, host: str, enabled: bool = False):
        self.net = net
        self.model = model
        self.host = host
        self.enabled = enabled

    def complete(self, prompt: Prompt, *, json_schema: dict[str, Any] | None = None,
                 temperature: float = 0.2) -> Completion:
        # Order matters: the data-class check comes first so a Private prompt is refused even
        # when the tier is disabled, and nothing about it is ever rendered or logged here.
        if not isinstance(prompt, PublicPrompt):
            raise EgressViolation(f"hosted client refuses a {type(prompt).__name__} (node={prompt.node})")
        if not self.enabled:
            raise HostedTierDisabled("hosted tier is disabled (models.hosted_enabled=false)")
        raise NotImplementedError("hosted transport lands in build-plan item 5.3")


def audit_lines(net: Net) -> list[str]:
    """Human-readable egress audit for a run."""
    out = []
    for r in net.records:
        out.append(f"{r.ts:%H:%M:%S}  {r.host:<24} {r.data_class:<8} {r.purpose:<28} "
                   f"out={r.bytes_out:>7} in={r.bytes_in:>8} status={r.status}")
    non_local = net.non_local_hosts()
    out.append(f"hosts: {sorted(net.hosts_contacted())}  non-local: {sorted(non_local) or 'none'}")
    return out
