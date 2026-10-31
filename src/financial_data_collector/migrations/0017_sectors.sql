-- 0017: each company's sector. Taken from the sibling classifications feed when it names the company, else from the
-- SEC industry code (securities.sic, 0016) through the package's sic_sector.csv. sector_source says which, in words
-- ("feed: classifications.json as of 2026-10-27", "sec: SIC 3571"), so a reader can tell a feed's word from a guess.
-- Only companies (stock, foreign_filer) get one; a fund's sector is NULL.

ALTER TABLE securities ADD COLUMN sector TEXT;
ALTER TABLE securities ADD COLUMN sector_source TEXT;
