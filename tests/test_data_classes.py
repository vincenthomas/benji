from decimal import Decimal

import pytest
from pydantic import Field, ValidationError

from benji.data.classes import (
    Private,
    PrivatePrompt,
    Public,
    PublicPrompt,
    build_prompt,
    classify,
)


class Quote(Public):
    ticker: str
    price: Decimal


class Position(Private):
    ticker: str
    qty: Decimal
    basis: Decimal


class Report(Public):
    ticker: str
    quotes: list[Quote] = Field(default_factory=list)
    notes: dict[str, str] = Field(default_factory=dict)


class Book(Private):
    positions: list[Position]
    cash: Decimal


def test_public_cannot_contain_private_anywhere():
    pos = Position(ticker="AAPL", qty=Decimal(10), basis=Decimal(100))

    class Bad(Public):
        inner: Position

    with pytest.raises(ValidationError, match="contains Private data at inner"):
        Bad(inner=pos)

    class BadList(Public):
        items: list[Position | Quote]

    with pytest.raises(ValidationError, match=r"items\[0\]"):
        BadList(items=[pos])


def test_private_may_contain_public_and_nested_private():
    q = Quote(ticker="AAPL", price=Decimal(230))

    class View(Private):
        book: Book
        quote: Quote

    v = View(book=Book(positions=[Position(ticker="AAPL", qty=Decimal(1), basis=Decimal(1))], cash=Decimal(0)),
             quote=q)
    assert classify(v) == "private" and classify(q) == "public"


def test_classify_sees_through_containers():
    pos = Position(ticker="AAPL", qty=Decimal(10), basis=Decimal(100))
    assert classify([Quote(ticker="A", price=Decimal(1)), {"x": [pos]}]) == "private"
    assert classify({"a": [Quote(ticker="A", price=Decimal(1))], "n": 3}) == "public"
    assert classify("just a string") == "public"


def test_models_are_frozen_and_strict():
    q = Quote(ticker="AAPL", price=Decimal(1))
    with pytest.raises(ValidationError):
        q.ticker = "MSFT"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        Quote(ticker="AAPL", price=Decimal(1), extra="no")  # type: ignore[call-arg]


def test_prompt_class_follows_context():
    q = Quote(ticker="AAPL", price=Decimal(230))
    pos = Position(ticker="AAPL", qty=Decimal(10), basis=Decimal(100))
    p = build_prompt("market_analyst", "You are an analyst.", "Assess AAPL.", [q])
    assert isinstance(p, PublicPrompt) and p.data_class == "public"
    p2 = build_prompt("allocator", "You size positions.", "Size AAPL.", [q, pos])
    assert isinstance(p2, PrivatePrompt) and p2.data_class == "private"
    assert "Position:" in p2.text() and '"basis": "100"' in p2.text()


def test_prompt_is_frozen_hashable_and_hash_is_stable():
    q = Quote(ticker="AAPL", price=Decimal(230))
    a = build_prompt("n", "s", "u", [q])
    b = build_prompt("n", "s", "u", [Quote(ticker="AAPL", price=Decimal(230))])
    c = build_prompt("n", "s", "u!", [q])
    assert a.hash == b.hash and a.hash != c.hash and len(a.hash) == 24
    with pytest.raises(ValidationError):
        a.node = "other"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        PublicPrompt(node="n", messages=())


def test_no_way_to_coerce_private_prompt_to_public():
    pos = Position(ticker="AAPL", qty=Decimal(10), basis=Decimal(100))
    p = build_prompt("allocator", "s", "u", [pos])
    assert not isinstance(p, PublicPrompt)
    # Rebuilding from the same messages under the Public class is the only route, and it is
    # a deliberate act with no helper; it must never appear in src/ (grep-tested in 0.3).
    assert isinstance(PublicPrompt(node=p.node, messages=p.messages), PublicPrompt)
