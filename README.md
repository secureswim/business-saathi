# Business Saathi

**A voice-first AI business partner for small merchants.** It turns the payment
signals a merchant already generates into plain advice, compares that merchant
against anonymised patterns from similar businesses, asks for context only when
the missing fact would change the answer, executes approved actions, measures
what happened, and writes the result back into a shared business-experience
graph so the next merchant gets a better answer.

Built for the Paytm Build for India AI Hackathon — Track 1, Merchant Growth AI.
Team 505 · Aditi Kumar · Siddharth Kumar.

---

## Run it

Three commands. No API keys, no network, no Docker.

```bash
pip install -r requirements.txt
python scripts/generate.py && python scripts/recompute_patterns.py
./run.sh                      # Windows: run.bat
```

| URL | Who opens it |
| --- | --- |
| <http://127.0.0.1:8000/> | The merchant. Interactive 3D Soundbox, voice conversation and payment demo. |
| <http://127.0.0.1:8000/ops> | Us and the judges. The whole pipeline, visible. |

Run the two on separate screens: the merchant route on a phone or second
window facing the room, `/ops` on the projector.

## Host the demo on Render

The repository includes a [Render Blueprint](render.yaml) for one free Python web
service. Open [Deploy to Render](https://render.com/deploy?repo=https://github.com/secureswim/business-saathi),
sign in, and create the Blueprint from this repository. The service generates
its own SQLite seed on startup, listens on Render's `PORT`, and serves both `/`
and `/ops` at its `onrender.com` address. In the service's **Environment** page,
copy the generated `SAATHI_SITE_PASSWORD`; a browser asks for it once (any
username works). Share the address and password with demo viewers.

The hosted service starts with offline adapters. To enable real reasoning, add
`SAATHI_REAL_LLM=1` and `NVIDIA_API_KEY` or `GEMINI_API_KEY` in Render's
Environment page. Never commit `.env` or put keys in the Blueprint. The other
adapters similarly require their own credentials and flags; see `.env.example`.
For n8n callbacks, set `PUBLIC_API_URL` to the hosted URL and configure n8n
with the generated `SAATHI_INTERNAL_SECRET` as its `X-Saathi-Secret` header.

Render's free service sleeps when idle and has ephemeral storage. A wake or
redeploy regenerates the synthetic database, resetting demo interactions. Use
a paid service with a persistent disk (or migrate storage) if changes must last.

The Soundbox can be dragged to rotate. Its top buttons control volume, replay,
and Saathi voice input. The payment demonstration is explicitly simulated;
business questions use the existing backend. See [the interaction notes](docs/SOUNDBOX.md).

Check everything before a rehearsal:

```bash
python scripts/verify_claims.py    # recomputes every demo number from the DB
python -m pytest -q                # active test suite (archive excluded by pytest.ini)
python scripts/full_flow_check.py  # every intent + memory + n8n + alerts + reset
python scripts/demo_check.py       # end-to-end gate; API must be running
```

## Try it

Ask these on `/ops` (type box, bottom right) or speak them on `/`:

- *"Bhai iss hafte sales kyun kam hain?"* — diagnosis against your own baseline **and** the cohort
- *"Mere jaise shops mein kya chal raha hai?"* — peer intelligence, counts only, never a name
- *"Agle hafte ke liye kya prepare karun?"* — payment forecast and strongest trading windows
- *"30% discount doon?"* — the failure evidence, and a smaller number recommended instead
- *"Offer bana de"* → Approve → Fast-forward — watch the cohort counter change
- Switch to `M999` (opened today, zero history) and ask anything — cold start
- Press **Trigger alert scan** — Saathi speaks without being asked

## What is real, synthetic and simulated

Say this to judges before being asked.

| Real | Synthetic | Simulated |
| --- | --- | --- |
| All analytics computation | 160 merchants, 12 months of transactions | Campaign execution (`CampaignAPI` has no real implementation) |
| Similarity scoring and cohort selection | Seeded behavioural patterns | The passage of three days |
| Graph retrieval and write-back | Historical actions and outcomes | Outcome uplift, sampled from the cohort's observed distribution |
| Evidence chain and grounding validation | Optional obligation and stock rows | — |
| n8n workflow execution, voice pipeline | — | — |

Every simulated row carries `simulated = 1` and every simulated step is labelled
on `/ops`. **The intelligence pipeline is real. The data and the external
execution are simulated, and the architecture is built so those adapters can be
replaced by real Paytm streams and services.**

## Architecture

```
voice/      speech in and out                    Sarvam | browser
reasoning/  NVIDIA NIM / Gemini planning, tools, synthesis, grounding validation
analytics/  deterministic computation over the ledger   (no LLM, no graph)
graph/      similarity, cohorts, collective patterns, write-back
actions/    proposal, guardrails, orchestration, measurement
data/       ledger, generator, conversational facts
```

No layer imports a layer above it. Analytics cannot reach the graph and the
graph cannot reach analytics; their results meet only inside the evidence array.

**The loop:** talk → understand → analyse → advise → approve → act → measure →
learn → *and the next merchant's answer is different.*

## The two rules that matter

**1. Never fabricate.** Every tool returns `{value, basis, source, tier}`. The
synthesis layer may quote only what is inside `value`, and a validator between
synthesis and speech discards any utterance containing an unbacked figure,
substituting the deterministic template answer. `/ops` shows a red badge when
that happens. See `backend/reasoning/validator.py`.

**2. Peers are counts, never names.** Percentages of each merchant's own
baseline enter the graph, never rupee figures. A minimum cohort of five is
enforced at the adapter boundary in `backend/graph/privacy.py`, not in the UI.
The merchant cannot be told which shop recovered, because that string never
reaches the synthesis layer.

## Where the data comes from

Every capability declares its tier, and the tier is carried in the evidence and
rendered on `/ops`:

| Tier | Meaning | Example |
| --- | --- | --- |
| **A** automatic | Derivable from payment activity | sales trend, hour patterns, peer cohort |
| **B** integration | Only if the merchant uses POS/billing/accounting | obligations or explicitly requested stock context |
| **C** conversational | The merchant volunteers it and it expires | "bees pachees bottle" |
| **D** unavailable | Never a dependency, never estimated | why a customer did not come |

Roughly 40% of the synthetic merchants have obligations and 30% have a stock
feed so the optional integration paths can be demonstrated. The normal planning
path uses payment data and does not ask the merchant for inventory.

## Turning the real adapters on

Defaults are all-fake. Flip one only after `tests/test_swap.py` is green.

```bash
SAATHI_REAL_COGNEE=1   # Cognee experience graph — run scripts/ingest_cognee.py first
SAATHI_REAL_SARVAM=1   # Sarvam STT/TTS, needs SARVAM_API_KEY
SAATHI_REAL_N8N=1      # n8n orchestration, see n8n/README.md
SAATHI_REAL_LLM=1      # Gemini planning + NVIDIA NIM fallback; configure either key
```

See `.env.example`. `config.py` is the file to put on screen when a judge asks
about production readiness.

## Layout

```
config.py                every flag and threshold
backend/api/             public, internal (n8n), admin routes + websocket
backend/reasoning/       router, toolsets, runner, templates, llm, validator
backend/analytics/       trend, patterns, money, anomaly, outcome
backend/graph/           similarity, privacy, sqlite_store, cognee_store, cards
backend/actions/         machine, guardrails, orchestrators, monitor, writeback
backend/data/            db, repository (the only module that writes SQL), context
frontend/                merchant.html/js, ops.html/js, tokens.css
n8n/workflows/           three workflow JSONs + docker-compose
scripts/                 generate, recompute_patterns, verify_claims,
                         ingest_cognee, demo_check
tests/                   10 suites; the critical three are privacy, grounding, learning
docs/                    DESIGN.md, DEMO_SCRIPT.md, JUDGE_ANSWERS.md
```

`data/*.db` is gitignored: 80MB, and it regenerates from a seed constant in
under five seconds.
