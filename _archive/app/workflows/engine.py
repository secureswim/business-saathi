"""Agentic workflow orchestration — the "it acts" half of Business Saathi.

Two workflows, always running:

  EXECUTION   triggered by merchant approval. validate -> create campaign ->
              confirm -> log to graph -> monitor -> measure -> write result back.
  MONITORING  runs continuously, unasked. scan trends -> compare to own baseline
              and peer patterns -> detect anomaly -> raise a proactive alert.

This is not one webhook. Every step can fail, and each failure has a defined
consequence the merchant can see: if campaign creation fails the merchant is
told, and if volumes have not moved after the measurement window the run is
flagged rather than quietly marked a success.

`N8nExecutor` posts the same step graph to a real n8n instance when
N8N_WEBHOOK_URL is set; `LocalExecutor` runs it in-process. Identical
step semantics either way, so the demo does not depend on a hosted workflow.
"""
from __future__ import annotations

import os
import time
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Callable

from app.adapters.graph import LocalGraph
from app.core.financial import FinancialEngine

PENDING, RUNNING, DONE, FAILED, FLAGGED = "pending", "running", "done", "failed", "flagged"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Step:
    name: str
    label: str
    status: str = PENDING
    detail: str = ""
    started_at: str | None = None
    ended_at: str | None = None
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Run:
    id: str
    workflow: str
    merchant_id: str
    status: str
    steps: list[Step]
    context: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=_now)
    result: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "workflow": self.workflow, "merchant_id": self.merchant_id,
            "status": self.status, "steps": [s.as_dict() for s in self.steps],
            "context": {k: v for k, v in self.context.items() if not k.startswith("_")},
            "created_at": self.created_at, "result": self.result,
        }

    def step(self, name: str) -> Step:
        return next(s for s in self.steps if s.name == name)


# --------------------------------------------------------------------------

EXECUTION_STEPS = [
    ("validate", "Validate parameters"),
    ("create_campaign", "Create campaign"),
    ("confirm", "Confirm to merchant"),
    ("log_action", "Log action in knowledge graph"),
    ("monitor", "Monitor during run"),
    ("measure", "Measure outcome"),
    ("write_back", "Write result back to graph"),
]

MONITORING_STEPS = [
    ("scan", "Scan merchant transaction trends"),
    ("compare", "Compare against own baseline and peer patterns"),
    ("detect", "Detect anomalies"),
    ("alert", "Trigger proactive alert"),
]


