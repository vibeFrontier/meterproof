"""meterproof command line."""

from __future__ import annotations

import argparse
import dataclasses
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import __version__, config as config_mod, demo, report
from .config import ConfigError
from .ledger import LedgerError, parse_timestamp
from .reconcile import KINDS, NothingToCheck, reconcile
from .stripe_source import StripeAPI, StripeError, StripeFixture

PERIODS = ("month-to-date", "previous-month", "last-7-days", "last-24-hours")

EXAMPLE_CONFIG = """\
# meterproof.yml: compare your own usage records with what a Stripe meter recorded.
# Docs: https://meterproof.vibefrontier.com/docs/
version: 1
meter:
  id: mtr_REPLACE_ME            # Stripe Dashboard > Billing > Meters
ledger:
  # One of csv / postgres / sqlite. Each row is one usage event your app sent (or should have sent).
  csv: usage.csv
  # postgres:
  #   dsn_env: DATABASE_URL      # read-only role recommended
  #   query: >
  #     select account_id, occurred_at, units from usage_events
  #     where occurred_at >= %(start)s and occurred_at < %(end)s
  columns:
    customer: account_id         # your customer id
    timestamp: occurred_at       # the timestamp you sent to Stripe
    quantity: units              # the value you sent to Stripe
customers:
  mapping_csv: customers.csv     # columns: customer, stripe_customer
  # column: stripe_customer_id   # or: the ledger already has the Stripe customer id
  # price: price_REPLACE_ME      # optional: only these prices (default: every price on the meter)
stripe:
  api_key_env: STRIPE_API_KEY    # restricted key, Read only: Billing meters, Billing meter events, Prices, Subscriptions
window:
  settle_minutes: 60             # ignore usage newer than this; Stripe processes events asynchronously
  skew_minutes: 5                # timestamp drift allowed at window edges (batching senders)
tolerance:
  units: 0
  percent: 0
"""


def _period(name: str, now: datetime) -> tuple[datetime, datetime]:
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if name == "month-to-date":
        return month_start, now
    if name == "previous-month":
        prev = (month_start - timedelta(days=1)).replace(day=1)
        return prev, month_start
    if name == "last-7-days":
        return now - timedelta(days=7), now
    if name == "last-24-hours":
        return now - timedelta(hours=24), now
    raise ConfigError(f"unknown period {name!r}")


def _when(value: str, flag: str) -> datetime:
    try:
        return parse_timestamp(value)
    except ValueError:
        raise ConfigError(f"{flag}: expected a date or ISO 8601 time, got {value!r}") from None


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _fail_kinds(value: str) -> set[str]:
    if value == "none":
        return set()
    if value == "all":
        return set(KINDS)
    kinds = {k.strip() for k in value.split(",") if k.strip()}
    unknown = kinds - set(KINDS)
    if unknown:
        raise ConfigError(f"--fail-on: unknown problem type(s) {', '.join(sorted(unknown))}; use {', '.join(KINDS)}, all or none")
    return kinds


def run_check(args: argparse.Namespace) -> int:
    cfg = config_mod.load(args.config)
    if args.settle_minutes is not None:
        if args.settle_minutes < 0:
            raise ConfigError("--settle-minutes must be 0 or more")
        cfg = dataclasses.replace(cfg, settle_minutes=args.settle_minutes)
    now = _when(args.now, "--now") if args.now else datetime.now(timezone.utc)
    if args.start:
        start = _when(args.start, "--start")
        end = _when(args.end, "--end") if args.end else now
    else:
        if args.end:
            raise ConfigError("--end needs --start")
        start, end = _period(args.period, now)
    if end <= start:
        raise ConfigError("the window end must be after its start")
    fail_on = _fail_kinds(args.fail_on)

    if cfg.stripe.fixture is not None:
        source = StripeFixture(cfg.stripe.fixture)
    else:
        key = os.environ.get(cfg.stripe.api_key_env, "")
        if not key:
            raise ConfigError(
                f"set {cfg.stripe.api_key_env} to a Stripe restricted key with Read on "
                "Billing meters, Billing meter events, Prices and Subscriptions"
            )
        source = StripeAPI(key)

    result = reconcile(cfg, source, start, end, now=now)
    md = report.markdown(result)
    md_path = Path(args.markdown) if args.markdown else cfg.markdown
    json_path = Path(args.json) if args.json else cfg.json
    if md_path:
        _write(md_path, md)
    if json_path:
        _write(json_path, report.to_json(result))
    if not args.quiet:
        sys.stdout.write(md)
    else:
        sys.stdout.write(report.headline(result) + "\n")
    return 1 if any(f.kind in fail_on for f in result.findings) else 0


