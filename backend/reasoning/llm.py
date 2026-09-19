"""Gemini synthesis over evidence, with deterministic template fallback.

Gemini explains evidence selected by the planner. It cannot add an executable
proposal: proposals and follow-up data contracts are copied from Python's
template reasoner after synthesis, then the numeric grounding gate runs.
"""
from __future__ import annotations

import json
import sys
from abc import ABC, abstractmethod
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import config  # noqa: E402
from backend.reasoning.planner import _llm_json  # noqa: E402
from backend.reasoning.templates import TemplateReasoner  # noqa: E402


SYSTEM_PROMPT = """You are Business Saathi, a payment-data business partner for a small
Indian merchant. Explain only the supplied evidence.

Rules:
1. State no number, percentage, count, rupee figure or peer claim absent from evidence.
2. Never name another merchant. Peers are anonymous counts and aggregate patterns.
3. Match the merchant's language. Hinglish should be natural Roman-script Hinglish.
4. Use two to four short sentences suitable for speech in a busy shop.
5. Attribute own_data as the merchant's payment data and graph data as similar merchants.
6. Omit unavailable evidence. Do not speculate about the missing data.
7. Payment data does not reveal inventory, product mix, profit, cash sales, margins or why
   customers behaved a certain way. Say the limitation plainly when relevant.
8. You may recommend a direction, but never invent action parameters or claim execution.
"""

ANSWER_SCHEMA = {
    "type": "object",
    "properties": {
        "hinglish": {"type": "string"},
        "english": {"type": "string"},
        "evidence_refs": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Tool names actually used in the answer.",
        },
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "limitations": {
            "type": "array", "maxItems": 2,
            "items": {"type": "string"},
            "description": "Material data limitations, or an empty list.",
        },
    },
    "required": ["hinglish", "english", "evidence_refs", "confidence", "limitations"],
    "additionalProperties": False,
}


class Reasoner(ABC):
    name = "abstract"

    @abstractmethod
    def synthesize(self, intent: str, question: str, evidence: list) -> dict: ...


class GeminiReasoner(Reasoner):
    """Grounded Gemini response with template fallback on any API/schema error."""
    name = "gemini"

    def __init__(self):
        self._fallback = TemplateReasoner()

    def synthesize(self, intent: str, question: str, evidence: list) -> dict:
        base = self._fallback.synthesize(intent, question, evidence)
        if not config.llm_ready():
            base["reasoner"] = "template (llm not configured)"
            return base
        try:
            payload = {
                "question": question,
                "intent": intent,
                # `basis` contains provenance for /ops and can include peer IDs.
                # Synthesis receives only the already privacy-filtered `value`.
                "evidence": [{
                    "tool": e.tool if hasattr(e, "tool") else e.get("tool"),
                    "value": e.value if hasattr(e, "value") else e.get("value", {}),
                    "source": e.source if hasattr(e, "source") else e.get("source"),
                    "tier": e.tier if hasattr(e, "tier") else e.get("tier"),
                    "available": e.available if hasattr(e, "available") else e.get("available", True),
                } for e in evidence],
            }
            out = _llm_json(SYSTEM_PROMPT,
                            json.dumps(payload, ensure_ascii=False, default=str),
                            ANSWER_SCHEMA, max_tokens=700, timeout=8.0)
            provider = out.pop("_provider", "llm")
            if not isinstance(out.get("hinglish"), str) or not out["hinglish"].strip():
                raise ValueError("missing Hinglish answer")
            if not isinstance(out.get("english"), str) or not out["english"].strip():
                raise ValueError("missing English answer")
            actual_tools = {e.tool if hasattr(e, "tool") else e.get("tool") for e in evidence}
            out["evidence_refs"] = [name for name in out.get("evidence_refs", [])
                                    if name in actual_tools]

            # These fields control application behaviour, so the model never owns them.
            out["action_proposal"] = base.get("action_proposal")
            if base.get("ask"):
                out["ask"] = base["ask"]
            out["reasoner"] = provider
            return out
        except Exception as exc:  # noqa: BLE001
            base["reasoner"] = f"template (llm unavailable: {type(exc).__name__})"
            return base


# Backward-compatible import used by the adapter-contract tests.
LLMReasoner = GeminiReasoner


def get_reasoner() -> Reasoner:
    if config.USE_REAL_LLM and config.llm_ready():
        return GeminiReasoner()
    return TemplateReasoner()
