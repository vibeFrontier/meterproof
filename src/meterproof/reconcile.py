"""Compare the ledger with what Stripe recorded and classify every difference."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal

from . import ledger as ledger_mod
from .config import Config, ConfigError
from .stripe_source import Price, StripeSource, aggregate

# Currencies whose Stripe amounts are already in whole units, or in thousandths.
ZERO_DECIMAL = frozenset(
    "bif clp djf gnf jpy kmf krw mga pyg rwf ugx vnd vuv xaf xof xpf".split()
)
THREE_DECIMAL = frozenset("bhd jod kwd omr tnd".split())

KINDS = ("unbilled", "unmapped", "no_subscription", "overbilled", "stripe_only")
KIND_ORDER = {k: i for i, k in enumerate(KINDS)}
# Kinds where Stripe will bill less than the ledger says it should.
REVENUE_AT_RISK = frozenset({"unbilled", "unmapped", "no_subscription"})


@dataclass(frozen=True)
class DayDiff:
    day: date
    ledger: Decimal
    stripe: Decimal


@dataclass
class Finding:
    kind: str
    stripe_customer: str | None
    customers: list[str]
    ledger: Decimal
    stripe: Decimal
    impact: Decimal | None  # money: stripe-billed minus ledger-billed; negative means lost revenue
    days: list[DayDiff] = field(default_factory=list)

    @property
    def diff(self) -> Decimal:
        return self.stripe - self.ledger


@dataclass
class Result:
    meter_id: str
    meter_name: str
    formula: str
    start: datetime
    end: datetime  # effective, exclusive
    requested_end: datetime
    now: datetime
    settle_minutes: int
    skew_minutes: int
    currency: str | None
    mixed_currency: bool
    pricing_source: str | None
    ledger_rows: int
    deferred_rows: int
    customers_checked: int
    matched: int
    findings: list[Finding]
    notes: list[str]
    stripe_requests: int

    def counts(self) -> dict[str, int]:
        out = {k: 0 for k in KINDS}
        for f in self.findings:
            out[f.kind] += 1
        return out

    def revenue_at_risk(self) -> Decimal | None:
        return self._total(REVENUE_AT_RISK, sign=-1)

    def overbilled_amount(self) -> Decimal | None:
        return self._total(frozenset({"overbilled", "stripe_only"}), sign=1)

    def _total(self, kinds: frozenset[str], sign: int) -> Decimal | None:
        values = [f.impact for f in self.findings if f.kind in kinds]
        if self.mixed_currency or any(v is None for v in values):
            return None
        return sign * sum((v for v in values if v is not None), Decimal(0))


def floor_hour(dt: datetime) -> datetime:
    return dt.replace(minute=0, second=0, microsecond=0)


class NothingToCheck(ConfigError):
    """The window has no settled hour yet (for example, early on the 1st with month-to-date)."""


def resolve_window(start: datetime, end: datetime, now: datetime, settle_minutes: int) -> tuple[datetime, datetime]:
    """Align to hours (Stripe summary boundaries) and stop at the settle cutoff."""
    if start >= now:
        raise ConfigError(f"the window starts in the future ({start:%Y-%m-%d %H:%M} UTC)")
    start = floor_hour(start)
    cutoff = floor_hour(now - timedelta(minutes=settle_minutes))
    effective = floor_hour(min(end, cutoff))
    if effective <= start:
        raise NothingToCheck(
            f"Nothing to check yet: the window starts {start:%Y-%m-%d %H:%M} UTC and usage before "
            f"{cutoff:%Y-%m-%d %H:%M} UTC has not settled for {settle_minutes} minutes."
        )
    return start, effective


class _Money:
    """Turn a metered quantity into an amount in major currency units."""

    def __init__(self, cfg: Config, price: Price | None):
        self.price = price
        self.override = cfg.pricing.unit_amount
        self.is_config = self.override is not None
        self.usable = self.is_config or (
            price is not None and price.billing_scheme == "per_unit" and price.unit_amount_decimal is not None
        )

    def amount(self, quantity: Decimal) -> Decimal | None:
        if not self.usable:
            return None
        if self.override is not None:
            return (quantity * self.override).quantize(Decimal("0.01"), ROUND_HALF_UP)
        price = self.price
        assert price is not None and price.unit_amount_decimal is not None
        units = quantity
        if price.divide_by:
            mode = ROUND_CEILING if price.round == "up" else ROUND_FLOOR
            units = (quantity / price.divide_by).to_integral_value(mode)
        minor = units * price.unit_amount_decimal
        if price.currency in ZERO_DECIMAL:
            major = minor
        elif price.currency in THREE_DECIMAL:
            major = minor / 1000
        else:
            major = minor / 100
        return major.quantize(Decimal("0.01"), ROUND_HALF_UP)


def _by_day(points: list[tuple[datetime, Decimal]], formula: str) -> dict[date, Decimal]:
    grouped: dict[date, list[tuple[datetime, Decimal]]] = defaultdict(list)
    for ts, value in points:
        grouped[ts.date()].append((ts, value))
    return {d: aggregate(items, formula) for d, items in grouped.items()}


def _allowed(cfg: Config, ledger: Decimal) -> Decimal:
    return max(cfg.tolerance.units, ledger.copy_abs() * cfg.tolerance.percent / 100)


def _points_between(points: list[tuple[datetime, Decimal]], lo: datetime, hi: datetime) -> list[tuple[datetime, Decimal]]:
    return [p for p in points if lo <= p[0] < hi]


class _Pricing:
    """Money for each Stripe customer: their own subscription's price, else the only price on the meter."""

    def __init__(self, cfg: Config, prices: list[Price], subscribed: dict[str, str] | None, notes: list[str]):
        self._by_price = {p.id: _Money(cfg, p) for p in prices}
        self._subscribed = subscribed or {}
        if cfg.pricing.unit_amount is not None:
            self._default: _Money | None = _Money(cfg, None)
        elif len(prices) == 1:
            self._default = self._by_price[prices[0].id]
        else:
            self._default = None
        currencies = {cfg.pricing.currency} if cfg.pricing.currency else {p.currency for p in prices}
        self.mixed = len(currencies) > 1
        self.currency = None if self.mixed or not currencies else currencies.pop()
        if cfg.pricing.unit_amount is not None:
            self.source: str | None = "config"
        elif len(prices) == 1:
            self.source = f"Stripe price {prices[0].id}" if self._default and self._default.usable else None
        elif prices:
            self.source = f"each customer's Stripe price ({len(prices)} prices bill this meter)"
        else:
            self.source = None
        tiered = [p.id for p in prices if not self._by_price[p.id].usable]
        if tiered and cfg.pricing.unit_amount is None:
            notes.append(
                f"Tiered price(s) {', '.join(tiered)}: money impact is not estimated for their customers; "
                "set pricing.unit_amount to estimate it."
            )
        if self.mixed:
            notes.append("Prices on this meter use several currencies, so totals are not added up.")

    def for_customer(self, customer: str | None) -> _Money | None:
        if self._default is not None and self._default.is_config:
            return self._default
        if customer and customer in self._subscribed:
            return self._by_price.get(self._subscribed[customer])
        return self._default


