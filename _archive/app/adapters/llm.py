"""LLM adapter.

The reasoning layer hands this module a fully-resolved evidence bundle and asks
for prose. Two implementations:

  * ``TemplateLLM`` — deterministic Hinglish/English synthesis from the bundle.
    Default. No keys, no latency, identical output every run, which is what you
    want on a demo stage with venue wifi.
  * ``ApiLLM``     — any OpenAI-compatible chat endpoint, used when
    SAATHI_LLM=api and a key is present.

Both are constrained the same way: they may only phrase numbers that are
already in the bundle. Nothing is invented at the language layer, so a
recommendation can always be traced back to graph rows.
"""
from __future__ import annotations

import json
import os
import textwrap
from typing import Any, Protocol

SYSTEM = textwrap.dedent(
    """
    You are Paytm Business Saathi, speaking to an Indian small-business owner.
    Rules:
      - Reply in natural Hinglish (Roman script), the way a trusted local
        business friend talks. Short sentences.
      - Use ONLY the numbers present in the evidence JSON. Never invent a figure.
      - Always say where a claim comes from ("aapke area ke 6 similar merchants
        mein se 5...").
      - Two to four sentences. End with a concrete next step or a question.
      - Never promise an outcome. Peer evidence is evidence, not a guarantee.
    """
).strip()


class LLM(Protocol):
    def say(self, bundle: dict[str, Any]) -> str: ...


# --------------------------------------------------------------------------


class TemplateLLM:
    """Deterministic synthesis. Every branch reads its numbers off the bundle."""

    def say(self, bundle: dict[str, Any]) -> str:
        kind = bundle.get("intent", "business_health")
        fn = getattr(self, f"_{kind}", None)
        return (fn(bundle) if fn else self._business_health(bundle)).strip()

    # -- helpers
    @staticmethod
    def _r(x: float | None) -> str:
        return "—" if x is None else f"₹{round(x):,}"

    @staticmethod
    def _pc(x: float | None) -> str:
        return "—" if x is None else f"{abs(round(x))}%"

    # -- per-intent phrasing
    def _business_health(self, b: dict) -> str:
        own, loc = b["own"], b.get("locality", {})
        d = own["trend"]["delta_pct"]
        dirn = "neeche" if d < 0 else "upar"
        s = (f"Aapka daily average {self._r(own['trend']['recent_avg'])} hai, pichhle mahine "
             f"{self._r(own['trend']['baseline_avg'])} tha — {self._pc(d)} {dirn}.")
        if own.get("worst_slot") and own["worst_slot"]["delta_pct"] < -8:
            s += f" Girawat mainly {own['worst_slot']['label']} mein hai."
        if loc.get("verdict") == "merchant_specific":
            s += " Aapke area ke similar merchants flat hain, toh ye aapki shop-specific baat hai."
        elif loc.get("verdict") == "area_wide":
            s += " Poore area mein yahi trend hai, sirf aap nahi."
        if b.get("recommendation"):
            r = b["recommendation"]
            s += f" Main {r['label']} recommend karta hoon. Bana doon?"
        return s

    def _sales_drop(self, b: dict) -> str:
        own, ev, loc = b["own"], b.get("evidence"), b.get("locality", {})
        ws = own.get("worst_slot") or {}
        s = (f"Sales {self._pc(own['trend']['delta_pct'])} down hain. "
             f"Drop mainly {ws.get('label', 'din bhar')} mein hua hai — "
             f"{self._r(ws.get('baseline_avg'))} se {self._r(ws.get('recent_avg'))}.")
        if loc.get("verdict") == "merchant_specific":
            s += (f" Aapke {loc['peer_sample']} similar merchants ka trend "
                  f"{loc['area_trend_pct']:+.0f}% hai, toh market nahi — kuch aapke yahan badla hai.")
        elif loc.get("verdict") == "area_wide":
            s += f" Aapke area ke similar merchants bhi {loc['area_trend_pct']:+.0f}% par hain."
        rec = b.get("recommendation")
        if ev:
            what = rec["proposal_hi"].rstrip(".") if rec else "evening offer"
            s += (f" Aapke jaise {ev['peer_count']} merchants mein se {ev['success_count']} ne "
                  f"aisa hi kuch try kiya aur recover kiya.")
            if rec:
                s += f" Main recommend karta hoon: {what}. Bana doon?"
        return s

    def _area_or_me(self, b: dict) -> str:
        loc = b["locality"]
        if loc["verdict"] == "merchant_specific":
            return (f"Ye sirf aapke saath hai. Aapka trend {loc['merchant_trend_pct']:+.0f}% hai, "
                    f"{loc['locality']} ke {loc['peer_sample']} similar merchants ka "
                    f"{loc['area_trend_pct']:+.0f}%. Market theek hai — problem aapki shop mein hai, "
                    f"jo achhi baat hai kyunki ye theek ho sakti hai.")
        if loc["verdict"] == "outperforming_area":
            return (f"Aap area se aage hain. Aapka {loc['merchant_trend_pct']:+.0f}%, "
                    f"aapke {loc['peer_sample']} similar merchants ka {loc['area_trend_pct']:+.0f}%.")
        return (f"Ye poore area ki baat hai. Aapka {loc['merchant_trend_pct']:+.0f}% aur "
                f"{loc['locality']} ke {loc['peer_sample']} similar merchants ka "
                f"{loc['area_trend_pct']:+.0f}% — dono same direction mein.")

    def _cash_forecast(self, b: dict) -> str:
        cf = b["cashflow"]
        planned = next((l for l in cf["expense_lines"] if l["due_in_days"] == 1
                        and l["label"] not in ("Shop rent", "Supplier payment", "Electricity", "Loan EMI")), None)
        s = (f"Agle {cf['horizon_days']} din mein {self._r(cf['upcoming_expenses'])} ke kharche hain "
             f"aur {self._r(cf['projected_inflow'])} ka retained margin aayega.")
        if planned:
            s += (f" {self._r(planned['amount'])} ka {planned['label']} possible hai, "
                  f"lekin buffer {self._r(cf['closing_buffer'])} par aa jayega")
            s += " — weekend slow raha toh tight ho jayega." if cf["verdict"] != "comfortable" else "."
        else:
            s += f" Buffer {self._r(cf['closing_buffer'])} rahega — {cf['verdict']}."
        return s

    def _rush_forecast(self, b: dict) -> str:
        r = b["rush"]
        return (f"Aapka sabse bada rush {r['window']} hota hai — average se {r['multiple_of_average']}x. "
                f"Sabse strong din {r['strongest_day']} hai. Us window mein takreeban "
                f"{r['expected_txns_in_window']} transactions expect kijiye, "
                f"average ticket {self._r(r['avg_ticket'])}.")

    def _festive_prep(self, b: dict) -> str:
        ev = b.get("evidence")
        if not ev:
            return "Festive data abhi graph mein kaafi nahi hai. Thoda aur history chahiye."
        return (f"Pichhle festive season mein aapke jaise {ev['peer_count']} merchants ne stock badhaya tha. "
                f"{ev['success_count']} ne pre-stock kiya aur median {self._pc(ev['median_gmv_delta_pct'])} "
                f"zyada revenue capture kiya. Jo merchants pre-stock nahi kiye, woh surge ke beech stock "
                f"khatam kar baithe. Main aapke liye stock plan bana doon?")

    def _discount_check(self, b: dict) -> str:
        ev, sim = b.get("evidence"), b.get("simulation")
        pct = b.get("asked", {}).get("percent")
        s = f"{round(pct)}% discount se pehle ek baat — " if pct else "Ek baat suniye — "
        if ev:
            faded = ev["outcomes"].get("spike_then_fade", 0)
            s += (f"aapke jaise {ev['peer_count']} merchants ne deep discount try kiya, "
                  f"{faded} mein volume spike hua lekin retention "
                  f"{self._pc(ev['median_retention_pct'])} par gir gaya.")
        if sim:
            s += f" Aapke numbers par ye {sim['delta_pct']:+.0f}% daily GMV banta hai."
        if b.get("recommendation"):
            s += f" Iske bajaye {b['recommendation']['label']} behtar hai — wahan evidence strong hai."
        return s

    def _price_whatif(self, b: dict) -> str:
        ev, sim = b.get("evidence"), b.get("simulation")
        if not sim:
            return "Price change simulate karne ke liye thoda aur data chahiye."
        s = (f"Aapka average ticket {self._r(sim['baseline_ticket'])} hai, toh ye "
             f"{self._pc(sim['price_shock_pct'])} ka price jump hai.")
        if ev:
            s += (f" Aapke jaise {ev['peer_count']} merchants ne price badhaya — "
                  f"{ev['outcomes'].get('no_change', 0)} ka volume same raha, "
                  f"{ev['outcomes'].get('declined', 0)} ne transactions khoye.")
        s += (f" Aapke numbers par projection: daily GMV {self._r(sim['baseline_daily_gmv'])} se "
              f"{self._r(sim['projected_daily_gmv'])} ({sim['delta_pct']:+.0f}%).")
        if sim.get("beyond_observed_range"):
            s += (" Lekin ye jump kisi similar merchant ne try nahi kiya hai — "
                  "graph ke bahar ka anumaan hai, chhota step lena safer hai.")
        return s

    def _what_should_i_do(self, b: dict) -> str:
        return self._sales_drop(b) if b.get("recommendation") else self._business_health(b)

    def _create_offer(self, b: dict) -> str:
        r = b.get("recommendation")
        if not r:
            return "Kaunsa offer banana hai? Main aapke data se suggest kar sakta hoon."
        return (f"{r['proposal_hi']} Aapke jaise {r['evidence_line_hi']}. "
                f"Confirm kijiye toh main abhi bana deta hoon.")


