"""The agent loop. The model runs the conversation; Python supplies the facts.

This replaces intent classification. There is no fixed list of thirteen things
a merchant is allowed to mean, no fixed toolset per label, and no template that
the model is only permitted to reword. The model sees the conversation, decides
what to look up, looks at what came back, decides whether that was enough, and
then says something. Python's job is narrower and harder: every number it is
allowed to say has to have come from a tool.

What remains deterministic, in code rather than in the prompt:

  * tools are read-only, except one that records something the merchant said
    and one that builds an action proposal;
  * action parameters are built in Python from what the cohort actually did --
    the model may recommend, it may not set the numbers;
  * nothing executes without spoken approval;
  * the privacy floor and peer anonymity, enforced in the graph adapter;
  * stock tools are not even offered unless stock is on the table;
  * every claim-bearing figure is checked against the evidence before it is
    spoken, and a failure is repaired rather than replaced.
"""
from __future__ import annotations

import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import config  # noqa: E402
from backend.data import context as ctx  # noqa: E402
from backend.reasoning import conversation, providers, tools as toolspec, validator  # noqa: E402

SYSTEM = """You are Saathi, a business partner for a small Indian merchant who takes
payments through Paytm. You are speaking to them out loud while they work.

WHAT YOU CAN SEE
You have tools over this merchant's payment data: what came in, when, how often, how
it compares to their own past, and how it compares to similar merchants nearby (as
anonymous counts and rates, never names). You also see what actions were tried before
and what happened.

WHAT YOU CANNOT SEE
Payment data does not contain: what was actually sold, product mix, inventory, cost,
margin or profit, cash sales that bypassed Paytm, or why any customer behaved as they
did. You cannot infer these. If the merchant asks about one, say plainly in one clause
that you cannot see it, then either offer the nearest thing you CAN answer or ask them
for the one fact that would let you answer. Never guess, and never pad the answer with
something they did not ask about.

HOW TO WORK
Call tools to get facts. You may call several at once and you may call more after
seeing results. Do not call tools you do not need. When you have enough, call
final_answer. Every figure you say must have come back from a tool in this
conversation, be something the merchant told you, or be a number from their own
question -- if you need a sum, a difference or a percentage, use the calculate tool
rather than working it out yourself.

When the merchant states a fact you cannot otherwise see -- how much stock is left,
how many units sell in a day, a bill that is coming -- call remember_fact immediately,
then carry on and answer the question that is waiting. Never ask again for something
already recorded.

ASKING
You may ask ONE question per turn, and only when the answer would actually change what
you tell them. Never ask two things. Never ask for something you can look up. Never
ask a question just to seem thorough.

STYLE
Match the merchant's language: Roman-script Hinglish by default, Hindi or English if
that is what they used. Put the answer or the verdict in the FIRST sentence. Two to
four short spoken sentences. No lists, no bullet points, no markdown, no headings --
this is spoken aloud in a noisy shop. Round figures the way a person speaks them
(56,500 rather than 56,534.21, "takreeban" for an approximation); units are whole
numbers. Say where something came from, naturally: "aapke payments ke hisaab se",
"aapke jaise 6 dukaanon mein", "aapne bataya tha". On a follow-up, answer the NEW
thing -- do not repeat what you just said. Greetings and "what can you do" get a short
friendly reply with two or three example questions, not a menu."""


FINAL_ANSWER_TOOL = {
    "name": "final_answer",
    "description": "Say this to the merchant and end the turn. Call it exactly once, "
                   "when you have what you need or when you have decided to ask them "
                   "something.",
    "parameters": {
        "type": "object",
        "properties": {
            "hinglish": {"type": "string",
                         "description": "What Saathi says aloud, in the merchant's "
                                        "language. 2-4 short spoken sentences."},
            "english": {"type": "string",
                        "description": "The same thing in English, for the console."},
            "used_tools": {"type": "array", "items": {"type": "string"},
                           "description": "Tools whose figures you actually used."},
            "ask": {"type": "string",
                    "description": "The single clarifying question, if you are asking "
                                   "one. Omit otherwise."},
            "ask_kind": {"type": "string",
                         "enum": ["stock_estimate", "daily_units", "capacity",
                                  "upcoming_expense", "other"],
                         "description": "What kind of fact the question is after."},
            "ask_subject": {"type": "string",
                            "description": "What the question is about, e.g. 'cold drink'."},
            "recommends_action": {"type": "boolean",
                                  "description": "True if you are recommending the "
                                                 "merchant run an action you proposed."},
            "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
            "limitations": {"type": "array", "maxItems": 2, "items": {"type": "string"},
                            "description": "Material gaps, in plain words. Usually empty."},
        },
        "required": ["hinglish", "english", "confidence"],
        "additionalProperties": False,
    },
}


def _history_messages(history: list[dict]) -> list[dict]:
    """The thread so far, compactly. Tool results are summaries, not full evidence."""
    out = []
    for turn in history:
        out.append({"role": "user", "content": turn["question"]})
        looked_up = ""
        if turn.get("tools"):
            looked_up = ("\n[you looked up: "
                         + json.dumps(turn["tools"], ensure_ascii=False)[:700] + "]")
        pending = ""
        if turn.get("open_ask"):
            pending = f"\n[you asked them: {turn['open_ask']}]"
        out.append({"role": "assistant", "content": turn["answer"] + looked_up + pending})
    return out


