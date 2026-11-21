"""The token must never be logged, echoed or returned."""

from __future__ import annotations

import logging

import pytest

from make_cents_please.config import ConfigError, Secret, load_config
from make_cents_please.logging_setup import RedactingFilter, configure_logging, redact

from .conftest import TOKEN


def test_secret_repr_hides_value():
    secret = Secret(TOKEN)
    assert TOKEN not in repr(secret) and TOKEN not in str(secret)
    assert secret.reveal() == TOKEN


def test_config_repr_hides_token():
    config = load_config({"YNAB_ACCESS_TOKEN": TOKEN})
    assert TOKEN not in repr(config)
    assert config.default_plan_id == "last-used"


def test_missing_token():
    with pytest.raises(ConfigError, match="YNAB_ACCESS_TOKEN"):
        load_config({})


def test_bad_ttl():
    with pytest.raises(ConfigError):
        load_config({"YNAB_ACCESS_TOKEN": "x", "YNAB_CACHE_TTL_SECONDS": "soon"})


def test_redact_token_and_bearer_header():
    text = f"token={TOKEN} Authorization: Bearer someOtherValue123"
    out = redact(text, [TOKEN])
    assert TOKEN not in out and "someOtherValue123" not in out


def test_logging_filter_redacts_message_args_and_exceptions(capsys):
    configure_logging("DEBUG", [TOKEN])
    log = logging.getLogger("make_cents_please.test")
    log.warning("calling with %s", TOKEN)
    try:
        raise RuntimeError(f"failed with Bearer {TOKEN}")
    except RuntimeError:
        log.exception("boom")
    captured = capsys.readouterr()
    assert captured.out == ""  # stdout is reserved for the MCP protocol
    assert TOKEN not in captured.err
    assert "calling with ***" in captured.err


def test_filter_instance_handles_plain_record():
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "hi %s", (TOKEN,), None)
    RedactingFilter([TOKEN]).filter(record)
    assert record.getMessage() == "hi ***"


async def test_token_never_in_tool_output(call, fake, caplog):
    caplog.set_level(logging.DEBUG)
    fake.respond(r"/payees$", 500, {"error": {"detail": TOKEN}})
    outputs = [
        await call("ynab_list_accounts"),
        await call("ynab_list_payees"),
        await call("ynab_get_api_status"),
    ]
    for _, payload in outputs:
        assert TOKEN not in str(payload)
    # The token is only ever sent as the Authorization header to YNAB.
    assert fake.calls[0].headers["Authorization"] == f"Bearer {TOKEN}"