def reconcile(
    cfg: Config,
    stripe: StripeSource,
    start: datetime,
    end: datetime,
    now: datetime | None = None,
) -> Result:
    now = now or datetime.now(timezone.utc)
    win_start, win_end = resolve_window(start, end, now, cfg.settle_minutes)
    skew = timedelta(minutes=cfg.skew_minutes)
    if 2 * skew >= win_end - win_start:
        skew = timedelta(0)
    notes: list[str] = []

    meter = stripe.meter(cfg.meter_id)
    if meter.status != "active":
        notes.append(f"Meter {meter.id} is {meter.status}: Stripe ignores new events sent to it.")
    formula = meter.formula
    if cfg.formula and cfg.formula != formula:
        raise ConfigError(
            f"meter.formula is '{cfg.formula}' but Stripe meter {meter.id} aggregates with '{formula}'"
        )
    if formula == "last":
        skew = timedelta(0)

    if cfg.customers.prices:
        prices = [stripe.price(p) for p in cfg.customers.prices]
        for price in prices:
            if price.meter and price.meter != meter.id:
                raise ConfigError(f"price {price.id} bills meter {price.meter}, not {meter.id}")
    else:
        prices = stripe.meter_prices(meter.id)
    subscribed: dict[str, str] | None = None
    if prices:
        subscribed = stripe.subscribers([p.id for p in prices], win_start)
    else:
        notes.append(f"No Stripe price bills meter {meter.id}, so subscriptions were not checked.")
    pricing = _Pricing(cfg, prices, subscribed, notes)

    def amount(customer: str | None, quantity: Decimal) -> Decimal | None:
        money = pricing.for_customer(customer)
        return money.amount(quantity) if money else None

    def impact(customer: str | None, ledger: Decimal, billed: Decimal) -> Decimal | None:
        expected, actual = amount(customer, ledger), amount(customer, billed)
        return None if expected is None or actual is None else actual - expected

    # Read past a closed window's end so the right-edge skew allowance has rows to widen with.
    rows = ledger_mod.read(cfg.ledger, win_start - skew, max(end, win_end + skew), stripe_column=cfg.customers.column)
    exact_rows = [r for r in rows if win_start <= r.timestamp < win_end]
    deferred = sum(1 for r in rows if win_end <= r.timestamp < end)

    mapped: dict[str, list[tuple[datetime, Decimal]]] = defaultdict(list)
    internal_ids: dict[str, set[str]] = defaultdict(set)
    unmapped: dict[str, list[tuple[datetime, Decimal]]] = defaultdict(list)
    for row in rows:
        if row.timestamp >= win_end + skew:
            continue
        target = row.stripe_customer or cfg.customers.mapping.get(row.customer)
        if target:
            mapped[target].append((row.timestamp, row.quantity))
            internal_ids[target].add(row.customer)
        else:
            unmapped[row.customer].append((row.timestamp, row.quantity))

    universe = set(mapped) | set(cfg.customers.mapping.values()) | set(subscribed or {})
    for internal, target in cfg.customers.mapping.items():
        internal_ids[target].add(internal)

    findings: list[Finding] = []
    matched = 0
    for customer in sorted(universe):
        points = mapped.get(customer, [])
        exact_points = _points_between(points, win_start, win_end)
        exact = aggregate(exact_points, formula)
        # Events near the window edges may carry a slightly different timestamp in Stripe (queued or
        # batched senders). Only usage clearly inside the window must be in Stripe, and Stripe may hold
        # at most the usage within the widened window.
        inner = aggregate(_points_between(points, win_start + skew, win_end - skew), formula)
        outer = aggregate(points, formula) if skew else exact
        billed = stripe.total(meter.id, customer, win_start, win_end)
        names = sorted(internal_ids.get(customer, set()))

        if subscribed is not None and exact > 0 and customer not in subscribed:
            lost = amount(customer, exact)
            findings.append(
                Finding("no_subscription", customer, names, exact, billed, None if lost is None else -lost)
            )
            continue

        if billed < inner - _allowed(cfg, inner):
            kind = "unbilled"
        elif billed > outer + _allowed(cfg, outer):
            kind = "stripe_only" if exact == 0 else "overbilled"
        else:
            if exact or billed:
                matched += 1
            continue

        finding = Finding(kind, customer, names, exact, billed, impact(customer, exact, billed))
        if formula != "last":
            stripe_days = _by_day(stripe.hourly(meter.id, customer, win_start, win_end), formula)
            ledger_days = _by_day(exact_points, formula)
            for day in sorted(set(stripe_days) | set(ledger_days)):
                lv, sv = ledger_days.get(day, Decimal(0)), stripe_days.get(day, Decimal(0))
                if lv != sv:
                    finding.days.append(DayDiff(day, lv, sv))
        findings.append(finding)

    for internal, points in sorted(unmapped.items()):
        value = aggregate(_points_between(points, win_start, win_end), formula)
        if value == 0:
            continue
        lost = amount(None, value)
        findings.append(Finding("unmapped", None, [internal], value, Decimal(0), None if lost is None else -lost))

    findings.sort(
        key=lambda f: (
            KIND_ORDER[f.kind],
            -(f.impact.copy_abs() if f.impact is not None else Decimal(0)),
            -f.diff.copy_abs(),
            f.stripe_customer or "",
        )
    )
    if deferred:
        notes.append(
            f"{deferred} ledger row(s) after {win_end:%Y-%m-%d %H:%M} UTC were left for the next run "
            f"(settle window {cfg.settle_minutes} min)."
        )

    return Result(
        meter_id=meter.id,
        meter_name=meter.display_name,
        formula=formula,
        start=win_start,
        end=win_end,
        requested_end=end,
        now=now,
        settle_minutes=cfg.settle_minutes,
        skew_minutes=int(skew.total_seconds() // 60),
        currency=pricing.currency,
        mixed_currency=pricing.mixed,
        pricing_source=pricing.source,
        ledger_rows=len(exact_rows),
        deferred_rows=deferred,
        customers_checked=len(universe) + len(unmapped),
        matched=matched,
        findings=findings,
        notes=notes,
        stripe_requests=getattr(stripe, "requests", 0),
    )
