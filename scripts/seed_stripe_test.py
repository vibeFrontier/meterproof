#!/usr/bin/env python3
"""Seed a Stripe TEST-mode account with the meterproof demo, planted mistakes included.

    python3 scripts/seed_stripe_test.py --key-file stripe-test-setup.rk --out ./stripe-demo
    STRIPE_API_KEY="$(cat read-only.rk)" meterproof check -c ./stripe-demo/meterproof.yml

The key file holds a test-mode secret or restricted key (rk_test_ / sk_test_) and must be mode 0600. It
needs Write on Products, Prices, Customers, Subscriptions, Billing meters and Billing meter events.
Every run creates fresh customers and subscriptions (tagged with metadata meterproof_demo_run) and
reuses the demo meter, product and price when they already exist. Stdlib only.
"""

from __future__ import annotations

import argparse
import csv
import json
import secrets
import stat
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

from meterproof import demo
from meterproof.stripe_source import STRIPE_VERSION

API = "https://api.stripe.com"
EVENT_NAME = "meterproof_demo_api_call"
METER_NAME = "Relay API calls"
PRODUCT_NAME = "Relay API demo (meterproof test fixture)"
ORPHAN = "cus_demo_orphan"
WORKERS = 6

CONFIG = """\
# meterproof against Stripe test mode: Relay API demo, seed run {run}.
# Export the read-only restricted key as STRIPE_API_KEY, then: meterproof check -c meterproof.yml
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
  api_key_env: STRIPE_API_KEY
window:
  settle_minutes: 60
tolerance:
  units: 0
  percent: 0
report:
  markdown: meterproof-report.md
  json: meterproof-report.json
"""


