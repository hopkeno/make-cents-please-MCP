"""End-to-end over the real stdio transport (no YNAB requests are made)."""

from __future__ import annotations

import os
import subprocess
import sys

from mcp import Client, StdioServerParameters

from .conftest import TOKEN


async def test_server_lists_tools_over_stdio(tmp_path):
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "make_cents_please"],
        env={**os.environ, "YNAB_ACCESS_TOKEN": TOKEN, "LOG_LEVEL": "DEBUG"},
        cwd=str(tmp_path),  # no .env here
    )
    async with Client(params) as client:
        tools = await client.list_tools()
        names = {t.name for t in tools.tools}
        assert "ynab_list_transactions" in names and len(names) == 21
        status = await client.call_tool("ynab_get_api_status", {})
        assert not status.is_error
        assert TOKEN not in str(status.content)


def test_missing_token_exits_with_clear_message(tmp_path):
    env = {k: v for k, v in os.environ.items() if k != "YNAB_ACCESS_TOKEN"}
    proc = subprocess.run(
        [sys.executable, "-m", "make_cents_please"],
        env=env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 2
    assert "YNAB_ACCESS_TOKEN is not set" in proc.stderr
    assert proc.stdout == ""
