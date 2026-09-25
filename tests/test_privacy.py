"""Nothing private may be tracked by git. Runs against the real repo."""
import subprocess
from pathlib import Path

from financial_data_collector import config as C

ROOT = Path(__file__).resolve().parents[1]
ALLOWED_DATA_DIRS = ("tests/fixtures/", "src/financial_data_collector/seeds/")


def _tracked() -> list[str]:
    out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True).stdout
    return [l.strip() for l in out.splitlines() if l.strip()]


def test_no_private_files_tracked():
    bad = []
    for f in _tracked():
        low = f.lower()
        if low.endswith((".db", ".db-wal", ".db-shm", ".sqlite", ".sqlite3")):
            bad.append(f)
        if f in (".env", "config.toml", "watchlist.txt"):
            bad.append(f)
        if (f.startswith("data/") or f.startswith("inbox/")) and not f.endswith(".gitkeep"):
            bad.append(f)
        if low.endswith((".csv", ".json")) and not f.startswith(ALLOWED_DATA_DIRS):
            bad.append(f)
    assert bad == [], f"private or data files tracked by git: {bad}"


def test_examples_match_package_constants():
    assert (ROOT / "config.example.toml").read_text(encoding="utf-8") == C.CONFIG_EXAMPLE
    assert (ROOT / ".env.example").read_text(encoding="utf-8") == C.ENV_EXAMPLE


def test_powershell_scripts_are_ascii_without_bom():
    scripts = list((ROOT / "scripts").glob("*.ps1"))
    assert scripts, "scripts/*.ps1 missing"
    for p in scripts:
        raw = p.read_bytes()
        assert not raw.startswith(b"\xef\xbb\xbf"), f"{p.name} has a BOM"
        assert all(b < 128 for b in raw), f"{p.name} is not pure ASCII"