def run_demo(args: argparse.Namespace) -> int:
    directory = Path(args.dir)
    demo.write(directory, demo.DEMO_NOW)
    sys.stderr.write(f"Wrote the Relay API demo to {directory}/ (ledger.csv, customers.csv, stripe-fixture.json, seed.sql)\n\n")
    ns = argparse.Namespace(
        config=str(directory / "meterproof.yml"),
        now=demo.DEMO_NOW.isoformat(),
        start=None,
        end=None,
        period="month-to-date",
        markdown=None,
        json=None,
        quiet=args.quiet,
        fail_on="all",
        settle_minutes=None,
    )
    return run_check(ns)


def run_init(args: argparse.Namespace) -> int:
    path = Path(args.path)
    if path.exists() and not args.force:
        raise ConfigError(f"{path} already exists (use --force to overwrite)")
    _write(path, EXAMPLE_CONFIG)
    sys.stdout.write(f"Wrote {path}. Fill in meter.id, your ledger and the customer mapping, then run: meterproof check\n")
    return 0


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="meterproof",
        description="Check that Stripe will bill the usage your app recorded, before the invoice finalizes.",
    )
    p.add_argument("--version", action="version", version=f"meterproof {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    c = sub.add_parser("check", help="reconcile your usage ledger against a Stripe meter")
    c.add_argument("-c", "--config", default="meterproof.yml", help="config file (default: meterproof.yml)")
    c.add_argument("--period", choices=PERIODS, default="month-to-date", help="window to check (default: month-to-date, UTC)")
    c.add_argument("--start", help="window start, e.g. 2026-11-01 (UTC unless an offset is given); overrides --period")
    c.add_argument("--end", help="window end, exclusive (default: now)")
    c.add_argument(
        "--settle-minutes",
        type=int,
        help="override window.settle_minutes for this run, e.g. 15 for a closed-period check right after the period ends",
    )
    c.add_argument("--now", help=argparse.SUPPRESS)
    c.add_argument("--markdown", help="also write the Markdown report here")
    c.add_argument("--json", help="also write the JSON report here")
    c.add_argument("-q", "--quiet", action="store_true", help="print only the one-line result")
    c.add_argument(
        "--fail-on",
        default="all",
        help="problem types that make the exit code 1: all (default), none, or a comma list of " + ", ".join(KINDS),
    )
    c.set_defaults(func=run_check)

    d = sub.add_parser("demo", help="run against a fictional SaaS with planted billing mistakes (no Stripe account needed)")
    d.add_argument("--dir", default="meterproof-demo", help="where to write the demo files (default: ./meterproof-demo)")
    d.add_argument("-q", "--quiet", action="store_true")
    d.set_defaults(func=run_demo)

    i = sub.add_parser("init", help="write a starter meterproof.yml")
    i.add_argument("path", nargs="?", default="meterproof.yml")
    i.add_argument("--force", action="store_true")
    i.set_defaults(func=run_init)
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        return args.func(args)
    except NothingToCheck as exc:
        sys.stdout.write(f"{exc}\n")
        return 0
    except (ConfigError, LedgerError, StripeError) as exc:
        sys.stderr.write(f"meterproof: {exc}\n")
        return 2
    except (OSError, ValueError, ArithmeticError) as exc:
        sys.stderr.write(f"meterproof: {type(exc).__name__}: {exc}\n")
        return 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
