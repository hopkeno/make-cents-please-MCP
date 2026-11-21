"""Two-step confirmation for tools that change YNAB data.

The first call (without a token) returns a preview and a one-time confirmation token.
The token is bound to the tool and to the exact request that was previewed, expires after
a few minutes and can be used once. The write only happens when the same request is sent
again with that token, so what executes is always what the user was shown.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

TOKEN_TTL_SECONDS = 600
MAX_PENDING = 50

CONFIRM_INSTRUCTIONS = (
    "Nothing has been changed yet. Show this preview to the user and ask whether to proceed. "
    "Only if the user explicitly agrees, call this tool again with exactly the same arguments "
    "plus confirmation_token. If anything should change, call again without a token to get a "
    "new preview."
)


class ConfirmationError(Exception):
    """The confirmation token is missing, expired, reused or does not match the request."""


@dataclass
class _Pending:
    tool: str
    fingerprint: str
    expires_at: float


def fingerprint(tool: str, request: Any) -> str:
    canonical = json.dumps([tool, request], sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


class ConfirmationStore:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._pending: dict[str, _Pending] = {}
        self._lock = threading.Lock()

    def _prune(self) -> None:
        now = self._clock()
        for token in [t for t, p in self._pending.items() if p.expires_at <= now]:
            del self._pending[token]
        while len(self._pending) > MAX_PENDING:
            del self._pending[next(iter(self._pending))]

    def issue(self, tool: str, request: Any) -> str:
        token = secrets.token_hex(4)
        with self._lock:
            self._prune()
            self._pending[token] = _Pending(
                tool, fingerprint(tool, request), self._clock() + TOKEN_TTL_SECONDS
            )
        return token

    def redeem(self, token: str, tool: str, request: Any) -> None:
        """Consume ``token`` if it matches this tool and request; raise otherwise."""
        with self._lock:
            self._prune()
            pending = self._pending.get(token.strip())
            if pending is None:
                raise ConfirmationError(
                    "confirmation_token is unknown, already used or expired (tokens last "
                    f"{TOKEN_TTL_SECONDS // 60} minutes). Call this tool again without a token "
                    "to get a fresh preview, and confirm with the user before using it."
                )
            if pending.tool != tool or pending.fingerprint != fingerprint(tool, request):
                raise ConfirmationError(
                    "The arguments differ from the ones that were previewed, so nothing was "
                    "changed. Call this tool again without a token to preview the new request "
                    "and confirm it with the user."
                )
            del self._pending[token.strip()]


def preview_response(summary: str, details: dict[str, Any], token: str) -> dict[str, Any]:
    return {
        "status": "confirmation_required",
        "summary": summary,
        "preview": details,
        "confirmation_token": token,
        "expires_in_seconds": TOKEN_TTL_SECONDS,
        "instructions": CONFIRM_INSTRUCTIONS,
    }
