# The eight-minute demo

Two screens. Merchant route (`/`) on a phone or second window facing the room;
`/ops` on the projector. The room hears a merchant talking to a phone and sees
the reasoning behind it on the big screen. That separation is the argument.

**Before you start:** `python scripts/demo_check.py` — all green, or you are not ready.

| Time | Beat | Say / do | On the projector |
| --- | --- | --- | --- |
| 0:00 | Problem | "A merchant who takes digital payments produces a perfect record of their business and gets nothing back from it. No analyst, no consultant, no way to know what worked for shops like theirs." | Title |
| 0:45 | **Diagnosis** | *"Bhai iss hafte sales kyun kam hain?"* | Pipeline lights stage by stage; evidence cards arrive |
| 1:30 | The answer | Saathi: sales down vs baseline, concentrated in the evening band, cohort median flat, 5 of 6 similar merchants recovered with an evening offer | Cohort canvas; peer-relative card |
| 2:15 | **Evidence reveal** | Hover a peer: "similarity 0.99 — same trade, same kind of locality, same volume band, and the customer time pattern. Not just 'both are chai stalls'." | Similarity components |
| 3:00 | **Peer intelligence** | *"Mere jaise shops mein kya chal raha hai?"* — then point out: counts and rates, never a name | Aggregate cohort patterns |
| 3:45 | **Planning with a gap** | *"Agle hafte ke liye kya prepare karun?"* → Saathi asks *"cold drink kitna bacha hai?"* → *"bees pachees"* | `get_optional_stock_context` greys out, then a tier-C `merchant_input` card appears |
| 4:30 | The point, once | "It did not ask us to maintain inventory. It asked one question, for one decision, because the answer changed." | Tier chips on the cards |
| 5:00 | **Risk** | *"30% discount doon?"* → the failure buckets, and 10–15% recommended instead | Failed-play buckets |
| 5:30 | **Action** | *"Offer bana de"* → parameters spoken → *"Haan"* | Workflow executing node by node |
| 6:15 | **Learning** | Fast-forward three days. Outcome measured, written back. | Counters animate 6/5 → 7/6; node flashes |
| 6:45 | **The proof** | Switch to M008, ask the same question. The spoken number matches the counter. | Both on screen |
| 7:15 | **Proactive** | Touch nothing. Press *Trigger alert scan* beforehand so it lands here. | Alert card with all four conditions shown as met |
| 7:45 | Close | "Not a chatbot over one merchant's data. A partner that combines your own signals with what similar businesses have actually tried, acts only with your approval, measures the result, and makes the next merchant's answer better." | `config.py` — adapters, all swappable |

## The three beats that carry the weight

**6:15 is the pitch.** Slow down. Say out loud what just changed and let the
counter move on screen before continuing.

**3:45 is the credibility beat.** The product admits a limit and handles it
gracefully. Judges who have seen ten demos that assume perfect data will notice.

**7:15 must be genuinely unprompted.** Do not touch anything. The silence before
Saathi speaks is the most effective two seconds in the demo.

## If something breaks

Keep going and say what you are doing. "Sarvam is timing out on the venue wifi,
so it has fallen back to on-device speech — that's the fallback ladder in the
design doc." A visible, designed fallback is a better demonstration of
engineering than a flawless run. What is fatal is silence and clicking.

## Controls you may need

- **Reset demo** — restores the known state in under two seconds
- **Fast-forward** — measures the outcome now instead of waiting
- **Force recovered** — pins the outcome. **Disclosed on screen as a presenter
  override.** Do not hide it; a judge who spots a rigged demo discounts
  everything else. A `no_change` outcome is also a fine demo: the success rate
  drops and the system is visibly honest.
- **Ctrl+Shift+T** on the merchant route — the hidden text input, for when the
  stage microphone fails

## Spare beat, if a question runs short

Switch to `M999` — opened today, zero history — and ask anything. It is the
clearest single illustration of why a collective graph is worth building.
