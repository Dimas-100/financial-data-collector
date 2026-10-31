-- 0014: benchmarks. securities.benchmark = 1 marks a symbol the config names as a yardstick (the market index and the
-- sector funds a move is measured against). Their prices are collected like any symbol's; they are never counted as
-- holdings and stay out of the export unless the owner also holds them or lists them. mark_benchmarks keeps it in step
-- with config.toml on every sync, so removing a benchmark there clears the mark.

ALTER TABLE securities ADD COLUMN benchmark INTEGER NOT NULL DEFAULT 0;
