"""Render a reconciliation result as Markdown or JSON."""

from __future__ import annotations

import json
from decimal import Decimal

from . import __version__
from .reconcile import KINDS, Finding, Result

LABELS = {
    "unbilled": "Unbilled",
    "unmapped": "Unmapped customer",
    "no_subscription": "No subscription",
    "overbilled": "Overbilled",
    "stripe_only": "Only in Stripe",
}

MEANINGS = {
    "unbilled": "Stripe recorded less usage than your ledger. The missing usage will not be on the invoice. "
    "Usual causes: events rejected by Stripe (bad customer id, payload key, or timestamp), a worker that "
    "stopped sending, or events sent after the invoice finalized.",
    "unmapped": "Your ledger has usage for a customer with no Stripe customer id in the mapping, so nobody is "
    "billed for it. Add the customer to the mapping, or check why it never got a Stripe customer.",
    "no_subscription": "The Stripe customer has usage but no active subscription on the metered price, so the "
    "usage never reaches an invoice.",
    "overbilled": "Stripe recorded more usage than your ledger. The customer will be charged too much. Usual "
    "cause: retries that sent the same event twice with different identifiers.",
    "stripe_only": "Stripe recorded usage for a customer your ledger has none for in this window. Check for "
    "events sent to the wrong customer or test traffic in the live account.",
}

SYMBOLS = {"usd": "$", "cad": "CA$", "aud": "A$", "eur": "€", "gbp": "£", "jpy": "¥"}


def fmt_qty(value: Decimal) -> str:
    if value == value.to_integral_value():
        return f"{int(value):,}"
    text = f"{value.normalize():,f}"
    return text


def fmt_signed(value: Decimal) -> str:
    return ("+" if value > 0 else "") + fmt_qty(value)


def fmt_money(value: Decimal | None, currency: str | None, signed: bool = False) -> str:
    if value is None:
        return "n/a"
    code = (currency or "").lower()
    sign = "-" if value < 0 else ("+" if signed and value > 0 else "")
    amount = f"{value.copy_abs():,.2f}"
    if code in SYMBOLS:
        return f"{sign}{SYMBOLS[code]}{amount}"
    return f"{sign}{amount} {code.upper()}".strip()


def headline(result: Result) -> str:
    n = len(result.findings)
    if n == 0:
        checked = result.matched
        return f"No problems found. Stripe matches your ledger for {checked} customer{'s' if checked != 1 else ''} with usage."
    parts = [f"{n} problem{'s' if n != 1 else ''} found."]
    risk = result.revenue_at_risk()
    over = result.overbilled_amount()
    has_risk = any(f.kind in ("unbilled", "unmapped", "no_subscription") for f in result.findings)
    has_over = any(f.kind in ("overbilled", "stripe_only") for f in result.findings)
    if has_risk and risk is not None:
        parts.append(f"{fmt_money(risk, result.currency)} of recorded usage will not be billed.")
    if has_over and over is not None:
        parts.append(f"{fmt_money(over, result.currency)} may be billed that your ledger does not support.")
    return " ".join(parts)


def _esc(text: str) -> str:
    """Keep ids and names from breaking Markdown tables and code spans."""
    return text.replace("|", "\\|").replace("`", "'").replace("\n", " ")


def _code(text: str) -> str:
    return f"`{_esc(text)}`"


def _customer_cell(f: Finding) -> str:
    return ", ".join(_code(c) for c in f.customers) if f.customers else "(none)"


