from __future__ import annotations

import io
import json
import sqlite3
import urllib.error
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from meterproof import cli, config, demo
from meterproof.config import ConfigError
from meterproof.reconcile import reconcile, resolve_window
from meterproof.stripe_source import StripeAPI, StripeError, StripeFixture

UTC = timezone.utc
NOW = datetime(2026, 11, 15, 10, 30, tzinfo=UTC)


def write_case(tmp: Path, ledger: list[tuple], events: list[tuple], *, formula="sum", extra="", price=None, subs=None):
    """ledger rows: (customer, iso, qty); events: (stripe_customer, iso, value)."""
    (tmp / "ledger.csv").write_text(
        "customer,timestamp,quantity\n" + "".join(f"{c},{t},{q}\n" for c, t, q in ledger)
    )
    (tmp / "events.csv").write_text(
        "stripe_customer,timestamp,value\n" + "".join(f"{c},{t},{v}\n" for c, t, v in events)
    )
    fixture = {"meter": {"id": "mtr_1", "formula": formula}, "events_csv": "events.csv"}
    if price:
        fixture["prices"] = {"price_1": price}
        fixture["subscriptions"] = subs or []
    (tmp / "fixture.json").write_text(json.dumps(fixture))
    (tmp / "meterproof.yml").write_text(
        "version: 1\nmeter: {id: mtr_1}\nledger: {csv: ledger.csv}\n"
        "customers:\n  map: {a: cus_a, b: cus_b}\n" + ("  price: price_1\n" if price else "")
        + "stripe: {fixture: fixture.json}\nwindow: {settle_minutes: 60}\n" + extra
    )
    return config.load(tmp / "meterproof.yml")


def run(cfg, start="2026-11-01T00:00:00Z", end=NOW):
    return reconcile(cfg, StripeFixture(cfg.stripe.fixture), datetime.fromisoformat(start.replace("Z", "+00:00")), end, now=NOW)


def test_demo_finds_exactly_the_planted_mistakes(tmp_path):
    demo.write(tmp_path, demo.DEMO_NOW)
    cfg = config.load(tmp_path / "meterproof.yml")
    start = demo.DEMO_NOW.replace(day=1, hour=0, minute=30)
    result = reconcile(cfg, StripeFixture(cfg.stripe.fixture), start, demo.DEMO_NOW, now=demo.DEMO_NOW)
    by_kind = {f.kind: f for f in result.findings}
    assert set(by_kind) == {"unbilled", "unmapped", "no_subscription", "overbilled", "stripe_only"}
    quarry = by_kind["unbilled"]
    assert quarry.stripe_customer == "cus_demo_quarry"
    assert quarry.diff == -90000 and quarry.impact == Decimal("-900.00")
    assert by_kind["unmapped"].customers == ["acct_ridge"]
    assert by_kind["no_subscription"].stripe_customer == "cus_demo_pine"
    assert by_kind["overbilled"].stripe_customer == "cus_demo_tallow"
    assert by_kind["stripe_only"].stripe_customer == "cus_demo_orphan"
    # acct_vale's newest events are still processing in Stripe; the settle window must hide that.
    assert all(f.stripe_customer != "cus_demo_vale" for f in result.findings)
    assert result.deferred_rows > 0
    # Day breakdown localizes the outage.
    assert sum(d.ledger - d.stripe for d in quarry.days) == 90000


def test_settle_window_ignores_usage_stripe_has_not_processed(tmp_path):
    cfg = write_case(
        tmp_path,
        [("a", "2026-11-15T08:10:00Z", 5), ("a", "2026-11-15T09:50:00Z", 7)],
        [("cus_a", "2026-11-15T08:10:00Z", 5)],
    )
    result = run(cfg)
    assert result.findings == []
    assert result.end == datetime(2026, 11, 15, 9, 0, tzinfo=UTC)
    assert result.deferred_rows == 1


def test_early_on_the_first_there_is_nothing_to_check_and_that_is_not_a_failure(tmp_path, capsys):
    write_case(tmp_path, [], [])
    code = cli.main(["check", "-c", str(tmp_path / "meterproof.yml"), "--now", "2026-12-01T00:40:00Z"])
    assert code == 0
    assert "Nothing to check yet" in capsys.readouterr().out