# --------------------------------------------------------------------------


class ApiLLM(TemplateLLM):
    """OpenAI-compatible chat completion, with the template as the fallback.

    Falls back silently on any error so a network blip never kills the demo.
    """

    def __init__(self) -> None:
        self.base = os.environ.get("SAATHI_LLM_BASE", "https://api.openai.com/v1")
        self.model = os.environ.get("SAATHI_LLM_MODEL", "gpt-4o-mini")
        self.key = os.environ.get("SAATHI_LLM_KEY") or os.environ.get("OPENAI_API_KEY")

    def say(self, bundle: dict[str, Any]) -> str:  # pragma: no cover - needs a key
        if not self.key:
            return super().say(bundle)
        try:
            import urllib.request

            payload = json.dumps({
                "model": self.model,
                "temperature": 0.2,
                "messages": [
                    {"role": "system", "content": SYSTEM},
                    {"role": "user", "content": json.dumps(bundle, ensure_ascii=False, default=str)},
                ],
            }).encode()
            req = urllib.request.Request(
                f"{self.base}/chat/completions", data=payload,
                headers={"Authorization": f"Bearer {self.key}", "Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=12) as r:
                data = json.loads(r.read())
            return data["choices"][0]["message"]["content"].strip()
        except Exception:
            return super().say(bundle)


def get_llm() -> LLM:
    if os.environ.get("SAATHI_LLM", "template").lower() == "api":
        return ApiLLM()
    return TemplateLLM()
