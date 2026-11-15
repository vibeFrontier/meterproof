"""A fictional usage-billed SaaS with planted billing mistakes.

"Relay API" bills $0.01 per API call through one Stripe meter. Its ledger and the meter events Stripe
received should agree, but six things went wrong this month:

1. acct_quarry: a usage worker crashed for 30 hours and its events never reached Stripe (90,000 calls,
   the missed $900 charge).
2. acct_tallow: a retry storm resent events with fresh identifiers, so Stripe counted them twice.
3. acct_ridge: a new signup never got a Stripe customer id in the mapping.
4. acct_pine: checkout was abandoned, so there is no subscription, but the app kept sending usage.
5. cus_demo_orphan: a staging environment sent usage to a live customer the ledger knows nothing about.
6. acct_vale: the newest events are still being processed by Stripe. That is timing noise, not a miss,
   and the settle window keeps it out of the report.
"""

from __future__ import annotations

import csv
import json
import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

METER_ID = "mtr_demo_relay_api_calls"
PRICE_ID = "price_demo_relay_per_call"

# internal id, Stripe customer id (None = never mapped), average calls per 15 minutes
CUSTOMERS = [
    ("acct_harbor", "cus_demo_harbor", 410),
    ("acct_lumen", "cus_demo_lumen", 260),
    ("acct_quarry", "cus_demo_quarry", 600),
    ("acct_tallow", "cus_demo_tallow", 330),
    ("acct_ridge", None, 22),
    ("acct_pine", "cus_demo_pine", 6),
    ("acct_vale", "cus_demo_vale", 180),
    ("acct_birch", "cus_demo_birch", 35),
]


@dataclass(frozen=True)
class Event:
    customer: str
    timestamp: datetime
    value: int


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def build(now: datetime, seed: int = 31) -> dict:
    """Return the ledger, the Stripe-side events and metadata for a month-to-date window ending at now."""
    rng = random.Random(seed)
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if (now - start) < timedelta(days=3):
        start = (start - timedelta(days=1)).replace(day=1)
    ledger: list[Event] = []
    t = start
    step = timedelta(minutes=15)
    while t < now - timedelta(minutes=5):
        for internal, _stripe, avg in CUSTOMERS:
            if internal == "acct_ridge" and t < start + (now - start) * 0.65:
                continue  # signed up partway through the month
            ledger.append(Event(internal, t + timedelta(seconds=rng.randint(0, 899)), max(1, int(rng.gauss(avg, avg * 0.2)))))
        t += step
    ledger = [e for e in ledger if e.timestamp < now - timedelta(minutes=5)]
    ledger.sort(key=lambda e: (e.timestamp, e.customer))

    mapping = {internal: stripe for internal, stripe, _ in CUSTOMERS}
    span = now - start
    outage_start = (start + span * 0.55).replace(minute=0, second=0, microsecond=0)
    outage_end = outage_start + timedelta(hours=30)
    retry_day = (start + span * 0.8).replace(hour=14, minute=0, second=0, microsecond=0)
    lag_cutoff = now - timedelta(minutes=50)

    # Plant exactly 90,000 calls in acct_quarry's outage window: rewrite its rows there to 750 each.
    rewritten: list[Event] = []
    for e in ledger:
        if e.customer == "acct_quarry" and outage_start <= e.timestamp < outage_end:
            e = Event(e.customer, e.timestamp, 750)
        rewritten.append(e)
    ledger = rewritten

    stripe_events: list[Event] = []
    for e in ledger:
        target = mapping[e.customer]
        if target is None:
            continue  # never mapped, so the app could not send it
        if e.customer == "acct_quarry" and outage_start <= e.timestamp < outage_end:
            continue  # worker crashed
        if e.customer == "acct_vale" and e.timestamp >= lag_cutoff:
            continue  # Stripe has not finished processing these yet
        stripe_events.append(Event(target, e.timestamp, e.value))
        if e.customer == "acct_tallow" and retry_day <= e.timestamp < retry_day + timedelta(hours=3):
            stripe_events.append(Event(target, e.timestamp + timedelta(seconds=2), e.value))  # retried twice
    orphan_t = start + span * 0.4
    for i in range(12):
        stripe_events.append(Event("cus_demo_orphan", orphan_t + timedelta(minutes=17 * i), 125))
    stripe_events.sort(key=lambda e: (e.timestamp, e.customer))

    subscriptions = [
        {"customer": stripe, "price": PRICE_ID, "status": "active"}
        for internal, stripe, _ in CUSTOMERS
        if stripe and internal != "acct_pine"
    ]
    subscriptions.append({"customer": "cus_demo_orphan", "price": PRICE_ID, "status": "active"})
    return {
        "start": start,
        "now": now,
        "ledger": ledger,
        "stripe_events": stripe_events,
        "mapping": mapping,
        "subscriptions": subscriptions,
    }