def _context_note(runner, merchant_id: str) -> str:
    c = runner.context
    facts = ctx.live_for(merchant_id)
    lines = []
    if c:
        lines.append(f"Merchant: {c.name}, a {c.category.replace('_', ' ')} in "
                     f"{c.locality} ({c.locality_type.replace('_', ' ')}), "
                     f"{c.volume_band} volume, {c.days_of_history} days of history.")
        if c.is_cold_start:
            lines.append("This merchant has almost no history of their own, so their "
                         "own trend is not meaningful yet -- use the cohort baseline.")
        lines.append(f"Connected billing/stock feed: {'yes' if c.has_stock_feed else 'no'}. "
                     f"Bills visible: {'yes' if c.has_obligations else 'no'}.")
    if facts:
        stated = [f"{f['kind']}={f.get('value_num')} {f.get('unit') or ''} "
                  f"({f.get('subject')})".strip() for f in facts]
        lines.append("Already told to you by this merchant (do NOT ask again): "
                     + "; ".join(stated))
    return "\n".join(lines)


def run(merchant_id: str, question: str, runner, history: list[dict] | None = None,
        emit=None, deadline: float | None = None) -> dict:
    """One turn. Returns the answer plus everything /ops needs to render it."""
    emit = emit or (lambda *a, **k: None)
    started = time.time()
    deadline = deadline or (started + config.AGENT_BUDGET_SECONDS)

    runner.call("get_merchant_context", {})
    allow_stock = toolspec.stock_is_live(question, merchant_id)
    offered = toolspec.schemas(allow_stock=allow_stock) + [FINAL_ANSWER_TOOL]

    messages = [{"role": "system", "content": SYSTEM},
                {"role": "system", "content": _context_note(runner, merchant_id)}]
    messages += _history_messages(history or [])
    messages.append({"role": "user", "content": question})

    calls_made = 0
    rounds = 0
    provider_name = None
    final: dict | None = None

    while rounds < config.AGENT_MAX_ROUNDS and time.time() < deadline:
        rounds += 1
        remaining = max(2.0, deadline - time.time())
        out = providers.chat(messages, offered, timeout=remaining)
        provider_name = out.get("provider")
        calls = out.get("calls") or []

        if not calls:
            # No tool call and no final_answer: take the text as the answer.
            text = (out.get("text") or "").strip()
            if text:
                try:
                    final = providers.parse_json(text)
                except Exception:                     # noqa: BLE001
                    final = {"hinglish": text, "english": text, "confidence": "medium"}
                break
            messages.append({"role": "user",
                             "content": "Call final_answer now with what you have."})
            continue

        closing = next((c for c in calls if c["name"] == "final_answer"), None)
        if closing:
            final = closing["arguments"]
            break

        # ---- execute this round's tools, concurrently
        calls = [c for c in calls if c["name"] in toolspec.BY_NAME]
        calls = calls[:max(0, config.AGENT_MAX_CALLS - calls_made)]
        if not calls:
            messages.append({"role": "user",
                             "content": "That tool is not available. Call final_answer "
                                        "with what you have, or choose another tool."})
            continue

        blocked = [c for c in calls if c["name"] in toolspec.STOCK_TOOLS and not allow_stock]
        calls = [c for c in calls if c not in blocked]
        emit("agent_round", round=rounds,
             tools=[{"tool": c["name"], "arguments": c["arguments"]} for c in calls])

        results = []
        if calls:
            if len(calls) == 1:
                results = [runner.call(calls[0]["name"], calls[0]["arguments"])]
            else:
                with ThreadPoolExecutor(max_workers=min(4, len(calls))) as pool:
                    results = list(pool.map(
                        lambda c: runner.call(c["name"], c["arguments"]), calls))
        calls_made += len(calls)

        if out.get("raw_message"):
            messages.append(out["raw_message"])
        else:
            messages.append({"role": "assistant", "content": "",
                             "tool_calls": [{"name": c["name"],
                                             "arguments": c["arguments"]} for c in calls]})
        for call, ev in zip(calls, results):
            messages.append({
                "role": "tool", "tool_call_id": call.get("id") or call["name"],
                "name": call["name"],
                "content": json.dumps(ev.value, ensure_ascii=False, default=str)[:2400]})
        for call in blocked:
            messages.append({
                "role": "tool", "tool_call_id": call.get("id") or call["name"],
                "name": call["name"],
                "content": json.dumps({"available": False, "reason":
                                       "stock tools are only available when the merchant "
                                       "has mentioned stock or already stated a figure"})})

        if calls_made >= config.AGENT_MAX_CALLS:
            messages.append({"role": "user",
                             "content": "You have used your tool budget. Call "
                                        "final_answer with what you have."})

    if final is None:
        final = _force_final(messages, deadline)

    return _finish(final, runner, question, merchant_id, provider_name, rounds,
                   calls_made, messages, deadline, started, emit)


