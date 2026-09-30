# Connections — SnapTrade and SimpleFIN, with the person's own keys

**Date:** 2026-09-29 · **Status:** design approved in conversation; this document awaits review · **Builds on:** `2026-09-25-financial-data-collector-design.md`, `2026-09-25-v0.2-analytics-and-cockpit-design.md`

## 1. Why

Today the warehouse fills from two places: Fidelity CSV exports, and an owner-only folder that another of the owner's projects writes. Someone who has neither can't get their own accounts in without writing code.

This is part 1 of four that make kestrel (the dashboard that reads this warehouse) something a non-technical person can set up:

| # | Part | Repo |
|---|---|---|
| **1** | **Connections in the collector (this spec)** | financial-data-collector |
| 2 | A setup wizard: a window that drives part 1 and writes kestrel's profile | kestrel |
| 3 | Double-click install | kestrel |
| 4 | Pages with nothing to show: plan inputs, hiding unused pages | kestrel |

**Goal of part 1.** A person comfortable with a terminal, with none of the owner's other projects, connects their brokerages and banks with their own keys, runs `fdc sync`, and has their accounts, holdings, cash and debts in the warehouse.

## 2. Decisions

| Decision | Choice | Why |
|---|---|---|
| Who holds the key | Each person brings their own | A key can't ship in a public repo, and hosting one means a server, a monthly fee and responsibility for other people's account access |
| Brokerages | SnapTrade, with its free personal key | Covers 35+ brokerages; free for personal use |
| Banks, cards, loans | SimpleFIN Bridge | The person signs up and pays it themselves; the app only ever receives a token |
| Not used | Plaid | Its sign-up is written for businesses |
| Where the code lives | Inside the collector, writing through the existing store | One path and one place for history. Rejected: porting the owner's exporters to write files the collector then reads (two hops, balances in extra files); handing bank data straight to kestrel as a feed (a feed keeps no history) |
| Bank data kept | Balances only | It is all kestrel's pages use; no merchant names on disk or reachable through the MCP server |
| SnapTrade client | Hand-written signed requests, read calls only | The collector then holds no code that can place an order |
| How many | One connection of each kind | One SimpleFIN subscription covers 25 institutions; one personal SnapTrade key covers all of a person's brokerages |
| The owner's own setup | Untouched | The owner tries the new connections in a separate warehouse; moving over is a later step of its own |
| Platform | Windows first | The key store library also covers macOS and Linux |

## 3. Step zero: a trial with real keys

Nothing is built on an assumption about either service until it has been tried. The first task runs by hand, in a separate root (`fdc --root <a test folder>`), against a real personal SnapTrade key and a real SimpleFIN token, and confirms:

1. **The personal key works alone.** Listing accounts, positions, balances and activity succeeds with the client id and consumer key only, with no registered user id or user secret.
2. **What an account carries.** Which fields give the institution, the account's name and its kind, and whether any field carries an account number (it must be dropped before anything is stored).
3. **A broken brokerage login.** How the service reports a connection the person must repair.
4. **SimpleFIN's answers.** That the bridge accepts `balances-only=1` and `version=2`; the shape of its `errors`; how a card's balance is signed; whether account names carry digits from the account number.
5. **Limits.** How often each service may be asked.

Findings go in Appendix A as facts about shapes and behaviour, never a value, a label or an id. **If (1) fails, the work stops and this design is revisited.**

## 4. Commands

| Command | What it does |
|---|---|
| `fdc connect snaptrade` | Prints the steps and opens SnapTrade's site. Asks for the client id and the consumer key, typed hidden. Checks them by listing accounts, saves them, and lists the accounts found (name and kind, never a balance). |
| `fdc connect simplefin` | Prints the steps and opens SimpleFIN's site. Asks for the setup token, typed hidden. Exchanges it for the access address, saves that, and lists the accounts found. |
| `fdc connections` | One row per connection: kind, when it was made, when it last worked, its last error, how many accounts. Never a key. |
| `fdc disconnect <name>` | Deletes the saved key and marks the connection removed. Stored history stays. |
| `fdc accounts` | Every account: label, institution, kind (marked `guessed` until confirmed), credit limit, rate, where it came from. |
| `fdc accounts set <label> [--kind K] [--limit N] [--rate P]` | Confirms or corrects a kind; sets a card's limit or a yearly rate in percent. |
| `fdc sync` | Gains a first step, `connections` (§9). |

