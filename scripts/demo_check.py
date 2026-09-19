"""The pre-demo gate. If this is not all green, the demo is not ready.

Usage:  python scripts/demo_check.py   (start the API first)
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import config  # noqa: E402

BASE = f"http://127.0.0.1:{config.PORT}"
QUESTIONS = [
    ("M001", "Bhai iss hafte sales kyun kam hain?", "sales_diagnosis"),
    ("M001", "mere jaise shops mein kya chal raha hai", "peer_insight"),
    ("M001", "agle hafte ke liye kya prepare karun", "planning"),
    ("M001", "30% discount doon?", "risk_check"),
    ("M001", "offer bana de", "action_request"),
    ("M001", "paisa theek rahega agle hafte", "money_check"),
    (config.COLDSTART_MERCHANT, "business kaisa chalega", "cold_start"),
]
OK, BAD = "  ok  ", " FAIL "
fails = 0


def check(label, value, passed):
    global fails
    if not passed:
        fails += 1
    print(f"{OK if passed else BAD} {label:<44} {value}")


def post(path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method="POST",
                                 headers={"content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read() or b"{}")


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=30) as r:
        return json.loads(r.read() or b"{}")


def main() -> int:
    print(f"\nchecking {BASE}\n")
    try:
        h = get("/api/health")
    except urllib.error.URLError:
        print(f" FAIL  API not reachable at {BASE}. Start it with ./run.sh first.\n")
        return 1

    check("api health", "ok" if h["ok"] else h, h["ok"])
    for k, v in h["adapters"].items():
        print(f"       adapter {k:<14} {v}")
    print()

    print("  claim verification")
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "verify_claims.py")],
                       capture_output=True, text=True)
    check("verify_claims.py", "all claims reproduce" if r.returncode == 0
          else "SEE OUTPUT", r.returncode == 0)
    if r.returncode:
        print(r.stdout[-2000:])
    print()

    print("  the seven demo questions")
    slow = 0
    for mid, q, expected in QUESTIONS:
        t0 = time.time()
        res = post("/api/query", {"merchant_id": mid, "text": q})
        ms = int((time.time() - t0) * 1000)
        ok = res["intent"] == expected and res["answer"]["validator"]["passed"]
        if ms > 12000:
            slow += 1
        check(f"{q[:38]:<38}", f"{res['intent']:<16} {ms:>5}ms", ok)
    check("all answers under 12s", f"{slow} over budget", slow == 0)
    print()

    print("  the learning loop")
    post("/api/admin/reset")                     # start from the known state
    baseline = get(f"/api/cohort/{config.PEER_MERCHANT}")
    res = post("/api/query", {"merchant_id": "M001", "text": "offer bana de"})
    run_id = res.get("run_id")
    check("action proposed", run_id or "none", bool(run_id))
    before = get(f"/api/cohort/{config.PEER_MERCHANT}")
    approved = post(f"/api/action/{run_id}/approve")
    if approved.get("orchestrator") == "n8n":
        # n8n owns measurement and graph write-back. Wait for its configured
        # campaign window instead of racing it with an admin measurement.
        deadline = time.time() + max(95, config.SIM_CLOCK_SECONDS_PER_DAY * 3 + 35)
        state = approved.get("state")
        while time.time() < deadline and state not in (
                "learned", "failed", "outcome_unavailable"):
            time.sleep(3)
            runs = get("/api/runs").get("runs", [])
            current = next((r for r in runs if r.get("run_id") == run_id), {})
            state = current.get("state", state)
        check("n8n completed learning", state, state == "learned")
    else:
        post(f"/api/admin/measure?run_id={run_id}&force=recovered")
    after = get(f"/api/cohort/{config.PEER_MERCHANT}")
    check("cohort tried increased", f"{before['tried']} -> {after['tried']}",
          after["tried"] == before["tried"] + 1)
    check("cohort worked increased", f"{before['worked']} -> {after['worked']}",
          after["worked"] == before["worked"] + 1)
    print()

    print("  proactive monitoring")
    alerts = post("/api/admin/trigger-alert")
    check("alert scan raises something", f"{alerts['count']} alert(s)",
          alerts["count"] > 0)
    print()

    print("  presenter controls")
    t0 = time.time()
    post("/api/admin/reset")
    ms = int((time.time() - t0) * 1000)
    check("reset under 2s", f"{ms}ms", ms < 2000)
    restored = get(f"/api/cohort/{config.PEER_MERCHANT}")
    check("reset restored the known state",
          f"{after['tried']} -> {restored['tried']} (seed {baseline['tried']})",
          restored["tried"] == baseline["tried"])

    print(f"\n{'READY' if not fails else str(fails) + ' CHECK(S) FAILED'}\n")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
