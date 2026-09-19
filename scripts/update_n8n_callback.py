"""Point the existing n8n campaign workflow at a hosted Business Saathi API.

Preview (read-only):
    python scripts/update_n8n_callback.py --api-url https://example.onrender.com

Apply (prompts for the hosted app's SAATHI_INTERNAL_SECRET):
    python scripts/update_n8n_callback.py --api-url https://example.onrender.com --apply

N8N_BASE_URL and N8N_API_KEY come from the local .env. This script never writes
the hosted secret to a file or prints it. It updates only HTTP Request nodes in
the existing saathi-action-execution workflow and then publishes that workflow.
"""
from __future__ import annotations

import argparse
import copy
import getpass
import sys
from pathlib import Path
from urllib.parse import urlsplit

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import config  # noqa: E402


WORKFLOW_NAME = "saathi-action-execution"
CALLBACK_PATHS = {
    "Validate": "/api/internal/validate-action",
    "Create campaign SIMULATED": "/api/internal/simulate-campaign",
    "Record action": "/api/internal/record-action",
    "Notify running": "/api/internal/workflow-node",
    "Measure outcome SIMULATED": "/api/internal/measure-outcome",
    "Graph write-back": "/api/internal/graph/writeback",
    "Notify learned": "/api/internal/workflow-node",
    "Fail callback": "/api/internal/workflow-node",
}


def api_base(value: str) -> str:
    parsed = urlsplit(value.rstrip("/"))
    if parsed.scheme != "https" or not parsed.netloc or parsed.path or parsed.query or parsed.fragment:
        raise ValueError("--api-url must be an HTTPS origin without a path or query")
    return value.rstrip("/")


def patch_workflow(workflow: dict, base: str, secret: str) -> tuple[dict, list[tuple[str, str, str]]]:
    updated = copy.deepcopy(workflow)
    found: set[str] = set()
    changes: list[tuple[str, str, str]] = []
    for node in updated.get("nodes", []):
        name = node.get("name")
        if name not in CALLBACK_PATHS:
            continue
        if name in found or node.get("type") != "n8n-nodes-base.httpRequest":
            raise ValueError(f"unexpected or duplicate HTTP node: {name}")
        found.add(name)
        params = node.get("parameters", {})
        old_url = params.get("url", "")
        if urlsplit(old_url).path != CALLBACK_PATHS[name]:
            raise ValueError(f"unexpected callback path in {name}: {urlsplit(old_url).path}")
        new_url = base + CALLBACK_PATHS[name]
        params["url"] = new_url
        params["sendHeaders"] = True
        headers = params.setdefault("headerParameters", {}).setdefault("parameters", [])
        matches = [h for h in headers if h.get("name", "").lower() == "x-saathi-secret"]
        if len(matches) > 1:
            raise ValueError(f"duplicate X-Saathi-Secret headers in {name}")
        if matches:
            matches[0]["value"] = secret
        else:
            headers.append({"name": "X-Saathi-Secret", "value": secret})
        changes.append((name, old_url, new_url))
    missing = CALLBACK_PATHS.keys() - found
    if missing:
        raise ValueError(f"workflow is missing HTTP nodes: {', '.join(sorted(missing))}")
    return updated, changes


def find_workflow(client: httpx.Client, base: str, workflow_id: str | None) -> dict:
    if workflow_id:
        result = client.get(f"{base}/workflows/{workflow_id}")
        result.raise_for_status()
        workflow = result.json()
        if workflow.get("name") != WORKFLOW_NAME:
            raise ValueError(f"workflow {workflow_id} is not {WORKFLOW_NAME}")
        return workflow

    cursor = None
    matches = []
    while True:
        params = {"limit": 100}
        if cursor:
            params["cursor"] = cursor
        result = client.get(f"{base}/workflows", params=params)
        result.raise_for_status()
        page = result.json()
        matches.extend(w for w in page.get("data", []) if w.get("name") == WORKFLOW_NAME)
        cursor = page.get("nextCursor")
        if not cursor:
            break
    if len(matches) != 1:
        raise ValueError(f"expected one {WORKFLOW_NAME} workflow; found {len(matches)}")
    result = client.get(f"{base}/workflows/{matches[0]['id']}")
    result.raise_for_status()
    return result.json()