def test_closed_period_check_allows_drift_past_the_period_end(tmp_path):
    # Recorded at 00:02 on Dec 1 but stamped 23:58 on Nov 30 by a batching sender.
    ledger = [("a", "2026-11-10T12:00:00Z", 5), ("a", "2026-12-01T00:02:00Z", 7)]
    events = [("cus_a", "2026-11-10T12:00:00Z", 5), ("cus_a", "2026-11-30T23:58:00Z", 7)]
    cfg = write_case(tmp_path, ledger, events)
    later = datetime(2026, 12, 2, tzinfo=UTC)
    result = reconcile(cfg, StripeFixture(cfg.stripe.fixture), datetime(2026, 11, 1, tzinfo=UTC), datetime(2026, 12, 1, tzinfo=UTC), now=later)
    assert result.findings == []


def test_a_broken_ledger_query_is_exit_2_not_a_billing_problem(tmp_path, capsys):
    sqlite3.connect(tmp_path / "app.db").close()
    write_case(tmp_path, [], [])
    (tmp_path / "meterproof.yml").write_text(
        "version: 1\nmeter: {id: mtr_1}\nledger:\n  sqlite: {path: app.db, query: 'select * from missing_table'}\n"
        "customers: {map: {a: cus_a}}\nstripe: {fixture: fixture.json}\n"
    )
    assert cli.main(["check", "-c", str(tmp_path / "meterproof.yml"), "--now", NOW.isoformat()]) == 2
    assert "SQLite query failed" in capsys.readouterr().err


def test_tolerance_percent_boundary_is_inclusive(tmp_path):
    ledger = [("a", "2026-11-02T00:00:00Z", 1000)]
    ok = write_case(tmp_path, ledger, [("cus_a", "2026-11-02T00:00:00Z", 995)], extra="tolerance: {percent: 0.5}\n")
    assert run(ok).findings == []
    bad = write_case(tmp_path, ledger, [("cus_a", "2026-11-02T00:00:00Z", 994)], extra="tolerance: {percent: 0.5}\n")
    [finding] = run(bad).findings
    assert finding.kind == "unbilled" and finding.diff == -6


def test_count_and_last_formulas_aggregate_like_stripe(tmp_path):
    ledger = [("a", "2026-11-02T00:00:00Z", 10), ("a", "2026-11-03T00:00:00Z", 3)]
    count = write_case(tmp_path, ledger, [("cus_a", "2026-11-02T00:00:00Z", 1)], formula="count")
    [f] = run(count).findings
    assert (f.ledger, f.stripe) == (2, 1)
    last = write_case(
        tmp_path, ledger, [("cus_a", "2026-11-02T00:00:00Z", 10), ("cus_a", "2026-11-03T00:00:00Z", 3)], formula="last"
    )
    assert run(last).findings == []


def test_configured_formula_must_match_the_stripe_meter(tmp_path):
    cfg = write_case(tmp_path, [], [], formula="sum")
    (tmp_path / "meterproof.yml").write_text(
        (tmp_path / "meterproof.yml").read_text().replace("meter: {id: mtr_1}", "meter: {id: mtr_1, formula: count}")
    )
    with pytest.raises(ConfigError, match="aggregates with 'sum'"):
        run(config.load(tmp_path / "meterproof.yml"))


def test_money_uses_transform_quantity_rounding(tmp_path):
    price = {
        "currency": "usd",
        "billing_scheme": "per_unit",
        "unit_amount_decimal": "250",
        "transform_quantity": {"divide_by": 1000, "round": "up"},
    }
    cfg = write_case(
        tmp_path,
        [("a", "2026-11-02T00:00:00Z", 2001)],
        [("cus_a", "2026-11-02T00:00:00Z", 1999)],
        price=price,
        subs=[{"customer": "cus_a", "price": "price_1"}, {"customer": "cus_b", "price": "price_1"}],
    )
    [f] = run(cfg).findings
    # 2001 calls bill as 3 packs ($7.50); 1999 as 2 packs ($5.00).
    assert f.impact == Decimal("-2.50")


