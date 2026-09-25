# AGENTS.md — financial-data-collector

Guidance for any coding agent working in this repo. `CLAUDE.md` points here.

## What this is

A local SQLite warehouse (`data/warehouse.db`) for brokerage positions, cash, transactions,
daily prices and full as-reported SEC financial statements, filled by `fdc sync`. Other
projects consume the database; this one owns collection and storage. Design spec:
`docs/superpowers/specs/2026-09-25-financial-data-collector-design.md`. Implementation plan:
`docs/superpowers/plans/2026-09-25-financial-data-collector.md`.

## Rules

- **Privacy.** `data/`, `inbox/`, `config.toml`, `.env`, `watchlist.txt` are gitignored and must stay so.
  Fixtures are synthetic: invented account names, invented quantities, widely held symbols only.
  Never paste a real balance, label or credential into code, tests, docs or commit messages.
  `tests/test_privacy.py` is the gate.
- **Store owns SQL.** Only `store.py` (and `migrate.py`, `readonly.py`) execute SQL. Adapters and
  collectors produce dataclasses from `models.py`.
- **Schema changes are migrations.** Add `migrations/NNNN_name.sql`; never edit an applied file.
  New statement line items are seed edits in `seeds/concept_map.csv`, not code.
- **No network in tests.** Collectors take `fetch` (and `yf`, `sleep`) as parameters.
- **Idempotent steps.** Re-running `fdc sync` with nothing new must not change data rows.
- **New feeds live here**, not in consumer projects.

## Layout

```
src/financial_data_collector/
  cli.py            fdc init | sync | import | status | query | mcp
  sync.py           runs steps ingest -> prices -> sec, logs sync_runs, exit codes
  ingest.py         inbox routing by file signature; SnapTrade folder walk; hash-based idempotency
  adapters/         fidelity_positions.py, fidelity_history.py, snaptrade.py -> models.Snapshot / TransactionRow
  collectors/       prices.py (Tiingo|yfinance), sec_cik.py, sec_facts.py (EDGAR company facts)
  statements.py     facts + concept_map -> financial_line_items (annual, quarter, YTD differencing, derived Q4)
  store.py          Store: every write; migrate.py: migrations, seed, generated wide views
  readonly.py       SQL guard + mode=ro connection; mcp_server.py: schema/query/status tools
  migrations/*.sql  0001 tables, 0002 views;  seeds/concept_map.csv
```

## Tables and views (start here when querying)

- `positions_latest`, `holdings_history`, `account_values_daily`, `portfolio_daily` (household total plus `fidelity_total`)
- `prices` (symbol, date, close, adj_close, dividend, split_factor)
- `transactions` (typed: buy, sell, dividend, reinvest, contribution, withdrawal, interest, fee, transfer, other)
- `financials_annual`, `financials_quarterly` (one wide row per company per period; `has_derived_items` marks rows with computed values, `financial_line_items.is_derived` says which)
- `sec_facts` (raw XBRL, every filing's value for every period), `financial_line_items` (shaped), `concept_map`
- `sync_status` / `sync_runs`, `ingested_files`, `securities`, `accounts`

More examples: `docs/QUERIES.md`.

## Commands

```
.venv\Scripts\python -m pytest            # all tests, no network
fdc sync --dry-run                        # list steps
fdc sync --only prices                    # one step
fdc query "select * from sync_status"
```
