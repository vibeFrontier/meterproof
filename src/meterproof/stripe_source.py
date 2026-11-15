"""What Stripe recorded: meter event summaries, the metered price and its subscriptions.

`StripeAPI` talks to api.stripe.com with a read-only restricted key. `StripeFixture` answers the same
questions from local files, for the demo and for tests.
"""

from __future__ import annotations

import csv
import http.client
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

from . import __version__
from .ledger import parse_timestamp

API_BASE = "https://api.stripe.com"
STRIPE_VERSION = "2026-10-28.endive"
# Subscription statuses that still produce invoices for metered usage.
BILLABLE_STATUSES = frozenset({"active", "past_due", "trialing", "unpaid"})


class StripeError(Exception):
    """Stripe refused or failed a request."""


@dataclass(frozen=True)
class Meter:
    id: str
    formula: str
    display_name: str
    status: str


@dataclass(frozen=True)
class Price:
    id: str
    currency: str
    billing_scheme: str  # per_unit | tiered
    unit_amount_decimal: Decimal | None  # minor units (cents), as Stripe reports it
    divide_by: int | None
    round: str | None
    meter: str | None


class StripeSource(Protocol):
    def meter(self, meter_id: str) -> Meter: ...
    def total(self, meter_id: str, customer: str, start: datetime, end: datetime) -> Decimal: ...
    def hourly(self, meter_id: str, customer: str, start: datetime, end: datetime) -> list[tuple[datetime, Decimal]]: ...
    def price(self, price_id: str) -> Price: ...
    def meter_prices(self, meter_id: str) -> list[Price]: ...
    def subscribers(self, price_ids: Iterable[str], since: datetime) -> dict[str, str]: ...


def billable_during(status: str | None, ended_at: int | None, since: datetime) -> bool:
    """A subscription can still invoice usage from the window if it is live or ended inside the window."""
    if status in BILLABLE_STATUSES:
        return True
    return status == "canceled" and ended_at is not None and ended_at >= _ts(since)


def _record(found: dict[str, tuple[bool, str]], customer: str, price_id: str, status: str | None) -> None:
    """Keep one price per customer, preferring a live subscription over one that ended in the window."""
    live = status in BILLABLE_STATUSES
    if customer not in found or (live and not found[customer][0]):
        found[customer] = (live, price_id)


def _ts(dt: datetime) -> int:
    return int(dt.timestamp())


def _price_from(obj: dict[str, Any]) -> Price:
    recurring = obj.get("recurring") or {}
    transform = obj.get("transform_quantity") or {}
    amount = obj.get("unit_amount_decimal")
    if amount is None and obj.get("unit_amount") is not None:
        amount = obj["unit_amount"]
    return Price(
        id=obj["id"],
        currency=str(obj.get("currency", "usd")).lower(),
        billing_scheme=obj.get("billing_scheme", "per_unit"),
        unit_amount_decimal=Decimal(str(amount)) if amount is not None else None,
        divide_by=int(transform["divide_by"]) if transform.get("divide_by") else None,
        round=transform.get("round"),
        meter=recurring.get("meter"),
    )


def _permission_for(path: str) -> str:
    """The restricted-key permission row (as named in the Stripe Dashboard) a read path needs."""
    if path.endswith("/event_summaries"):
        return "Billing meter events"
    if path.startswith("/v1/billing/meters"):
        return "Billing meters"
    if path.startswith("/v1/prices"):
        return "Prices"
    if path.startswith("/v1/subscriptions"):
        return "Subscriptions"
    return path


