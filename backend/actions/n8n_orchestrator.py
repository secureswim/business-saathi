"""The real orchestrator: hands the run to n8n and lets the workflow drive.

n8n calls back into /api/internal/* for every step, so the transitions and the
events are identical to the local implementation. If n8n is unreachable this
falls back to the local state machine rather than failing the demo, and says so.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import config  # noqa: E402
from backend import health  # noqa: E402
from backend.actions.orchestrator import LocalOrchestrator, Orchestrator  # noqa: E402
from backend.models.events import event  # noqa: E402


class N8nOrchestrator(Orchestrator):
    name = "n8n"

    def __init__(self):
        self._fallback = LocalOrchestrator()

    def _post(self, path: str, payload: dict, timeout: float = 8.0) -> dict:
        import httpx
        r = httpx.post(f"{config.N8N_BASE_URL}{path}", json=payload, timeout=timeout,
                       headers={"X-Saathi-Secret": config.N8N_WEBHOOK_SECRET})
        r.raise_for_status()
        return r.json() if r.content else {}

    def execute(self, run, emit):
        try:
            emit(event("workflow_started", None, run_id=run.run_id,
                       orchestrator=self.name, workflow="action_execution"))
            run.orchestrator = self.name
            # Mark the hand-off before calling n8n. Its callbacks can arrive while
            # this request is still in flight; advancing afterwards could regress
            # an already-running action back to ``validating``.
            run.advance("validating", "handed to n8n workflow action_execution")
            self._post("/webhook/saathi/execute", {
                "run_id": run.run_id, "merchant_id": run.merchant_id,
                "action_type": run.type, "params": run.params,
                "situation_id": run.situation_id,
                # n8n CLOUD runs on their servers: host.docker.internal is a
                # self-hosted-in-Docker address and resolves to nothing there.
                # The public tunnel is the only way back to this API.
                "callback_url": f"{config.PUBLIC_API_URL or f'http://127.0.0.1:{config.PORT}'}"
                                f"/api/internal",
            })
            health.note_serving("orchestrator", "n8n")
            return run
        except Exception as exc:                      # noqa: BLE001
            emit(event("workflow_node", None, run_id=run.run_id, node="n8n webhook",
                       status="failed", attempt=1,
                       detail=f"{type(exc).__name__}; falling back to the local state machine"))
            # the chip must stop claiming n8n the moment n8n stops answering
            health.note_serving("orchestrator", "state_machine (n8n unreachable)")
            return self._fallback.execute(run, emit)

    def measure(self, run, emit, force: str | None = None):
        try:
            self._post("/webhook/saathi/measure",
                       {"run_id": run.run_id, "action_id": run.action_id, "force": force})
            run.advance("measuring", "handed to n8n workflow outcome_learning")
            health.note_serving("orchestrator", "n8n")
            return run
        except Exception as exc:                      # noqa: BLE001
            emit(event("workflow_node", None, run_id=run.run_id, node="n8n webhook",
                       status="failed", attempt=1,
                       detail=f"{type(exc).__name__}; falling back to the local state machine"))
            health.note_serving("orchestrator", "state_machine (n8n unreachable)")
            return self._fallback.measure(run, emit, force=force)
