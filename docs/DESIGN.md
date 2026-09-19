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
   wording.
3. **Ask only when it changes the answer.** One question per exchange, never a
   form.
4. **LLM planning, deterministic maths.** Gemini selects evidence tools and explains the
   result; every figure and executable action still comes from Python.
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
| Reasoning | `reasoning/` | arithmetic, SQL |
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

## Planning and fallback intents

`cold_start`, `action_status`, `action_request`, `risk_check`, `what_if`,
`money_check`, `planning`, `demand_forecast`, `peer_insight`, `time_pattern`,
`anomaly_check`, `sales_diagnosis`, `business_health`, plus `unknown`.

When Gemini is enabled it returns a structured intent, evidence-tool selection,
and bounded parameters. Python validates the selection, inserts dependencies,
and runs it in up to three waves. The mappings in `reasoning/toolsets.py` remain
the offline and API-failure fallback. `cold_start` always uses the deterministic,
cohort-safe path.

## Sixteen tools

Context, health, trend, time patterns, recent situations, peer cohort,
peer-relative anomaly, peer playbook, failed plays, local pattern, cohort
seasonality, cohort profile, demand forecast, money position, optional stock
context, action history, propose action. All in `reasoning/runner.py`.

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

## The three tests that matter

- `tests/test_grounding.py` — empty evidence must produce an admission, and a
  fabricated figure must be caught and substituted.
- `tests/test_privacy.py` — a cohort of four returns nothing, and no peer
  identifier appears in any spoken answer.
- `tests/test_learning.py` — action → outcome → write-back → the same question
  returns different evidence, and a failure lowers the rate.