class StripeAPI:
    def __init__(self, api_key: str, *, base: str = API_BASE, max_retries: int = 4, opener=None):
        if not api_key:
            raise StripeError("no Stripe API key")
        self._key = api_key
        self._base = base.rstrip("/")
        self._max_retries = max_retries
        self._open = opener or urllib.request.urlopen
        self.requests = 0

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        query = urllib.parse.urlencode({k: v for k, v in (params or {}).items() if v is not None})
        url = f"{self._base}{path}" + (f"?{query}" if query else "")
        req = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {self._key}",
                "Stripe-Version": STRIPE_VERSION,
                "User-Agent": f"meterproof/{__version__}",
            },
        )
        delay = 1.0
        last_problem = "no response"
        for attempt in range(self._max_retries + 1):
            if attempt:
                time.sleep(delay)
                delay *= 2
            self.requests += 1
            try:
                with self._open(req, timeout=30) as resp:
                    raw = resp.read()
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", "replace")
                if exc.code == 429 or exc.code >= 500:
                    last_problem = f"HTTP {exc.code}"
                    continue  # GETs are safe to retry
                try:
                    message = json.loads(body)["error"]["message"]
                except (ValueError, KeyError, TypeError):
                    message = body[:200]
                if exc.code == 401:
                    raise StripeError("Stripe rejected the API key (401). Check the key and its mode (test/live).") from None
                if exc.code == 403:
                    raise StripeError(
                        f"the Stripe key needs Read on {_permission_for(path)} (403): {message}"
                    ) from None
                raise StripeError(f"Stripe {exc.code} on {path}: {message}") from None
            except (OSError, http.client.HTTPException) as exc:  # URLError, timeouts, resets, short reads
                last_problem = str(getattr(exc, "reason", exc)) or type(exc).__name__
                continue
            try:
                return json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                raise StripeError(f"Stripe returned an unreadable response on {path}") from None
        raise StripeError(f"could not get a response from Stripe on {path} after {self._max_retries + 1} tries ({last_problem})")

    def _list(self, path: str, params: dict[str, Any]) -> Iterator[dict[str, Any]]:
        params = dict(params, limit=100)
        while True:
            page = self._get(path, params)
            data = page.get("data", [])
            yield from data
            if not page.get("has_more") or not data:
                return
            params["starting_after"] = data[-1]["id"]

    def meter(self, meter_id: str) -> Meter:
        obj = self._get(f"/v1/billing/meters/{urllib.parse.quote(meter_id)}")
        return Meter(
            id=obj["id"],
            formula=(obj.get("default_aggregation") or {}).get("formula", "sum"),
            display_name=obj.get("display_name") or obj.get("event_name") or obj["id"],
            status=obj.get("status", "active"),
        )

    def total(self, meter_id: str, customer: str, start: datetime, end: datetime) -> Decimal:
        summaries = self._list(
            f"/v1/billing/meters/{urllib.parse.quote(meter_id)}/event_summaries",
            {"customer": customer, "start_time": _ts(start), "end_time": _ts(end)},
        )
        return sum((Decimal(str(s["aggregated_value"])) for s in summaries), Decimal(0))

    def hourly(self, meter_id: str, customer: str, start: datetime, end: datetime) -> list[tuple[datetime, Decimal]]:
        summaries = self._list(
            f"/v1/billing/meters/{urllib.parse.quote(meter_id)}/event_summaries",
            {
                "customer": customer,
                "start_time": _ts(start),
                "end_time": _ts(end),
                "value_grouping_window": "hour",
            },
        )
        return sorted(
            (datetime.fromtimestamp(s["start_time"], tz=timezone.utc), Decimal(str(s["aggregated_value"])))
            for s in summaries
        )

    def price(self, price_id: str) -> Price:
        return _price_from(self._get(f"/v1/prices/{urllib.parse.quote(price_id)}"))

    def meter_prices(self, meter_id: str) -> list[Price]:
        # Includes archived prices: legacy subscriptions can still bill through them.
        return [
            price
            for price in (_price_from(p) for p in self._list("/v1/prices", {"type": "recurring"}))
            if price.meter == meter_id
        ]

    def subscribers(self, price_ids: Iterable[str], since: datetime) -> dict[str, str]:
        found: dict[str, tuple[bool, str]] = {}
        for price_id in price_ids:
            for sub in self._list("/v1/subscriptions", {"price": price_id, "status": "all"}):
                if billable_during(sub.get("status"), sub.get("ended_at"), since):
                    customer = sub["customer"] if isinstance(sub["customer"], str) else sub["customer"]["id"]
                    _record(found, customer, price_id, sub.get("status"))
        return {customer: price for customer, (_, price) in found.items()}


