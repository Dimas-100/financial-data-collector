-- 0005: Fidelity history rows stored before the adapter's classification fixes.
-- IRA deposits ("CASH CONTRIBUTION CURRENT YEAR") were stored as 'other', so nothing counted them as money put in,
-- and the import's cross-source check (which compares types) kept the ones the SnapTrade feed also holds; a
-- "DIRECT DEBIT" was 'other' too; a share distribution (a split) was a cash withdrawal; and cash-only rows kept
-- 0 units where the adapter now stores NULL. Each repaired row is re-keyed the way the adapter keys it today
-- (keeping a "#2"-style repeat suffix), so downloading the same history again still adds nothing.
-- fdc_dedupe_key is adapters.base.dedupe_key, registered by apply_migrations.

DELETE FROM transactions
WHERE source = 'fidelity_csv' AND type = 'other' AND UPPER(description) LIKE 'CASH CONTRIBUTION%'
  AND EXISTS (SELECT 1 FROM transactions o
              WHERE o.account_id = transactions.account_id AND o.trade_date = transactions.trade_date
                AND o.type = 'contribution' AND o.source != 'fidelity_csv'
                AND ABS(COALESCE(o.amount, 0) - COALESCE(transactions.amount, 0)) <= 0.02);

UPDATE transactions SET type = 'contribution'
WHERE source = 'fidelity_csv' AND type = 'other' AND UPPER(description) LIKE 'CASH CONTRIBUTION%';

UPDATE transactions SET type = 'withdrawal'
WHERE source = 'fidelity_csv' AND type = 'other' AND UPPER(description) LIKE 'DIRECT DEBIT%';

UPDATE transactions SET type = 'other', amount = NULL
WHERE source = 'fidelity_csv' AND type = 'withdrawal' AND UPPER(description) LIKE 'DISTRIBUTION%'
  AND symbol IS NOT NULL AND units > 0;

UPDATE transactions SET units = NULL WHERE source = 'fidelity_csv' AND units = 0;

UPDATE transactions
SET dedupe_key = fdc_dedupe_key((SELECT label FROM accounts WHERE accounts.id = transactions.account_id),
                                trade_date, type, symbol, units, amount)
                 || CASE WHEN INSTR(dedupe_key, '#') > 0 THEN SUBSTR(dedupe_key, INSTR(dedupe_key, '#')) ELSE '' END
WHERE source = 'fidelity_csv';
