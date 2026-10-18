"""Fake HTTP transport for provider tests.

Maps (method, URL) pairs to pre-configured responses.
Records all requests for assertion.  Fails on unexpected requests.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Optional

from docker.versioning.providers.base import HttpResponse, HttpTransport


@dataclass
class FakeHttpTransport:
    """A fake HTTP transport that returns fixtures keyed by (method, URL).

    Usage::

        fake = FakeHttpTransport()
        fake.set("GET", "https://registry.npmjs.org/foo",
                 status=200, body=b'{"versions": {}}')
        result = fake.request("GET", "https://registry.npmjs.org/foo")
        assert fake.requests == [("GET", "https://registry.npmjs.org/foo", {})]
    """

    _responses: dict[tuple[str, str], HttpResponse] = field(default_factory=dict)
    _exceptions: dict[tuple[str, str], Exception] = field(default_factory=dict)
    requests: list[tuple[str, str, Mapping[str, str]]] = field(default_factory=list)

    def set_exception(
        self,
        method: str,
        url: str,
        exc: Exception,
    ) -> None:
        self._exceptions[(method, url)] = exc

    def set(
        self,
        method: str,
        url: str,
        *,
        status: int = 200,
        headers: Optional[Mapping[str, str]] = None,
        body: bytes = b"",
    ) -> None:
        self._responses[(method, url)] = HttpResponse(
            status=status,
            headers=dict(headers or {}),
            body=body,
        )

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] = (),
        nocache: bool = False,
    ) -> HttpResponse:
        self.requests.append((method, url, dict(headers)))
        key = (method, url)

        if key in self._exceptions:
            raise self._exceptions[key]

        if key not in self._responses:
            raise AssertionError(
                f"Unexpected HTTP request: {method} {url}\n"
                f"Known: {list(self._responses.keys())}"
            )
        return self._responses[key]


class FailingHttpTransport:
    """An HTTP transport that fails on any request — for regression tests."""

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] = (),
        nocache: bool = False,
    ) -> HttpResponse:
        raise AssertionError(f"Unexpected HTTP request: {method} {url}")
