"""One question in, one grounded answer out.

Every stage emits a websocket event, which is what the /ops pipeline column
renders. The validator sits between synthesis and speech: if the utterance
contains a figure the evidence does not back, the template answer is spoken
instead and the substitution is visible.
"""
from __future__ import annotations

import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from backend.data import repository as repo  # noqa: E402
from backend.models.events import event  # noqa: E402
from backend.reasoning import conversation, llm, planner, runner, toolsets, validator  # noqa: E402
from backend.reasoning.templates import TemplateReasoner  # noqa: E402


def ask(merchant_id: str, question: str, emit=None, params: dict | None = None,
        query_id: str | None = None, source: str = "text",
        conversation_id: str | None = None) -> dict:
    t0 = time.time()
    emit = emit or (lambda e: None)
    qid = query_id or "q_" + uuid.uuid4().hex[:6]

    emit(event("query_started", qid, merchant_id=merchant_id, text=question, source=source))

    ctx = repo.merchant_context(merchant_id)
    memory_key = f"{merchant_id}:{conversation_id}" if conversation_id else None
    history = conversation.recent(memory_key)
    active_planner = planner.get_planner()
    plan = active_planner.plan(question, ctx, history)
    intent = plan.intent
    ts = plan.toolset()
    emit(event("intent_detected", qid, intent=intent, method=plan.method,
               matched=plan.rationale, confidence=0.9 if plan.method == "gemini" else 0.8,
               normalised=plan.normalised, planner_params=plan.params,
               memory_turns=len(history), review_after_tools=plan.review_after_tools,
               toolset=ts["wave1"] + ts["wave2"] + ts.get("wave3", [])))
    emit(event("context_loaded", qid, merchant=ctx.to_dict()))

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

    run = runner.Runner(merchant_id, emit=tool_emit,
                        params={**plan.params, **(params or {})})
    for wave_no, wave in enumerate((ts["wave1"], ts["wave2"], ts.get("wave3", [])), 1):
        for tool in wave:
            emit(event("tool_started", qid, tool=tool, wave=wave_no))
    evidence = run.run_tools(plan.tools)
    useful_initial = [e for e in evidence
                      if e.tool != "get_merchant_context" and e.available]
    unavailable_initial = [e for e in evidence if not e.available]
    should_review = (plan.review_after_tools
                     and (len(useful_initial) < 2 or bool(unavailable_initial)))
    if should_review:
        additional_tools, review_rationale = active_planner.review(
            question, ctx, plan, evidence, history)
    else:
        additional_tools = []
        review_rationale = ("initial evidence sufficient; review skipped"
                            if plan.review_after_tools else "review not requested")
    if additional_tools:
        extra_ts = toolsets.for_tools(additional_tools)
        already = set(run.by_tool)
        for tool in (extra_ts["wave1"] + extra_ts["wave2"] + extra_ts["wave3"]):
            if tool not in already:
                emit(event("tool_started", qid, tool=tool, wave=4,
                           planned_by="gemini-review"))
        evidence = run.run_tools(additional_tools)
    emit(event("evidence_complete", qid, count=len(evidence),
               unavailable=[e.tool for e in evidence if not e.available],
               total_ms=int((time.time() - t0) * 1000)))

    reasoner = llm.get_reasoner()
    emit(event("reasoning_started", qid, reasoner=reasoner.name,
               evidence_count=len(evidence)))
    answer = reasoner.synthesize(intent, question, evidence)

    merchant_facing = answer.get("hinglish", "") + "\n" + answer.get("english", "")
    passed, unbacked = validator.validate(merchant_facing, evidence)
    substituted = False
    if not passed:
        # discard the utterance; speak the deterministic template answer instead
        answer = TemplateReasoner().synthesize(intent, question, evidence)
        answer["reasoner"] = "template (substituted after grounding failure)"
        substituted = True
        merchant_facing = answer.get("hinglish", "") + "\n" + answer.get("english", "")
        passed, unbacked = validator.validate(merchant_facing, evidence)
    answer.setdefault("confidence", "medium" if any(not e.available for e in evidence)
                      else "high")
    answer.setdefault("limitations", [])
    answer.setdefault("evidence_refs", [e.tool for e in evidence if e.available])
    emit(event("validation_result", qid, passed=passed, unbacked=unbacked,
               substituted=substituted))

    elapsed = int((time.time() - t0) * 1000)
    emit(event("response_ready", qid, hinglish=answer.get("hinglish"),
               english=answer.get("english"), intent=intent,
               reasoner=answer.get("reasoner"), confidence=answer.get("confidence"),
               limitations=answer.get("limitations"),
               evidence_refs=answer.get("evidence_refs"), elapsed_ms=elapsed))

    conversation.remember(memory_key, question, intent,
                          answer.get("hinglish", ""),
                          [e.tool for e in evidence if e.available])

    return {
        "query_id": qid,
        "merchant_id": merchant_id,
        "question": question,
        "intent": intent,
        "answer": {**answer,
                   "validator": {"passed": passed, "unbacked": unbacked,
                                 "substituted": substituted}},
        "evidence": [e.to_dict() for e in evidence],
        "action_proposal": answer.get("action_proposal"),
        "ask": answer.get("ask"),
        "planning": {"method": plan.method, "rationale": plan.rationale,
                     "initial_tools": plan.tools, "additional_tools": additional_tools,
                     "review_rationale": review_rationale,
                     "memory_turns": len(history)},
        "elapsed_ms": elapsed,
    }
