"""GraphStore: the Business Experience Graph interface, and the store selector.

SqliteGraph is the reference implementation and what the demo runs on.
CogneeGraph is the real sponsor integration and must return identically shaped
results -- tests/test_swap.py asserts that.
"""
from __future__ import annotations

import sys
from abc import ABC, abstractmethod
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import config  # noqa: E402


class GraphStore(ABC):
    name = "abstract"

    @abstractmethod
    def peers(self, merchant_id: str) -> dict: ...

    @abstractmethod
    def peer_playbook(self, situation_kind: str, peer_ids: list[str]) -> dict: ...

    @abstractmethod
    def failed_plays(self, action_family: str, peer_ids: list[str]) -> dict: ...

    @abstractmethod
    def local_pattern(self, category: str, locality: str) -> dict: ...

    @abstractmethod
    def cohort_profile(self, category: str, locality: str) -> dict: ...

    @abstractmethod
    def cohort_seasonality(self, category: str, locality_type: str, horizon: int) -> dict: ...

    @abstractmethod
    def record_action(self, merchant_id: str, atype: str, params: dict,
                      situation_id: int | None, started_on: str, ended_on: str,
                      run_id: str) -> int: ...

    @abstractmethod
    def record_outcome(self, action_id: int, metric: str, before: float, after: float,
                       verdict: str, measured_on: str, cohort_key: str,
                       situation_kind: str, action_type: str) -> dict: ...


_store: GraphStore | None = None


def get_store() -> GraphStore:
    global _store
    if _store is None:
        if config.USE_REAL_COGNEE:
            from backend.graph.cognee_store import CogneeGraph
            _store = CogneeGraph()
        else:
            from backend.graph.sqlite_store import SqliteGraph
            _store = SqliteGraph()
    return _store


def reset_store() -> None:
    global _store
    _store = None
