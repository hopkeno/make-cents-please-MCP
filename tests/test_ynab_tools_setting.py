"""YNAB_TOOLS=off hides the ynab_* tools (e.g. alongside YNAB's own MCP server)."""

from __future__ import annotations

import pytest

from make_cents_please.config import ConfigError, load_config
from make_cents_please.server import build_server

from .conftest import TOKEN


async def tool_names(config) -> set[str]:
    return {t.name for t in await build_server(config).list_tools()}


def test_defaults_to_on():
    assert load_config({"YNAB_ACCESS_TOKEN": "x"}).ynab_tools is True


@pytest.mark.parametrize("value", ["off", "false", "0", "no", "OFF"])
def test_off_values(tmp_path, value):
    config = load_config(
        {"YNAB_ACCESS_TOKEN": "x", "YNAB_TOOLS": value, "CHARITYRECORD_DIR": str(tmp_path)}
    )
    assert config.ynab_tools is False


def test_bad_value():
    with pytest.raises(ConfigError, match="YNAB_TOOLS"):
        load_config({"YNAB_ACCESS_TOKEN": "x", "YNAB_TOOLS": "sometimes"})


def test_off_without_charityrecord_has_nothing_to_serve():
    with pytest.raises(ConfigError, match="no tools to serve"):
        load_config({"YNAB_ACCESS_TOKEN": "x", "YNAB_TOOLS": "off"})


def test_token_still_required_when_on():
    with pytest.raises(ConfigError, match="YNAB_ACCESS_TOKEN"):
        load_config({})


async def test_off_with_token_keeps_ynab_backed_charityrecord_tools(fake, tmp_path):
    config = load_config(
        {
            "YNAB_ACCESS_TOKEN": TOKEN,
            "YNAB_TOOLS": "off",
            "YNAB_ALLOW_WRITES": "true",  # ignored while YNAB tools are off
            "CHARITYRECORD_DIR": str(tmp_path),
        }
    )
    names = await tool_names(config)
    assert not any(n.startswith("ynab_") for n in names)
    assert "charityrecord_reconcile" in names
    assert "charityrecord_find_ynab_donations" in names
    assert "charityrecord_create_import_files" in names


async def test_off_without_token_drops_only_ynab_backed_tools(tmp_path):
    config = load_config({"YNAB_TOOLS": "off", "CHARITYRECORD_DIR": str(tmp_path)})
    assert config.token is None
    names = await tool_names(config)
    assert not any(n.startswith("ynab_") for n in names)
    assert "charityrecord_reconcile" not in names
    assert "charityrecord_find_ynab_donations" not in names
    assert {"charityrecord_list_donations", "charityrecord_create_import_files"} <= names


async def test_reconcile_works_with_ynab_tools_off(fake, tmp_path):
    from mcp import Client

    (tmp_path / "cash.csv").write_text(
        '"Charity","Charity Address","Donation Date","Payment Type",'
        '"Addtional Payment Details","Additional Description","Receipt",'
        '"Donation Value in $","Recurring Frequency","Donation Notes"\r\n'
        '"Example Properties","","October 2, 2026","Check","","","Yes","1200","",""\r\n',
        encoding="utf-8",
    )
    config = load_config(
        {"YNAB_ACCESS_TOKEN": TOKEN, "YNAB_TOOLS": "off", "CHARITYRECORD_DIR": str(tmp_path)}
    )
    async with Client(build_server(config)) as client:
        result = await client.call_tool(
            "charityrecord_reconcile",
            {
                "since_date": "2026-10-01",
                "until_date": "2026-10-31",
                "payee_names": ["Example Properties"],
            },
        )
    assert not result.is_error
    assert result.structured_content["matched"] == 1


def test_main_starts_without_token_when_ynab_off(tmp_path):
    import os
    import subprocess
    import sys

    env = {k: v for k, v in os.environ.items() if k != "YNAB_ACCESS_TOKEN"}
    env.update({"YNAB_TOOLS": "off", "CHARITYRECORD_DIR": str(tmp_path)})
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "from make_cents_please.config import load_config; "
            "c = load_config(); print(c.ynab_tools, c.token)",
        ],
        env=env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "False None"
