"""Exercise every user-visible flow against a running API.

This is intentionally live: it records planner/provider choice, validates the
privacy boundary, lets n8n own its full asynchronous learning loop, checks
conversation memory, alerts, rejection, and reset, then restores seed data.
"""
from __future__ import annotations

import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import config  # noqa: E402

BASE = f"http://127.0.0.1:{config.PORT}"
ID_RE = re.compile(r"\bM\d{3,}\b", re.I)
CASES = [
    ("M001", "Pichli baar offer ka result kya hua?", "action_status"),
    ("M001", "30% discount doon?", "risk_check"),
    ("M001", "Agar main offer ka time ghata doon toh kya hoga?", "what_if"),
    ("M001", "Bhai iss hafte sales kyun kam hain?", "sales_diagnosis"),
    ("M001", "Mera business kaisa chal raha hai?", "business_health"),
    ("M001", "Aaj kuch unusual hai kya?", "anomaly_check"),
    ("M001", "Agle hafte demand kaisi rahegi?", "demand_forecast"),
    ("M001", "Mere jaise shops mein kya chal raha hai?", "peer_insight"),
    ("M001", "Sabse zyada sales kis time hoti hai?", "time_pattern"),
    ("M001", "Agle hafte ke liye kya prepare karun?", "planning"),
    ("M001", "Paisa theek rahega agle hafte?", "money_check"),
    ("M001", "Aaj mausam kaisa hai?", "unknown"),
    (config.COLDSTART_MERCHANT, "Business kaisa chalega?", "cold_start"),
]


def request(method: str, path: str, body=None, timeout=60):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read() or b"{}")


def get(path: str):
    return request("GET", path)


def post(path: str, body=None, timeout=60):
    return request("POST", path, body, timeout)


def find_run(run_id: str):
    payload = get("/api/runs")
    runs = payload.get("runs", payload if isinstance(payload, list) else [])
    return next((run for run in runs if run.get("run_id") == run_id), None)


def main() -> int:
    failures = []
    print(f"Full-flow check: {BASE}")
    health = get("/api/health")
    print("Adapters:", json.dumps(health.get("adapters", {}), sort_keys=True))
    post("/api/admin/reset")

    print("\nAll intent and synthesis paths")
    for merchant, question, expected in CASES:
        started = time.perf_counter()
        result = post("/api/query", {"merchant_id": merchant, "text": question,
                                     "conversation_id": "full-flow"})
        elapsed = time.perf_counter() - started
        answer = result.get("answer", {})
        actual = result.get("intent")
        validator = answer.get("validator", {})
        leaked = ID_RE.findall((answer.get("hinglish") or "") + " "
                               + (answer.get("english") or ""))
        ok = actual == expected and validator.get("passed") and not leaked
        if not ok:
            failures.append(f"{expected}: got={actual}, validator={validator}, ids={leaked}")
        print(f"  {'OK' if ok else 'FAIL':4} {expected:18} {elapsed:5.1f}s  "
              f"planner={result.get('planning', {}).get('method')}  "
              f"reasoner={answer.get('reasoner')}")

    print("\nConversation memory")
    first = post("/api/query", {"merchant_id": "M001", "text": "Sales kyun kam hain?",
                                "conversation_id": "memory-check"})
    second = post("/api/query", {"merchant_id": "M001", "text": "Aur peers ka kya?",
                                 "conversation_id": "memory-check"})
    memory_ok = second.get("planning", {}).get("memory_turns") == 1
    print(f"  {'OK' if memory_ok else 'FAIL':4} second turn sees "
          f"{second.get('planning', {}).get('memory_turns')} prior turn")
    if not memory_ok:
        failures.append("conversation memory did not retain one prior turn")

    print("\nAction rejection")
    rejected = post("/api/query", {"merchant_id": "M001", "text": "Offer bana de"})
    reject_id = rejected.get("run_id")
    reject_result = post(f"/api/action/{reject_id}/reject") if reject_id else {}
    reject_ok = reject_result.get("state") == "rejected"
    print(f"  {'OK' if reject_ok else 'FAIL':4} run={reject_id} state={reject_result.get('state')}")
    if not reject_ok:
        failures.append("rejection flow failed")

    print("\nn8n approval -> measurement -> graph learning")
    before = get(f"/api/cohort/{config.PEER_MERCHANT}")
    proposed = post("/api/query", {"merchant_id": "M001", "text": "Offer bana de"})
    run_id = proposed.get("run_id")
    approved = post(f"/api/action/{run_id}/approve", timeout=30) if run_id else {}
    print(f"  accepted run={run_id} state={approved.get('state')} "
          f"orchestrator={approved.get('orchestrator')}")
    deadline = time.time() + max(95, config.SIM_CLOCK_SECONDS_PER_DAY * 3 + 35)
    state, learned = approved.get("state"), None
    while run_id and time.time() < deadline:
        learned = find_run(run_id)
        state = (learned or {}).get("state", state)
        print(f"  poll state={state}", end="\r", flush=True)
        if state in {"learned", "failed", "outcome_unavailable"}:
            break
        time.sleep(3)
    print(f"  final state={state}{' ' * 20}")
    after = get(f"/api/cohort/{config.PEER_MERCHANT}")
    learning_ok = (state == "learned"
                   and after.get("tried") == before.get("tried", 0) + 1
                   and bool((learned or {}).get("outcome")))
    print(f"  {'OK' if learning_ok else 'FAIL':4} cohort tried "
          f"{before.get('tried')} -> {after.get('tried')}")
    if not learning_ok:
        failures.append(f"n8n learning flow ended in {state}; cohort {before} -> {after}")

    print("\nProactive monitoring")
    alerts = post("/api/admin/trigger-alert")
    alert_ok = alerts.get("count", 0) > 0
    print(f"  {'OK' if alert_ok else 'FAIL':4} {alerts.get('count', 0)} alert(s)")
    if not alert_ok:
        failures.append("proactive scan produced no alerts")

    print("\nReset")
    reset = post("/api/admin/reset")
    restored = get(f"/api/cohort/{config.PEER_MERCHANT}")
    reset_ok = reset.get("ok") and restored.get("tried") == before.get("tried")
    print(f"  {'OK' if reset_ok else 'FAIL':4} restored tried={restored.get('tried')}")
    if not reset_ok:
        failures.append("reset did not restore the seed cohort")

    if failures:
        print("\nFAILURES")
        for item in failures:
            print(" -", item)
        return 1
    print("\nALL FLOWS PASSED")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except urllib.error.HTTPError as exc:
        print(f"HTTP {exc.code}: {exc.read().decode(errors='replace')}")
        raise