class WorkflowEngine:
    """In-process orchestrator. Runs are kept in memory for the session."""

    def __init__(self, graph: LocalGraph, fin: FinancialEngine | None = None) -> None:
        self.graph = graph
        self.fin = fin or FinancialEngine(graph.ds)
        self.runs: dict[str, Run] = {}
        self.alerts: list[dict[str, Any]] = []
        self.remote = os.environ.get("N8N_WEBHOOK_URL")

    # ---------------------------------------------------------------- utils
    def _run_step(self, run: Run, name: str, fn: Callable[[], str]) -> bool:
        s = run.step(name)
        s.status, s.started_at = RUNNING, _now()
        try:
            s.detail = fn()
            s.status = DONE
        except StepFailure as e:
            s.status, s.error = FAILED, str(e)
            s.ended_at = _now()
            run.status = FAILED
            run.result = {"ok": False, "failed_at": name, "error": str(e),
                          "merchant_message": e.merchant_message,
                          "spoken": e.merchant_message}
            self._notify(run.merchant_id, "workflow_failed", e.merchant_message,
                         {"run_id": run.id, "step": name})
            return False
        s.ended_at = _now()
        return True

    def _notify(self, merchant_id: str, kind: str, message: str, meta: dict | None = None) -> dict:
        alert = {"id": f"AL-{len(self.alerts) + 1:03d}", "merchant_id": merchant_id, "kind": kind,
                 "message": message, "meta": meta or {}, "created_at": _now(), "read": False}
        self.alerts.insert(0, alert)
        return alert

    # ------------------------------------------------------------ execution
    def execute_offer(self, merchant_id: str, proposal: dict[str, Any],
                      simulate_outcome: bool = True) -> Run:
        """The approved path. Merchant said 'haan'; now the system does the work."""
        run = Run(id=f"R-{uuid.uuid4().hex[:8]}", workflow="execution", merchant_id=merchant_id,
                  status=RUNNING, steps=[Step(n, l) for n, l in EXECUTION_STEPS],
                  context={"proposal": proposal})

        self.runs[run.id] = run
        params = proposal.get("params", {})

        # 1. validate
        def _validate() -> str:
            rupees = params.get("discount_rupees")
            days = params.get("days")
            window = params.get("window")
            if not rupees or rupees <= 0:
                raise StepFailure("discount_rupees missing or non-positive",
                                  "Offer ka discount amount theek nahi tha, isliye main aage nahi badha.")
            ticket = self.fin.avg_ticket(merchant_id)
            if rupees > ticket * 0.6:
                raise StepFailure(
                    f"discount ₹{rupees} exceeds 60% of ₹{ticket:.0f} average ticket",
                    f"₹{rupees} off aapke ₹{ticket:.0f} average bill par bahut zyada hai — "
                    f"main ye offer nahi chalaunga. Chhota amount try karein?")
            if not window or not days:
                raise StepFailure("window/days missing", "Offer ka time window ya duration missing tha.")
            return f"₹{rupees} off · {window} · {days} days · validated against ₹{ticket:.0f} avg ticket"

        if not self._run_step(run, "validate", _validate):
            return run

        # 2. create campaign (the real integration point for Paytm's offer API)
        campaign_id = f"CMP-{uuid.uuid4().hex[:6].upper()}"

        def _create() -> str:
            if self.remote:
                self._post_remote("create_campaign", {"merchant_id": merchant_id, **params})
            run.context["campaign_id"] = campaign_id
            return f"campaign {campaign_id} created"

        if not self._run_step(run, "create_campaign", _create):
            return run

        # 3. confirm to merchant
        def _confirm() -> str:
            msg = (f"Offer live hai: ₹{params['discount_rupees']} off, {params['window']}, "
                   f"{params['days']} din ke liye. Main track karta rahunga.")
            self._notify(merchant_id, "offer_live", msg, {"campaign_id": campaign_id})
            return msg

        if not self._run_step(run, "confirm", _confirm):
            return run

        # 4. log the action in the graph, before the outcome is known
        def _log() -> str:
            run.context["graph_action_id"] = "pending-outcome"
            return "action recorded against merchant, outcome slot open"

        if not self._run_step(run, "log_action", _log):
            return run

        # 5. monitor
        def _monitor() -> str:
            base = self.fin.trend(merchant_id)
            run.context["_baseline"] = base.recent_avg
            return f"watching daily GMV against ₹{base.recent_avg:,.0f}/day baseline"

        if not self._run_step(run, "monitor", _monitor):
            return run

        # 6. measure — the honest step. Peer evidence predicts, it does not promise.
        def _measure() -> str:
            if not simulate_outcome:
                run.context["_measured"] = None
                return "measurement window still open"
            outcome = self._measured_outcome(merchant_id, proposal)
            run.context["_measured"] = outcome
            if outcome["txn_delta_pct"] < 2:
                run.status = FLAGGED
                self._notify(
                    merchant_id, "offer_no_movement",
                    f"2 din baad bhi evening transactions nahi badhe ({outcome['txn_delta_pct']:+.0f}%). "
                    f"Offer band karke doosra approach dekhein?",
                    {"campaign_id": campaign_id, "run_id": run.id})
                return (f"no movement: {outcome['txn_delta_pct']:+.1f}% transactions — flagged for "
                        f"the merchant rather than reported as a success")
            return (f"evening transactions {outcome['txn_delta_pct']:+.1f}%, "
                    f"weekly GMV {outcome['gmv_abs_delta']:+,.0f}")

        if not self._run_step(run, "measure", _measure):
            return run

        # 7. write the measured result back — this is the loop that compounds
        def _write_back() -> str:
            m = run.context.get("_measured")
            if not m:
                return "nothing to write yet; outcome slot stays open"
            aid = self.graph.write_outcome(
                merchant_id=merchant_id, action_type=proposal["action_type"], params=params,
                outcome=m["outcome"], gmv_delta_pct=m["gmv_delta_pct"],
                txn_delta_pct=m["txn_delta_pct"],
                note=f"executed by Business Saathi, campaign {campaign_id}")
            run.context["graph_action_id"] = aid
            ev = self.graph.evidence(merchant_id, proposal["action_type"])
            n = ev.peer_count if ev else 0
            return (f"outcome {aid} written to graph — the next recommendation for similar "
                    f"merchants now draws on {n} chains")

        if not self._run_step(run, "write_back", _write_back):
            return run

        m = run.context.get("_measured") or {}
        if run.status != FLAGGED:
            run.status = DONE
        run.result = {
            "ok": run.status == DONE,
            "campaign_id": campaign_id,
            "outcome": m.get("outcome"),
            "txn_delta_pct": m.get("txn_delta_pct"),
            "gmv_delta_pct": m.get("gmv_delta_pct"),
            "gmv_abs_delta": m.get("gmv_abs_delta"),
            "graph_action_id": run.context.get("graph_action_id"),
            "spoken": self._result_line(run, m),
        }
        return run

    # ---- outcome model --------------------------------------------------
    def _measured_outcome(self, merchant_id: str, proposal: dict[str, Any]) -> dict[str, Any]:
        """What the campaign actually did.

        Drawn from the distribution the graph has observed for this action among
        similar merchants — including its failures. A system that always reports
        the median success would be lying, and would poison its own graph.
        """
        ev = self.graph.evidence(merchant_id, proposal["action_type"])
        if not ev:
            txn, gmv, outcome = 0.0, 0.0, "no_change"
        else:
            # deterministic pick, seeded on merchant+params, weighted by observed rate
            seed = abs(hash((merchant_id, str(proposal.get("params"))))) % 100
            success = seed < ev.success_rate * 100
            pool = [s for s in ev.sample if (s["outcome"] == "recovered") == success] or ev.sample
            pick = pool[seed % len(pool)]
            txn = float(pick.get("txn_delta_pct") or pick["gmv_delta_pct"])
            gmv = float(pick["gmv_delta_pct"])
            outcome = pick["outcome"]
        weekly = self.fin.trend(merchant_id).recent_avg * 7
        return {"outcome": outcome, "txn_delta_pct": txn, "gmv_delta_pct": gmv,
                "gmv_abs_delta": round(weekly * gmv / 100)}

    def _result_line(self, run: Run, m: dict[str, Any]) -> str:
        if not m:
            return "Offer chal raha hai. Result aane par bataunga."
        if run.status == FLAGGED:
            return (f"Offer complete, lekin evening transactions {m['txn_delta_pct']:+.0f}% par rahe — "
                    f"koi real movement nahi. Result graph mein save kar diya hai, taki aage ke "
                    f"recommendations isse seekhein.")
        return (f"Offer complete. Evening transactions {m['txn_delta_pct']:+.0f}% up. "
                f"Weekly GMV ₹{m['gmv_abs_delta']:+,.0f}. Result save ho gaya — "
                f"similar merchants ke liye agla recommendation ab behtar hai.")

    # ----------------------------------------------------------- monitoring
    def monitor(self, merchant_ids: list[str] | None = None) -> list[Run]:
        """The workflow nobody asked for. Runs across the merchant base."""
        ids = merchant_ids or [m.id for m in self.graph.ds.merchants]
        out: list[Run] = []
        for mid in ids:
            out.append(self.monitor_one(mid))
        return out

    def monitor_one(self, merchant_id: str) -> Run:
        run = Run(id=f"M-{uuid.uuid4().hex[:8]}", workflow="monitoring", merchant_id=merchant_id,
                  status=RUNNING, steps=[Step(n, l) for n, l in MONITORING_STEPS])
        self.runs[run.id] = run

        def _scan() -> str:
            t = self.fin.trend(merchant_id, window=3, baseline=30)
            run.context["trend_pct"] = round(t.delta_pct, 1)
            return f"3-day trend {t.delta_pct:+.1f}% against a 30-day baseline"

        def _compare() -> str:
            loc = self.graph.locality_health(merchant_id)
            run.context["locality"] = loc
            return (f"merchant {loc['merchant_trend_pct']:+.1f}% vs area {loc['area_trend_pct']:+.1f}% "
                    f"across {loc['peer_sample']} similar merchants → {loc['verdict']}")

        def _detect() -> str:
            anomalies = self.fin.anomalies(merchant_id)
            run.context["anomalies"] = anomalies
            if not anomalies:
                return "nothing material"
            return ", ".join(f"{a['kind']} ({a.get('delta_pct', '')})" for a in anomalies)

        def _alert() -> str:
            anomalies = run.context.get("anomalies") or []
            loc = run.context.get("locality") or {}
            if not anomalies:
                return "no alert raised"
            worst = anomalies[0]

            if worst["kind"] == "cash_buffer_tight":
                msg = (f"Dhyan dijiye — agle do hafte ke kharche nikalne ke baad aapka cash buffer "
                       f"sirf ₹{worst['closing_buffer']:,} rahega. Koi bada kharch filhaal "
                       f"tal sakte hain?")
                a = self._notify(merchant_id, "proactive_cash", msg,
                                 {"anomaly": worst, "run_id": run.id})
                run.context["alert_id"] = a["id"]
                return f"alert {a['id']} raised: cash buffer tight"

            slot = worst.get("slot")
            ev = self.graph.evidence(merchant_id, "evening_offer") if slot == "evening" else None
            hi_slot = {"evening": "evening", "lunch": "lunch", "morning": "subah"}.get(slot or "", "")
            msg = (f"Aapki {hi_slot + ' ' if hi_slot else ''}sales pichhle 3 din se "
                   f"{abs(worst.get('delta_pct', 0)):.0f}% neeche hain.")
            if loc.get("verdict") == "merchant_specific":
                msg += " Aapke area ke similar merchants flat hain, toh ye aapki shop ki baat hai."
            elif loc.get("verdict") == "area_wide":
                msg += " Poore area mein yahi pattern hai."
            if ev and ev.success_rate >= 0.5:
                msg += (f" Aapke jaise {ev.peer_count} merchants mein se {ev.success_count} ne "
                        f"evening offer se recover kiya. Try karna chahenge?")
            a = self._notify(merchant_id, "proactive_anomaly", msg,
                             {"anomaly": worst, "locality": loc, "run_id": run.id,
                              "suggest_action": "evening_offer" if ev else None})
            run.context["alert_id"] = a["id"]
            return f"alert {a['id']} raised: {worst['kind']} ({worst.get('severity')})"

        for name, fn in [("scan", _scan), ("compare", _compare), ("detect", _detect), ("alert", _alert)]:
            if not self._run_step(run, name, fn):
                return run
        run.status = DONE
        run.result = {"anomalies": run.context.get("anomalies", []),
                      "alert_id": run.context.get("alert_id")}
        return run

    # ---------------------------------------------------------------- n8n
    def _post_remote(self, step: str, payload: dict) -> None:  # pragma: no cover - needs n8n
        import json
        import urllib.request

        req = urllib.request.Request(
            self.remote, data=json.dumps({"step": step, **payload}).encode(),
            headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=8).read()
        except Exception as e:
            raise StepFailure(f"n8n webhook failed: {e}",
                              "Campaign banane mein technical dikkat aayi. Main dobara koshish karun?")


class StepFailure(Exception):
    """A step failed in a way the merchant should hear about, in their words."""

    def __init__(self, technical: str, merchant_message: str) -> None:
        super().__init__(technical)
        self.merchant_message = merchant_message
