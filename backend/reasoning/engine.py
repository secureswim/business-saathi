"""One turn in, one grounded answer out.

Two paths, and the second is a genuine fallback rather than the thing that
usually happens. When a model provider is reachable, the agent runs the turn:
it decides what to look up, looks again if it needs to, and says something.
When no provider is reachable, the deterministic router answers instead, and
says so -- the answer is labelled `offline` here and on /ops, because a simpler
answer that admits what it is beats a confident one that is wrong.

Every stage emits a websocket event, which is what the /ops pipeline column
renders.
"""
from __future__ import annotations

import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import config  # noqa: E402
from backend import health  # noqa: E402
from backend.data import repository as repo  # noqa: E402
from backend.models.events import event  # noqa: E402
from backend.reasoning import (agent, conversation, providers, router,  # noqa: E402
                               runner, speech, toolsets, validator)
from backend.reasoning.templates import TemplateReasoner  # noqa: E402


def ask(merchant_id: str, question: str, emit=None, params: dict | None = None,
        query_id: str | None = None, source: str = "text",
        conversation_id: str | None = None) -> dict:
    t0 = time.time()
    emit = emit or (lambda e: None)
    qid = query_id or "q_" + uuid.uuid4().hex[:6]

    emit(event("query_started", qid, merchant_id=merchant_id, text=question,
               source=source, conversation_id=conversation_id))

    ctx = repo.merchant_context(merchant_id)
    emit(event("context_loaded", qid, merchant=ctx.to_dict()))
    history = conversation.recent(conversation_id)

    def tool_emit(ev):
        payload = ev.to_dict()
        if ev.tool in run.ops_values:
            payload["ops_value"] = run.ops_values[ev.tool]
        if ev.source == "graph" and ev.available:
            emit(event("graph_retrieval", qid, store=ev.basis.get("store", "sqlite"),
                       tool=ev.tool, hits=ev.basis.get("experiences_matched",
                                                       ev.basis.get("peer_count", 0)),
                       ms=ev.basis.get("retrieval_ms", 0)))
        emit(event("tool_result", qid, tool=ev.tool, evidence=payload))

    run = runner.Runner(merchant_id, emit=tool_emit, params=dict(params or {}))

    if providers.available():
        answer = _agent_turn(merchant_id, question, run, history, qid, emit)
    else:
        answer = _offline_turn(merchant_id, question, run, qid, emit)

    evidence = run.evidence
    _note_what_served(answer, evidence)
    elapsed = int((time.time() - t0) * 1000)
    emit(event("response_ready", qid, hinglish=answer.get("hinglish"),
               english=answer.get("english"), reasoner=answer.get("reasoner"),
               confidence=answer.get("confidence"),
               limitations=answer.get("limitations"),
               evidence_refs=answer.get("evidence_refs"),
               ask=(answer.get("ask") or {}).get("question"),
               elapsed_ms=elapsed))

    conversation.remember(conversation_id, merchant_id, question,
                          answer.get("hinglish", ""), evidence,
                          open_ask=(answer.get("ask") or {}).get("question"))

    return {
        "query_id": qid,
        "conversation_id": conversation_id,
        "merchant_id": merchant_id,
        "question": question,
        # kept for the /ops header and older callers; the agent has no fixed
        # intent, so this is a description of the turn, not a routing decision
        "intent": answer.get("intent", "agent"),
        "answer": answer,
        "evidence": [e.to_dict() for e in evidence],
        "action_proposal": answer.get("action_proposal"),
        "ask": answer.get("ask"),
        "planning": answer.get("agent", {"mode": answer.get("reasoner")}),
        "elapsed_ms": elapsed,
    }