class Stripe:
    def __init__(self, key: str):
        self._key = key

    def call(self, method: str, path: str, data: dict | None = None, idempotency_key: str | None = None) -> dict:
        body = urllib.parse.urlencode(list(_flatten(data or {}))).encode() if method == "POST" else None
        url = API + path + ("?" + urllib.parse.urlencode(list(_flatten(data or {}))) if method == "GET" and data else "")
        headers = {"Authorization": f"Bearer {self._key}", "Stripe-Version": STRIPE_VERSION}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        delay = 1.0
        for attempt in range(8):
            req = urllib.request.Request(url, data=body, method=method, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    return json.loads(resp.read())
            except urllib.error.HTTPError as exc:
                text = exc.read().decode("utf-8", "replace")
                if (exc.code == 429 or exc.code >= 500) and attempt < 7:
                    time.sleep(delay)
                    delay = min(delay * 2, 30)
                    continue
                try:
                    message = json.loads(text)["error"]["message"]
                except (ValueError, KeyError, TypeError):
                    message = text[:200]
                raise SystemExit(f"Stripe {exc.code} on {method} {path}: {message}") from None
            except urllib.error.URLError as exc:
                if attempt < 7:
                    time.sleep(delay)
                    delay = min(delay * 2, 30)
                    continue
                raise SystemExit(f"could not reach Stripe: {exc.reason}") from None
        raise SystemExit(f"Stripe kept failing on {method} {path}")

    def list(self, path: str, params: dict | None = None):
        params = dict(params or {}, limit=100)
        while True:
            page = self.call("GET", path, params)
            yield from page["data"]
            if not page.get("has_more") or not page["data"]:
                return
            params["starting_after"] = page["data"][-1]["id"]


def _flatten(data: dict, prefix: str = ""):
    for k, v in data.items():
        name = f"{prefix}[{k}]" if prefix else k
        if isinstance(v, dict):
            yield from _flatten(v, name)
        else:
            yield name, str(v).lower() if isinstance(v, bool) else str(v)


def read_key(path: Path) -> str:
    mode = path.stat().st_mode
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        sys.exit(f"{path} must not be readable by group or others (chmod 600)")
    key = path.read_text(encoding="utf-8").strip()
    if not key.startswith(("rk_test_", "sk_test_")):
        sys.exit("refusing to run: the key is not a test-mode key (rk_test_ / sk_test_)")
    return key


def find_or_create_meter(api: Stripe) -> str:
    for m in api.list("/v1/billing/meters", {"status": "active"}):
        if m["event_name"] == EVENT_NAME:
            return m["id"]
    meter = api.call(
        "POST",
        "/v1/billing/meters",
        {
            "display_name": METER_NAME,
            "event_name": EVENT_NAME,
            "default_aggregation": {"formula": "sum"},
            "customer_mapping": {"event_payload_key": "stripe_customer_id", "type": "by_id"},
            "value_settings": {"event_payload_key": "value"},
        },
    )
    return meter["id"]


def find_or_create_price(api: Stripe, meter_id: str) -> tuple[str, str]:
    product_id = next((p["id"] for p in api.list("/v1/products", {"active": True}) if p["name"] == PRODUCT_NAME), None)
    if product_id is None:
        product_id = api.call("POST", "/v1/products", {"name": PRODUCT_NAME})["id"]
    for price in api.list("/v1/prices", {"product": product_id, "active": True}):
        recurring = price.get("recurring") or {}
        if (
            recurring.get("meter") == meter_id
            and recurring.get("interval") == "month"
            and price.get("currency") == "usd"
            and price.get("unit_amount_decimal") in ("1", "1.0")
        ):
            return product_id, price["id"]
    price = api.call(
        "POST",
        "/v1/prices",
        {
            "product": product_id,
            "currency": "usd",
            "unit_amount_decimal": "1",
            "recurring": {"interval": "month", "usage_type": "metered", "meter": meter_id},
        },
    )
    return product_id, price["id"]


def hourly_events(events, real: dict[str, str], now: datetime):
    """Sum each customer's events per clock hour; one meter event per (customer, hour)."""
    sums: dict[tuple[str, datetime], int] = defaultdict(int)
    for e in events:
        sums[(real[e.customer], e.timestamp.replace(minute=0, second=0, microsecond=0))] += e.value
    for (customer, hour), value in sorted(sums.items(), key=lambda kv: (kv[0][1], kv[0][0])):
        ts = hour + timedelta(minutes=30)
        if ts > now - timedelta(minutes=1):
            ts = hour  # the current hour: half past may still be in the future
        yield customer, ts, value


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--key-file", required=True, type=Path, help="0600 file holding a Stripe test-mode key")
    ap.add_argument("--out", required=True, type=Path, help="directory for ledger.csv, customers.csv, meterproof.yml")
    args = ap.parse_args()

    api = Stripe(read_key(args.key_file))
    now = datetime.now(timezone.utc).replace(microsecond=0)
    data = demo.build(now)
    run = now.strftime("%Y%m%d%H%M%S") + "-" + secrets.token_hex(2)
    print(f"run {run}")

    meter_id = find_or_create_meter(api)
    product_id, price_id = find_or_create_price(api, meter_id)
    print(f"meter {meter_id}  product {product_id}  price {price_id}")

    # A fresh Stripe customer for every mapped demo customer, plus a stand-in for the orphan.
    real: dict[str, str] = {}
    for demo_id in [s for s in data["mapping"].values() if s] + [ORPHAN]:
        slug = demo_id.removeprefix("cus_demo_")
        customer = api.call(
            "POST",
            "/v1/customers",
            {
                "name": f"Relay demo {slug}",
                "email": f"relay-demo-{slug}@example.com",  # send_invoice subscriptions need one
                "metadata": {"meterproof_demo_run": run, "demo_id": demo_id},
            },
            idempotency_key=f"{run}-customer-{demo_id}",
        )
        real[demo_id] = customer["id"]
    print(f"customers {len(real)}")

    # acct_pine never completed checkout, so its customer gets no subscription; the app kept sending usage.
    subscribed = {sub["customer"] for sub in data["subscriptions"] if sub["status"] == "active"}
    for demo_id in sorted(subscribed):
        api.call(
            "POST",
            "/v1/subscriptions",
            {
                "customer": real[demo_id],
                "items": {"0": {"price": price_id}},
                "collection_method": "send_invoice",
                "days_until_due": 30,
                "metadata": {"meterproof_demo_run": run},
            },
            idempotency_key=f"{run}-subscription-{demo_id}",
        )
    print(f"subscriptions {len(subscribed)}")

    events = list(hourly_events(data["stripe_events"], real, now))

    def send(item) -> None:
        customer, ts, value = item
        api.call(
            "POST",
            "/v1/billing/meter_events",
            {
                "event_name": EVENT_NAME,
                "identifier": f"{run}-{customer}-{int(ts.timestamp())}",
                "timestamp": int(ts.timestamp()),
                "payload": {"stripe_customer_id": customer, "value": value},
            },
        )

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for _ in pool.map(send, events):
            pass
    print(f"meter events {len(events)} ({sum(v for _, _, v in events)} calls)")

    out = args.out
    out.mkdir(parents=True, exist_ok=True)
    with (out / "ledger.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["account_id", "occurred_at", "units"])
        for e in data["ledger"]:
            w.writerow([e.customer, e.timestamp.strftime("%Y-%m-%dT%H:%M:%SZ"), e.value])
    with (out / "customers.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["customer", "stripe_customer"])
        for internal, demo_id in data["mapping"].items():
            w.writerow([internal, real[demo_id] if demo_id else ""])
    (out / "meterproof.yml").write_text(CONFIG.format(run=run, meter=meter_id, price=price_id), encoding="utf-8")
    print(f"wrote {out}/ledger.csv customers.csv meterproof.yml")


if __name__ == "__main__":
    main()
