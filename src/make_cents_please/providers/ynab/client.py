"""Thin wrapper around the official ``ynab`` SDK.

Responsibilities:
- resolve which plan (budget) to use,
- track the 200 requests/hour rate limit,
- cache responses in memory and refresh them with YNAB delta requests,
- turn API failures into short, actionable messages (never containing the token).
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import deque
from collections.abc import Callable, Hashable
from datetime import UTC, datetime
from typing import Any

import urllib3
import ynab
import ynab.exceptions
from pydantic import ValidationError

from ...config import Config
from ...logging_setup import redact
from .cache import ResponseCache, merge_entities

logger = logging.getLogger(__name__)

RATE_LIMIT_PER_HOUR = 200
RATE_WINDOW_SECONDS = 3600
# Above this many requests in the window, serve cached data (even stale) instead of calling.
RATE_SOFT_LIMIT = 180


_MAYBE_APPLIED = (
    " The change may or may not have been applied; check the current data before retrying."
)


class YnabError(Exception):
    """An error with a message that is safe and useful to show the model."""


class RateLimitError(YnabError):
    pass


class RateTracker:
    """Counts requests in a sliding one-hour window, informed by YNAB's X-Rate-Limit header."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._calls: deque[float] = deque()
        self._lock = threading.Lock()
        self.header_used: int | None = None
        self.header_limit: int | None = None

    def _prune(self) -> None:
        cutoff = self._clock() - RATE_WINDOW_SECONDS
        while self._calls and self._calls[0] <= cutoff:
            self._calls.popleft()

    def used(self) -> int:
        with self._lock:
            self._prune()
            local = len(self._calls)
        # YNAB's own count can be higher if the token is used elsewhere too.
        return max(local, self.header_used or 0)

    def limit(self) -> int:
        return self.header_limit or RATE_LIMIT_PER_HOUR

    def record(self) -> None:
        with self._lock:
            self._calls.append(self._clock())

    def update_from_header(self, value: str | None) -> None:
        # Format is "<used>/<limit>", e.g. "36/200".
        if not value or "/" not in value:
            return
        used, _, limit = value.partition("/")
        try:
            self.header_used, self.header_limit = int(used), int(limit)
        except ValueError:
            return

    def retry_after_minutes(self) -> int:
        with self._lock:
            self._prune()
            if not self._calls:
                return 60
            wait = self._calls[0] + RATE_WINDOW_SECONDS - self._clock()
        return max(1, int(wait // 60) + 1)

    def near_limit(self) -> bool:
        return self.used() >= RATE_SOFT_LIMIT

    def exhausted(self) -> bool:
        return self.used() >= self.limit()


def merge_collections(
    collections: dict[str, dict[str, str] | None], root: str | None = None
) -> Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]:
    """Build a merge function for delta responses.

    ``collections`` maps a list field in the response (e.g. ``"accounts"``) to an optional
    nested-merge spec. ``root`` names a wrapper object (``"plan"`` for the full plan export).
    """

    def merge(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
        result = dict(new)
        old_body = old.get(root, {}) if root else old
        new_body = dict(new.get(root, {})) if root else result
        for name, nested in collections.items():
            new_body[name] = merge_entities(
                old_body.get(name) or [], new_body.get(name) or [], nested
            )
        if root:
            result[root] = new_body
        return result

    return merge


def raw_operation(api: Any, operation: str) -> Callable[..., Any]:
    """Send an SDK operation without its model validation.

    The SDK's request models drop fields set to ``None``, but some YNAB endpoints use an
    explicit ``null`` to clear a value (e.g. a category's goal_target). Bodies sent this
    way keep nulls, so callers must validate inputs themselves.
    """
    serialize = getattr(api, f"_{operation}_serialize")

    def send(**kwargs: Any) -> Any:
        params = serialize(
            **kwargs, _request_auth=None, _content_type=None, _headers=None, _host_index=0
        )
        return api.api_client.call_api(*params).response

    return send


class YnabClient:
    def __init__(
        self,
        config: Config,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._secret = config.token.reveal()
        self.default_plan_id = config.default_plan_id
        configuration = ynab.Configuration(access_token=self._secret)
        self._api_client = ynab.ApiClient(configuration)
        self.cache = ResponseCache(config.cache_ttl_seconds, clock=clock)
        self.rate = RateTracker(clock=clock)

        self.user_api = ynab.UserApi(self._api_client)
        self.plans_api = ynab.PlansApi(self._api_client)
        self.accounts_api = ynab.AccountsApi(self._api_client)
        self.categories_api = ynab.CategoriesApi(self._api_client)
        self.months_api = ynab.MonthsApi(self._api_client)
        self.payees_api = ynab.PayeesApi(self._api_client)
        self.payee_locations_api = ynab.PayeeLocationsApi(self._api_client)
        self.transactions_api = ynab.TransactionsApi(self._api_client)
        self.scheduled_api = ynab.ScheduledTransactionsApi(self._api_client)
        self.money_movements_api = ynab.MoneyMovementsApi(self._api_client)

    # ------------------------------------------------------------------ helpers

    def plan(self, plan_id: str | None) -> str:
        return (plan_id or "").strip() or self.default_plan_id

    def _safe(self, text: str) -> str:
        return redact(text, [self._secret])

    def call(
        self, method: Callable[..., Any], *, write: bool = False, **kwargs: Any
    ) -> dict[str, Any]:
        """Make one API request via an SDK ``*_without_preload_content`` method.

        Returns the parsed ``data`` object of the JSON response. ``write`` marks a request
        that changes data, so failures say whether the change might have been applied and
        the cache is cleared afterwards.
        """
        if self.rate.exhausted():
            raise RateLimitError(
                f"YNAB rate limit reached ({self.rate.used()}/{self.rate.limit()} requests this "
                f"hour). Try again in about {self.rate.retry_after_minutes()} minutes."
            )
        try:
            response = method(**kwargs)
            status = int(response.status)
            body = response.data
            headers = response.headers or {}
        except ValidationError as exc:
            # The SDK validates arguments (e.g. ids must be UUIDs) before sending anything.
            problems = [
                f"{'.'.join(str(p) for p in err['loc'] if p != 'data')}: {err['msg']}"
                for err in exc.errors()[:3]
            ]
            raise YnabError(
                f"Invalid value ({'; '.join(problems)}). Ids must be UUIDs taken from the "
                "matching list_* tool, and dates must be YYYY-MM-DD. Nothing was sent to YNAB."
            ) from None
        except ynab.exceptions.ApiException as exc:
            self.rate.record()
            logger.warning("YNAB request failed: %s", self._safe(str(exc.reason or exc.status)))
            raise YnabError(
                "Could not reach the YNAB API (connection or TLS error). Check the network "
                "connection and try again." + (_MAYBE_APPLIED if write else "")
            ) from None
        except (urllib3.exceptions.HTTPError, OSError) as exc:
            self.rate.record()
            logger.warning("YNAB request failed: %s", self._safe(type(exc).__name__))
            raise YnabError(
                "Could not reach the YNAB API (network error). Check the network connection "
                "and try again." + (_MAYBE_APPLIED if write else "")
            ) from None

        self.rate.record()
        self.rate.update_from_header(headers.get("X-Rate-Limit"))
        if write:
            # Any cached view may now be out of date.
            self.cache.clear()
        payload = self._parse(body)
        if 200 <= status < 300:
            data = payload.get("data")
            if not isinstance(data, dict):
                raise YnabError("YNAB returned an unexpected response (no data object).")
            return data
        raise self._error_for(status, payload)

    @staticmethod
    def _parse(body: bytes | str | None) -> dict[str, Any]:
        if not body:
            return {}
        try:
            parsed = json.loads(body)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}

    def _error_for(self, status: int, payload: dict[str, Any]) -> YnabError:
        error = payload.get("error") or {}
        detail = self._safe(str(error.get("detail") or "")).strip()
        suffix = f" (YNAB says: {detail})" if detail else ""
        if status == 401:
            return YnabError(
                "YNAB rejected the access token (401). The token is missing, revoked or "
                "expired; create a new one at https://app.ynab.com/settings/developer and "
                "update YNAB_ACCESS_TOKEN."
            )
        if status == 403:
            return YnabError(
                "YNAB denied access (403). The subscription may have lapsed or the token lacks "
                f"access to this plan.{suffix}"
            )
        if status == 404:
            return YnabError(
                "Not found (404): an id or month in the request does not exist. Use list_plans, "
                f"list_accounts, list_categories, list_payees or list_months to get valid ids."
                f"{suffix}"
            )
        if status == 429:
            self.rate.header_used = self.rate.limit()
            return RateLimitError(
                "YNAB rate limit reached (429: 200 requests per hour per token). Try again in "
                f"about {self.rate.retry_after_minutes()} minutes."
            )
        if status == 400:
            return YnabError(f"YNAB rejected the request as invalid (400).{suffix}")
        if status >= 500:
            return YnabError(
                f"YNAB had a server error ({status}). This is usually temporary; try again "
                "in a few minutes."
            )
        return YnabError(f"YNAB request failed with HTTP {status}.{suffix}")

    def cached(
        self,
        key: Hashable,
        fetch: Callable[[int | None], dict[str, Any]],
        merge: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]] | None = None,
    ) -> tuple[dict[str, Any], str | None]:
        """Return (data, warning). Uses the cache and, when ``merge`` is given, delta refreshes.

        ``fetch`` receives ``last_knowledge_of_server`` (or None for a full fetch).
        """
        entry = self.cache.get(key)
        if entry and self.cache.is_fresh(entry):
            return entry.data, None
        if entry and self.rate.near_limit():
            return entry.data, self._stale_warning(entry.wall_time)

        knowledge = entry.server_knowledge if (entry and merge) else None
        try:
            data = fetch(knowledge)
        except RateLimitError:
            if entry:
                return entry.data, self._stale_warning(entry.wall_time)
            raise
        if entry and merge and knowledge is not None:
            data = merge(entry.data, data)
        self.cache.put(key, data, data.get("server_knowledge"))
        return data, None

    @staticmethod
    def _stale_warning(wall_time: float) -> str:
        stamp = datetime.fromtimestamp(wall_time, UTC).strftime("%Y-%m-%d %H:%M UTC")
        return (
            f"Showing cached data from {stamp} because the YNAB rate limit is nearly or fully "
            "used; it may be out of date."
        )

    def status(self) -> dict[str, Any]:
        return {
            "requests_used_this_hour": self.rate.used(),
            "request_limit_per_hour": self.rate.limit(),
            "cached_responses": len(self.cache),
            "cache_ttl_seconds": self.cache.ttl_seconds,
            "default_plan_id": self.default_plan_id,
        }
