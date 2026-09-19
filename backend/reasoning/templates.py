"""The offline reasoner: evidence -> Hinglish and English, by template.

Every figure is pulled directly out of an evidence `value`, so a fabricated
number is structurally impossible here. This runs with no key and no network,
which is what makes a clean clone demo-ready.

It is NOT the fallback for a rejected model answer any more. Substituting an
unrelated template for an answer the merchant was waiting for is what made
Saathi feel like it kept reverting mid-conversation; a bad figure is now
repaired instead. This path runs only when no model provider can be reached at
all, and the answer says so.

House rules: two to four sentences, attribute by source, never name a merchant,
never explain a cause the evidence does not contain.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from backend.models.evidence import Evidence  # noqa: E402

BAND_HI = {"7-10": "subah 7 se 10", "10-13": "din 10 se 1", "13-16": "dopahar 1 se 4",
           "16-19": "shaam 4 se 7", "19-22": "raat 7 se 10"}
WEEKDAY_HI = {"Monday": "Somvaar", "Tuesday": "Mangalvaar", "Wednesday": "Budhvaar",
              "Thursday": "Guruvaar", "Friday": "Shukravaar", "Saturday": "Shanivaar",
              "Sunday": "Ravivaar"}


def rs(x) -> str:
    return f"Rs {round(float(x)):,}"


def band_hi(b: str | None) -> str:
    return BAND_HI.get(b or "", (b or "").replace("-", " se ") + " baje")


def _f(ev, name, default=None):
    """Evidence objects and their dict form are both accepted."""
    if isinstance(ev, dict):
        return ev.get(name, default)
    return getattr(ev, name, default)


def v(evidence, tool: str) -> dict | None:
    for ev in evidence:
        if _f(ev, "tool") == tool and _f(ev, "available"):
            return _f(ev, "value")
    return None


def ask_of(evidence):
    for ev in evidence:
        if _f(ev, "ask"):
            return ev
    return None


class TemplateReasoner:
    name = "template"

    def synthesize(self, intent: str, question: str, evidence) -> dict:
        fn = getattr(self, f"_s_{intent}", self._s_unknown)
        out = fn(evidence)
        out.setdefault("action_proposal", None)
        out["reasoner"] = self.name

        # at most one clarifying question, appended by the runner's `ask`
        a = ask_of(evidence)
        if a and _f(a, "ask") == "stock_estimate" and out.get("allow_ask", True):
            subject = _f(a, "ask_subject") or "stock"
            out["hinglish"] += (f" Agar aapko roughly pata ho ki {subject} kitna bacha hai, "
                                f"main aur behtar bata sakta hoon.")
            out["english"] += (f" If you know roughly how much {subject} is left, "
                               f"I can plan this more precisely.")
            out["ask"] = {"kind": "stock_estimate", "subject": subject}
        out.pop("allow_ask", None)
        return out

    # ------------------------------------------------------- diagnosis
    def _s_sales_diagnosis(self, ev):
        t = v(ev, "get_sales_trend")
        if not t:
            return self._s_business_health(ev)
        anom = v(ev, "get_peer_relative_anomaly")
        pb = v(ev, "get_peer_playbook")
        prop = v(ev, "propose_action")

        drop = abs(t["change_pct"])
        word = "neeche" if t["direction"] == "down" else "upar"
        hi = [f"Aapki sales apne normal se {drop}% {word} hain. "
              f"Daily average {rs(t['current_daily'])} hai, pehle {rs(t['baseline_daily'])} tha. "
              f"Sabse zyada farak {band_hi(t['worst_band'])} ke beech hai."]
        en = [f"Sales are {t['change_pct']}% versus your 30-day baseline. "
              f"Daily average {rs(t['current_daily'])}, previously {rs(t['baseline_daily'])}. "
              f"The change concentrates in the {t['worst_band']} band."]

        if anom:
            if anom["verdict"] == "specific_to_merchant":
                hi.append(f"Aapke jaise {anom['peers_used']} merchants ka median change "
                          f"{anom['peer_median_change_pct']}% hai - matlab ye sirf aapke "
                          f"yahan hai, poore market mein nahi.")
                en.append(f"The cohort median change is {anom['peer_median_change_pct']}% "
                          f"across {anom['peers_used']} similar merchants. This is specific "
                          f"to your shop, not the market.")
            elif anom["verdict"] == "market_wide":
                hi.append(f"Aapke jaise {anom['peers_used']} merchants mein bhi yahi pattern "
                          f"hai ({anom['peer_median_change_pct']}%), toh ye poore area ka "
                          f"asar lag raha hai.")
                en.append(f"Similar merchants show the same pattern "
                          f"({anom['peer_median_change_pct']}%), so this looks area-wide.")
            else:
                hi.append(f"Aapke jaise {anom['peers_used']} merchants se aap behtar kar "
                          f"rahe hain.")
                en.append(f"You are ahead of the {anom['peers_used']} similar merchants.")

        if pb and pb.get("best"):
            b = pb["best"]
            # merchants and attempts are different numbers: say which is which
            hi.append(f"Aapke jaise {b['merchants']} merchants ne yahi situation mein "
                      f"{_action_hi(b['action'])} try kiya - {b['tried']} koshishon "
                      f"mein se {b['worked']} baar kaam kiya ({b['success_rate']}%).")
            en.append(f"{b['merchants']} similar merchants tried a "
                      f"{b['action'].replace('_', ' ')} in this situation; it worked in "
                      f"{b['worked']} of {b['tried']} attempts ({b['success_rate']}%).")
        else:
            hi.append("Kyun gir rahi hai, ye exactly main nahi keh sakta - mere paas sirf "
                      "transaction data hai.")
            en.append("I can see the drop but not its cause; I only have transaction data.")

        out = {"hinglish": " ".join(hi), "english": " ".join(en)}
        if prop:
            out["action_proposal"] = prop
            out["hinglish"] += (f" Main {_proposal_hi(prop)} suggest karta hoon. Bana doon?")
            out["english"] += f" Suggested: {_proposal_en(prop)}."
            out["allow_ask"] = False
        return out

    # ---------------------------------------------------- business health
    def _s_business_health(self, ev):
        h = v(ev, "get_business_health")
        if not h:
            return self._s_unknown(ev)
        anom = v(ev, "get_peer_relative_anomaly")
        head = {"growing": "achha chal raha hai", "steady": "theek chal raha hai",
                "needs_attention": "thoda dhyaan maangta hai",
                "erratic": "kaafi upar-neeche ho raha hai"}[h["headline"]]
        hi = [f"Business {head}. Pichhle hafte daily average {rs(h['current_daily'])} raha, "
              f"normal {rs(h['baseline_daily'])} hai - {abs(h['change_pct'])}% ka farak. "
              f"Roz takreeban {h['txns_per_day']} transactions ho rahe hain."]
        en = [f"Business is {h['headline'].replace('_', ' ')}. Daily average "
              f"{rs(h['current_daily'])} last week versus a {rs(h['baseline_daily'])} "
              f"baseline, a {h['change_pct']}% change, on {h['txns_per_day']} "
              f"transactions a day."]
        if h.get("avg_ticket"):
            hi.append(f"Average bill {rs(h['avg_ticket'])} ka hai.")
            en.append(f"Average ticket {rs(h['avg_ticket'])}.")
        if anom and anom["verdict"] == "market_wide":
            hi.append(f"Aapke jaise {anom['peers_used']} merchants mein bhi yahi trend hai.")
            en.append(f"The same trend appears across {anom['peers_used']} similar merchants.")
        elif anom and anom["verdict"] == "specific_to_merchant":
            hi.append(f"Aapke jaise {anom['peers_used']} merchants ka median "
                      f"{anom['peer_median_change_pct']}% hai - farak sirf aapke yahan hai.")
            en.append(f"Cohort median is {anom['peer_median_change_pct']}%; this is "
                      f"specific to your shop.")
        return {"hinglish": " ".join(hi), "english": " ".join(en)}

    # -------------------------------------------------------- anomaly
    def _s_anomaly_check(self, ev):
        h = v(ev, "get_business_health")
        anom = v(ev, "get_peer_relative_anomaly")
        sits = v(ev, "get_recent_situations")
        if not h:
            return self._s_unknown(ev)

        unusual = h["headline"] in ("needs_attention", "erratic") or (
            anom and anom["verdict"] == "specific_to_merchant")
        if not unusual:
            hi = [f"Abhi sab normal lag raha hai. Daily average {rs(h['current_daily'])} hai, "
                  f"{abs(h['change_pct'])}% ka hi farak hai."]
            en = [f"Nothing unusual. Daily average {rs(h['current_daily'])}, a "
                  f"{h['change_pct']}% change against baseline."]
        else:
            hi = [f"Haan, ek cheez dikh rahi hai. Aapki sales {abs(h['change_pct'])}% "
                  f"{'neeche' if h['change_pct'] < 0 else 'upar'} hain normal se."]
            en = [f"Yes. Sales are {h['change_pct']}% against your baseline."]
            if anom and anom["verdict"] == "specific_to_merchant":
                hi.append(f"Aapke jaise {anom['peers_used']} merchants flat hain, toh ye "
                          f"aapke shop tak hi seemit hai.")
                en.append(f"The {anom['peers_used']} similar merchants are flat, so this is "
                          f"specific to you.")
        if sits and sits.get("count"):
            hi.append(f"Pichhle mahine {sits['count']} baar aisa pattern detect hua hai.")
            en.append(f"{sits['count']} situations detected in the last 30 days.")
        return {"hinglish": " ".join(hi), "english": " ".join(en)}

    # ---------------------------------------------------------- peers
    def _s_peer_insight(self, ev):
        coh = v(ev, "get_peer_cohort")
        lp = v(ev, "get_local_pattern")
        pb = v(ev, "get_peer_playbook")
        if not coh:
            return {"hinglish": "Aapke jaise itne merchants abhi nahi hain ki main "
                                "safely compare kar sakoon.",
                    "english": "There are not enough similar merchants to compare "
                               "against without exposing individual businesses."}
        hi = [f"Aapke jaise {coh['cohort_size']} merchants ko main compare kar raha hoon - "
              f"same trade, same tarah ka area, same volume band."]
        en = [f"I compare you against {coh['cohort_size']} similar merchants - same trade, "
              f"same kind of locality, same volume band."]
        if lp:
            dir_hi = {"down": "neeche", "up": "upar", "flat": "flat"}[lp["direction"]]
            hi.append(f"Pichhle hafte unka overall {abs(lp['cohort_change_pct'])}% "
                      f"{dir_hi} raha. Sabse busy time {', '.join(lp['busiest_bands'])} "
                      f"baje hai.")
            en.append(f"Their cohort moved {lp['cohort_change_pct']}% last week; busiest "
                      f"bands are {', '.join(lp['busiest_bands'])}.")
        if pb and pb.get("best"):
            b = pb["best"]
            hi.append(f"Inn mein se {b['merchants']} ne {_action_hi(b['action'])} try "
                      f"kiya - {b['tried']} koshishon mein se {b['worked']} baar fayda hua.")
            en.append(f"{b['merchants']} of them tried a "
                      f"{b['action'].replace('_', ' ')}; it worked in {b['worked']} of "
                      f"{b['tried']} attempts.")
        hi.append("Kisi ek dukaan ke numbers main nahi bata sakta - sirf group ka pattern.")
        en.append("I never share an individual shop's figures, only cohort patterns.")
        return {"hinglish": " ".join(hi), "english": " ".join(en)}

    # ----------------------------------------------------- time pattern
    def _s_time_pattern(self, ev):
        tp = v(ev, "get_time_patterns")
        if not tp:
            return self._s_unknown(ev)
        hi = [f"Aapki sabse zyada sales {', '.join(tp['peak_bands'])} baje hoti hai."]
        en = [f"Your busiest bands are {', '.join(tp['peak_bands'])}."]
        if tp.get("best_weekday"):
            hi.append(f"Hafte mein {WEEKDAY_HI.get(tp['best_weekday'], tp['best_weekday'])} "
                      f"sabse strong rehta hai.")
            en.append(f"{tp['best_weekday']} is your strongest day.")
        if tp.get("weekend_ratio"):
            comp = "zyada" if tp["weekend_ratio"] > 1 else "kam"
            hi.append(f"Weekend par weekday se {tp['weekend_ratio']} guna {comp} business "
                      f"hota hai.")
            en.append(f"Weekends run at {tp['weekend_ratio']}x weekdays.")
        return {"hinglish": " ".join(hi), "english": " ".join(en)}

    # -------------------------------------------------------- forecast
    def _s_demand_forecast(self, ev):
        f = v(ev, "get_demand_forecast")
        if not f:
            return self._s_unknown(ev)
        hi = [f"Agle {f['days']} din mein takreeban {rs(f['total_expected'])} ka business "
              f"expected hai - {rs(f['total_low'])} se {rs(f['total_high'])} ke beech."]
        en = [f"Next {f['days']} days: {rs(f['total_low'])} to {rs(f['total_high'])}, "
              f"expected {rs(f['total_expected'])}."]
        if f.get("busiest_day"):
            hi.append(f"Sabse busy din {WEEKDAY_HI.get(f['busiest_day'], f['busiest_day'])} "
                      f"rahega.")
            en.append(f"{f['busiest_day']} looks busiest.")
        if f.get("next_rush"):
            r = f["next_rush"]
            hi.append(f"Next rush {WEEKDAY_HI.get(r['weekday'], r['weekday'])} ko "
                      f"{r['hour']} baje, normal se {r['multiple']} guna.")
            en.append(f"Next peak {r['weekday']} at {r['hour']}:00, {r['multiple']}x your "
                      f"hourly average.")
        return {"hinglish": " ".join(hi), "english": " ".join(en)}

    # -------------------------------------------------------- planning
    def _s_planning(self, ev):
        f = v(ev, "get_demand_forecast")
        stock = v(ev, "get_optional_stock_context")
        pb = v(ev, "get_peer_playbook")
        if not f:
            return self._s_unknown(ev)

        hi = [f"Agle {f['days']} din ka expected business {rs(f['total_expected'])} hai. "
              f"Sabse busy din {WEEKDAY_HI.get(f.get('busiest_day', ''), f.get('busiest_day', ''))} "
              f"rahega."]
        en = [f"Next {f['days']} days: expected {rs(f['total_expected'])}, busiest day "
              f"{f.get('busiest_day')}."]
        if f.get("next_rush"):
            r = f["next_rush"]
            hi.append(f"Peak {r['hour']} baje aata hai, normal se {r['multiple']} guna - "
                      f"uss time ki tayyari zyada rakhiye.")
            en.append(f"The peak is {r['hour']}:00 at {r['multiple']}x, so prepare more for "
                      f"that window.")
        if stock:
            src_hi = ("aapne bataya tha" if stock["source"] == "merchant_stated"
                      else "aapke connected system ke hisaab se")
            hi.append(f"{src_hi} {stock['subject']} ka stock {stock['quantity']} "
                      f"{stock.get('unit') or ''} hai - uss hisaab se tayyari kar lijiye.")
            en.append(f"Stock for {stock['subject']} is {stock['quantity']} "
                      f"{stock.get('unit') or ''} ({stock['source'].replace('_', ' ')}).")
        if pb and pb.get("best"):
            b = pb["best"]
            hi.append(f"Aapke jaise {b['merchants']} merchants ne iss situation mein "
                      f"{_action_hi(b['action'])} try kiya - {b['worked']}/{b['tried']} "
                      f"koshishein kaam kar gayin.")
            en.append(f"{b['merchants']} similar merchants tried a "
                      f"{b['action'].replace('_', ' ')} here; {b['worked']} of "
                      f"{b['tried']} attempts worked.")
        return {"hinglish": " ".join(hi), "english": " ".join(en)}

    # ------------------------------------------------------------ risk
    def _s_risk_check(self, ev):
        failed = v(ev, "get_failed_plays")
        pos = v(ev, "get_money_position")
        hi, en = [], []

        if failed and failed.get("evidence"):
            worst = max(failed["buckets"], key=lambda b: b["failure_rate"])
            safest = failed["safest_bucket"]
            hi.append(f"Aapke jaise merchants mein {worst['range']} wale {worst['tried']} "
                      f"cases mein se {worst['failed']} mein lasting fayda nahi hua.")
            en.append(f"In the {worst['range']} band, {worst['failed']} of {worst['tried']} "
                      f"cases showed no lasting benefit ({worst['failure_rate']}% failure).")
            if safest["range"] != worst["range"]:
                hi.append(f"{safest['range']} wale {safest['tried']} cases mein median "
                          f"{safest['median_delta']}% ka farak aaya - main wahi recommend "
                          f"karta hoon.")
                en.append(f"The {safest['range']} band shows a median "
                          f"{safest['median_delta']}% change across {safest['tried']} "
                          f"cases. I would recommend that instead.")

        if pos:
            if pos.get("scope") == "net_position" and "verdict" in pos:
                verdict_hi = {"unsafe": "abhi safe nahi hai", "tight": "tight rahega",
                              "comfortable": "theek rahega"}[pos["verdict"]]
                hi.append(f"{rs(pos['purchase'])} kharch karna {verdict_hi} - "
                          f"{pos['days']} din baad buffer {rs(pos['buffer_expected'])} bachta hai.")
                en.append(f"A {rs(pos['purchase'])} spend is {pos['verdict']}: expected "
                          f"buffer {rs(pos['buffer_expected'])}, "
                          f"{rs(pos['buffer_stressed'])} if the week underperforms.")
                if pos.get("safe_after_days"):
                    hi.append(f"{pos['safe_after_days']} din baad ye safe ho jaata hai.")
                    en.append(f"It becomes safe after {pos['safe_after_days']} days.")
            elif pos.get("scope") == "inflow_only":
                hi.append(f"Agle {pos['days']} din mein {rs(pos['revenue_expected'])} aane "
                          f"ki ummeed hai, lekin aapke kharche mujhe nahi dikhte - toh "
                          f"main safe ya nahi, ye pakka nahi keh sakta.")
                en.append(f"Expected inflow {rs(pos['revenue_expected'])} over "
                          f"{pos['days']} days, but I cannot see your outgoings, so I "
                          f"cannot judge whether this is safe.")
        if not hi:
            return self._s_unknown(ev)
        return {"hinglish": " ".join(hi), "english": " ".join(en)}

    def _s_what_if(self, ev):
        return self._s_risk_check(ev)

    # ----------------------------------------------------------- money
    def _s_money_check(self, ev):
        pos = v(ev, "get_money_position")
        if not pos:
            return self._s_unknown(ev)
        if pos["scope"] == "inflow_only":
            hi = [f"Agle {pos['days']} din mein takreeban {rs(pos['revenue_expected'])} ka "
                  f"business aane ki ummeed hai - {rs(pos['revenue_low'])} se "
                  f"{rs(pos['revenue_high'])} ke beech.",
                  "Aapke kharche mujhe nahi dikhte, toh ye sirf aane wala paisa hai, "
                  "poora hisaab nahi."]
            en = [f"Expected inflow {rs(pos['revenue_expected'])} over {pos['days']} days "
                  f"({rs(pos['revenue_low'])}-{rs(pos['revenue_high'])}).",
                  "I cannot see your outgoings, so this is incoming business only, not a "
                  "cash position."]
            return {"hinglish": " ".join(hi), "english": " ".join(en)}
        hi = [f"Agle {pos['days']} din mein {rs(pos['revenue_expected'])} aane ki ummeed "
              f"hai aur {rs(pos['obligations_due'])} ke kharche pata hain. "
              f"Net {rs(pos['net_expected'])} bachega.",
              f"Agar hafta thoda kamzor raha toh {rs(pos['net_low'])} tak reh sakta hai."]
        en = [f"Expected {rs(pos['revenue_expected'])} in over {pos['days']} days against "
              f"{rs(pos['obligations_due'])} of known obligations. Net "
              f"{rs(pos['net_expected'])}.",
              f"If the week underperforms, closer to {rs(pos['net_low'])}."]
        return {"hinglish": " ".join(hi), "english": " ".join(en)}

    # ---------------------------------------------------------- actions
    def _s_action_request(self, ev):
        prop = v(ev, "propose_action")
        pb = v(ev, "get_peer_playbook")
        if not prop:
            return {"hinglish": "Abhi mere paas itna data nahi hai ki main koi action "
                                "confidently suggest kar sakoon.",
                    "english": "I do not have enough cohort evidence to propose an action "
                               "I can stand behind."}
        best = (pb or {}).get("best") or {}
        return {
            "hinglish": (f"{_proposal_hi(prop)}. Yahi wo configuration hai jo aapke jaise "
                         f"{best.get('worked', '')} merchants ne chalayi thi. Confirm karun?"
                         ).replace("  ", " "),
            "english": (f"{_proposal_en(prop)} - the configuration "
                        f"{best.get('worked')} of {best.get('tried')} similar merchants "
                        f"used. Awaiting your approval."),
            "action_proposal": prop,
            "allow_ask": False,
        }

    def _s_action_status(self, ev):
        h = v(ev, "get_action_history")
        if not h:
            return {"hinglish": "Abhi tak aapne mere through koi action nahi chalaya hai.",
                    "english": "No actions have been run through me yet."}
        last = h["actions"][0]
        if last.get("verdict") is None:
            return {"hinglish": f"{_action_hi(last['type'])} chal raha hai, result abhi "
                                f"aana baaki hai.",
                    "english": f"The {last['type'].replace('_', ' ')} is still running; "
                               f"no measured outcome yet."}
        verdict_hi = {"recovered": "recover ho gaya", "sustained": "fayda tika raha",
                      "no_change": "khaas farak nahi pada",
                      "temporary_spike": "sirf thodi der ka ubhaar aaya",
                      "worse": "ulta nuksan hua"}.get(last["verdict"], last["verdict"])
        return {
            "hinglish": (f"Pichhla {_action_hi(last['type'])} {last['started_on']} ko "
                         f"chalaya tha. Uska result: {verdict_hi}, "
                         f"{abs(last['delta_pct'])}% ka farak."),
            "english": (f"Your last {last['type'].replace('_', ' ')} started "
                        f"{last['started_on']}. Outcome: {last['verdict']}, "
                        f"{last['delta_pct']}%."),
        }

    # ------------------------------------------------------- cold start
    def _s_cold_start(self, ev):
        cp = v(ev, "get_cohort_profile")
        if not cp:
            return {"hinglish": "Aapka business abhi naya hai aur aapke area mein itne "
                                "similar merchants nahi hain ki main pattern bata sakoon.",
                    "english": "Your business is new and there are not enough similar "
                               "merchants nearby for me to share a pattern safely."}
        hi = [f"Aapka apna history abhi nahi hai, lekin aapke area ke {cp['cohort_size']} "
              f"similar merchants {', '.join(cp['peak_bands'])} baje peak karte hain.",
              f"Pehle mahine ka daily business usually {rs(cp['expected_daily_low'])} se "
              f"{rs(cp['expected_daily_high'])} ke beech rehta hai."]
        en = [f"You have no history of your own yet, but the {cp['cohort_size']} similar "
              f"merchants in your area peak at {', '.join(cp['peak_bands'])}.",
              f"First-month daily revenue in that cohort runs "
              f"{rs(cp['expected_daily_low'])} to {rs(cp['expected_daily_high'])}."]
        if cp.get("proven_action"):
            pa = cp["proven_action"]
            hi.append(f"Inn mein se {pa['worked']} ko {_action_hi(pa['action'])} se fayda "
                      f"hua hai.")
            en.append(f"{pa['worked']} of {pa['tried']} benefited from a "
                      f"{pa['action'].replace('_', ' ')}.")
        if cp.get("common_mistake"):
            wb = cp["common_mistake"]["worst_bucket"]
            hi.append(f"Sabse aam galti: {wb['range']} discount - {wb['tried']} mein se "
                      f"{wb['failed']} baar kaam nahi aaya.")
            en.append(f"Most common mistake: {wb['range']} discounts, which failed in "
                      f"{wb['failed']} of {wb['tried']} cases.")
        return {"hinglish": " ".join(hi), "english": " ".join(en)}

    # ------------------------------------------------------ lookups
    def _s_sales_lookup(self, ev):
        s = v(ev, "sales_lookup")
        if not s:
            return self._s_business_health(ev)
        if s.get("no_activity"):
            return {
                "hinglish": (f"{s['period'].capitalize()} abhi tak koi payment record "
                             f"nahi hua hai. Jaise hi aayega, main bata dunga."),
                "english": (f"No payments recorded {s['period']} so far."),
                "allow_ask": False,
            }
        hi = [f"{s['period'].capitalize()} mein {rs(s['total'])} ka business hua, "
              f"{s['txns']} payments."]
        en = [f"{s['period'].capitalize()}: {rs(s['total'])} across {s['txns']} payments."]
        if s.get("avg_ticket"):
            hi.append(f"Average bill {rs(s['avg_ticket'])} ka raha.")
            en.append(f"Average ticket {rs(s['avg_ticket'])}.")
        if s.get("days_open", 1) > 1:
            hi.append(f"Roz ka average {rs(s['per_day'])}.")
            en.append(f"That is {rs(s['per_day'])} per trading day.")
        return {"hinglish": " ".join(hi), "english": " ".join(en)}

    def _s_period_compare(self, ev):
        c = v(ev, "compare_periods")
        if not c:
            return self._s_sales_lookup(ev)
        word = {"up": "behtar", "down": "kam", "flat": "lagbhag barabar"}[c["direction"]]
        hi = [f"{c['period'].capitalize()} {word} hai — roz ka {rs(c['per_day'])}, "
              f"{c['compared_to']} {rs(c['per_day_before'])} tha."]
        en = [f"{c['period'].capitalize()} is {c['direction']} versus "
              f"{c['compared_to']}: {rs(c['per_day'])} per day against "
              f"{rs(c['per_day_before'])}."]
        if abs(c["change_pct"]) >= 2:
            hi.append(f"Yani {abs(c['change_pct'])}% ka farak.")
            en.append(f"A {abs(c['change_pct'])}% difference.")
        return {"hinglish": " ".join(hi), "english": " ".join(en)}

    def _s_afford_check(self, ev):
        a = v(ev, "afford_check") or v(ev, "get_money_position")
        if not a:
            return self._s_money_check(ev)
        if a.get("scope") == "inflow_only":
            return {
                "hinglish": (f"Aapke payments ke hisaab se agle {a.get('days', 30)} din "
                             f"mein takreeban {rs(a.get('revenue_expected', 0))} aane "
                             f"chahiye. Lekin aapke kharche mujhe dikhte nahi, isliye "
                             f"pakka nahi keh sakta ki {rs(a.get('purchase', 0))} "
                             f"nikalna safe hai."),
                "english": (f"About {rs(a.get('revenue_expected', 0))} should come in "
                            f"over the next {a.get('days', 30)} days. I cannot see your "
                            f"outgoings, so I cannot say whether "
                            f"{rs(a.get('purchase', 0))} is safe."),
            }
        verdict_hi = {"comfortable": "haan, aaram se ho jayega",
                      "tight": "ho jayega lekin tight rahega",
                      "unsafe": "abhi safe nahi hai"}[a["verdict"]]
        hi = [f"{rs(a['purchase'])} ka payment — {verdict_hi}."]
        en = [f"{rs(a['purchase'])}: {a['verdict']}."]
        hi.append(f"Agle {a['days']} din mein bills nikalne ke baad takreeban "
                  f"{rs(a['buffer_expected'])} bachega, kam chale toh "
                  f"{rs(a['buffer_stressed'])}.")
        en.append(f"After known bills over {a['days']} days the expected buffer is "
                  f"{rs(a['buffer_expected'])}, or {rs(a['buffer_stressed'])} on a "
                  f"weak run.")
        if a.get("bills"):
            labels = ", ".join(dict.fromkeys(b["label"] for b in a["bills"]))
            hi.append(f"Isme {labels} shamil hain.")
            en.append(f"That counts {labels}.")
        if a.get("safe_after_days"):
            hi.append(f"{a['safe_after_days']} din baad safe ho jayega.")
            en.append(f"It becomes safe after about {a['safe_after_days']} days.")
        return {"hinglish": " ".join(hi), "english": " ".join(en)}

    # ------------------------------------------------------- small talk
    def _s_help(self, ev):
        return {
            "hinglish": ("Namaste! Main aapke Paytm payments dekh kar bata sakta hoon ki "
                         "dhandha kaisa chal raha hai. Poochh ke dekhiye — 'aaj kitna "
                         "business hua', 'sales kyun kam hai', ya 'mere jaise dukaanon "
                         "mein kya chal raha hai'."),
            "english": ("I read your Paytm payments and tell you how the shop is doing. "
                        "Try: 'how much today', 'why are sales down', or 'what are shops "
                        "like mine doing'."),
            "allow_ask": False,
        }

    # ---------------------------------------------------------- unknown
    def _s_unknown(self, ev):
        """Honest, and never a clarify loop.

        The old version asked whether they meant sales or money, which is a
        question the merchant has already answered by asking. If the offline
        path cannot route something, it says what it can see and stops.
        """
        h = v(ev, "get_business_health") or v(ev, "get_sales_trend")
        hi = ["Ye mere paas nahi hai — main sirf aapke payments dekh sakta hoon, "
              "isliye profit, margin, cash sale ya customer ne kya socha, ye nahi "
              "bata sakta."]
        en = ["I can't see that. I only read your Paytm payments, so profit, margins, "
              "cash sales and why a customer did something are outside what I have."]
        if h:
            daily = h.get("current_daily") or h.get("revenue_per_day")
            if daily:
                hi.append(f"Jo main bata sakta hoon: abhi roz ka {rs(daily)} chal raha "
                          f"hai. Sales, timing ya aas-paas ke dukaanon ke baare mein "
                          f"poochhiye.")
                en.append(f"What I can tell you: takings are running at {rs(daily)} a "
                          f"day. Ask me about sales, timings or similar shops.")
        else:
            hi.append("Sales, timing ya aas-paas ke dukaanon ke baare mein poochhiye.")
            en.append("Ask me about sales, timings or similar shops nearby.")
        return {"hinglish": " ".join(hi), "english": " ".join(en), "allow_ask": False}


def _action_hi(atype: str) -> str:
    return {"evening_offer": "shaam ka offer", "moderate_discount": "halka discount",
            "deep_discount": "bada discount", "price_increase": "daam badhana",
            "prep_increase": "zyada tayyari", "festive_prestock": "tyohar ki stocking",
            }.get(atype, atype.replace("_", " "))


def _proposal_hi(p: dict) -> str:
    if p["type"] == "evening_offer":
        pr = p["params"]
        win = pr.get("window", "18-21").replace("-", " se ")
        return f"Rs {pr.get('discount_rs', 10)} off, shaam {win} baje, {pr.get('days', 3)} din"
    return _action_hi(p["type"])


def _proposal_en(p: dict) -> str:
    if p["type"] == "evening_offer":
        pr = p["params"]
        return (f"Rs {pr.get('discount_rs', 10)} off, {pr.get('window', '18-21')}, "
                f"{pr.get('days', 3)} days")
    return p["type"].replace("_", " ")
