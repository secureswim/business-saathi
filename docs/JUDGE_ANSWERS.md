# Judge questions — the card to carry on stage

Do not bluff any of these. Every one separates what the prototype proves from
what production would require.

**1. Is this real Paytm data?** No. 160 synthetic merchants, 12 months,
generated deterministically with seeded behavioural patterns. Say it before
being asked. Then: the pipeline computing over it is real, and replacing the
generator with a real transaction stream is one adapter.

**2. Why isn't this just ChatGPT over transaction data?** An LLM over one
merchant's data can only describe that merchant. Every answer here cites what
*other* merchants did and what happened — and the model is structurally
forbidden from producing a number that is not in the evidence. Show the
validator and a greyed-out unavailable card.

**3. What does Cognee actually do?** Be precise here, because the code is more
modest than the pitch and a judge who reads it will notice.

Cognee holds experience cards — situation, action, outcome — for every
merchant, plus profiles and cohort patterns, and a successful action writes a
new card back. What it does **not** do is compute the numbers. `CogneeGraph`
wraps `SqliteGraph`: the ledger computes the cohort, the playbook and every
figure, exactly reproducibly, and Cognee is queried in parallel for semantic
retrieval whose hits and latency are recorded in `basis` and shown on /ops.

Say it as: *"the retrieval is real and the write-back is real; the arithmetic
is deliberately not Cognee's job, because a figure a merchant hears has to be
reproducible from rows we can show you."* That is a stronger answer than
claiming a four-hop traversal produces the counts, and it survives someone
opening `backend/graph/cognee_store.py`.

Retrieval is also **non-blocking**: a voice answer is never held waiting for
it. On a cold cache the first question is answered from the ledger while
retrieval is still in flight, and the /ops chip says `sqlite (cognee retrieval
in flight)` rather than claiming Cognee. `scripts/ingest_cognee.py` then
`/api/adapters` before you present, so the cache is warm and the chip is green.

**4. Why do you need n8n?** The action path has waits, retries, partial failure
and a scheduled trigger, and it has to survive the web process restarting.
That is an orchestrator's job, not a request handler's. Three workflows:
execution, monitoring, learning. Show the n8n canvas and the `/ops` workflow
column tracking it node by node.

Two honest caveats. n8n Cloud cannot reach a laptop, so the callbacks come back
through a tunnel (`PUBLIC_API_URL`); if that is down the orchestrator falls
back to the local state machine, the demo continues, and the /ops chip flips to
`n8n → state_machine (n8n unreachable)` rather than lying about it. And what
the workflow executes is still a simulated campaign API — n8n really runs, the
thing it calls is not real.

**5. How does the system learn?** Action → measured outcome → ledger →
experience card → the cohort aggregate changes → the next merchant's answer
cites the new count. Including failures: a failed action lowers the rate.

**6. How do you protect merchant privacy?** Percentages of each merchant's own
baseline enter the graph, never rupee figures. A minimum cohort of five is
enforced at the adapter boundary. Two filters: the synthesis path receives
counts and rates only. The merchant cannot be told which shop recovered because
that string never leaves the graph layer. `backend/graph/privacy.py`.

**7. Where does inventory data come from?** Usually nowhere, which is why the
product does not depend on it. Saathi reasons about *demand* from transactions.
With a POS feed it reads stock; without one it asks for a rough number when the
answer depends on it; otherwise it answers on demand alone.

**8. What if the merchant doesn't use a POS?** Nothing core breaks. All eight
core capabilities run on payment data alone.

**9. What if they have no digital inventory?** Same answer. Saathi says "cold
drink ki demand normal se upar hai", never "aapke paas 17 bottles hain".

**10. What if there is no expense data?** `get_money_position` returns
inflow-only scope and the wording changes to "aane wala paisa". It never claims
a cash position it cannot see. 60% of the synthetic merchants are in this state,
so the path is exercised constantly.

**11. How does this work for a salon?** Demand over time, weekend
concentration, chair-hour capacity as a conversational fact. Inventory is
irrelevant there, which is exactly why the product is not built around it.

**12. And for a chai stall?** The densest cohorts and sharpest hour patterns in
the dataset. The demo merchant is one.

**13. How does this scale to millions?** Honestly: cohort computation is O(n²)
as written. At scale it needs precomputed cohort buckets keyed on
category×locality×band, approximate nearest-neighbour over the pattern vector,
and incremental aggregate maintenance. The retrieval and write-back shape does
not change. Naming the real engineering problem reads as competence; claiming it
is solved reads as bluffing.

**14. What happens when the AI is wrong?** Three layers. It cannot be wrong
about numbers — the validator blocks unbacked figures. It cannot act alone —
every action needs spoken approval. And when a recommendation does not work, the
outcome is recorded and the success rate falls for the next merchant. Error
correction and learning are the same mechanism.

**15. Why voice?** The user is working with their hands in a noisy shop and is
more comfortable speaking Hinglish than reading English. A dashboard is a thing
you have to remember to open; a partner is someone you can ask.

**16. Why would merchants actually use this?** It answers a question they
already ask each other and cannot get answered: *what are shops like mine
doing?* And it costs them nothing — no forms, no data entry, no new software to
maintain.

**17. What would Paytm need to provide in production?** Transaction stream with
timestamps and amounts; merchant category and locality; settlement records; and
for the action layer, a campaign or offer API. Everything else the product
derives itself.

**18. What would make this production-ready?** Real data instead of the
generator; a real `CampaignAPI`; authentication and merchant identity; the scale
work in 13; human review of recommendation quality before actions go live; and a
consent model for contributing outcomes to the collective graph. The
architecture accommodates all of these; none of them are built.
