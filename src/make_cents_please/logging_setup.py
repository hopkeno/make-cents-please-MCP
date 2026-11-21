"""Logging to stderr with secret redaction.

stdout is the MCP stdio channel, so nothing may be logged there.
"""

from __future__ import annotations

import logging
import re
import sys

_BEARER = re.compile(r"(Bearer\s+)[A-Za-z0-9._\-~+/=]+", re.IGNORECASE)
REDACTED = "***"


def redact(text: str, secrets: list[str]) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, REDACTED)
    return _BEARER.sub(r"\1" + REDACTED, text)


class RedactingFilter(logging.Filter):
    """Rewrites every record so the token (or any bearer header) never reaches output."""

    def __init__(self, secrets: list[str]) -> None:
        super().__init__()
        self._secrets = [s for s in secrets if s]

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        if record.exc_info:
            message += "\n" + logging.Formatter().formatException(record.exc_info)
            record.exc_info = None
            record.exc_text = None
        record.msg = redact(message, self._secrets)
        record.args = None
        return True


def configure_logging(level: str, secrets: list[str]) -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    handler.addFilter(RedactingFilter(secrets))

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level if level in logging.getLevelNamesMapping() else "WARNING")

    # The SDK and HTTP layer can log request details at DEBUG; keep them quiet.
    for noisy in ("ynab", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