def markdown(result: Result) -> str:
    name = _esc(result.meter_name)
    out: list[str] = []
    out.append(f"# meterproof report: {name}")
    out.append("")
    out.append(f"**{headline(result)}**")
    out.append("")
    out.append("| | |")
    out.append("|---|---|")
    out.append(f"| Meter | {name} ({_code(result.meter_id)}), aggregated by {result.formula} |")
    out.append(
        f"| Window | {result.start:%Y-%m-%d %H:%M} to {result.end:%Y-%m-%d %H:%M} UTC |"
    )
    timing = f"usage newer than {result.settle_minutes} min is left for the next run"
    if result.skew_minutes:
        timing += f"; edges allow {result.skew_minutes} min of timestamp drift"
    out.append(f"| Timing | {timing} |")
    out.append(
        f"| Customers checked | {result.customers_checked} ({result.matched} match, {len(result.findings)} with problems) |"
    )
    out.append(f"| Ledger rows | {result.ledger_rows:,} |")
    pricing = result.pricing_source or "not configured, so money impact is not estimated"
    if result.pricing_source and result.currency:
        pricing += f" ({result.currency.upper()})"
    out.append(f"| Pricing | {pricing} |")
    out.append("")

    if result.findings:
        out.append("## Problems")
        out.append("")
        out.append("| Problem | Stripe customer | Your customer | Your ledger | Stripe | Difference | Money |")
        out.append("|---|---|---|---:|---:|---:|---:|")
        for f in result.findings:
            out.append(
                f"| {LABELS[f.kind]} | {_code(f.stripe_customer) if f.stripe_customer else '(none)'} | "
                f"{_customer_cell(f)} | {fmt_qty(f.ledger)} | {fmt_qty(f.stripe)} | {fmt_signed(f.diff)} | "
                f"{fmt_money(f.impact, result.currency, signed=True)} |"
            )
        out.append("")
        detailed = [f for f in result.findings if f.days]
        if detailed:
            out.append("## Days that differ")
            out.append("")
            for f in detailed:
                who = _code(f.stripe_customer) if f.stripe_customer else ""
                if f.customers:
                    who += f" ({_customer_cell(f)})"
                out.append(f"### {who}: {LABELS[f.kind].lower()}")
                out.append("")
                out.append("| Day (UTC) | Your ledger | Stripe | Difference |")
                out.append("|---|---:|---:|---:|")
                for d in f.days[:31]:
                    out.append(
                        f"| {d.day.isoformat()} | {fmt_qty(d.ledger)} | {fmt_qty(d.stripe)} | {fmt_signed(d.stripe - d.ledger)} |"
                    )
                if len(f.days) > 31:
                    out.append(f"| ... | | | {len(f.days) - 31} more days in the JSON report |")
                out.append("")
        out.append("## What each problem means")
        out.append("")
        for kind in KINDS:
            if any(f.kind == kind for f in result.findings):
                out.append(f"- **{LABELS[kind]}**: {MEANINGS[kind]}")
        out.append("")

    if result.notes:
        out.append("## Notes")
        out.append("")
        for note in result.notes:
            out.append(f"- {note}")
        out.append("")
    out.append(
        f"<sub>meterproof {__version__}, run {result.now:%Y-%m-%d %H:%M} UTC, "
        f"{result.stripe_requests} Stripe read requests. Read-only: nothing was changed in Stripe. "
        "Help reading this report: https://meterproof.vibefrontier.com/docs/#problems-it-finds</sub>"
    )
    out.append("")
    return "\n".join(out)


def _d(value: Decimal | None) -> str | None:
    return None if value is None else format(value.normalize(), "f")


def to_json(result: Result) -> str:
    risk = result.revenue_at_risk()
    over = result.overbilled_amount()
    doc = {
        "meterproof": __version__,
        "headline": headline(result),
        "ok": not result.findings,
        "meter": {"id": result.meter_id, "name": result.meter_name, "formula": result.formula},
        "window": {
            "start": result.start.isoformat(),
            "end": result.end.isoformat(),
            "requested_end": result.requested_end.isoformat(),
            "settle_minutes": result.settle_minutes,
            "skew_minutes": result.skew_minutes,
        },
        "run_at": result.now.isoformat(),
        "currency": result.currency,
        "pricing_source": result.pricing_source,
        "customers_checked": result.customers_checked,
        "customers_matched": result.matched,
        "ledger_rows": result.ledger_rows,
        "deferred_rows": result.deferred_rows,
        "counts": result.counts(),
        "revenue_at_risk": _d(risk) if any(f.kind in ("unbilled", "unmapped", "no_subscription") for f in result.findings) else "0",
        "overbilled_amount": _d(over) if any(f.kind in ("overbilled", "stripe_only") for f in result.findings) else "0",
        "findings": [
            {
                "kind": f.kind,
                "stripe_customer": f.stripe_customer,
                "customers": f.customers,
                "ledger": _d(f.ledger),
                "stripe": _d(f.stripe),
                "difference": _d(f.diff),
                "impact": _d(f.impact),
                "days": [
                    {"day": d.day.isoformat(), "ledger": _d(d.ledger), "stripe": _d(d.stripe)} for d in f.days
                ],
            }
            for f in result.findings
        ],
        "notes": result.notes,
        "stripe_requests": result.stripe_requests,
    }
    return json.dumps(doc, indent=2) + "\n"
