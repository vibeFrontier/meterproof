"""Load and validate meterproof.yml."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import yaml


class ConfigError(Exception):
    """The configuration file is missing, malformed or inconsistent."""


FORMULAS = ("sum", "count", "last")


@dataclass(frozen=True)
class Columns:
    customer: str = "customer"
    timestamp: str = "timestamp"
    quantity: str = "quantity"


@dataclass(frozen=True)
class LedgerConfig:
    kind: str  # "csv" | "postgres" | "sqlite"
    columns: Columns
    path: Path | None = None  # csv file or sqlite database
    query: str | None = None
    dsn_env: str | None = None


@dataclass(frozen=True)
class CustomerConfig:
    mapping: dict[str, str] = field(default_factory=dict)
    column: str | None = None  # ledger column already holding the Stripe customer id
    prices: tuple[str, ...] = ()  # empty: every price that bills the meter


@dataclass(frozen=True)
class StripeConfig:
    api_key_env: str = "STRIPE_API_KEY"
    fixture: Path | None = None


@dataclass(frozen=True)
class Tolerance:
    units: Decimal = Decimal(0)
    percent: Decimal = Decimal(0)


@dataclass(frozen=True)
class Pricing:
    unit_amount: Decimal | None = None
    currency: str | None = None


@dataclass(frozen=True)
class Config:
    path: Path
    meter_id: str
    formula: str | None
    ledger: LedgerConfig
    customers: CustomerConfig
    stripe: StripeConfig
    settle_minutes: int
    skew_minutes: int
    tolerance: Tolerance
    pricing: Pricing
    markdown: Path | None
    json: Path | None


def _section(raw: dict[str, Any], name: str) -> dict[str, Any]:
    value = raw.get(name) or {}
    if not isinstance(value, dict):
        raise ConfigError(f"'{name}' must be a mapping")
    return value


def _decimal(value: Any, name: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise ConfigError(f"'{name}' must be a number, got {value!r}") from exc
    if not result.is_finite() or result < 0:
        raise ConfigError(f"'{name}' must be a number, 0 or more, got {value!r}")
    return result


def _resolve(base: Path, value: Any, name: str) -> Path:
    if not isinstance(value, str) or not value:
        raise ConfigError(f"'{name}' must be a file path")
    path = Path(value).expanduser()
    return path if path.is_absolute() else base / path


def load_mapping_csv(path: Path) -> dict[str, str]:
    import csv

    if not path.is_file():
        raise ConfigError(f"customer mapping file not found: {path}")
    mapping: dict[str, str] = {}
    with path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        fields = set(reader.fieldnames or [])
        if not {"customer", "stripe_customer"} <= fields:
            raise ConfigError(
                f"{path} needs the columns 'customer' and 'stripe_customer' (found: {', '.join(sorted(fields)) or 'none'})"
            )
        for line, row in enumerate(reader, start=2):
            internal = (row["customer"] or "").strip()
            stripe_id = (row["stripe_customer"] or "").strip()
            if not internal:
                continue
            if not stripe_id:
                continue  # an explicit blank mapping is reported as unmapped
            if internal in mapping and mapping[internal] != stripe_id:
                raise ConfigError(f"{path}:{line}: customer {internal!r} is mapped twice")
            mapping[internal] = stripe_id
    return mapping


def load(path: str | Path) -> Config:
    path = Path(path).expanduser().resolve()
    if not path.is_file():
        raise ConfigError(f"config file not found: {path}")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path} is not valid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{path} must contain a mapping at the top level")
    base = path.parent

    version = raw.get("version", 1)
    if version != 1:
        raise ConfigError(f"unsupported config version {version!r}; this meterproof reads version 1")

    meter = _section(raw, "meter")
    meter_id = meter.get("id")
    if not isinstance(meter_id, str) or not meter_id:
        raise ConfigError("'meter.id' is required (the Stripe meter id, mtr_...)")
    formula = meter.get("formula")
    if formula is not None and formula not in FORMULAS:
        raise ConfigError(f"'meter.formula' must be one of {', '.join(FORMULAS)}")

    ledger_raw = _section(raw, "ledger")
    cols_raw = _section(ledger_raw, "columns")
    columns = Columns(**{k: str(v) for k, v in cols_raw.items() if k in ("customer", "timestamp", "quantity")})
    unknown_cols = set(cols_raw) - {"customer", "timestamp", "quantity"}
    if unknown_cols:
        raise ConfigError(f"unknown 'ledger.columns' keys: {', '.join(sorted(unknown_cols))}")
    sources = [k for k in ("csv", "postgres", "sqlite") if k in ledger_raw]
    if len(sources) != 1:
        raise ConfigError("'ledger' needs exactly one of: csv, postgres, sqlite")
    kind = sources[0]
    if kind == "csv":
        ledger = LedgerConfig(kind="csv", columns=columns, path=_resolve(base, ledger_raw["csv"], "ledger.csv"))
    else:
        spec = ledger_raw[kind]
        if not isinstance(spec, dict) or not isinstance(spec.get("query"), str):
            raise ConfigError(f"'ledger.{kind}' needs a 'query'")
        if kind == "postgres":
            ledger = LedgerConfig(
                kind="postgres",
                columns=columns,
                query=spec["query"],
                dsn_env=str(spec.get("dsn_env", "DATABASE_URL")),
            )
        else:
            ledger = LedgerConfig(
                kind="sqlite",
                columns=columns,
                query=spec["query"],
                path=_resolve(base, spec.get("path"), "ledger.sqlite.path"),
            )

    cust_raw = _section(raw, "customers")
    mapping: dict[str, str] = {}
    if "mapping_csv" in cust_raw:
        mapping.update(load_mapping_csv(_resolve(base, cust_raw["mapping_csv"], "customers.mapping_csv")))
    inline = cust_raw.get("map") or {}
    if not isinstance(inline, dict):
        raise ConfigError("'customers.map' must be a mapping of your customer id to Stripe customer id")
    for k, v in inline.items():
        if str(k) in mapping and mapping[str(k)] != str(v):
            raise ConfigError(f"customer {k!r} is mapped twice")
        mapping[str(k)] = str(v)
    column = cust_raw.get("column")
    if column is not None and not isinstance(column, str):
        raise ConfigError("'customers.column' must be a column name")
    if not mapping and not column:
        raise ConfigError(
            "'customers' needs a mapping_csv, a map, or a column holding Stripe customer ids"
        )
    price = cust_raw.get("price")
    if price is None:
        prices: tuple[str, ...] = ()
    elif isinstance(price, str) and price:
        prices = (price,)
    elif isinstance(price, list) and price and all(isinstance(p, str) and p for p in price):
        prices = tuple(price)
    else:
        raise ConfigError("'customers.price' must be a Stripe price id (price_...) or a list of them")
    customers = CustomerConfig(mapping=mapping, column=column, prices=prices)

    stripe_raw = _section(raw, "stripe")
    stripe = StripeConfig(
        api_key_env=str(stripe_raw.get("api_key_env", "STRIPE_API_KEY")),
        fixture=_resolve(base, stripe_raw["fixture"], "stripe.fixture") if "fixture" in stripe_raw else None,
    )

    window = _section(raw, "window")
    settle = window.get("settle_minutes", 60)
    if not isinstance(settle, int) or settle < 0:
        raise ConfigError("'window.settle_minutes' must be a whole number of minutes, 0 or more")
    skew = window.get("skew_minutes", 5)
    if not isinstance(skew, int) or skew < 0:
        raise ConfigError("'window.skew_minutes' must be a whole number of minutes, 0 or more")

    tol_raw = _section(raw, "tolerance")
    tolerance = Tolerance(
        units=_decimal(tol_raw.get("units", 0), "tolerance.units"),
        percent=_decimal(tol_raw.get("percent", 0), "tolerance.percent"),
    )

    pricing_raw = _section(raw, "pricing")
    pricing = Pricing(
        unit_amount=_decimal(pricing_raw["unit_amount"], "pricing.unit_amount")
        if "unit_amount" in pricing_raw
        else None,
        currency=str(pricing_raw["currency"]).lower() if "currency" in pricing_raw else None,
    )

    report = _section(raw, "report")
    return Config(
        path=path,
        meter_id=meter_id,
        formula=formula,
        ledger=ledger,
        customers=customers,
        stripe=stripe,
        settle_minutes=settle,
        skew_minutes=skew,
        tolerance=tolerance,
        pricing=pricing,
        markdown=_resolve(base, report["markdown"], "report.markdown") if "markdown" in report else None,
        json=_resolve(base, report["json"], "report.json") if "json" in report else None,
    )
