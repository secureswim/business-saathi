"""Did the ingest actually land? Answer it properly, without truncation.

graph-summary is the least reliable thing to judge this by: it reports the last
COMPLETED pipeline run, so while a fresh cognify is still building it keeps
returning the previous run's numbers. Retrieval is the honest test -- if a
search comes back with our own card text, the data is in and usable, whatever
the summary says.

    python scripts/cognee_status.py
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import config  # noqa: E402
from backend.graph.cognee_store import CogneeClient  # noqa: E402

# Phrases that appear ONLY in our generated cards. If these come back, the
# hit is ours and not something else in the tenant.
PROBES = [
    "evening sales fell below its own baseline",
    "food stall in a college area",
    "has been tried",
]


def show(label: str, value) -> None:
    print(f"\n--- {label} ---")
    print(json.dumps(value, indent=1, default=str) if not isinstance(value, str)
          else value)


def main() -> int:
    if not config.cognee_ready():
        print("COGNEE_API_KEY / COGNEE_BASE_URL missing from .env")
        return 1

    client = CogneeClient()
    ds = client.ensure_dataset()
    print(f"dataset {client.dataset} = {ds}")

    show("graph-summary (full, untruncated)", client.graph_summary())
    show("datasets/status", client.status())

    try:
        g = client.graph()
        nodes = g.get("nodes") or g.get("data", {}).get("nodes") or []
        edges = g.get("edges") or g.get("data", {}).get("edges") or []
        print(f"\n--- graph endpoint ---\nnodes: {len(nodes)}  edges: {len(edges)}")
        for n in nodes[:5]:
            print(f"  {json.dumps(n, default=str)[:160]}")
    except Exception as exc:      # noqa: BLE001
        print(f"\n--- graph endpoint ---\n{type(exc).__name__}: {str(exc)[:200]}")

    print("\n--- retrieval (the test that matters) ---")
    landed = False
    for probe in PROBES:
        try:
            t0 = time.time()
            res = client.search(probe)
            ms = int((time.time() - t0) * 1000)
            print(f"\n  latency: {ms} ms"
                  f"{'   <-- too slow for the voice path' if ms > 2500 else ''}")
            blob = json.dumps(res, default=str)
            items = res if isinstance(res, list) else (
                res.get("results") or res.get("context") or res.get("data") or [])
            n = len(items) if isinstance(items, list) else 1
            ours = any(k in blob for k in ("verdict", "cohort", "merchant", "stall"))
            landed = landed or (n > 0 and ours)
            print(f"\n  query: {probe!r}")
            print(f"  hits: {n}   looks like our cards: {ours}")
            first = items[0] if isinstance(items, list) and items else res
            print(f"  first: {json.dumps(first, default=str)[:400]}")
        except Exception as exc:      # noqa: BLE001
            print(f"\n  query: {probe!r}\n  FAILED: {str(exc)[:250]}")

    print("\n" + "=" * 70)
    if landed:
        print("Retrieval returns our cards. The ingest landed -- you can set\n"
              "SAATHI_REAL_COGNEE=1 and restart the API.")
    else:
        print("No card text came back yet. Either cognify is still running on the\n"
              "tenant (751 texts takes a while) or the search_type is returning\n"
              "something other than chunk text. Re-run this in a few minutes\n"
              "before changing anything.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
