from __future__ import annotations

import json
from typing import Any

import pytest
from mcp import Client

from make_cents_please.config import load_config
from make_cents_please.providers.ynab import YnabClient
from make_cents_please.server import build_server

from .fakes import FakeYnab

TOKEN = "test-token-DO-NOT-LEAK-1234567890"


class Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def fake(monkeypatch) -> FakeYnab:
    fake = FakeYnab()
    monkeypatch.setattr(
        "ynab.rest.RESTClientObject.request", lambda self, *a, **kw: fake.handle(*a, **kw)
    )
    return fake


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def allow_writes() -> bool:
    return False


@pytest.fixture
def charityrecord_dir() -> str:
    return ""


@pytest.fixture
def config(allow_writes, charityrecord_dir):
    return load_config(
        {
            "YNAB_ACCESS_TOKEN": TOKEN,
            "YNAB_CACHE_TTL_SECONDS": "300",
            "YNAB_ALLOW_WRITES": "true" if allow_writes else "false",
            "CHARITYRECORD_DIR": str(charityrecord_dir),
        }
    )


@pytest.fixture
def ynab_client(fake, config, clock) -> YnabClient:
    return YnabClient(config, clock=clock)


@pytest.fixture
def server(config, ynab_client):
    return build_server(config, ynab_client)


@pytest.fixture
def call(server):
    """Call a tool through a real in-process MCP client; returns (is_error, payload)."""

    async def _call(name: str, /, **arguments: Any) -> tuple[bool, Any]:
        async with Client(server) as client:
            result = await client.call_tool(name, arguments)
        text = "".join(getattr(c, "text", "") for c in result.content)
        if result.is_error:
            return True, text
        return False, result.structured_content or json.loads(text)

    return _call


@pytest.fixture
async def ok(call):
    """Call a tool and assert it succeeded; returns the payload."""

    async def _ok(name: str, /, **arguments: Any) -> Any:
        is_error, payload = await call(name, **arguments)
        assert not is_error, payload
        return payload

    return _ok
