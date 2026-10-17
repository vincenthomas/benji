"""Data classes: every value an agent can see is `Public` or `Private`.

`Private` = anything about the owner (positions, lots, basis, cash, NAV, tax profile, account
ids, approvals, fills, memos, scores). `Public` = market data and agent reasoning about tickers.

The rules are enforced by the types, not by convention:
- A `Public` model may not contain a `Private` value anywhere inside it (fields, lists, dicts,
  nested models). Validation fails at construction, so a private value cannot be laundered into
  a public container.
- A prompt built from any `Private` context is a `PrivatePrompt`; only `PublicPrompt` may ever
  leave the laptop (see `benji.egress.policy`).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

DataClass = Literal["public", "private"]


class _Tagged(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    def render(self) -> str:
        """Compact, deterministic text for inclusion in a prompt."""
        body = json.dumps(self.model_dump(mode="json"), sort_keys=True, default=str)
        return f"{type(self).__name__}: {body}"


class Public(_Tagged):
    """Safe for any model tier. Must not contain Private values."""

    @model_validator(mode="after")
    def _no_private_inside(self) -> Public:
        found = find_private(self.model_dump(mode="python", exclude_none=False), self)
        if found:
            raise ValueError(f"Public model {type(self).__name__} contains Private data at {found}")
        return self


class Private(_Tagged):
    """The owner's data. Local models only. Never serialized into a Public model or hosted prompt."""


def _walk(obj: Any, path: str = "") -> Iterable[tuple[str, Any]]:
    if isinstance(obj, BaseModel):
        yield path, obj
        for name in type(obj).model_fields:
            yield from _walk(getattr(obj, name), f"{path}.{name}" if path else name)
    elif isinstance(obj, dict):
        for k, v in obj.items():
            yield from _walk(v, f"{path}[{k!r}]")
    elif isinstance(obj, list | tuple | set | frozenset):
        for i, v in enumerate(obj):
            yield from _walk(v, f"{path}[{i}]")
    else:
        yield path, obj


def find_private(_dumped: Any, model: BaseModel) -> str | None:
    """Path of the first Private value nested inside `model` (fields are walked on the live
    objects, not the dump, so the class tag survives)."""
    for path, value in _walk(model):
        if path and isinstance(value, Private):
            return path
    return None


def classify(obj: Any) -> DataClass:
    """'private' if obj is, or contains, any Private value; else 'public'.
    Plain strings/numbers are public; anything untyped that carries owner data must be wrapped."""
    for _, value in _walk(obj):
        if isinstance(value, Private):
            return "private"
    return "public"


# ---- prompts -------------------------------------------------------------

Role = Literal["system", "user", "assistant", "tool"]


class Message(BaseModel):
    model_config = ConfigDict(frozen=True)
    role: Role
    content: str


class _Prompt(BaseModel):
    """A fully rendered prompt. Its data class is fixed at construction and cannot be changed."""

    model_config = ConfigDict(frozen=True)
    node: str
    messages: tuple[Message, ...]
    schema_name: str | None = None  # structured-output schema the caller expects, if any

    @field_validator("messages")
    @classmethod
    def _non_empty(cls, v: tuple[Message, ...]) -> tuple[Message, ...]:
        if not v:
            raise ValueError("prompt has no messages")
        return v

    @property
    def data_class(self) -> DataClass:
        raise NotImplementedError

    @property
    def hash(self) -> str:
        payload = json.dumps({"node": self.node, "schema": self.schema_name,
                              "messages": [m.model_dump() for m in self.messages]}, sort_keys=True)
        return hashlib.sha256(payload.encode()).hexdigest()[:24]

    def text(self) -> str:
        return "\n\n".join(f"[{m.role}] {m.content}" for m in self.messages)


class PublicPrompt(_Prompt):
    @property
    def data_class(self) -> DataClass:
        return "public"


class PrivatePrompt(_Prompt):
    @property
    def data_class(self) -> DataClass:
        return "private"


def build_prompt(node: str, system: str, user: str, context: Iterable[Public | Private] = (),
                 schema_name: str | None = None) -> PublicPrompt | PrivatePrompt:
    """Render `context` models into the user message; the prompt's class follows the context.
    There is no way to pass Private context and receive a PublicPrompt."""
    ctx = list(context)
    private = any(classify(c) == "private" for c in ctx)
    rendered = "\n".join(c.render() for c in ctx)
    body = f"{user}\n\n{rendered}" if rendered else user
    msgs = (Message(role="system", content=system), Message(role="user", content=body))
    cls = PrivatePrompt if private else PublicPrompt
    return cls(node=node, messages=msgs, schema_name=schema_name)
