# The agent pipeline

## What changed, and why

Saathi used to classify every question into one of thirteen intents. The intent
picked a fixed set of tools and a fixed response template, and the model's only
job was to reword that template. Anything that did not fit a label got the wrong
tools or a clarify loop — *"aap sales ke baare mein pooch rahe hain, ya paise ke
baare mein?"* — asked of a merchant who had already been perfectly clear.

Worse, every failure path led back to the template. If the model's answer failed
the grounding check, the whole utterance was thrown away and an unrelated
template answer was spoken instead. That is why Saathi seemed to revert
mid-conversation.

It is now the other way round. **The model runs the conversation and decides
what to look up. Python supplies the facts, the numbers, and the limits.**

## The loop

```text
merchant speaks
  -> Sarvam STT
  -> agent loop, up to 4 rounds within a ~9s budget:
       model sees: system prompt, merchant profile, facts the merchant has
                   already stated, the last ~10 turns, and this question
       model calls tools  ->  Python executes them, concurrently within a round
       model sees the results and decides whether to look further
  -> model calls final_answer
  -> grounding gate over every claim-bearing figure
       fails -> the offending figures go BACK to the model, up to 2 repairs
       still fails -> keep the grounded part, say plainly what it cannot stand behind
  -> optional split voice: a second model re-voices it in Hinglish, same gate
  -> Soundbox speaks
```

There is no intent classification anywhere on this path.

## Providers

`backend/reasoning/providers.py` exposes one call:

```python
chat(messages, tools) -> {"calls": [{name, arguments, id}], "text": str}
```

OpenAI is primary and speaks tool calling natively. NVIDIA NIM uses the same
OpenAI-compatible protocol. Gemini's tool-calling format is translated in the
same module. A provider that fails is put on a short cooldown (or a five-minute
one for a quota or schema error) and the next one is tried.

```dotenv
OPENAI_API_KEY=...
OPENAI_MODEL=gpt-5.2
SAATHI_LLM_PROVIDER=auto      # openai | gemini | nvidia | gemini-first | nvidia-first
```

Verify with `python scripts/check_integrations.py openai`. That probe sends a
real tool definition and fails the check if the model replies with prose instead
of a tool call — a model that cannot call tools cannot drive this loop, and
finding that out on stage is not the plan.

## The reasoning model and the speaking model are separable

Two different jobs are being asked of a language model here, and they reward
different models: deciding which tools to call rewards reliable structured
output; saying it to a shopkeeper in natural Hinglish rewards having heard a lot
of Hinglish.

By default they are the same model — one call, one dependency, nothing extra to
fail on venue wifi. Set `SAATHI_SPEECH_PROVIDER` to another configured provider
and the reasoning model still decides everything, but the spoken line is written
by the voice model from the same evidence. The voice model gets no tools, cannot
choose what to look up, and cannot introduce a figure: whatever it writes goes
through the same grounding gate, and is discarded if it fails.

This is also the answer to "your model isn't trained on Hinglish". Correctness
does not depend on the language model at all — every figure is computed in
Python before the model sees it. Only fluency does, and fluency is swappable.

## The tools

`backend/reasoning/tools.py` is the whole trust boundary: a JSON Schema per tool,
with real typed parameters. That is the concrete difference from the old
planner, which had to guess an amount out of the question text and mostly could
not. "Can I make a 50k payment next month?" is now
`afford_check(amount=50000, days=30)`.

| Tool | What it answers |
| --- | --- |
| `get_merchant_context` | who this merchant is, how much history exists |
| `sales_lookup` | actual takings for a named period or date range |
| `compare_periods` | this week against last, per trading day |
| `get_sales_trend` | change against own baseline, by time-of-day band |
| `get_business_health` | headline revenue, tickets, payment success |
| `get_time_patterns` | strong and weak hours and weekdays |
| `get_recent_situations` | conditions already detected from payments |
| `get_demand_forecast` | projected takings, low/expected/high, plus next rush |
| `get_money_position` | inflow, and net position when bills are visible |
| `afford_check` | can they spend X over N days: verdict, buffer, bills, safe-after |
| `get_peer_cohort` | the privacy-safe cohort and why those merchants are similar |
| `get_peer_relative_anomaly` | is this specific to them, or area-wide |
| `get_peer_playbook` | what the cohort did in this situation, as counts and rates |
| `get_failed_plays` | how an action family has failed, bucketed by size |
| `get_local_pattern` | how the locality is trading |
| `get_cohort_seasonality` | weekday seasonality across the cohort |
| `get_cohort_profile` | the cold-start baseline |
| `get_action_history` | this merchant's past actions and measured results |
| `get_optional_stock_context` | stock from POS or from what they said |
| `stock_cover` | days of cover, run-out day, reorder day, 7-day shortfall |
| `calculate` | arithmetic over figures already in evidence |
| `remember_fact` | record something the merchant just told you |
| `propose_action` | build a proposal Python controls |

