"""The speaking layer, separable from the reasoning layer.

Two different jobs are being asked of a language model here, and they reward
different models. Deciding which tools to call and when the evidence is
sufficient rewards reliable structured output. Saying it in natural Hinglish to
a shopkeeper rewards having heard a lot of Hinglish. Those are not the same
model, and on this stack they no longer have to be.

By default (`SAATHI_SPEECH_PROVIDER=same`) the reasoning model also speaks, and
this module does nothing -- one call, one dependency, nothing extra to fail on
venue wifi. Set it to another configured provider and the reasoning model still
decides everything, but the spoken line is written by the voice model from the
same evidence, under the same grounding gate.

The voice model never sees tools, never chooses what to look up, and cannot
introduce a figure: whatever it writes goes back through the validator.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import config  # noqa: E402
from backend.reasoning import providers  # noqa: E402

SYSTEM = """You are the voice of Saathi, speaking to a small Indian shopkeeper while
they work behind their counter.

You are given the facts that were looked up and a plain English answer that is already
correct. Your only job is to say that same answer the way a trusted person would say it
out loud, in natural Roman-script Hinglish -- the everyday mix a shopkeeper in Delhi
actually speaks, not textbook Hindi and not translated English.

Rules:
- Say the same thing. Do not add a fact, a number, a recommendation or a question that
  is not already in the English answer.
- Every figure must appear exactly as it does in the evidence or the English answer.
  You may round the way people speak (56,500 rather than 56,534) and say "takreeban".
- 2 to 4 short spoken sentences. No lists, no markdown. It is heard, not read.
- Keep English words where a shopkeeper would use English words: sale, offer, stock,
  payment, customer, discount. Do not translate them into formal Hindi.
- Match their register. Warm and direct, never salesy, never lecturing.

Reply with a JSON object only: {"hinglish": "..."}"""


def enabled() -> bool:
    name = providers.speech_provider()
    return bool(name) and name in providers.order()


def voice(english: str, evidence, question: str, timeout: float = 5.0) -> str | None:
    """Re-voice a finished answer. Returns None if the split layer is off or fails."""
    if not enabled() or not (english or "").strip():
        return None
    facts = [{"tool": e.tool, "value": e.value}
             for e in evidence if getattr(e, "available", True)][:10]
    payload = {"merchant_said": question, "answer_in_english": english,
               "evidence": facts}
    try:
        out = providers.chat(
            [{"role": "system", "content": SYSTEM},
             {"role": "user", "content": json.dumps(payload, ensure_ascii=False,
                                                    default=str)[:4000]}],
            None, timeout=timeout, force_json=True,
            provider=providers.speech_provider())
        spoken = providers.parse_json(out.get("text") or "").get("hinglish", "")
        return spoken.strip() or None
    except Exception:                                 # noqa: BLE001
        return None


def model_label() -> str:
    if not enabled():
        return "same"
    return config.SPEECH_MODEL or providers.speech_provider() or "same"
