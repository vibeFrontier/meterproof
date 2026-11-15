"""Read the SaaS's own usage records (the ledger) from CSV, Postgres or SQLite."""

from __future__ import annotations

import csv
import os
import sqlite3
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from .config import ConfigError, LedgerConfig


class LedgerError(Exception):
    """A usage row could not be read."""


@dataclass(frozen=True)
class UsageRow:
    customer: str
    timestamp: datetime  # timezone-aware, UTC
    quantity: Decimal
    stripe_customer: str | None = None


def parse_timestamp(value: Any) -> datetime:
    """Accept ISO 8601 (naive means UTC), a date, epoch seconds, or a datetime."""
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return datetime.fromtimestamp(value, tz=timezone.utc)
    text = str(value).strip()
    if not text:
        raise ValueError("empty timestamp")
    if text.lstrip("-").replace(".", "", 1).isdigit():
        return datetime.fromtimestamp(float(text), tz=timezone.utc)
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _quantity(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value).strip())
    except InvalidOperation as exc:
        raise ValueError(f"not a number: {value!r}") from exc


def _rows_from_dicts(
    records: Iterable[dict[str, Any]], cfg: LedgerConfig, stripe_column: str | None, where: str
) -> Iterator[UsageRow]:
    cols = cfg.columns
    for n, rec in enumerate(records, start=1):
        loc = f"{where} row {n}"
        try:
            customer = rec[cols.customer]
            ts = rec[cols.timestamp]
            qty = rec[cols.quantity]
        except KeyError as exc:
            raise LedgerError(f"{loc}: missing column {exc.args[0]!r}") from None
        if customer is None or str(customer).strip() == "":
            raise LedgerError(f"{loc}: empty customer")
        try:
            timestamp = parse_timestamp(ts)
            quantity = _quantity(qty)
        except ValueError as exc:
            raise LedgerError(f"{loc}: {exc}") from None
        stripe_customer = None
        if stripe_column:
            if stripe_column not in rec:
                raise LedgerError(f"{loc}: missing column {stripe_column!r}")
            value = rec[stripe_column]
            stripe_customer = str(value).strip() if value not in (None, "") else None
        yield UsageRow(str(customer).strip(), timestamp, quantity, stripe_customer)


def _check_header(found: Iterable[str], cfg: LedgerConfig, stripe_column: str | None, where: str) -> None:
    needed = [cfg.columns.customer, cfg.columns.timestamp, cfg.columns.quantity]
    if stripe_column:
        needed.append(stripe_column)
    missing = [c for c in needed if c not in set(found)]
    if missing:
        raise LedgerError(
            f"{where} is missing column(s) {', '.join(missing)}; set ledger.columns to match your export"
        )


def read(cfg: LedgerConfig, start: datetime, end: datetime, stripe_column: str | None = None) -> list[UsageRow]:
    """Return usage rows with start <= timestamp < end."""
    if cfg.kind == "csv":
        assert cfg.path is not None
        if not cfg.path.is_file():
            raise ConfigError(f"ledger CSV not found: {cfg.path}")
        with cfg.path.open(newline="", encoding="utf-8-sig") as fh:
            reader = csv.DictReader(fh)
            _check_header(reader.fieldnames or [], cfg, stripe_column, str(cfg.path))
            rows = list(_rows_from_dicts(reader, cfg, stripe_column, str(cfg.path)))
    elif cfg.kind == "sqlite":
        assert cfg.path is not None and cfg.query is not None
        if not cfg.path.is_file():
            raise ConfigError(f"SQLite database not found: {cfg.path}")
        conn = None
        try:
            conn = sqlite3.connect(f"file:{cfg.path}?mode=ro", uri=True)
            cur = conn.execute(cfg.query, {"start": start.isoformat(), "end": end.isoformat()})
            names = [d[0] for d in cur.description or []]
            _check_header(names, cfg, stripe_column, "SQLite query result")
            rows = list(_rows_from_dicts((dict(zip(names, r)) for r in cur), cfg, stripe_column, "SQLite query"))
        except sqlite3.Error as exc:
            raise LedgerError(f"SQLite query failed: {exc}") from None
        finally:
            if conn is not None:
                conn.close()
    elif cfg.kind == "postgres":
        assert cfg.query is not None and cfg.dsn_env is not None
        dsn = os.environ.get(cfg.dsn_env)
        if not dsn:
            raise ConfigError(f"set {cfg.dsn_env} to a read-only Postgres connection string")
        try:
            import psycopg
        except ImportError:
            raise ConfigError("Postgres needs psycopg: pip install 'meterproof[postgres]'") from None
        try:
            with psycopg.connect(dsn) as conn:
                conn.read_only = True
                with conn.cursor() as cur:
                    cur.execute(cfg.query, {"start": start, "end": end})
                    names = [d.name for d in cur.description or []]
                    _check_header(names, cfg, stripe_column, "Postgres query result")
                    rows = list(
                        _rows_from_dicts((dict(zip(names, r)) for r in cur), cfg, stripe_column, "Postgres query")
                    )
        except psycopg.Error as exc:
            raise LedgerError(f"Postgres query failed: {exc}") from None
    else:  # pragma: no cover - config validation prevents this
        raise ConfigError(f"unknown ledger kind {cfg.kind}")
    return [r for r in rows if start <= r.timestamp < end]
