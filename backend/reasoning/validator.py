"""The grounding gate: the mechanism that makes "never fabricate" a property
of the system rather than a hope.

It sits between synthesis and speech. If the utterance contains a figure that
is not backed by the evidence, the utterance is DISCARDED and the deterministic
template answer is spoken instead. The substitution is logged, emitted as an
event, and shown on /ops as a red badge.
"""
from __future__ import annotations

import re
from typing import Any

NUM_RE = re.compile(r"-?\d[\d,]*\.?\d*")
INTERNAL_MERCHANT_ID_RE = re.compile(r"\bM\d{3,}\b", re.I)
# figures that are not claims about the business
IGNORE = {0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0, 11.0, 12.0,
          18.0, 19.0, 20.0, 21.0, 22.0, 24.0}


def extract_numbers(text: str) -> list[float]:
    out = []
    for m in NUM_RE.findall(text or ""):
        try:
            out.append(float(m.replace(",", "")))
        except ValueError:
            continue
    return out


def _walk(obj: Any, sink: list[float]) -> None:
    if isinstance(obj, bool):
        return
    if isinstance(obj, (int, float)):
        sink.append(float(obj))
    elif isinstance(obj, dict):
        for v in obj.values():
            _walk(v, sink)
    elif isinstance(obj, list):
        for v in obj:
            _walk(v, sink)
    elif isinstance(obj, str):
        for n in extract_numbers(obj):
            sink.append(n)


def numeric_closure(evidence) -> set[float]:
    """Every number inside every evidence `value`, plus tolerant spoken forms."""
    raw: list[float] = []
    for ev in evidence:
        value = ev.value if hasattr(ev, "value") else ev.get("value", {})
        _walk(value, raw)

    closure: set[float] = set()
    for n in raw:
        closure.add(n)
        closure.add(abs(n))
        closure.add(round(n))
        closure.add(round(abs(n)))
        closure.add(round(n, 1))
        # spoken rounding: 4389 -> 4400, 4,389 -> 4,390
        if abs(n) >= 100:
            closure.add(round(n / 100) * 100)
            closure.add(round(n / 10) * 10)
        if abs(n) >= 1000:
            closure.add(round(n / 1000) * 1000)
    return closure


def matches_any(n: float, allowed: set[float], tol: float = 0.5) -> bool:
    if n in IGNORE:
        return True
    for a in allowed:
        if abs(a - n) <= tol:
            return True
        # percentage stated without its sign, or a value rounded for speech
        if a != 0 and abs(abs(a) - abs(n)) <= max(tol, abs(a) * 0.02):
            return True
    return False


def validate(utterance: str, evidence) -> tuple[bool, list[float | str]]:
    allowed = numeric_closure(evidence)
    spoken = extract_numbers(utterance)
    unbacked: list[float | str] = [n for n in spoken if not matches_any(n, allowed)]
    # IDs live in ops provenance but are never merchant-facing evidence. This
    # catches a model copying one even if its numeric suffix happens to be an
    # otherwise ignorable small number.
    unbacked.extend(dict.fromkeys(INTERNAL_MERCHANT_ID_RE.findall(utterance or "")))
    return (not unbacked), unbacked