def check_render(client: httpx.Client, base: str, secret: str) -> None:
    health = client.get(f"{base}/api/health")
    health.raise_for_status()
    if not health.json().get("ok"):
        raise ValueError("Render /api/health did not report ok=true")
    check = client.get(
        f"{base}/api/internal/monitor/candidates",
        params={"limit": 1}, headers={"X-Saathi-Secret": secret},
    )
    check.raise_for_status()
    if not isinstance(check.json().get("merchant_ids"), list):
        raise ValueError("Render did not return a valid authenticated callback response")


def publish(client: httpx.Client, base: str, workflow_id: str) -> None:
    result = client.post(f"{base}/workflows/{workflow_id}/publish", json={})
    if result.status_code in (404, 405):
        result = client.post(f"{base}/workflows/{workflow_id}/activate", json={})
    result.raise_for_status()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-url", required=True, help="Hosted Business Saathi HTTPS origin")
    parser.add_argument("--workflow-id", help="Use if n8n has more than one workflow with this name")
    parser.add_argument("--apply", action="store_true", help="Update and publish the n8n workflow")
    args = parser.parse_args()
    try:
        target = api_base(args.api_url)
        if not config.N8N_BASE_URL.startswith("https://") or not config.N8N_API_KEY:
            raise ValueError("set N8N_BASE_URL and N8N_API_KEY in .env first")
        n8n_api = config.N8N_BASE_URL + "/api/v1"
        with httpx.Client(timeout=30.0, headers={"X-N8N-API-KEY": config.N8N_API_KEY}) as n8n:
            workflow = find_workflow(n8n, n8n_api, args.workflow_id)
            preview, changes = patch_workflow(workflow, target, "<hidden>")
            print(f"Workflow: {workflow['name']} ({workflow['id']})")
            for name, old_url, new_url in changes:
                print(f"  {name}: {old_url} -> {new_url}")
            if not args.apply:
                print("Preview only. Add --apply to verify the hosted secret, update, and publish.")
                return 0

            secret = getpass.getpass("Render SAATHI_INTERNAL_SECRET (hidden): ").strip()
            if not secret:
                raise ValueError("Render SAATHI_INTERNAL_SECRET cannot be empty")
            with httpx.Client(timeout=30.0) as render:
                check_render(render, target, secret)
            updated, _ = patch_workflow(workflow, target, secret)
            body = {key: updated[key] for key in ("name", "nodes", "connections", "settings")}
            result = n8n.put(f"{n8n_api}/workflows/{workflow['id']}", json=body)
            result.raise_for_status()
            publish(n8n, n8n_api, workflow["id"])
            saved = find_workflow(n8n, n8n_api, workflow["id"])
            patch_workflow(saved, target, secret)  # validates all expected nodes still exist
            for node in saved["nodes"]:
                if node["name"] in CALLBACK_PATHS:
                    params = node["parameters"]
                    if params["url"] != target + CALLBACK_PATHS[node["name"]]:
                        raise ValueError(f"n8n did not save the URL for {node['name']}")
                    headers = params["headerParameters"]["parameters"]
                    if not any(h.get("name", "").lower() == "x-saathi-secret" and
                               h.get("value") == secret for h in headers):
                        raise ValueError(f"n8n did not save the secret for {node['name']}")
            print(f"Updated and published {len(changes)} callback nodes; verified URLs and headers.")
            print("Reset the demo in /ops, then create and approve a new campaign.")
            return 0
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        if isinstance(exc, httpx.HTTPStatusError):
            print(f"Failed: HTTP {exc.response.status_code} at {exc.request.url}", file=sys.stderr)
        else:
            print(f"Failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
