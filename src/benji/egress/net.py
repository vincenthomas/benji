"""The one door to the network.

Every outbound HTTP request Benji makes goes through `Net.request`. It enforces an allow-list of
hosts, refuses to carry Private data to any host but localhost, and records
(host, purpose, bytes, data_class) for `benji egress audit`. Nothing else in the package may
import `urllib.request`, `requests`, or `httpx` directly (a test greps for it).
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from benji.data.classes import DataClass

LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


class EgressViolation(RuntimeError):
    """Raised before any bytes leave the process. Never catch this to continue."""


@dataclass(frozen=True)
class NetRecord:
    ts: datetime
    host: str
    purpose: str
    data_class: DataClass
    bytes_out: int
    bytes_in: int
    status: int | None


@dataclass
class Response:
    status: int
    body: bytes

    def json(self) -> Any:
        return json.loads(self.body.decode())


Transport = Callable[[str, str, dict[str, str], bytes | None, float], Response]


def _urllib_transport(method: str, url: str, headers: dict[str, str], body: bytes | None,
                      timeout: float) -> Response:
    req = urllib.request.Request(url, data=body, method=method, headers=headers)  # noqa: S310 — host already allow-listed
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            return Response(resp.status, resp.read())
    except urllib.error.HTTPError as e:
        return Response(e.code, e.read())


@dataclass
class Net:
    """Allow-listed, data-class-aware HTTP. One instance per run."""

    allowed_hosts: frozenset[str] = field(default_factory=lambda: frozenset(LOCAL_HOSTS))
    transport: Transport = _urllib_transport
    records: list[NetRecord] = field(default_factory=list)

    def allow(self, *hosts: str) -> Net:
        self.allowed_hosts = self.allowed_hosts | frozenset(h.lower() for h in hosts)
        return self

    def check(self, url: str, data_class: DataClass) -> str:
        host = (urllib.parse.urlsplit(url).hostname or "").lower()
        if not host:
            raise EgressViolation(f"no host in url {url!r}")
        if host not in self.allowed_hosts:
            raise EgressViolation(f"host {host!r} is not on the allow-list {sorted(self.allowed_hosts)}")
        if data_class == "private" and host not in LOCAL_HOSTS:
            raise EgressViolation(f"refusing to send Private data to non-local host {host!r}")
        return host

    def request(self, method: str, url: str, *, purpose: str, data_class: DataClass,
                json_body: Any | None = None, headers: dict[str, str] | None = None,
                timeout: float = 120.0) -> Response:
        host = self.check(url, data_class)
        body = json.dumps(json_body).encode() if json_body is not None else None
        hdrs = {"Content-Type": "application/json", **(headers or {})}
        status: int | None = None
        resp_len = 0
        try:
            resp = self.transport(method, url, hdrs, body, timeout)
            status, resp_len = resp.status, len(resp.body)
            return resp
        finally:
            self.records.append(NetRecord(datetime.now(UTC), host, purpose, data_class,
                                          len(body or b""), resp_len, status))

    def note(self, host: str, *, purpose: str, data_class: DataClass = "public") -> None:
        """Record egress performed by a delegated library (e.g. PortfolioPilot's vendor adapter)
        that does not go through `request`. Same allow-list and data-class rules apply, so the
        audit trail stays complete and a Private note to a remote host is still a violation."""
        self.check(f"https://{host}/", data_class)
        self.records.append(NetRecord(datetime.now(UTC), host.lower(), purpose, data_class, 0, 0, None))

    def hosts_contacted(self) -> set[str]:
        return {r.host for r in self.records}

    def non_local_hosts(self) -> set[str]:
        return {h for h in self.hosts_contacted() if h not in LOCAL_HOSTS}
