# Connections

`fdc connect` links a service you hold your own key for. The collector then fetches from it on every `fdc sync`.
Nothing here is written back to any service, and no key is ever kept in the database, `config.toml`, a log or a
message.

| Command | What it does |
|---|---|
| `fdc connect snaptrade` | Your brokerages, through [SnapTrade](https://snaptrade.com/) (free for personal use). Connect each brokerage on SnapTrade's site, create a personal API key there, and paste its client id and consumer key. A key that has a registered user (four values) works too: `fdc connect snaptrade --with-user`. |
| `fdc connect simplefin` | Your banks, cards and loans, through [SimpleFIN Bridge](https://beta-bridge.simplefin.org/) ($15 a year, up to 25 institutions). Connect them on its site, choose *New app*, copy the setup token and paste it. It works once. |
| `fdc connections` | Each connection: since when, when it last worked, how many accounts, its last error. |
| `fdc disconnect snaptrade` | Forgets the key. The accounts and their history stay. |
| `fdc accounts` | Every account with its kind (marked *guessed* until you confirm it), credit limit and rate. |
| `fdc accounts set "Example Bank Visa" --kind credit_card --limit 5000 --rate 24.9` | Confirms or corrects a kind, sets a card's limit or a yearly rate. A sync never changes what you set. |

Use one route per account. A file that names an account you have connected is skipped with a note; an account
that reaches the warehouse under two names, one from a CSV and one from a connection, is counted twice.

## What is fetched and stored

| From | Fetched | Stored |
|---|---|---|
| SnapTrade | accounts, positions, cash balances, activity, and which brokerage logins need repair | the same tables a Fidelity export fills (`position_snapshots`, `cash_balances`, `transactions`) |
| SimpleFIN | institution, account name, balance, available balance, balance date | one `cash_balances` row per account per day; a debt is below zero. Transactions are never requested (`balances-only=1`), so they never reach this computer |

Every SnapTrade request is a signed GET to one of five read paths; the collector holds no code that can place,
change or cancel an order, and `tests/test_snaptrade_connection.py` pins that.

**Accounts.** A connected account is known by a hash of the service's own id, never by its name, so a rename at
the service keeps its history. Its label is *institution + name*, fixed when first seen; digits that look like
part of an account number are stripped first (`Checking ...1234` becomes `Checking`), and a second account with
the same name gets ` 2`. Its kind comes from SnapTrade's own text when there is one, else from its name
(`Cash Rewards Visa` is a card); a SimpleFIN account with nothing in its name is a card when its balance is below
zero. `fdc accounts` shows which kinds are guesses. A SimpleFIN account is marked `flows = balance`: it has no
transactions to explain its changes, so a reader counts every change as money moved, never growth.

**Keys** live in your computer's key store (Windows Credential Manager, the macOS Keychain, the Linux Secret
Service), one entry per connection per warehouse. On a computer without one, `fdc connect` writes the key to
`.env` beside `config.toml` and says so; a key put there by hand (the variable `FDC_SIMPLEFIN_ACCESS_URL`) is a
connection too.

**Sync.** `connections` is the first step of `fdc sync`. A connection is asked at most every `[connections]
min_hours` (6 by default). One failing connection never stops the other; what happened is in `fdc connections`
and in `sync_runs`.

## When something fails

| The message | What to do |
|---|---|
| `no SnapTrade key saved: run fdc connect snaptrade` | Connect (again). |
| `SnapTrade refused the key: …` | Make a new personal key on SnapTrade's site and connect again. |
| `<brokerage> needs reconnecting on SnapTrade's site` | Sign in to that brokerage again through SnapTrade's site. The other accounts still update. |
| `SimpleFIN refused the saved token: …` | Make a new setup token on SimpleFIN's site and connect again. |
| `this setup token has been used: …` | A token works once: make a new one. |
| `SimpleFIN says the subscription has lapsed: …` | Renew it on SimpleFIN's site. |
| `<institution> needs attention on SimpleFIN's site: …` | SimpleFIN's own words: usually a bank wants you to sign in again there. |
| `<host> couldn't be reached (…)` | The service or your network is down; the next sync tries again. |
| `no key store on this computer: the key is in .env instead` | Not an error. Keep `.env` where only you can read it. |

## Trying it in a separate warehouse

The first real use is a trial in its own root, so a working setup is never touched:

```bash
fdc --root ..\fdc-trial init
fdc --root ..\fdc-trial connect snaptrade
fdc --root ..\fdc-trial connect simplefin
fdc --root ..\fdc-trial sync --only connections,derive
fdc --root ..\fdc-trial accounts
fdc --root ..\fdc-trial query "select account, as_of_date, total from account_values_daily order by 1, 2"
```

What to confirm, and to note in the spec's Appendix A as shapes and behaviour only, never a value:

1. A personal SnapTrade key lists accounts, positions, balances and activity on its own (no `--with-user`).
2. Which fields carry the institution, the name and the kind, and that `fdc accounts` shows no account number.
3. How a brokerage whose login expired shows up in `fdc connections` after a sync.
4. That SimpleFIN accepts `balances-only=1` and `version=2`, how a card's balance is signed, and whether names
   carried digits (the labels in `fdc accounts` say).
5. How often each service may be asked (a refusal shows as its status in the message).