def _agent_turn(merchant_id, question, run, history, qid, emit) -> dict:
    def agent_emit(kind, **payload):
        emit(event(kind, qid, **payload))

    emit(event("reasoning_started", qid, reasoner="agent",
               provider=config.llm_provider_label(),
               speech_model=speech.model_label(), memory_turns=len(history)))
    try:
        answer = agent.run(merchant_id, question, run, history=history, emit=agent_emit)
    except providers.ProviderError as exc:
        emit(event("provider_failed", qid, error=str(exc)[:200]))
        return _offline_turn(merchant_id, question, run, qid, emit)

    # optional split voice: reasoning model decided, voice model speaks
    if speech.enabled():
        spoken = speech.voice(answer.get("english", ""), run.evidence, question)
        if spoken:
            ok, unbacked = validator.validate(spoken, run.evidence, question)
            if ok:
                answer["hinglish"] = spoken
                answer["reasoner"] += f"+voice:{speech.model_label()}"
            else:
                emit(event("voice_rejected", qid,
                           unbacked=validator.describe(unbacked)))

    emit(event("validation_result", qid, passed=answer["validator"]["passed"],
               unbacked=[u.get("text") for u in answer["validator"]["unbacked"]],
               repairs=answer["validator"]["repairs"],
               degraded=answer["validator"]["degraded"], substituted=False))
    return answer


def _offline_turn(merchant_id, question, run, qid, emit) -> dict:
    """No provider reachable. Deterministic, simpler, and labelled as such."""
    plan = router.route(question, repo.days_of_history(merchant_id))
    emit(event("intent_detected", qid, intent=plan.intent, method="offline",
               matched=plan.matched, normalised=plan.normalised,
               planner_params=plan.params, toolset=toolsets.all_tools(plan.intent)))
    run.params.update(plan.params)
    evidence = run.run_tools(toolsets.all_tools(plan.intent))
    emit(event("evidence_complete", qid, count=len(evidence),
               unavailable=[e.tool for e in evidence if not e.available]))

    answer = TemplateReasoner().synthesize(plan.intent, question, evidence)
    spoken = f"{answer.get('hinglish', '')}\n{answer.get('english', '')}"
    passed, unbacked = validator.validate(spoken, evidence, question)
    emit(event("validation_result", qid, passed=passed,
               unbacked=[u.get("text") for u in unbacked], repairs=0,
               degraded=False, substituted=False))

    ask = None
    pending = runner.pending_ask(evidence)
    if answer.get("ask"):
        ask = {"question": answer["ask"].get("question") if isinstance(answer["ask"], dict)
               else answer["ask"],
               "kind": (answer["ask"].get("kind") if isinstance(answer["ask"], dict)
                        else "other"),
               "subject": (answer["ask"].get("subject") if isinstance(answer["ask"], dict)
                           else None)}
    elif pending is not None:
        ask = {"question": None, "kind": pending.ask, "subject": pending.ask_subject}

    return {
        "hinglish": answer.get("hinglish", ""),
        "english": answer.get("english", ""),
        "confidence": answer.get("confidence", "medium"),
        "limitations": answer.get("limitations", []),
        "evidence_refs": [e.tool for e in evidence if e.available],
        "action_proposal": answer.get("action_proposal"),
        "ask": ask,
        "intent": plan.intent,
        "reasoner": "offline",
        "validator": {"passed": passed, "unbacked": unbacked, "repairs": 0,
                      "degraded": False, "substituted": False},
        "agent": {"mode": "offline", "intent": plan.intent, "matched": plan.matched},
    }


def _note_what_served(answer: dict, evidence) -> None:
    """Record the implementation that actually produced this answer.

    The graph line is the one that matters. `basis.store` distinguishes a real
    Cognee retrieval from the ledger answering while retrieval was still in
    flight, and only the first of those earns a green chip on /ops.
    """
    stores = {e.basis.get("store") for e in evidence
              if getattr(e, "source", None) == "graph" and e.available
              and isinstance(e.basis, dict) and e.basis.get("store")}
    if stores:
        health.note_serving("graph", "cognee" if any(s == "cognee" for s in stores)
                            else sorted(stores)[0])
    health.note_serving("reasoner", answer.get("reasoner", "unknown"))