- **Logic in functions, commands thin.** Each command calls a function in the new `connections/` package that takes its inputs as arguments and returns data. Part 2's wizard calls the same functions; nothing is reachable only through a prompt.
- **`fdc connect` on an existing connection** replaces its key after the new one has been checked; a failed check leaves the old key in place.
- **Exit status:** 0 done; 1 the service refused the key or couldn't be reached; 2 a usage or config problem.

## 5. Keys

- **Stored in the operating system's key store** through `keyring` (Windows Credential Manager; Keychain on macOS; Secret Service on Linux). New dependency: `keyring`.
- **One entry per connection per warehouse.** `connect` makes a random reference and stores it in the `connections` table (§7); the key store entry is named by that reference, under the service name `financial-data-collector`. Two warehouses on one computer (the trial and a real one) therefore never share or overwrite a key. The database holds the reference, never the key.
- **What is saved.** SnapTrade: the client id and consumer key. SimpleFIN: the access address, which contains its own user name and password and is treated as a secret throughout.
- **Without a key store** (a server with no desktop session), the variables `FDC_SNAPTRADE_CLIENT_ID`, `FDC_SNAPTRADE_CONSUMER_KEY` and `FDC_SIMPLEFIN_ACCESS_URL` are read instead, from the environment or `.env`.
- **Never anywhere else:** not `config.toml`, the database, a log, `sync_runs`, or an error message. Every message that leaves the fetchers passes through one `redact()` that removes the user name and password from any address and any text equal to a loaded secret.
- The secrets already in `.env` (`SEC_USER_AGENT`, `TIINGO_API_TOKEN`) are unchanged.

## 6. Fetching

Both fetchers take `fetch` (and `post`, `now`) as parameters, as the collectors do, and return the dataclasses in `models.py`. No SQL.

### 6.1 SnapTrade (`connections/snaptrade.py`)

