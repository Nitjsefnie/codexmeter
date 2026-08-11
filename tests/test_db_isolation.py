"""A test process must never be pointed at the production database.

backend/app.py loads the repo's .env at import time, and .env names the live
codexmeter database in DATABASE_URL_VIZ. Nothing but luck kept the suite off it:
every DB-touching test happens to monkeypatch the variable at its own scratch
database, so the production DSN was never dialled — but any test that forgot
to, or any module-scope `import backend.app`, would have silently connected to
production and (in an ingest test) written to it.

conftest.py closes that by claiming DATABASE_URL_VIZ before any backend import.
These tests hold that claim in place.
"""
import os
from pathlib import Path

from backend import db

_REPO_ROOT = Path(__file__).resolve().parents[1]

# The database backend/../.env names. Hardcoded so the check still means
# something in a checkout that has no .env at all (CI).
PRODUCTION_DB = "codexmeter"


def _dbname(dsn: str) -> str:
    """Database name out of a postgresql:// DSN, sans query string."""
    return dsn.rsplit("/", 1)[-1].split("?", 1)[0]


def _dotenv_value(path: Path, key: str) -> str | None:
    if not path.is_file():
        return None
    for line in path.read_text().splitlines():
        line = line.strip()
        if line.startswith(f"{key}="):
            return line.split("=", 1)[1].strip()
    return None


def test_suite_dsn_is_not_the_production_database():
    dsn = os.environ["DATABASE_URL_VIZ"]
    assert _dbname(dsn) != PRODUCTION_DB, (
        f"the suite is pointed at the production database: {dsn}"
    )

    configured = _dotenv_value(_REPO_ROOT / ".env", "DATABASE_URL_VIZ")
    if configured is not None:
        assert dsn != configured, (
            f"the suite is pointed at the DSN .env deploys against: {dsn}"
        )


def test_a_later_dotenv_load_cannot_repoint_the_suite_at_production(tmp_path):
    """load_dotenv is setdefault-only, which is what makes conftest's claim
    stick. If that ever changed, importing backend.app would drag every test
    onto production.
    """
    dotenv = tmp_path / ".env"
    dotenv.write_text(f"DATABASE_URL_VIZ=postgresql:///{PRODUCTION_DB}\n")

    before = os.environ["DATABASE_URL_VIZ"]
    db.load_dotenv(str(dotenv))

    assert os.environ["DATABASE_URL_VIZ"] == before
    assert _dbname(os.environ["DATABASE_URL_VIZ"]) != PRODUCTION_DB
