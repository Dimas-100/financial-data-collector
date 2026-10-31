-- 0018: raw inputs for drop causes. current_reports keeps every 8-K / 8-K/A SEC's submissions list shows for a
-- company (accession, form, filing date, item numbers as SEC writes them, "2.02,9.01"); a row is kept as first
-- stored and never rewritten. news_headlines keeps the sibling news feed's headlines: the headline, its outlet,
-- date, publish time and a safe https link, with the feed's own date; never the article text or tags.

CREATE TABLE current_reports (
  accession TEXT PRIMARY KEY,
  cik TEXT NOT NULL,
  form TEXT NOT NULL,
  filed TEXT NOT NULL,
  items TEXT NOT NULL DEFAULT ''
);
CREATE INDEX idx_current_reports_cik ON current_reports (cik, filed);

CREATE TABLE news_headlines (
  symbol TEXT NOT NULL,
  date TEXT NOT NULL,
  title TEXT NOT NULL,
  outlet TEXT,
  url TEXT,
  published TEXT,
  feed_as_of TEXT,
  first_seen TEXT NOT NULL,
  PRIMARY KEY (symbol, date, title)
);
