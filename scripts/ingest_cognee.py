"""Mirror the ledger into Cognee.

Runs once after generation and incrementally on write-back. Takes minutes, not
seconds, so do not put it in the demo path.

Usage:  python scripts/ingest_cognee.py [--dry-run] [--append]

REPLACES the dataset by default. Cognee's add_text only appends, so after a
regeneration -- new merchants, re-measured outcomes, a changed cohort key --
appending would leave the previous version's cards in the graph to be
retrieved alongside the new ones. Two contradictory answers to "what worked
for merchants like you" is worse than not using Cognee at all. Pass --append
only if you know the ledger has not changed.

Running this script IS the intent, so it does not check SAATHI_REAL_COGNEE --
that flag governs whether the live demo reads from Cognee, which is a separate
decision you should make only after this ingest has finished. All it needs is
COGNEE_API_KEY and COGNEE_BASE_URL in .env.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import config  # noqa: E402
from backend.data import db  # noqa: E402
from backend.graph import cards  # noqa: E402


def main() -> int:
    dry = "--dry-run" in sys.argv
    replace = "--append" not in sys.argv
    payload = cards.all_cards()
    fingerprint = cards.fingerprint(payload)
    ingested = db.meta_get("cognee_fingerprint")
    print(f"  data fingerprint    {fingerprint}")
    if ingested:
        print(f"  cognee holds        {ingested}"
              f"{'  (already current)' if ingested == fingerprint else '  (STALE)'}")
    print(f"  merchant profiles   {len(payload['profiles']):>6,}")
    print(f"  experience cards    {len(payload['experiences']):>6,}")
    print(f"  cohort patterns     {len(payload['patterns']):>6,}")
    below = payload.get("patterns_below_floor", 0)
    if below:
        print(f"  ...plus {below:,} pattern(s) NOT ingested: fewer than "
              f"{config.MIN_ATTEMPTS_TO_RECOMMEND} attempts, so the cohort sentence "
              f"would describe one merchant rather than a pattern")

    if dry:
        print("\n--- sample experience card ---")
        print(payload["experiences"][0]["text"])
        print(json.dumps(payload["experiences"][0]["metadata"], indent=1))
        print("\n--- sample cohort pattern card ---")
        if payload["patterns"]:
            print(payload["patterns"][0]["text"])
        print("\ndry run: nothing was sent to Cognee")
        return 0

    if not config.cognee_ready():
        print("\nCOGNEE_API_KEY / COGNEE_BASE_URL are missing from .env. "
              "Use --dry-run to inspect the cards without a key.")
        return 1

    from backend.graph.cognee_store import CogneeClient, CogneeGraph
    print("\ningesting into Cognee (this takes a few minutes)...")
    if replace:
        print("  replacing the dataset; pass --append to add without dropping")
    result = CogneeGraph().ingest_all(replace=replace)
    print(json.dumps(result, indent=1))
    db.meta_set("cognee_fingerprint", fingerprint)
    db.meta_set("cognee_ingested_at", time.strftime("%Y-%m-%dT%H:%M:%S"))

    # cognify runs on the tenant after we return, so poll instead of leaving
    # you to guess whether the graph actually built.
    print("\nwaiting for the tenant to build the graph...")
    client = CogneeClient()
    for attempt in range(20):
        time.sleep(15)
        summary = client.graph_summary() or {}
        nodes = summary.get("numNodes") or summary.get("num_nodes") or 0
        print(f"  +{(attempt + 1) * 15:>4}s  {json.dumps(summary)[:120]}")
        if nodes:
            print(f"\ngraph built: {nodes} nodes. "
                  f"Now set SAATHI_REAL_COGNEE=1 in .env and restart the API.")
            return 0
    print("\nStill empty after 5 minutes. The texts are accepted either way -- "
          "re-run `python scripts/check_integrations.py cognee` later to check.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
