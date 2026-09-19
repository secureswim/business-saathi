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


# ---------------------------------------------------------------------------
# Scripted model, so the agent loop is testable without a network or a key.
#
# A test hands over a list of rounds. Each round is either a list of tool calls
# the model "decides" to make, or a final answer. The loop, the tool execution,
# the grounding gate and the repair path are all exercised for real -- the only
# thing faked is the model's choice, which is exactly the part a test should
# be pinning anyway.
# ---------------------------------------------------------------------------
class ScriptedLLM:
    def __init__(self, rounds):
        self.rounds = list(rounds)
        self.seen = []          # every message list the "model" was shown
        self.calls = []         # every tool the script asked for
        self.turns = 0

    def __call__(self, messages, tools=None, **kwargs):
        self.turns += 1
        self.seen.append(messages)
        if not self.rounds:
            return {"provider": "scripted", "text": "", "calls": [
                {"id": "z", "name": "final_answer", "arguments": {
                    "hinglish": "Theek hai.", "english": "Okay.",
                    "confidence": "low"}}]}
        step = self.rounds.pop(0)
        if isinstance(step, dict) and "final" in step:
            return {"provider": "scripted", "text": "", "calls": [
                {"id": f"f{self.turns}", "name": "final_answer",
                 "arguments": step["final"]}]}
        if isinstance(step, dict) and "text" in step:
            return {"provider": "scripted", "text": step["text"], "calls": []}
        calls = [{"id": f"c{i}", "name": name, "arguments": args}
                 for i, (name, args) in enumerate(step)]
        self.calls.extend((c["name"], c["arguments"]) for c in calls)
        return {"provider": "scripted", "text": "", "calls": calls}

    def tools_called(self):
        return [name for name, _ in self.calls]

    def args_for(self, name):
        return next((a for n, a in self.calls if n == name), None)


@pytest.fixture
def scripted(monkeypatch):
    """Install a scripted model and return the harness."""
    from backend.reasoning import providers

    def install(rounds):
        llm = ScriptedLLM(rounds)
        monkeypatch.setattr(providers, "chat", llm)
        monkeypatch.setattr(providers, "available", lambda: True)
        return llm

    return install
