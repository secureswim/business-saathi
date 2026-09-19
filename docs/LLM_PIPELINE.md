# NVIDIA NIM and Gemini planning pipeline

## What changed

With `SAATHI_REAL_LLM=1`, the selected LLM provider participates in three bounded stages.
This demo uses NVIDIA NIM first and Gemini as the fallback:

1. **Evidence planning** — interpret the merchant's request, choose the minimum
   useful read-only tools, and extract bounded parameters such as forecast days.
2. **Evidence review** — for ambiguous or multi-part questions, inspect the first
   results and request at most one additional tool wave.
3. **Grounded explanation** — turn the returned evidence into short Hinglish and
   English responses suitable for the Soundbox.

The planner receives up to four compact prior turns from the current browser
session, allowing follow-ups such as "what about weekends?" without treating
conversation memory as business evidence.

The deterministic router and response templates remain the automatic fallback
when both providers are disabled, time out, or return invalid structured output.

## Request flow

```text
merchant question
  -> provider QueryPlan JSON
  -> Python validates tools, parameters and dependencies
  -> analytics / SQLite / Cognee return Evidence envelopes
  -> optional provider evidence review -> one additional bounded tool wave
  -> provider answer JSON with evidence references
  -> Python numeric grounding validator
  -> deterministic template substitution if validation fails
  -> Soundbox speaks
```

The LLM never receives SQL access and never executes a selected tool. The runner
accepts only names from the Python allowlist. It automatically inserts required
dependencies such as `get_peer_cohort` before a peer comparison.

The final structured answer includes `confidence`, up to two material
`limitations`, and `evidence_refs`. The operator console displays all three.

## Data boundary

The default plan is based on payment information: amount, time, status,
historical patterns, merchant profile, previous actions and privacy-safe cohort
patterns. Inventory, product mix, profit, margins, cash sales and customer intent
are unavailable by default.

`get_optional_stock_context` survives as an optional integration capability, but
Python removes it from a model-created plan unless the merchant explicitly says
stock or inventory. Ordinary planning questions therefore do not ask for stock.

## Action boundary

The LLM may select evidence that supports a recommendation. It cannot create an
executable campaign payload. `propose_action` builds parameters in Python from a
privacy-safe peer playbook; existing guardrails validate the result, the merchant
approves it, and only then can n8n execute the workflow.

## Configuration

Put these values in `.env`:

```dotenv
SAATHI_REAL_LLM=1
GEMINI_API_KEY=your-key
GEMINI_MODEL=gemini-3.6-flash
SAATHI_LLM_PROVIDER=nvidia-first
NVIDIA_API_KEY=your-nvidia-key
NVIDIA_MODEL=nvidia/nemotron-3-super-120b-a12b
```

Then verify the credential and structured-output endpoint:

```powershell
.\.venv\Scripts\python.exe scripts\check_integrations.py gemini
.\.venv\Scripts\python.exe scripts\check_integrations.py nvidia
```

If the check fails, the application continues through the deterministic path.
With `SAATHI_LLM_PROVIDER=nvidia-first`, NIM is primary and Gemini is the
fallback. Transient overloads receive a short cooldown; quota and schema errors
receive a five-minute cooldown. Use `auto` for Gemini-first fallback order, or
set the value to `gemini` or `nvidia` to force one provider.
