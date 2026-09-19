"""Generate the three n8n workflows with your tunnel URL baked in, and push them.

n8n Cloud cannot reach 127.0.0.1, so the workflows call this API through a
public tunnel. A `trycloudflare.com` URL changes every time cloudflared
restarts, which is exactly the kind of thing that breaks a demo an hour before
it starts. So the workflows are GENERATED from PUBLIC_API_URL rather than
hand-edited: when the tunnel changes, re-run this and re-push.

    python scripts/build_n8n_workflows.py            # write the JSONs
    python scripts/build_n8n_workflows.py --push     # write and upload to n8n
    python scripts/build_n8n_workflows.py --push --activate

Without --push, import n8n/workflows/*.json through the n8n UI by hand.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import config  # noqa: E402

OUT = ROOT / "n8n" / "workflows"


def http(name, path, body=None, method="POST", retries=2, x=0, y=0,
         on_error="continueErrorOutput"):
    """An HTTP node with the API URL and secret baked in."""
    node = {
        "parameters": {
            "method": method,
            "url": f"{config.PUBLIC_API_URL}{path}",
            "sendHeaders": True,
            "headerParameters": {"parameters": [
                {"name": "X-Saathi-Secret", "value": config.INTERNAL_SECRET}]},
            "options": {"timeout": 20000,
                        "retry": {"retry": {"maxTries": retries,
                                            "waitBetweenTries": 2000}}},
        },
        "id": name.lower().replace(" ", "-").replace("(", "").replace(")", ""),
        "name": name,
        "type": "n8n-nodes-base.httpRequest",
        "typeVersion": 4.2,
        "position": [x, y],
        "onError": on_error,
    }
    if body is not None:
        node["parameters"].update({"sendBody": True, "specifyBody": "json",
                                   "jsonBody": body})
    return node


def node(name, ntype, params, x, y, tv=1):
    return {"parameters": params, "id": name.lower().replace(" ", "-"), "name": name,
            "type": ntype, "typeVersion": tv, "position": [x, y]}


def conn(pairs):
    out = {}
    for src, groups in pairs:
        out[src] = {"main": [[{"node": d, "type": "main", "index": 0} for d in g]
                             for g in groups]}
    return out


def bool_if(name, expr, x, y):
    return node(name, "n8n-nodes-base.if",
                {"conditions": {"options": {"caseSensitive": True, "version": 2},
                                "conditions": [{"leftValue": expr, "rightValue": True,
                                                "operator": {"type": "boolean",
                                                             "operation": "true"}}],
                                "combinator": "and"}}, x, y, 2)


# ---------------------------------------------------------------- workflow 1
def action_execution():
    nodes = [
        node("Webhook", "n8n-nodes-base.webhook",
             {"httpMethod": "POST", "path": "saathi/execute",
              # Acknowledge the hand-off immediately. The workflow contains a
              # simulated multi-day wait and must continue asynchronously.
              "responseMode": "onReceived"}, -600, 300, 2),
        node("Normalise", "n8n-nodes-base.code",
             {"jsCode": (
                 "// idempotency key: a retried webhook must not create two campaigns\n"
                 "const b = $input.first().json.body || $input.first().json;\n"
                 "return [{json: {run_id: b.run_id, merchant_id: b.merchant_id,\n"
                 "  action_type: b.action_type, params: b.params || {},\n"
                 "  situation_id: b.situation_id ?? null,\n"
                 "  idempotency_key: b.run_id}}];")}, -400, 300, 2),
        http("Validate", "/api/internal/validate-action",
             '={"run_id": {{ JSON.stringify($json.run_id) }}}', x=-200, y=300),
        bool_if("Valid?", "={{ $json.ok }}", 0, 300),
        http("Create campaign SIMULATED", "/api/internal/simulate-campaign",
             '={"run_id": {{ JSON.stringify($(\'Normalise\').item.json.run_id) }}}',
             retries=3, x=200, y=200),
        http("Record action", "/api/internal/record-action",
             '={"run_id": {{ JSON.stringify($(\'Normalise\').item.json.run_id) }}}',
             x=400, y=200),
        http("Notify running", "/api/internal/workflow-node",
             '={"run_id": {{ JSON.stringify($(\'Normalise\').item.json.run_id) }},'
             ' "node": "campaign running", "status": "success", "simulated": true}',
             retries=1, x=600, y=200, on_error="continueRegularOutput"),
        node("Wait for the campaign window", "n8n-nodes-base.wait",
             {"amount": config.SIM_CLOCK_SECONDS_PER_DAY * 3, "unit": "seconds"},
             800, 200, 1.1),
        http("Measure outcome SIMULATED", "/api/internal/measure-outcome",
             '={"run_id": {{ JSON.stringify($(\'Normalise\').item.json.run_id) }}}',
             x=1000, y=200),
        http("Graph write-back", "/api/internal/graph/writeback",
             '={"run_id": {{ JSON.stringify($(\'Normalise\').item.json.run_id) }}}',
             retries=3, x=1200, y=200),
        http("Notify learned", "/api/internal/workflow-node",
             '={"run_id": {{ JSON.stringify($(\'Normalise\').item.json.run_id) }},'
             ' "node": "learning complete", "status": "success"}',
             retries=1, x=1400, y=200, on_error="continueRegularOutput"),
        http("Fail callback", "/api/internal/workflow-node",
             '={"run_id": {{ JSON.stringify($(\'Normalise\').item.json.run_id) }},'
             ' "node": "workflow", "status": "failed",'
             ' "detail": {{ JSON.stringify($json.reason || "step failed") }}}',
             retries=1, x=200, y=460, on_error="continueRegularOutput"),
    ]
    return {"name": "saathi-action-execution", "nodes": nodes,
            "connections": conn([
                ("Webhook", [["Normalise"]]),
                ("Normalise", [["Validate"]]),
                ("Validate", [["Valid?"], ["Fail callback"]]),
                ("Valid?", [["Create campaign SIMULATED"], ["Fail callback"]]),
                ("Create campaign SIMULATED", [["Record action"], ["Fail callback"]]),
                ("Record action", [["Notify running"], ["Fail callback"]]),
                ("Notify running", [["Wait for the campaign window"]]),
                ("Wait for the campaign window", [["Measure outcome SIMULATED"]]),
                ("Measure outcome SIMULATED", [["Graph write-back"], ["Fail callback"]]),
                ("Graph write-back", [["Notify learned"], ["Fail callback"]]),
            ]),
            "settings": {"executionOrder": "v1"}}


# ---------------------------------------------------------------- workflow 2
def proactive_monitoring():
    nodes = [
        node("Every 5 minutes", "n8n-nodes-base.scheduleTrigger",
             {"rule": {"interval": [{"field": "minutes", "minutesInterval": 5}]}},
             -600, 300, 1.2),
        http("Candidates", "/api/internal/monitor/candidates?limit=20",
             method="GET", retries=1, x=-400, y=300),
        http("Evaluate", "/api/internal/monitor/evaluate",
             '={"merchant_ids": {{ JSON.stringify($json.merchant_ids || []) }}}',
             x=-200, y=300),
        node("Split evaluations", "n8n-nodes-base.splitOut",
             {"fieldToSplitOut": "evaluations", "options": {}}, 0, 300, 1),
        node("All four conditions met?", "n8n-nodes-base.filter",
             {"conditions": {"options": {"caseSensitive": True, "version": 2},
                             "conditions": [{"leftValue": "={{ $json.all_met }}",
                                             "rightValue": True,
                                             "operator": {"type": "boolean",
                                                          "operation": "true"}}],
                             "combinator": "and"}}, 200, 300, 2),
        http("Peer playbook", "/api/internal/peer-playbook",
             '={"merchant_id": {{ JSON.stringify($json.merchant_id) }},'
             ' "situation_kind": "evening_decline"}',
             retries=1, x=400, y=300, on_error="continueRegularOutput"),
        http("Raise alert", "/api/internal/alerts",
             '={"merchant_id": {{ JSON.stringify($(\'All four conditions met?\').item.json.merchant_id) }},'
             ' "alert_type": {{ JSON.stringify($(\'All four conditions met?\').item.json.alert_type) }},'
             ' "severity": {{ $(\'All four conditions met?\').item.json.trend.change_pct }},'
             ' "conditions": {{ JSON.stringify($(\'All four conditions met?\').item.json.conditions) }},'
             ' "hinglish": "", "english": ""}', x=600, y=300),
    ]
    return {"name": "saathi-proactive-monitoring", "nodes": nodes,
            "connections": conn([
                ("Every 5 minutes", [["Candidates"]]),
                ("Candidates", [["Evaluate"]]),
                ("Evaluate", [["Split evaluations"]]),
                ("Split evaluations", [["All four conditions met?"]]),
                ("All four conditions met?", [["Peer playbook"]]),
                ("Peer playbook", [["Raise alert"]]),
            ]),
            "settings": {"executionOrder": "v1"}}


# ---------------------------------------------------------------- workflow 3
def outcome_learning():
    nodes = [
        node("Measure webhook", "n8n-nodes-base.webhook",
             {"httpMethod": "POST", "path": "saathi/measure",
              "responseMode": "lastNode"}, -600, 300, 2),
        node("Read run", "n8n-nodes-base.code",
             {"jsCode": ("const b = $input.first().json.body || $input.first().json;\n"
                         "return [{json: {run_id: b.run_id, force: b.force ?? null}}];")},
             -400, 300, 2),
        http("Measure outcome SIMULATED", "/api/internal/measure-outcome",
             '={"run_id": {{ JSON.stringify($json.run_id) }},'
             ' "force": {{ JSON.stringify($json.force) }}}', x=-200, y=300),
        node("Classify", "n8n-nodes-base.code",
             {"jsCode": (
                 "// the API classifies; this node makes the rule visible on the canvas\n"
                 "const d = $json;\n"
                 "const verdict = d.delta_pct > 8 ? 'recovered'\n"
                 "  : (d.delta_pct > -5 ? 'no_change' : 'worse');\n"
                 "return [{json: {...d, verdict_check: verdict,\n"
                 "  run_id: $('Read run').item.json.run_id}}];")}, 0, 300, 2),
        http("Graph write-back", "/api/internal/graph/writeback",
             '={"run_id": {{ JSON.stringify($json.run_id) }}}', retries=3, x=200, y=300),
        http("Notify learned", "/api/internal/workflow-node",
             '={"run_id": {{ JSON.stringify($(\'Classify\').item.json.run_id) }},'
             ' "node": "learning complete", "status": "success"}',
             retries=1, x=400, y=300, on_error="continueRegularOutput"),
        http("Outcome unavailable", "/api/internal/workflow-node",
             '={"run_id": {{ JSON.stringify($(\'Read run\').item.json.run_id) }},'
             ' "node": "measure", "status": "failed",'
             ' "detail": "outcome could not be measured"}',
             retries=1, x=0, y=460, on_error="continueRegularOutput"),
    ]
    return {"name": "saathi-outcome-learning", "nodes": nodes,
            "connections": conn([
                ("Measure webhook", [["Read run"]]),
                ("Read run", [["Measure outcome SIMULATED"]]),
                ("Measure outcome SIMULATED", [["Classify"], ["Outcome unavailable"]]),
                ("Classify", [["Graph write-back"]]),
                ("Graph write-back", [["Notify learned"], ["Outcome unavailable"]]),
            ]),
            "settings": {"executionOrder": "v1"}}


WORKFLOWS = {"action_execution": action_execution,
             "proactive_monitoring": proactive_monitoring,
             "outcome_learning": outcome_learning}


def push(wf: dict) -> None:
    import httpx
    headers = {"X-N8N-API-KEY": config.N8N_API_KEY, "Content-Type": "application/json"}
    base = f"{config.N8N_BASE_URL}/api/v1"
    body = {k: wf[k] for k in ("name", "nodes", "connections", "settings")}

    existing = httpx.get(f"{base}/workflows", headers=headers, timeout=30.0)
    existing.raise_for_status()
    match = next((w for w in (existing.json().get("data") or [])
                  if w.get("name") == wf["name"]), None)

    if match:
        r = httpx.put(f"{base}/workflows/{match['id']}", json=body, headers=headers,
                      timeout=30.0)
        action = "updated"
    else:
        r = httpx.post(f"{base}/workflows", json=body, headers=headers, timeout=30.0)
        action = "created"
    if r.status_code >= 400:
        print(f"  FAIL {wf['name']}: {r.status_code} {r.text[:300]}")
        return
    wid = r.json().get("id") or (match or {}).get("id")
    print(f"  {action}: {wf['name']}  (id {wid})")

    if "--activate" in sys.argv and wid:
        a = httpx.post(f"{base}/workflows/{wid}/activate", headers=headers, timeout=30.0)
        print(f"    activate -> {a.status_code}")


def main() -> int:
    if not config.PUBLIC_API_URL:
        print("\nPUBLIC_API_URL is not set in .env.\n"
              "n8n Cloud cannot reach 127.0.0.1, so the workflows need your tunnel:\n"
              "    cloudflared tunnel --url http://localhost:8000\n"
              "then put the https://...trycloudflare.com URL in .env as PUBLIC_API_URL.\n")
        return 1

    OUT.mkdir(parents=True, exist_ok=True)
    print(f"\nAPI URL baked into the workflows: {config.PUBLIC_API_URL}")
    built = {}
    for name, fn in WORKFLOWS.items():
        wf = fn()
        (OUT / f"{name}.json").write_text(json.dumps(wf, indent=2))
        built[name] = wf
        print(f"  wrote n8n/workflows/{name}.json  ({len(wf['nodes'])} nodes)")

    if "--push" in sys.argv:
        if not config.N8N_API_KEY:
            print("\nN8N_API_KEY is not set; import the JSONs through the n8n UI instead.")
            return 1
        print(f"\npushing to {config.N8N_BASE_URL}")
        for wf in built.values():
            push(wf)

    print("\nWebhook URLs once the workflows are active:")
    print(f"  {config.N8N_BASE_URL}/webhook/saathi/execute")
    print(f"  {config.N8N_BASE_URL}/webhook/saathi/measure\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