Everything is read-only except the last two.

## What stays deterministic

These are enforced in code, not asked for in a prompt, because a prompt is a
request and this is a guarantee.

**Arithmetic.** The model never computes. If it needs a sum or a difference it
calls `calculate`, which evaluates a plain expression in Python with an AST
walker that permits numbers and `+ - * / ( )` and nothing else — no names, no
calls, no attributes.

**Action parameters.** The model may recommend an action. It cannot set the
numbers: `propose_action` builds them in Python from the cohort's modal values,
and guardrails validate the result. A test asserts that a model demanding a
₹900 discount for 60 days gets neither.

**Approval.** Nothing executes without spoken approval. Unchanged.

**Privacy.** The cohort floor of five and peer anonymity are enforced in the
graph adapter, and the model is handed each tool's `value` only — never `basis`,
which is where peer identifiers live for /ops. A test asserts no merchant id
ever appears in anything sent to a provider.

**Stock.** Stock tools are not offered at all unless the merchant has mentioned
stock or already stated a figure, so an ordinary question never turns into an
inventory interrogation.

**Grounding.** Below.

## The grounding gate

Every figure in the answer is classified by the role it plays in the sentence —
money, percentage, count, duration, clock time, date — and only the
claim-bearing ones are checked. This is the second thing that was making Saathi
look stupid: the old gate checked *every* numeral, so "teen din", "6 baje" and
"18-21" counted as fabricated business claims and threw away a good answer.

A figure is grounded if it appears in an evidence value, in something the
merchant stated in this conversation, or in their own question. If they asked
about ₹50,000, the answer may say ₹50,000.

On failure the offending figures are named and handed back: *"these figures are
not in the evidence: 63 (percent); either call a tool that produces them or say
this without them."* Up to two repairs. If it still fails, the sentences
carrying bad figures are dropped and Saathi says plainly that it cannot put a
reliable number on that part. **It never substitutes an unrelated answer.**

## Conversation

Turns are persisted per `conversation_id` in SQLite: the question, the answer, a
compact summary of each tool result, and any open question. The agent sees the
thread, so "aur kal?", "60 bottles" and "haan" resolve against what was just
said.

The client posts every utterance to `/api/query` with the conversation id.
It used to branch: a reply to a question went to `/api/context`, which filed it
as a fact and then re-asked the *original* question with no conversation
attached — so the merchant heard the same answer again, minus the question.
`/api/context` still exists for /ops and direct fact entry, but it no longer
re-runs anything.

Facts the merchant states are written by the `remember_fact` tool into
`merchant_inputs` with a TTL enforced in the query. They are listed in the
system prompt as *already told to you — do not ask again*.

## Offline

With no provider reachable, the deterministic router answers and the response is
labelled `offline`, here and on /ops. It is allowed to be simpler. It is not
allowed to be wrong, and it is held to the same grounding gate.

Several routing bugs were fixed in the same pass. `kal` is both yesterday and
tomorrow in Hindi and was being folded to "tomorrow", so "kal ki sale kitni thi"
asked for a forecast of a day that had already happened; the verb now decides.
A bare `\d+%` routed to a risk check, so "sales 20% kyun giri" was treated as a
proposal to discount. `down` matched inside "downtime" and `update` inside
"offer ka update do". And a set of ordinary questions — "aaj kitna business
hua", "is mahine ki kamai", "average bill kitna hai", "Saturday ko kaisa rehta
hai", "namaste" — had no route at all and fell into the clarify loop. Those now
have `sales_lookup`, `period_compare`, `afford_check` and `help` paths, and an
amount parser that understands "50k", "50,000" and "2 lakh".

`unknown` no longer asks which of two things they meant. It says what it cannot
see, and offers what it can.

## Budgets

| | |
| --- | --- |
| `SAATHI_AGENT_MAX_ROUNDS` | 4 |
| `SAATHI_AGENT_MAX_CALLS` | 10 |
| `SAATHI_AGENT_BUDGET_SECONDS` | 9 |
| `SAATHI_AGENT_REPAIR_ATTEMPTS` | 2 |
| `SAATHI_CONVERSATION_TURNS` | 10 |

Independent tools in the same round run concurrently. Prerequisites the model
did not think to request — a cohort before a peer playbook — are inserted by
Python rather than being the model's problem.

## Testing it

`tests/test_agent_conversations.py` scripts the model's decisions and runs
everything else for real: the loop, tool execution, the grounding gate, the
repair path, the conversation store. The scenarios are the ones that used to
break — the 50k question, the three-turn stock conversation, diagnosis followed
by a peer follow-up followed by a proposal, "aur kal?", profit, "namaste", a
forced repair, and a full provider outage.

A live suite runs the same questions against the real model behind
`SAATHI_LIVE_LLM=1`.
