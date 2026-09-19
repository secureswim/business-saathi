# Design

The full product and technical design lives in the build document shared with
the team. This file is the short form: the decisions a developer needs in front
of them while writing code.

## The loop

    talk -> understand -> analyse -> advise -> approve -> act -> measure -> learn
                ^                                                            |
                +------------------------------------------------------------+

The edge from `learn` back into `understand` is the only cycle in the system and
the whole technical argument.

## Nine principles

1. **The knowledge hierarchy.** Automatic (payments) → integration (POS,
   accounting) → ask the merchant → never fabricated.
2. **Never fabricate.** Enforced by `reasoning/validator.py`, not by prompt
   wording. A bad figure is repaired, never swapped for an unrelated answer.
3. **Ask only when it changes the answer.** One question per exchange, never a
   form, and never for something already stated.
4. **The model decides what to look up; Python owns every number.** The agent
   chooses tools and when it has enough. It cannot do arithmetic, touch SQL,
   or set an action's parameters.
5. **The merchant sees no interface.** One object, four states, spoken approval.
6. **Nothing acts without approval.** No autonomous actions exist.
7. **Peers are counts, never names.** `graph/privacy.py`, floor of five.
8. **Every animation maps to a real event.** A faked animation is a claim the
   system did something it did not do.
9. **Simulated components are labelled** — in code, on `/ops`, and to judges.

## Layers

| Layer | Package | Never does |
| --- | --- | --- |
| Interaction | `voice/` | business logic |
| Reasoning | `reasoning/` | arithmetic, SQL, action parameters |
| Analytics | `analytics/` | call the LLM or the graph |
| Knowledge | `graph/` | return individual figures |
| Action | `actions/` | execute without approval |
| Data | `data/` | interpret anything |

`data/repository.py` is the only module that writes SQL.

## Evidence

```python
{"tool": str, "value": dict, "basis": dict,
 "source": "own_data|graph|finance|merchant_input|integration",
 "tier": "A|B|C", "available": bool, "ask": str|None}
```

`value` is the only part an answer may quote. `basis` is provenance.
`available: False` is a first-class result: the synthesis layer omits that
dimension rather than hedging, and `/ops` renders a greyed card.

## The agent, not an intent classifier

There is no intent classification on the live path. The model receives the
conversation, the merchant's profile, the facts they have already stated, and a
JSON Schema per tool. It calls tools, sees the results, decides whether to look
further, and then calls `final_answer`. Up to 4 rounds, 10 calls, ~9 seconds;
independent tools in a round run concurrently; prerequisites Python knows about
(a cohort before a peer playbook) are inserted rather than left to the model.

Intents survive in `reasoning/router.py` and `reasoning/toolsets.py` for ONE
purpose: answering when no model provider is reachable. Those answers are
labelled `offline` in the response and on /ops. See `docs/LLM_PIPELINE.md`.

The reasoning model and the speaking model are separately configurable, so an
Indic-tuned model can take the Hinglish voice without touching the part where
structured-output reliability matters. The voice model gets no tools and is held
to the same grounding gate.

## The tools

Twenty-three, defined with typed JSON Schemas in `reasoning/tools.py` and
executed by `reasoning/runner.py`. Beyond the original evidence tools, the agent
era added the ones the old pipeline had no way to express: `sales_lookup`,
`compare_periods`, `afford_check`, `stock_cover`, `calculate` and
`remember_fact`. All read-only except `remember_fact` and `propose_action`.

`calculate` exists so that a figure the model needed to derive is still a figure
Python produced — it evaluates a plain arithmetic expression through an AST
walker that permits numbers and `+ - * / ( )` and nothing else.

## Data tiers

| Tier | Meaning | Rule |
| --- | --- | --- |
| A | automatic, from payments | core capabilities may depend on this only |
| B | integration | never a core dependency; absence must degrade gracefully |
| C | conversational | usable for the current decision; expires |
| D | unavailable | never a dependency; never estimated |

## Build order

1. schema + generator + `verify_claims`
2. `recompute_patterns` + situation detection
3. analytics
4. graph + similarity + privacy
5. API + router + tools + templates + validator + **reset**
6. `/ops` + websocket
7. action machine + guardrails + local orchestrator
8. Cognee ingest + adapter + swap test
9. n8n workflows
10. conversational context
11. merchant voice route
12. Sarvam, LLM, polish

Do not start voice until step 7 works in text.

## Conversation

Turns persist per `conversation_id` in SQLite: question, answer, a compact
summary of each tool result, and any open question. Only headline values are
kept — conversation memory must never become a second source of business truth,
and the ledger is re-read every turn.

Facts the merchant volunteers go through `remember_fact` into `merchant_inputs`
with a TTL enforced in the query, and are surfaced to the model as *already told
to you, do not ask again*.

## The four tests that matter

- `tests/test_grounding.py` — a fabricated figure is caught; a duration or a
  clock time is not mistaken for one; a figure from the merchant's own question
  counts as grounded.
- `tests/test_privacy.py` — a cohort of four returns nothing, and no peer
  identifier appears in anything sent to a model provider.
- `tests/test_learning.py` — action → outcome → write-back → the same question
  returns different evidence, and a failure lowers the rate.
- `tests/test_agent_conversations.py` — the multi-turn conversations the old
  architecture could not hold, with the model scripted and everything else real.

## The generator owes the ledger an explanation

Actions are planned first, their effect is written into `txn_hourly` as the
transactions are generated, and each outcome is then MEASURED back out of those
rows. Previously the outcome figures were invented alongside the ledger and
agreed with it about half the time. `scripts/generate.py` now fails the build if
the demo merchant's decline does not survive into the rows, or if either demo
merchant's cohort falls below the privacy floor — both were silent, and both
broke the demo in ways that looked like software bugs.