def aggregate(values: Iterable[tuple[datetime, Decimal]], formula: str) -> Decimal:
    """Aggregate (timestamp, value) pairs the way a Stripe meter does."""
    items = list(values)
    if not items:
        return Decimal(0)
    if formula == "sum":
        return sum((v for _, v in items), Decimal(0))
    if formula == "count":
        return Decimal(len(items))
    if formula == "last":
        return max(items, key=lambda item: item[0])[1]
    raise ValueError(f"unknown aggregation formula {formula!r}")


class StripeFixture:
    """Offline stand-in for Stripe, built from a JSON file and a CSV of meter events.

    The events CSV has the columns stripe_customer, timestamp, value: one row per meter event Stripe
    accepted. Used by `meterproof demo` and the tests; never for real reconciliation.
    """

    def __init__(self, path: Path):
        try:
            spec = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise StripeError(f"cannot read Stripe fixture {path}: {exc}") from None
        m = spec["meter"]
        self._meter = Meter(m["id"], m.get("formula", "sum"), m.get("display_name", m["id"]), "active")
        self._prices = {pid: _price_from(dict(p, id=pid)) for pid, p in (spec.get("prices") or {}).items()}
        self._subs = spec.get("subscriptions") or []
        self._events: dict[str, list[tuple[datetime, Decimal]]] = {}
        events_path = path.parent / spec["events_csv"]
        with events_path.open(newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                self._events.setdefault(row["stripe_customer"], []).append(
                    (parse_timestamp(row["timestamp"]), Decimal(row["value"]))
                )
        self.requests = 0

    def _check_meter(self, meter_id: str) -> None:
        if meter_id != self._meter.id:
            raise StripeError(f"Stripe 404 on /v1/billing/meters/{meter_id}: No such billing meter")

    def meter(self, meter_id: str) -> Meter:
        self._check_meter(meter_id)
        return self._meter

    def total(self, meter_id: str, customer: str, start: datetime, end: datetime) -> Decimal:
        self._check_meter(meter_id)
        self.requests += 1
        events = (e for e in self._events.get(customer, []) if start <= e[0] < end)
        return aggregate(events, self._meter.formula)

    def hourly(self, meter_id: str, customer: str, start: datetime, end: datetime) -> list[tuple[datetime, Decimal]]:
        self._check_meter(meter_id)
        self.requests += 1
        buckets: dict[datetime, list[tuple[datetime, Decimal]]] = {}
        for ts, value in self._events.get(customer, []):
            if start <= ts < end:
                buckets.setdefault(ts.replace(minute=0, second=0, microsecond=0), []).append((ts, value))
        return sorted((hour, aggregate(items, self._meter.formula)) for hour, items in buckets.items())

    def price(self, price_id: str) -> Price:
        if price_id not in self._prices:
            raise StripeError(f"Stripe 404 on /v1/prices/{price_id}: No such price")
        return self._prices[price_id]

    def meter_prices(self, meter_id: str) -> list[Price]:
        return [p for p in self._prices.values() if p.meter == meter_id]

    def subscribers(self, price_ids: Iterable[str], since: datetime) -> dict[str, str]:
        wanted = set(price_ids)
        found: dict[str, tuple[bool, str]] = {}
        for s in self._subs:
            status = s.get("status", "active")
            ended = parse_timestamp(s["ended_at"]) if s.get("ended_at") else None
            if s.get("price") in wanted and billable_during(status, _ts(ended) if ended else None, since):
                _record(found, s["customer"], s["price"], status)
        return {customer: price for customer, (_, price) in found.items()}
