import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pytest
import config
from backend.data import db

# Tests must never spend live service credits merely because a developer's
# .env has production adapters enabled. Integration health is checked explicitly
# by scripts/check_integrations.py instead.
config.USE_REAL_LLM = False
config.USE_REAL_COGNEE = False
config.USE_REAL_SARVAM = False
config.USE_REAL_N8N = False


@pytest.fixture(scope="session", autouse=True)
def _require_db():
    """Give the test run its OWN copy of the database.

    Two reasons, both learned the hard way:

    1. The tests deliberately write outcomes into the ledger. Sharing the demo
       database meant a test run left it drifted and the next rehearsal started
       from the wrong numbers.
    2. Restoring the shared file required deleting data/saathi.db-wal, which on
       Windows fails with "being used by another process" whenever the API is
       running -- so the whole suite errored out unless you stopped the server
       first. Nothing about these tests actually needs the server stopped.

    Every module reads config.DB_PATH at call time, so pointing it at a private
    copy here is enough. The copy is per-process, so parallel runs don't
    collide either.
    """
    source = config.SEED_DB_PATH if config.SEED_DB_PATH.exists() else config.DB_PATH
    if not source.exists():
        pytest.skip("no database; run python scripts/generate.py first")

    original = config.DB_PATH
    private = original.with_name(f"{original.stem}.test-{os.getpid()}.db")
    shutil.copyfile(source, private)

    db.reset_connection()
    config.DB_PATH = private
    db.connect()
    try:
        yield
    finally:
        db.reset_connection()
        config.DB_PATH = original
        for suffix in ("", "-wal", "-shm"):
            leftover = Path(str(private) + suffix)
            try:
                if leftover.exists():
                    leftover.unlink()
            except OSError:
                pass       # a stray temp file is not worth failing a run over