def _force_final(messages: list[dict], deadline: float) -> dict:
    """Out of rounds or out of time: ask once for the answer, with no tools."""
    messages = messages + [{
        "role": "user",
        "content": "Reply now with a JSON object only: "
                   '{"hinglish": "...", "english": "...", "confidence": "medium"}'}]
    try:
        out = providers.chat(messages, None, timeout=max(2.0, deadline - time.time()),
                             force_json=True)
        return providers.parse_json(out.get("text") or "")
    except Exception:                                 # noqa: BLE001
        return {"hinglish": "Abhi iska pakka jawab nahi de pa raha. Thoda baad mein "
                            "poochhiye.",
                "english": "I can't give a reliable answer to that right now.",
                "confidence": "low"}


def _repair(messages: list[dict], unbacked: list, deadline: float) -> dict | None:
    """Hand the bad figures back and ask for a corrected answer."""
    messages = messages + [{
        "role": "user",
        "content": ("These figures in your answer are not in the evidence: "
                    + validator.describe(unbacked)
                    + ". Either call a tool that produces them, or say the same thing "
                      "without those figures. Reply with a JSON object only: "
                      '{"hinglish": "...", "english": "...", "confidence": "..."}')}]
    try:
        out = providers.chat(messages, None,
                             timeout=max(2.0, deadline - time.time()), force_json=True)
        return providers.parse_json(out.get("text") or "")
    except Exception:                                 # noqa: BLE001
        return None


def _strip_claims(text: str, unbacked: list) -> str:
    """Last resort: drop the sentences carrying an unbacked figure, keep the rest."""
    bad = {str(u["text"]) for u in unbacked}
    kept = [s for s in __import__("re").split(r"(?<=[.!?])\s+", text or "")
            if not any(b in s for b in bad)]
    return " ".join(kept).strip()


def _finish(final: dict, runner, question: str, merchant_id: str, provider_name,
            rounds: int, calls_made: int, messages: list[dict], deadline: float,
            started: float, emit) -> dict:
    evidence = runner.evidence
    stated = ctx.live_for(merchant_id)

    hinglish = (final.get("hinglish") or "").strip()
    english = (final.get("english") or "").strip() or hinglish
    spoken = f"{hinglish}\n{english}"

    passed, unbacked = validator.validate(spoken, evidence, question, stated)
    repairs = 0
    while not passed and repairs < config.AGENT_REPAIR_ATTEMPTS and time.time() < deadline:
        repairs += 1
        emit("grounding_repair", attempt=repairs, unbacked=validator.describe(unbacked))
        fixed = _repair(messages, unbacked, deadline)
        if not fixed or not (fixed.get("hinglish") or "").strip():
            break
        hinglish = fixed["hinglish"].strip()
        english = (fixed.get("english") or "").strip() or hinglish
        spoken = f"{hinglish}\n{english}"
        passed, unbacked = validator.validate(spoken, evidence, question, stated)

    degraded = False
    if not passed:
        # Never substitute an unrelated answer. Keep what IS grounded and be
        # honest about the rest.
        hinglish = _strip_claims(hinglish, unbacked)
        english = _strip_claims(english, unbacked)
        tail_h = "Iska pakka hisaab abhi nahi de pa raha."
        tail_e = "I can't put a reliable figure on that part."
        hinglish = (hinglish + " " + tail_h).strip() if hinglish else tail_h
        english = (english + " " + tail_e).strip() if english else tail_e
        degraded = True

    # ---- the fields the model does not own
    proposal = None
    proposal_ev = runner.by_tool.get("propose_action")
    if proposal_ev is not None and proposal_ev.available:
        proposal = proposal_ev.value

    ask = None
    if not proposal:
        wanted = (final.get("ask") or "").strip()
        subject = final.get("ask_subject")
        kind = final.get("ask_kind") or "other"
        already = any(f["kind"] == kind and (not subject or f.get("subject") == subject)
                      for f in stated)
        if wanted and not already:
            ask = {"question": wanted, "kind": kind, "subject": subject}
        elif not wanted:
            pending = next((e for e in evidence if getattr(e, "ask", None)), None)
            if pending is not None:
                ask = {"question": None, "kind": pending.ask,
                       "subject": pending.ask_subject}

    used = [t for t in (final.get("used_tools") or []) if t in runner.by_tool]
    if not used:
        used = [e.tool for e in evidence if e.available]

    return {
        "hinglish": hinglish,
        "english": english,
        "confidence": final.get("confidence", "medium"),
        "limitations": (final.get("limitations") or [])[:2],
        "evidence_refs": used,
        "action_proposal": proposal,
        "ask": ask,
        "reasoner": f"agent:{provider_name}" if provider_name else "agent",
        "validator": {"passed": passed, "unbacked": unbacked, "repairs": repairs,
                      "degraded": degraded, "substituted": False},
        "agent": {"rounds": rounds, "tool_calls": calls_made,
                  "elapsed_ms": int((time.time() - started) * 1000),
                  "provider": provider_name},
    }
