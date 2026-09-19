"""The Evidence envelope -- the only channel through which facts reach the merchant.

Agreed between both developers before anything else is written. Every tool
returns one of these; the synthesis layer may quote ONLY what is inside
`value`; `basis` carries provenance so /ops can answer "where did that come
from?" and so the grounding validator has something to check against.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Literal

Source = Literal["own_data", "graph", "finance", "merchant_input", "integration", "router"]
Tier = Literal["A", "B", "C"]


@dataclass
class Evidence:
    tool: str
    value: dict[str, Any]
    basis: dict[str, Any]
    source: Source
    tier: Tier = "A"
    available: bool = True
    ask: str | None = None          # a clarifying question this tool would like answered
    ask_subject: str | None = None  # what the question is about, e.g. "cold drink"

    def to_dict(self) -> dict:
        return asdict(self)


def ok(tool: str, result: dict, source: Source, tier: Tier = "A") -> Evidence:
    """Wrap an analytics/graph result that already has value + basis."""
    return Evidence(tool=tool, value=result["value"], basis=result["basis"],
                    source=source, tier=tier)


def unavailable(tool: str, reason: str, source: Source, tier: Tier = "A",
                ask: str | None = None, ask_subject: str | None = None,
                extra_basis: dict | None = None) -> Evidence:
    """A tool ran and found nothing usable. This is a result, not an error.

    The synthesis layer omits that dimension rather than hedging about it, and
    /ops renders a greyed card so "why didn't it mention cash?" has a visible
    answer.
    """
    basis = {"source": tool, "reason": reason}
    if extra_basis:
        basis.update(extra_basis)
    return Evidence(tool=tool, value={"available": False, "reason": reason},
                    basis=basis, source=source, tier=tier, available=False,
                    ask=ask, ask_subject=ask_subject)


@dataclass
class MerchantContext:
    """Loaded once per query and passed to every tool."""
    id: str
    name: str
    category: str
    locality: str
    locality_type: str
    volume_band: str
    days_of_history: int
    has_obligations: bool
    has_stock_feed: bool
    cohort_key: str = field(default="")

    def __post_init__(self):
        if not self.cohort_key:
            self.cohort_key = f"{self.category}|{self.locality_type}|{self.volume_band}"

    @property
    def is_cold_start(self) -> bool:
        return self.days_of_history < 14

    def to_dict(self) -> dict:
        return asdict(self)
