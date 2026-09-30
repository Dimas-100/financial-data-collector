-- 0006: connections to SnapTrade and SimpleFIN, and what an account needs to have come through one.

CREATE TABLE connections (
  name           TEXT PRIMARY KEY,               -- snaptrade | simplefin
  key_ref        TEXT NOT NULL,                  -- the key store entry's name, or 'env' when the key is in .env
  created_at     TEXT NOT NULL,
  removed_at     TEXT,
  last_fetch_at  TEXT,
  last_ok_at     TEXT,
  last_error     TEXT NOT NULL DEFAULT ''        -- redacted: never an address with a password, never a key
);

ALTER TABLE accounts ADD COLUMN external_key TEXT;                                 -- a hash; the identity of a connected account
ALTER TABLE accounts ADD COLUMN origin TEXT NOT NULL DEFAULT 'file';               -- file | snaptrade | simplefin
ALTER TABLE accounts ADD COLUMN kind_confirmed INTEGER NOT NULL DEFAULT 1;         -- 0 while account_type is a guess
ALTER TABLE accounts ADD COLUMN credit_limit REAL;                                 -- what the person entered
ALTER TABLE accounts ADD COLUMN rate_pct REAL;                                     -- yearly, in percent
ALTER TABLE accounts ADD COLUMN flows TEXT NOT NULL DEFAULT 'transactions';        -- balance: no transactions explain its changes
CREATE UNIQUE INDEX idx_accounts_external_key ON accounts (external_key) WHERE external_key IS NOT NULL;

ALTER TABLE cash_balances ADD COLUMN available REAL;                               -- only when the service reports one