- **Requests** are signed as SnapTrade's request-signing documentation describes and step zero confirms. Every request is a GET.
- **Calls, and only these:** list accounts; per account its positions, its balances and its activity.
- **Activity window:** from seven days before the newest activity already stored for that account; on the first fetch, as far back as the service gives. The existing `dedupe_key` makes the overlap harmless.
- **Mapped to** the existing `Snapshot` (`source = "snaptrade"`, `as_of_date` the fetch's UTC date, `fetched_at` its time) and `TransactionRow`, with the activity-type map the folder adapter already uses (moved to one shared place). A money-market sweep position counts as cash, as it does there.
- **Not fetched:** open orders, option positions.
- **Never called:** any endpoint that places, changes or cancels an order. A test pins the module's request paths to the read set above and its method to GET.

### 6.2 SimpleFIN (`connections/simplefin.py`)

- **Claim (once).** The setup token is base64 for a claim address. It must decode to an `https://` address, which is sent one POST; the answer must be an `https://` address carrying a user name and password. A token is single-use, so after the POST has been sent a failure is reported and never retried.
- **Fetch.** One GET to `<address>/accounts?balances-only=1&version=2`, with the user name and password moved from the address into a Basic `Authorization` header. Transactions are never requested.
- **Read per account:** its id, name, institution, currency, balance, available balance and balance date; and the answer's `errors`.
- **Mapped to** one `CashRow` per account as of the balance date's UTC day, `source = "simplefin"`: the balance with the service's own sign, so what is owed is below zero. `CashRow` gains an optional `available`.
- **A currency other than USD** is stored as it is. Converting it is out of scope.

## 7. Schema (migration `0006_connections.sql`)

**`connections`** — `name` (PK: `snaptrade` | `simplefin`), `key_ref`, `created_at`, `removed_at`, `last_fetch_at`, `last_ok_at`, `last_error` (redacted text).

**`accounts`, new columns:**

| Column | Values | Default |
|---|---|---|
| `external_key` | a hash (§8); unique where set | null |
| `origin` | `file` \| `snaptrade` \| `simplefin` | `file` |
| `kind_confirmed` | 1, or 0 while the kind is a guess | 1 |
| `credit_limit` | what the person entered | null |
| `rate_pct` | yearly, in percent: what cash earns or debt costs | null |
| `flows` | `transactions` \| `balance` (§8) | `transactions` |

`AccountRef` gains `external_key`, `origin`, `kind_confirmed` and `flows`, each defaulting to the value above, so the file adapters are unchanged.

**`cash_balances`, new column:** `available` (null unless the service reports one).

**New values of `accounts.account_type`:** `credit_card`, `loan`, `mortgage`, `line_of_credit`, beside the existing ones.

No existing view changes shape. The schema version becomes 6; kestrel's reader asks for 3 or later, so it keeps working before its own follow-up (§14).

## 8. Accounts

- **Identity.** `external_key` is the SHA-256 of `<origin>:<the service's own id for the account>`. A fetched account is matched by that key, never by its label. The service's id is not stored, and neither is any account number.
- **Label.** The institution, then the account's name: `Example Bank Checking`. Made once, when the account is first seen, and then fixed, so history and kestrel's addresses don't move. A label already taken gets ` 2`, ` 3`.
- **Digits are stripped from the name first.** A run of three or more digits goes, with the mask in front of it (`...`, `x`, `*`, `#`, `-`) or the brackets around it: `Checking ...1234`, `Visa x1234` and `Savings (1234)` become `Checking`, `Visa` and `Savings`. A run that is part of a word stays (`401k`).
- **Kind.** From SnapTrade's own text for the account's kind when it supplies one, else from the account's name (never the institution's). Either goes through this table, top to bottom, first match wins. A keyword matches where a word starts, in any case: `invest` finds `Investment`.

  | The text contains | Kind |
  |---|---|
  | `mortgage` | `mortgage` |
  | `line of credit`, `heloc` | `line_of_credit` |
  | `loan` | `loan` |
  | `checking` | `checking` |
  | `saving` | `savings` |
  | `money market` | `money_market` |
  | `credit`, `card`, `visa`, `mastercard`, `amex`, `discover` | `credit_card` |
  | `roth` | `roth_ira` |
  | `ira`, `rollover` | `traditional_ira` |
  | `401` | `401k` |
  | `brokerage`, `invest`, `individual`, `joint` | `brokerage` |
  | nothing above | `credit_card` when the balance is below zero, else `other` |

  Checking and savings come before the card words, so `Debit Card Checking` is a checking account. A kind taken from SnapTrade's own text is stored as confirmed; one taken from a name, or from the last row, is a guess, stored with `kind_confirmed = 0`.
