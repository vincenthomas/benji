"""Egress tests. Any PR touching benji/egress/ or a client must keep these green."""

import inspect
import re
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import BaseModel

import benji
from benji.data import classes as dc
from benji.data.classes import Private, PrivatePrompt, Public, PublicPrompt, build_prompt
from benji.egress.net import EgressViolation, Net, Response
from benji.egress.policy import HostedClient, HostedTierDisabled, LocalClient, audit_lines

SRC = Path(benji.__file__).parent


# ---- a transport that never touches the network ------------------------------------

class FakeTransport:
    def __init__(self, reply: dict | None = None):
        self.calls: list[tuple[str, str]] = []
        self.reply = reply or {"message": {"content": "ok"}, "prompt_eval_count": 5, "eval_count": 2}

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append((method, url))
        import json
        return Response(200, json.dumps(self.reply).encode())


# ---- sample Private models, to fuzz the hosted client with every kind of owner data ---

class Position(Private):
    ticker: str
    qty: Decimal


class TaxProfile(Private):
    state: str
    income: Decimal


class Approval(Private):
    ticket_id: str
    decision: str


class Quote(Public):
    ticker: str
    price: Decimal


def every_private_model() -> list[type[Private]]:
    return [cls for cls in Private.__subclasses__()]


def sample(cls: type[BaseModel]) -> BaseModel:
    """Build an instance with placeholder values for every field, recursing into nested models
    and lists so any Private subclass declared anywhere in the test session can be fuzzed."""
    import typing

    def value(ann):
        origin = typing.get_origin(ann)
        args = typing.get_args(ann)
        if ann is Decimal:
            return Decimal(1)
        if ann is str:
            return "x"
        if ann in (int, float):
            return 1
        if origin in (list, tuple, set, frozenset) and args:
            return [value(args[0])]
        if origin is dict and args:
            return {"k": value(args[1])}
        if origin is typing.Union or str(origin) == "types.UnionType":
            return value(args[0])
        if isinstance(ann, type) and issubclass(ann, BaseModel):
            return sample(ann)
        return "x"

    return cls(**{name: value(f.annotation) for name, f in cls.model_fields.items()})


# ---- net allow-list and data-class gate ------------------------------------------

def test_net_refuses_unlisted_hosts_and_private_to_remote():
    net = Net(transport=FakeTransport())
    with pytest.raises(EgressViolation, match="not on the allow-list"):
        net.request("GET", "https://example.com/x", purpose="t", data_class="public")
    net.allow("api.example.com")
    net.request("GET", "https://api.example.com/x", purpose="t", data_class="public")
    with pytest.raises(EgressViolation, match="Private data to non-local"):
        net.request("POST", "https://api.example.com/x", purpose="t", data_class="private")
    net.request("POST", "http://localhost:11434/api/chat", purpose="t", data_class="private")
    assert net.non_local_hosts() == {"api.example.com"}
    assert [r.data_class for r in net.records] == ["public", "private"]  # refused calls are not recorded


def test_local_client_round_trip_and_audit():
    t = FakeTransport()
    net = Net(transport=t)
    c = LocalClient(net, model="test-model")
    p = build_prompt("allocator", "s", "u", [sample(Position)])
    assert isinstance(p, PrivatePrompt)
    out = c.complete(p, json_schema={"type": "object"})
    assert out.text == "ok" and out.prompt_tokens == 5 and out.completion_tokens == 2
    assert t.calls == [("POST", "http://localhost:11434/api/chat")]
    assert net.non_local_hosts() == set()
    lines = audit_lines(net)
    assert "localhost" in lines[0] and "non-local: none" in lines[-1]


def test_local_client_must_point_at_localhost():
    with pytest.raises(EgressViolation):
        LocalClient(Net(transport=FakeTransport()).allow("ollama.example.com"), "m",
                    endpoint="http://ollama.example.com")


# ---- hosted client: the fuzz ---------------------------------------------------------

@pytest.mark.parametrize("cls", every_private_model(), ids=lambda c: c.__name__)
def test_hosted_client_rejects_every_private_model(cls):
    t = FakeTransport()
    hosted = HostedClient(Net(transport=t).allow("api.anthropic.com"), "m", "api.anthropic.com", enabled=True)
    p = build_prompt("x", "s", "u", [sample(cls)])
    with pytest.raises(EgressViolation):
        hosted.complete(p)
    assert t.calls == []  # nothing was sent


def test_hosted_client_rejects_private_even_when_disabled_and_disabled_blocks_public():
    hosted = HostedClient(Net(transport=FakeTransport()), "m", "api.anthropic.com", enabled=False)
    with pytest.raises(EgressViolation):
        hosted.complete(build_prompt("x", "s", "u", [sample(Position)]))
    with pytest.raises(HostedTierDisabled):
        hosted.complete(build_prompt("x", "s", "u", [sample(Quote)]))


def test_private_context_mixed_with_public_is_still_refused():
    hosted = HostedClient(Net(transport=FakeTransport()), "m", "h", enabled=True)
    p = build_prompt("x", "s", "u", [sample(Quote), sample(Position), sample(Quote)])
    with pytest.raises(EgressViolation):
        hosted.complete(p)


# ---- static guarantees over the source tree ----------------------------------------

def test_only_net_module_touches_http_libraries():
    offenders = []
    for f in SRC.rglob("*.py"):
        if f.name == "net.py":
            continue
        text = f.read_text()
        if re.search(r"^\s*(import|from)\s+(urllib\.request|requests|httpx|aiohttp)\b", text, re.M):
            offenders.append(str(f.relative_to(SRC)))
    assert offenders == []


def test_no_public_prompt_is_built_from_another_prompts_messages_in_src():
    """The only way to launder a PrivatePrompt into a PublicPrompt is PublicPrompt(messages=p.messages);
    that construct must not exist in src/ (tests may use it to prove the point)."""
    pat = re.compile(r"PublicPrompt\(\s*node=\w+\.node,\s*messages=\w+\.messages")
    hits = [str(f) for f in SRC.rglob("*.py") if pat.search(f.read_text())]
    assert hits == []


def test_prompt_types_have_no_setter_for_data_class():
    for cls in (PublicPrompt, PrivatePrompt):
        prop = inspect.getattr_static(cls, "data_class")
        assert isinstance(prop, property) and prop.fset is None
    assert dc.PublicPrompt.model_config.get("frozen") is True
