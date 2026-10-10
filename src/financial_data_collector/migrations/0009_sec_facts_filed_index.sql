-- 0009: the newest filing date answered from an index. The console asks MAX(filed) on every refresh; over a
-- million facts a scan took most of a second.

CREATE INDEX idx_sec_facts_filed ON sec_facts (filed);