- **What the person sets wins.** `fdc accounts set` stores the kind with `kind_confirmed = 1`. A later sync never changes a confirmed kind, a limit or a rate. (Today's account upsert overwrites the kind on every write; for an account with an `external_key` it sets it only on insert or while the kind is still a guess.)
- **`flows`.** `balance` for every SimpleFIN account, `transactions` for the rest. It tells a reader that the account has no transactions to explain its changes, so every change in its balance is money moved in or out, never growth. Interest on such an account is therefore not shown as growth in this version.

## 9. Sync

- `STEPS = ("connections", "ingest", "prices", "sec", "derive", "export")`; `connections` joins `NETWORK_STEPS`.
- **Each connection runs on its own.** One that fails is recorded in `connections.last_error` and named in the step's message; the other still runs. The step is `skipped` with no connections, `error` when every connection failed, else `ok`.
- **Not too often.** `[connections] min_hours = 6` in `config.toml`: a connection fetched more recently than that is skipped with `fetched 2 hours ago`.
- **Idempotent.** A second fetch on the same day replaces that day's rows for the accounts it covers with the same values; with nothing new, no data row changes.
- **`derive`** needs no new rule: a bank account is an account with cash snapshots and no transactions, which the replay already carries forward between the days it has a balance for. The plan checks that such an account adds no rows to `reconciliation`.

## 10. When something fails

| Situation | The message |
|---|---|
| No key saved | `no SnapTrade key saved: run fdc connect snaptrade` |
| SnapTrade refuses the key | `SnapTrade refused the key: make a new one on SnapTrade's site, then run fdc connect snaptrade` |
| A brokerage login has expired | `<brokerage> needs reconnecting on SnapTrade's site`; the other accounts still update |
| SimpleFIN answers 403 | `SimpleFIN refused the saved token: run fdc connect simplefin again` |
| The setup token was already used | `this setup token has been used: make a new one on SimpleFIN's site` |
| A bank needs attention | SimpleFIN's own message, redacted |
| The service can't be reached | `<host> couldn't be reached (<reason>)`: the host only, never the address |
| No key store on this computer | `no key store on this computer: set FDC_… in .env instead` |

## 11. Privacy and safety

- A key exists in the key store (or the environment) and in memory while a request is made, and nowhere else.
- No account number and no service id is stored. Names are stripped of digits before they are.
- Bank transactions are never requested, so they never reach this computer.
- The MCP server reads the whole database. Nothing this spec adds to the database is a secret: `key_ref` is a random reference that opens nothing on its own, and `last_error` is redacted.
- The collector gains no code that can place, change or cancel an order, and a test keeps it so.

## 12. Tests

No network; fixtures are synthetic (invented institutions, names and amounts).

- **Signing:** a known key, path and time give a known signature.
- **SnapTrade mapping:** a fixture answer becomes the expected `Snapshot` and `TransactionRow`s; an account-number field in the answer reaches no dataclass.
- **The read set:** the module's request paths and method are exactly those in §6.1.
- **SimpleFIN claim:** a good token; a token that isn't base64; one that decodes to `http://`; an answer without a user name and password; a failed POST is not sent twice.
- **SimpleFIN mapping:** signs, the available balance, the balance date's day, `errors` passed on.
- **Names and kinds:** every row of §8's table; every digit pattern; `401k` kept; two accounts with one label.
- **Identity:** the same account fetched twice is one row; a confirmed kind, a limit and a rate survive a sync.
- **Keys:** with a fake key store, `connect` saves, `disconnect` deletes, two roots don't collide, a failed check keeps the old key, the environment is used when there is no key store.
- **Redaction:** an address with a password, and each secret's own text, appear in no message, no `sync_runs` row and no `connections.last_error`.
- **Sync:** one connection failing leaves the other's rows written; `min_hours`; a second run changes no data row.
- **Migration:** a version-5 database becomes version 6 with its rows and views intact.
- **Privacy test:** no key name with a value, and no file from the trial, is tracked.

## 13. Out of scope

The setup wizard and the installer (parts 2 and 3); Plaid; bank transactions; open orders and option positions; renaming an account; currency conversion; more than one connection of a kind; moving the owner's own setup onto these connections; any change to the owner-only folder source, which keeps working as it does.

## 14. Follow-up: kestrel's reader

A small spec of its own, in kestrel, once this has landed. Its `fdc` reader learns to:

- file `credit_card`, `loan`, `mortgage` and `line_of_credit` under `debt`, with the value turned to what is owed;
- read `credit_limit` and `rate_pct`, and offer owed + available as the limit when no limit was entered and the service reports a real available amount;
- count every change in a `flows = 'balance'` account as money moved.

Its Reserves page then fills from the collector alone, and its connector guide stops saying that a debt can only come from a feed.

## 15. Docs changed with the code

`README.md` (quick start gains the two `connect` commands; "Other brokers and sources"), `AGENTS.md` (layout, tables), `CHANGELOG.md`, `config.example.toml` and `.env.example` with their constants in `config.py`.

## Appendix A: what the trial found

Written by step zero (§3): for each of its five questions, what was confirmed and anything this design must change because of it. Shapes and behaviour only.