CONFIG = """\
# meterproof demo: Relay API, $0.01 per API call, one Stripe meter.
# The Stripe side comes from a local fixture so the demo needs no Stripe account.
version: 1
meter:
  id: {meter}
ledger:
  csv: ledger.csv
  columns:
    customer: account_id
    timestamp: occurred_at
    quantity: units
customers:
  mapping_csv: customers.csv
  price: {price}
stripe:
  fixture: stripe-fixture.json
window:
  settle_minutes: 60
tolerance:
  units: 0
  percent: 0
report:
  markdown: meterproof-report.md
  json: meterproof-report.json
"""


def write(directory: Path, now: datetime) -> dict:
    data = build(now)
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "ledger.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["account_id", "occurred_at", "units"])
        for e in data["ledger"]:
            w.writerow([e.customer, _iso(e.timestamp), e.value])
    with (directory / "customers.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["customer", "stripe_customer"])
        for internal, stripe in data["mapping"].items():
            w.writerow([internal, stripe or ""])
    with (directory / "stripe-meter-events.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["stripe_customer", "timestamp", "value"])
        for e in data["stripe_events"]:
            w.writerow([e.customer, _iso(e.timestamp), e.value])
    fixture = {
        "meter": {"id": METER_ID, "formula": "sum", "display_name": "Relay API calls"},
        "prices": {
            PRICE_ID: {
                "currency": "usd",
                "billing_scheme": "per_unit",
                "unit_amount_decimal": "1",
                "recurring": {"meter": METER_ID, "usage_type": "metered", "interval": "month"},
            }
        },
        "subscriptions": data["subscriptions"],
        "events_csv": "stripe-meter-events.csv",
    }
    (directory / "stripe-fixture.json").write_text(json.dumps(fixture, indent=2) + "\n", encoding="utf-8")
    (directory / "meterproof.yml").write_text(CONFIG.format(meter=METER_ID, price=PRICE_ID), encoding="utf-8")
    (directory / "seed.sql").write_text(postgres_seed(data), encoding="utf-8")
    return data


def postgres_seed(data: dict) -> str:
    lines = [
        "-- Relay API demo ledger for Postgres. Load with: psql \"$DATABASE_URL\" -f seed.sql",
        "CREATE TABLE IF NOT EXISTS usage_events (",
        "  id bigserial PRIMARY KEY,",
        "  account_id text NOT NULL,",
        "  occurred_at timestamptz NOT NULL,",
        "  units bigint NOT NULL",
        ");",
        "CREATE TABLE IF NOT EXISTS billing_customers (",
        "  account_id text PRIMARY KEY,",
        "  stripe_customer_id text",
        ");",
        "TRUNCATE usage_events, billing_customers;",
    ]
    for internal, stripe in data["mapping"].items():
        value = f"'{stripe}'" if stripe else "NULL"
        lines.append(f"INSERT INTO billing_customers VALUES ('{internal}', {value});")
    batch: list[str] = []
    for e in data["ledger"]:
        batch.append(f"('{e.customer}', '{_iso(e.timestamp)}', {e.value})")
        if len(batch) == 500:
            lines.append("INSERT INTO usage_events (account_id, occurred_at, units) VALUES\n" + ",\n".join(batch) + ";")
            batch = []
    if batch:
        lines.append("INSERT INTO usage_events (account_id, occurred_at, units) VALUES\n" + ",\n".join(batch) + ";")
    return "\n".join(lines) + "\n"


DEMO_NOW = datetime(2026, 11, 15, 10, 30, tzinfo=timezone.utc)
