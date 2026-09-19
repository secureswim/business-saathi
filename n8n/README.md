# n8n workflows

Three workflows. Import each JSON through the n8n UI (Workflows → Import from
file), then activate. The API must be running on port 8000.

    docker compose -f n8n/docker-compose.yml up -d
    open http://localhost:5678

Then turn the adapter on:

    SAATHI_REAL_N8N=1 python -m uvicorn backend.api.main:app --port 8000

| File | Trigger | What it does |
| --- | --- | --- |
| `action_execution.json` | webhook `POST /webhook/saathi/execute` | validate → simulated campaign → record → notify → wait → hand to workflow 3 |
| `proactive_monitoring.json` | schedule, every 5 minutes | candidates → batch → evaluate → filter on four conditions → playbook → raise alert |
| `outcome_learning.json` | webhook `POST /webhook/saathi/measure` | measure → classify → record → graph write-back → notify |

Every node posts progress to `POST /api/internal/workflow-node`, which is what
the `/ops` workflow column renders live.

**If n8n is unavailable** the app falls back to `backend/actions/orchestrator.py`,
which implements the identical transitions and emits the identical events.
Rehearse both paths.
