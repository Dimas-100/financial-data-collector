-- 0007: money the records don't explain yet (rebuilt by `derive`): each day's change in the replay that no
-- transaction or split accounts for, most often a deposit the broker's balance shows before its activity feed does.

CREATE TABLE unexplained_daily (
  as_of_date  TEXT NOT NULL,
  account_id  INTEGER NOT NULL REFERENCES accounts(id),
  cash        REAL NOT NULL,   -- the cash change no transaction explains
  holdings    REAL NOT NULL,   -- the share changes no transaction or split explains, at the day's close
  amount      REAL NOT NULL,   -- cash + holdings: money moved that the records don't explain yet
  PRIMARY KEY (as_of_date, account_id)
);
