"""What the Sources tab costs and counts: SEC coverage comes from the securities table and an indexed MAX, not a
scan of every fact, so a two-second refresh stays cheap on a warehouse with a million facts."""
from pathlib import Path

from financial_data_collector.store import Store


def test_sec_facts_filed_is_indexed_and_companies_come_from_securities(tmp_path: Path):
    s = Store.open(tmp_path / "w.db")
    try:
        indexes = {r[0] for r in s.query("SELECT name FROM sqlite_master WHERE type = 'index'")}
        assert "idx_sec_facts_filed" in indexes
        assert 9 in {r[0] for r in s.query("SELECT version FROM schema_version")}
        s.upsert_security("AAPL", first_seen="2026-01-01")
        s.upsert_security("KO", first_seen="2026-01-01")
        s.set_security_cik("AAPL", "0000320193", "Apple Inc.")
        s.set_security_cik("KO", "0000021344", "Coca-Cola")
        s.mark_sec_fetch("AAPL", "2026-01-02T00:00:00Z")
        facts = s.source_facts()
        assert facts["sec"] == {"companies": 1, "latest_filed": None}     # fetched companies, no facts stored yet
        plan = " ".join(str(tuple(r)) for r in s.query("EXPLAIN QUERY PLAN SELECT MAX(filed) FROM sec_facts"))
        assert "idx_sec_facts_filed" in plan
    finally:
        s.close()
