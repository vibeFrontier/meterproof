# meterproof

Check that Stripe will bill the usage your app recorded, before the invoice finalizes.

meterproof compares your own usage records (a CSV export or a read-only SQL query) with what a Stripe
meter recorded for each customer, and reports every difference with the money it is worth.

It runs where your data already is (your laptop, CI, a cron job). It reads Stripe with a read-only
restricted key and never writes to Stripe. No usage data leaves your machine.

## Problems it finds

| Problem | What happened |
|---|---|
| **Unbilled** | Stripe recorded less than your ledger: events were rejected, a worker stopped sending, or they arrived after the invoice finalized. |
| **Unmapped customer** | Usage for a customer with no Stripe customer id, so nobody is billed. |
| **No subscription** | The Stripe customer has usage but no active subscription on the metered price, so it never reaches an invoice. |
| **Overbilled** | Stripe recorded more than your ledger, usually retries sent twice with different identifiers. |
| **Only in Stripe** | Stripe has usage for a customer your ledger has none for (wrong customer, staging traffic). |

The report lists each problem with the Stripe customer, your customer ids, both totals, the
difference, its value in money and, for unbilled and overbilled usage, the days that differ.

## Quick start

```sh
pip install git+https://github.com/vibeFrontier/meterproof
meterproof demo
```

The demo is a fictional API company billing $0.01 per call. It has five planted mistakes, including a
30-hour worker outage that would have lost a $900 charge, plus usage Stripe is still processing that
must not raise a false alarm. The report it prints is the one your own data produces.

## Check your own data

1. `meterproof init` writes a starter `meterproof.yml`.
2. Point it at your Stripe meter, your usage records and a customer mapping (below).
3. Create a Stripe **restricted key** with **Read** on *Billing meters*, *Billing meter events*, *Prices*
   and *Subscriptions*, nothing else (Dashboard > Developers > API keys > Create restricted key). Export it
   as `STRIPE_API_KEY`.
4. `meterproof check` prints the report and exits 1 if it found problems.

```yaml
version: 1
meter:
  id: mtr_61Q8...               # Stripe Dashboard > Billing > Meters
ledger:
  postgres:
    dsn_env: DATABASE_URL       # use a read-only role
    query: >
      select account_id, occurred_at, units
      from usage_events
      where occurred_at >= %(start)s and occurred_at < %(end)s
  columns:
    customer: account_id
    timestamp: occurred_at
    quantity: units
customers:
  mapping_csv: customers.csv    # columns: customer, stripe_customer
window:
  settle_minutes: 60
  skew_minutes: 5
tolerance:
  units: 0
  percent: 0
report:
  markdown: meterproof-report.md
  json: meterproof-report.json
```

### The ledger

Each row is one usage event your app sent, or should have sent, to the meter: your customer id, the
event timestamp you gave Stripe, and the value. Sources:

- `csv: usage.csv`: any CSV export.
- `postgres: {dsn_env, query}`: needs `pip install 'meterproof[postgres]'`. The query receives
  `%(start)s` and `%(end)s`; the connection is opened read-only.
- `sqlite: {path, query}`: the query receives `:start` and `:end` as ISO 8601 strings. Compare with
  `datetime()` so any stored format works: `where datetime(occurred_at) >= datetime(:start) and
  datetime(occurred_at) < datetime(:end)` (for epoch seconds, `datetime(occurred_at, 'unixepoch')`).

Rename columns with `ledger.columns`. For a meter that aggregates by **count**, each row counts once;
for **last**, the newest row's value is compared.

### Customer mapping

Stripe meters attribute usage to Stripe customer ids. Tell meterproof how yours map, with one of:

- `mapping_csv`: a CSV with `customer,stripe_customer`. A blank `stripe_customer` is reported as an
  unmapped customer.
- `map`: inline, `{acct_42: cus_Q1...}`.
- `column`: your ledger already has the Stripe customer id in this column.

Every mapped Stripe customer is checked, including ones with no usage in your ledger, so usage sent to
the wrong customer shows up.

### Subscriptions

meterproof finds every price that bills the meter, archived ones included, and checks every customer
subscribed to one of them. A customer with usage counts as subscribed if a subscription on one of those
prices is active, trialing, past due or unpaid, or was cancelled after the window started. To limit the
check to certain prices, set `customers.price` to a price id or a list of them.

### Timing

Stripe processes meter events asynchronously, so the newest usage may not be summarized yet.
meterproof stops the window at the last full hour before `settle_minutes` ago (default 60) on both
sides, and the report says how many newer rows it left for the next run.

Senders that queue or batch events can stamp them a little later in Stripe than in your ledger.
`skew_minutes` (default 5) allows that much drift at the window edges: usage clearly inside the window
must be in Stripe, and Stripe may hold at most the usage of the slightly wider window. Set it to your
batch flush interval, or 0 if you pass your ledger's own timestamp to Stripe.

Meter summaries are time ranges, independent of billing periods, so any window is a valid comparison.
Stripe only bills events that arrive before an invoice finalizes, about an hour after the period ends by
default. Run meterproof daily, and once more 15 to 30 minutes after each period ends with a shorter
settle window, so the last hour is covered while it can still be fixed:

```sh
meterproof check --period previous-month --settle-minutes 15
```

With the default 60-minute settle window, the last hour of a period is only checked once the invoice
has usually finalized. `--start` and `--end` check any window, such as a subscription's own billing
period. Early on the 1st, a month-to-date run with nothing settled yet prints "Nothing to check yet"
and exits 0.

### Money

Each problem is priced with the customer's own subscription price, or the meter's only price, including
`transform_quantity` (package) rounding. For tiered prices, or to override, set `pricing.unit_amount`
(per unit, in major currency units) and `pricing.currency`. Without a per-unit price, the report shows
quantities only.

## Command line

```
meterproof check [-c meterproof.yml] [--period month-to-date|previous-month|last-7-days|last-24-hours]
                 [--start 2026-11-01] [--end 2026-12-01] [--settle-minutes N] [--markdown FILE]
                 [--json FILE] [--fail-on all|none|unbilled,overbilled,...] [-q]
meterproof demo [--dir meterproof-demo]
meterproof init [meterproof.yml]
```

Exit codes: `0` no problems (or nothing settled to check yet), `1` problems found (of the `--fail-on`
types), `2` configuration, ledger or Stripe error. Stripe rate limits, server errors and dropped
connections are retried before giving up.

## GitHub Action

```yaml
- uses: vibeFrontier/meterproof@v0
  with:
    config: meterproof.yml
    period: month-to-date
    postgres: "true"
  env:
    STRIPE_API_KEY: ${{ secrets.METERPROOF_STRIPE_KEY }}
    DATABASE_URL: ${{ secrets.METERPROOF_DATABASE_URL }}
```

The report goes to the job summary. Outputs: `ok`, `problems`, `revenue-at-risk`, `report-markdown`,
`report-json`. A full scheduled workflow is in [`examples/meterproof-workflow.yml`](examples/meterproof-workflow.yml).

## Demo data in Stripe test mode and Postgres

`meterproof demo` also writes `seed.sql`, the demo ledger for Postgres. `scripts/seed_stripe_test.py`
creates the demo meter, price, customers and subscriptions in a Stripe **test-mode** account and sends
the demo's meter events, planted mistakes included, so you can run `meterproof check` against real
Stripe responses.

## License

MIT. meterproof is made by [Vibe Frontier](https://www.vibefrontier.com).