def test_tiered_price_reports_no_money_instead_of_a_wrong_number(tmp_path):
    price = {"currency": "usd", "billing_scheme": "tiered"}
    cfg = write_case(
        tmp_path,
        [("a", "2026-11-02T00:00:00Z", 10)],
        [],
        price=price,
        subs=[{"customer": "cus_a", "price": "price_1"}],
    )
    result = run(cfg)
    [f] = result.findings
    assert f.impact is None and result.revenue_at_risk() is None
    assert any("tiered" in n.lower() for n in result.notes)


def test_zero_decimal_currency(tmp_path):
    price = {"currency": "jpy", "billing_scheme": "per_unit", "unit_amount_decimal": "3"}
    cfg = write_case(
        tmp_path,
        [("a", "2026-11-02T00:00:00Z", 100)],
        [],
        price=price,
        subs=[{"customer": "cus_a", "price": "price_1"}],
    )
    [f] = run(cfg).findings
    assert f.impact == Decimal("-300.00")


def test_sqlite_ledger_gets_window_parameters(tmp_path):
    db = tmp_path / "app.db"
    conn = sqlite3.connect(db)
    conn.execute("create table u (acct text, at text, n int)")
    conn.executemany(
        "insert into u values (?,?,?)",
        [("a", "2026-10-31T23:00:00+00:00", 99), ("a", "2026-11-02T00:00:00+00:00", 4)],
    )
    conn.commit()
    conn.close()
    write_case(tmp_path, [], [("cus_a", "2026-11-02T00:00:00Z", 4)])
    (tmp_path / "meterproof.yml").write_text(
        "version: 1\nmeter: {id: mtr_1}\n"
        "ledger:\n  sqlite: {path: app.db, query: 'select acct, at, n from u where at >= :start and at < :end'}\n"
        "  columns: {customer: acct, timestamp: at, quantity: n}\n"
        "customers: {map: {a: cus_a}}\nstripe: {fixture: fixture.json}\n"
    )
    assert run(config.load(tmp_path / "meterproof.yml")).findings == []


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_stripe_api_paginates_and_retries_rate_limits(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    seen: list[str] = []
    pages = [
        urllib.error.HTTPError("u", 429, "rate", {}, io.BytesIO(b"{}")),
        {"data": [{"id": "s1", "aggregated_value": 2}], "has_more": True},
        {"data": [{"id": "s2", "aggregated_value": 3.5}], "has_more": False},
    ]

    def opener(req, timeout):
        seen.append(req.full_url)
        assert req.get_header("Authorization") == "Bearer rk_test_x"
        item = pages.pop(0)
        if isinstance(item, Exception):
            raise item
        return FakeResponse(json.dumps(item).encode())

    api = StripeAPI("rk_test_x", opener=opener)
    total = api.total("mtr_1", "cus_a", datetime(2026, 11, 1, tzinfo=UTC), datetime(2026, 11, 2, tzinfo=UTC))
    assert total == Decimal("5.5")
    assert "customer=cus_a" in seen[0] and "start_time=1793491200" in seen[0]
    assert "starting_after=s1" in seen[2]


def test_stripe_api_explains_a_rejected_key():
    def opener(req, timeout):
        raise urllib.error.HTTPError("u", 401, "no", {}, io.BytesIO(b'{"error": {"message": "Invalid API Key"}}'))

    with pytest.raises(StripeError, match="rejected the API key"):
        StripeAPI("rk_test_bad", opener=opener).meter("mtr_1")


def test_cli_exit_codes(tmp_path, capsys):
    write_case(tmp_path, [("a", "2026-11-02T00:00:00Z", 5)], [("cus_a", "2026-11-02T00:00:00Z", 4)])
    base = ["check", "-c", str(tmp_path / "meterproof.yml"), "--now", NOW.isoformat(), "-q"]
    assert cli.main(base) == 1
    assert cli.main(base + ["--fail-on", "overbilled"]) == 0
    assert cli.main(base + ["--json", str(tmp_path / "r.json")]) == 1
    doc = json.loads((tmp_path / "r.json").read_text())
    assert doc["counts"]["unbilled"] == 1 and doc["findings"][0]["difference"] == "-1"
    assert cli.main(["check", "-c", str(tmp_path / "missing.yml")]) == 2
    assert "config file not found" in capsys.readouterr().err


def test_unmapped_blank_mapping_row_is_reported(tmp_path):
    (tmp_path / "customers.csv").write_text("customer,stripe_customer\na,cus_a\nb,\n")
    write_case(tmp_path, [("a", "2026-11-02T00:00:00Z", 1), ("b", "2026-11-02T00:00:00Z", 2)], [("cus_a", "2026-11-02T00:00:00Z", 1)])
    text = (tmp_path / "meterproof.yml").read_text().replace("  map: {a: cus_a, b: cus_b}\n", "  mapping_csv: customers.csv\n")
    (tmp_path / "meterproof.yml").write_text(text)
    [f] = run(config.load(tmp_path / "meterproof.yml")).findings
    assert f.kind == "unmapped" and f.customers == ["b"] and f.ledger == 2


def test_skew_window_absorbs_edge_timestamp_drift_but_not_real_misses(tmp_path):
    # The app recorded usage at 08:58; a batching sender stamped it 09:02 in Stripe, past the window end.
    ledger = [("a", "2026-11-15T08:58:00Z", 5), ("a", "2026-11-10T12:00:00Z", 7)]
    events = [("cus_a", "2026-11-15T09:02:00Z", 5), ("cus_a", "2026-11-10T12:00:00Z", 7)]
    assert run(write_case(tmp_path, ledger, events)).findings == []
    [f] = run(write_case(tmp_path, ledger, events, extra="window: {settle_minutes: 60, skew_minutes: 0}\n")).findings
    assert f.kind == "unbilled" and f.diff == -5
    # Usage well inside the window that Stripe never got is still a miss with the default skew.
    [f] = run(write_case(tmp_path, ledger, events[:1])).findings
    assert (f.kind, f.ledger, f.stripe) == ("unbilled", 12, 0)


def test_subscription_cancelled_during_window_still_bills_but_earlier_one_does_not(tmp_path):
    price = {"currency": "usd", "billing_scheme": "per_unit", "unit_amount_decimal": "1", "recurring": {"meter": "mtr_1"}}
    ledger = [("a", "2026-11-02T00:00:00Z", 10), ("b", "2026-11-02T00:00:00Z", 10)]
    events = [("cus_a", "2026-11-02T00:00:00Z", 10), ("cus_b", "2026-11-02T00:00:00Z", 10)]
    subs = [
        {"customer": "cus_a", "price": "price_1", "status": "canceled", "ended_at": "2026-11-09T00:00:00Z"},
        {"customer": "cus_b", "price": "price_1", "status": "canceled", "ended_at": "2026-10-20T00:00:00Z"},
    ]
    cfg = write_case(tmp_path, ledger, events, price=price, subs=subs)
    [f] = run(cfg).findings
    assert (f.kind, f.stripe_customer, f.impact) == ("no_subscription", "cus_b", Decimal("-0.10"))


def test_prices_are_discovered_from_the_meter_and_each_customer_is_priced_by_their_own(tmp_path):
    (tmp_path / "events.csv").write_text("stripe_customer,timestamp,value\n")
    write_case(tmp_path, [], [])
    (tmp_path / "ledger.csv").write_text(
        "customer,timestamp,quantity\na,2026-11-02T00:00:00Z,100\nb,2026-11-02T00:00:00Z,100\n"
    )
    fixture = {
        "meter": {"id": "mtr_1", "formula": "sum"},
        "events_csv": "events.csv",
        "prices": {
            "price_std": {"currency": "usd", "unit_amount_decimal": "1", "recurring": {"meter": "mtr_1"}},
            "price_ent": {"currency": "usd", "unit_amount_decimal": "0.5", "recurring": {"meter": "mtr_1"}},
            "price_other": {"currency": "usd", "unit_amount_decimal": "9", "recurring": {"meter": "mtr_9"}},
        },
        "subscriptions": [
            {"customer": "cus_a", "price": "price_std"},
            {"customer": "cus_b", "price": "price_ent"},
        ],
    }
    (tmp_path / "fixture.json").write_text(json.dumps(fixture))
    cfg = config.load(tmp_path / "meterproof.yml")  # no customers.price: discover from the meter
    result = run(cfg)
    impacts = {f.stripe_customer: (f.kind, f.impact) for f in result.findings}
    assert impacts == {"cus_a": ("unbilled", Decimal("-1.00")), "cus_b": ("unbilled", Decimal("-0.50"))}
    assert result.revenue_at_risk() == Decimal("1.50")
