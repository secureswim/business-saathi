"""Merchant Knowledge Graph — the reasoning layer, not a database.

`KnowledgeGraph` is the interface the rest of Business Saathi talks to. Two
implementations live behind it:

  * ``LocalGraph``  — NetworkX, runs anywhere, no keys. Default.
  * ``CogneeGraph`` — Cognee-backed, same surface; set COGNEE_API_KEY to use.

Everything above this file (reasoning, workflows, API) is written against the
interface, so swapping the backend changes no call sites.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Protocol

import networkx as nx

from app.core.synth import CATEGORIES, LOCALITIES, Dataset, dataset, volume_band

# --------------------------------------------------------------------------
# similarity
# --------------------------------------------------------------------------

BAND_ORDER = ["micro", "small", "mid", "large"]

# Similarity is NOT just business type. These weights are what let the graph say
# "merchants like you" and mean it.
WEIGHTS = {"category": 0.34, "locality": 0.26, "volume_band": 0.22, "pattern": 0.18}


@dataclass
class Peer:
    merchant_id: str
    score: float
    shares: list[str]

    def as_dict(self) -> dict[str, Any]:
        return {"merchant_id": self.merchant_id, "score": round(self.score, 3), "shares": self.shares}


@dataclass
class Evidence:
    """One traversal result, carrying its own provenance.

    Every recommendation the merchant hears is built from these, which is why the
    system can say "5 of 6 similar stalls recovered" instead of "our AI thinks".
    """

    action_type: str
    params: dict[str, Any]
    peer_count: int
    success_count: int
    success_rate: float
    median_gmv_delta_pct: float
    median_retention_pct: float | None
    outcomes: dict[str, int]
    path: list[str]
    sample: list[dict[str, Any]]
    cohort: str = "merchants like you"

    def as_dict(self) -> dict[str, Any]:
        d = self.__dict__.copy()
        d["success_rate"] = round(self.success_rate, 3)
        return d


class KnowledgeGraph(Protocol):
    def peers(self, merchant_id: str, limit: int = 20, min_score: float = 0.45) -> list[Peer]: ...
    def evidence(self, merchant_id: str, action_type: str, **kw) -> Evidence | None: ...
    def action_catalogue(self, merchant_id: str) -> list[Evidence]: ...
    def locality_health(self, merchant_id: str) -> dict[str, Any]: ...
    def write_outcome(self, merchant_id: str, action_type: str, params: dict, outcome: str,
                      gmv_delta_pct: float, txn_delta_pct: float, note: str = "") -> str: ...
    def snapshot(self, merchant_id: str) -> dict[str, Any]: ...


# --------------------------------------------------------------------------
# local implementation
# --------------------------------------------------------------------------

SUCCESS = {"recovered"}
PARTIAL = {"spike_then_fade"}


class LocalGraph:
    """NetworkX-backed knowledge graph over the synthetic merchant dataset."""

    def __init__(self, ds: Dataset | None = None) -> None:
        self.ds = ds or dataset()
        self.g = nx.MultiDiGraph()
        self._build()

    # ---- construction --------------------------------------------------
    def _build(self) -> None:
        g = self.g
        for cat, spec in CATEGORIES.items():
            g.add_node(f"category:{cat}", kind="category", label=spec["label"])
        for loc, spec in LOCALITIES.items():
            g.add_node(f"locality:{loc}", kind="locality", label=spec["label"], area_kind=spec["kind"])
        for band in BAND_ORDER:
            g.add_node(f"band:{band}", kind="volume_band", label=band)

        for m in self.ds.merchants:
            band = volume_band(self.ds.avg_daily_gmv(m.id))
            g.add_node(f"merchant:{m.id}", kind="merchant", label=m.name, category=m.category,
                       locality=m.locality, pattern=m.pattern, volume_band=band,
                       opened_days_ago=m.opened_days_ago)
            g.add_edge(f"merchant:{m.id}", f"category:{m.category}", key="in_category", rel="in_category")
            g.add_edge(f"merchant:{m.id}", f"locality:{m.locality}", key="in_locality", rel="in_locality")
            g.add_edge(f"merchant:{m.id}", f"band:{band}", key="in_band", rel="in_band")
            g.add_node(f"pattern:{m.pattern}", kind="pattern", label=m.pattern)
            g.add_edge(f"merchant:{m.id}", f"pattern:{m.pattern}", key="has_pattern", rel="has_pattern")

        for a in self.ds.actions:
            an = f"action:{a.id}"
            on = f"outcome:{a.id}"
            g.add_node(an, kind="action", action_type=a.action_type, params=a.params,
                       started_days_ago=a.started_days_ago)
            g.add_node(on, kind="outcome", outcome=a.outcome, gmv_delta_pct=a.gmv_delta_pct,
                       txn_delta_pct=a.txn_delta_pct, retention=a.retention_after_30d_pct, note=a.note)
            g.add_edge(f"merchant:{a.merchant_id}", an, key="took_action", rel="took_action")
            g.add_edge(an, on, key="produced", rel="produced")
            g.add_node(f"action_type:{a.action_type}", kind="action_type", label=a.action_type)
            g.add_edge(an, f"action_type:{a.action_type}", key="of_type", rel="of_type")

    # ---- similarity ----------------------------------------------------
    def peers(self, merchant_id: str, limit: int = 20, min_score: float = 0.45) -> list[Peer]:
        me = self.g.nodes[f"merchant:{merchant_id}"]
        out: list[Peer] = []
        for node, d in self.g.nodes(data=True):
            if d.get("kind") != "merchant" or node == f"merchant:{merchant_id}":
                continue
            score = 0.0
            shares: list[str] = []
            if d["category"] == me["category"]:
                score += WEIGHTS["category"]
                shares.append("category")
            if d["locality"] == me["locality"]:
                score += WEIGHTS["locality"]
                shares.append("locality")
            elif LOCALITIES[d["locality"]]["kind"] == LOCALITIES[me["locality"]]["kind"]:
                score += WEIGHTS["locality"] * 0.6
                shares.append("locality_type")
            # adjacent volume bands still count, at a discount
            gap = abs(BAND_ORDER.index(d["volume_band"]) - BAND_ORDER.index(me["volume_band"]))
            if gap == 0:
                score += WEIGHTS["volume_band"]
                shares.append("volume_band")
            elif gap == 1:
                score += WEIGHTS["volume_band"] * 0.5
                shares.append("volume_band_adjacent")
            if d["pattern"] == me["pattern"]:
                score += WEIGHTS["pattern"]
                shares.append("customer_pattern")
            if score >= min_score:
                out.append(Peer(node.split(":", 1)[1], score, shares))
        return sorted(out, key=lambda p: -p.score)[:limit]

    # ---- traversal -----------------------------------------------------
    def _peer_actions(self, merchant_id: str, action_type: str, peer_ids: set[str]) -> list[Any]:
        return [a for a in self.ds.actions if a.action_type == action_type and a.merchant_id in peer_ids]

    MIN_SAMPLE = 3

    def evidence(self, merchant_id: str, action_type: str, limit: int = 20,
                 min_score: float = 0.45) -> Evidence | None:
        peers = self.peers(merchant_id, limit=limit, min_score=min_score)
        peer_ids = {p.merchant_id for p in peers}
        acts = self._peer_actions(merchant_id, action_type, peer_ids)

        # Too thin to reason from? Widen the cohort deliberately and say so,
        # rather than quietly presenting a sample of one as a pattern.
        widened = None
        if len(acts) < self.MIN_SAMPLE:
            wider = self.peers(merchant_id, limit=40, min_score=0.20)
            wider_ids = {p.merchant_id for p in wider}
            wider_acts = self._peer_actions(merchant_id, action_type, wider_ids)
            if len(wider_acts) > len(acts):
                acts, widened = wider_acts, "loosely similar merchants"
        if not acts:
            return None

        outcomes: dict[str, int] = {}
        for a in acts:
            outcomes[a.outcome] = outcomes.get(a.outcome, 0) + 1
        succ = sum(1 for a in acts if a.outcome in SUCCESS)
        deltas = sorted(a.gmv_delta_pct for a in acts)
        rets = sorted(a.retention_after_30d_pct for a in acts if a.retention_after_30d_pct is not None)

        me = self.g.nodes[f"merchant:{merchant_id}"]
        path = [
            f"category:{me['category']}",
            f"locality:{me['locality']}",
            f"pattern:{me['pattern']}",
            f"action_type:{action_type}",
            "outcome",
        ]
        if widened:
            path.insert(0, f"widened:{widened}")
        sample = [
            {"peer": a.merchant_id, "params": a.params, "outcome": a.outcome,
             "gmv_delta_pct": a.gmv_delta_pct, "txn_delta_pct": a.txn_delta_pct,
             "retention_pct": a.retention_after_30d_pct, "note": a.note}
            for a in acts
        ]
        return Evidence(
            action_type=action_type,
            params=_modal_params(acts),
            peer_count=len(acts),
            success_count=succ,
            success_rate=succ / len(acts),
            median_gmv_delta_pct=round(_median(deltas), 1),
            median_retention_pct=round(_median(rets), 1) if rets else None,
            outcomes=outcomes,
            path=path,
            sample=sample,
            cohort=widened or "merchants like you",
        )

    def action_catalogue(self, merchant_id: str) -> list[Evidence]:
        types = sorted({a.action_type for a in self.ds.actions})
        found = [self.evidence(merchant_id, t) for t in types]
        return [e for e in found if e]

    # ---- locality signal -----------------------------------------------
    def locality_health(self, merchant_id: str) -> dict[str, Any]:
        """Is it me, or the whole area? Impossible to answer without the graph."""
        me = self.ds.merchant(merchant_id)
        same_area = [m for m in self.ds.merchants
                     if m.locality == me.locality and m.category == me.category and m.id != me.id]
        def trend(mid: str) -> float:
            rs = sorted(self.ds.rolls_for(mid), key=lambda r: r.day)
            recent = rs[-7:]
            base = rs[-37:-7]
            a = sum(r.gmv for r in recent) / max(len(recent), 1)
            b = sum(r.gmv for r in base) / max(len(base), 1)
            return (a - b) / b * 100 if b else 0.0

        mine = trend(merchant_id)
        peer_trends = [trend(m.id) for m in same_area]
        area = _median(sorted(peer_trends)) if peer_trends else 0.0
        gap = mine - area
        if gap < -8:
            verdict = "merchant_specific"
        elif gap > 8:
            verdict = "outperforming_area"
        else:
            verdict = "area_wide"
        return {
            "merchant_trend_pct": round(mine, 1),
            "area_trend_pct": round(area, 1),
            "peer_sample": len(peer_trends),
            "locality": LOCALITIES[me.locality]["label"],
            "verdict": verdict,
        }

    # ---- cold start ----------------------------------------------------
    def cold_start(self, category: str, locality: str, pattern: str = "evening_heavy") -> dict[str, Any]:
        """Day 1 intelligence: what a brand-new merchant inherits from the graph."""
        cohort = [m for m in self.ds.merchants if m.category == category and m.locality == locality]
        if not cohort:
            cohort = [m for m in self.ds.merchants if m.category == category]
        peak_hours: dict[int, float] = {}
        revenues: list[float] = []
        for m in cohort:
            rs = sorted(self.ds.rolls_for(m.id), key=lambda r: r.day)[-30:]
            for r in rs:
                for h, v in r.by_hour.items():
                    peak_hours[h] = peak_hours.get(h, 0) + v
            revenues.append(sum(r.gmv for r in rs) / max(len(rs), 1))
        top = sorted(peak_hours.items(), key=lambda kv: -kv[1])[:4]
        cat_actions = [a for a in self.ds.actions
                       if a.merchant_id in {m.id for m in cohort}]
        best = {}
        for a in cat_actions:
            b = best.setdefault(a.action_type, {"n": 0, "ok": 0})
            b["n"] += 1
            b["ok"] += 1 if a.outcome in SUCCESS else 0
        revenues.sort()
        return {
            "cohort_size": len(cohort),
            "peak_hours": sorted(h for h, _ in top),
            "expected_daily_revenue": [round(_percentile(revenues, 0.25), -2),
                                       round(_percentile(revenues, 0.75), -2)],
            "proven_actions": [{"action_type": k, "success_rate": round(v["ok"] / v["n"], 2), "n": v["n"]}
                               for k, v in sorted(best.items(), key=lambda kv: -kv[1]["ok"] / kv[1]["n"])],
            "locality": LOCALITIES.get(locality, {}).get("label", locality),
            "category": CATEGORIES.get(category, {}).get("label", category),
        }

    # ---- feedback loop -------------------------------------------------
    def write_outcome(self, merchant_id: str, action_type: str, params: dict, outcome: str,
                      gmv_delta_pct: float, txn_delta_pct: float, note: str = "") -> str:
        """Measured outcomes return to the graph. This is the loop that compounds."""
        from app.core.synth import ActionRecord

        new_id = f"A-{len(self.ds.actions) + 1:03d}"
        rec = ActionRecord(id=new_id, merchant_id=merchant_id, action_type=action_type, params=params,
                           started_days_ago=0, duration_days=int(params.get("days", 3)),
                           outcome=outcome, gmv_delta_pct=gmv_delta_pct, txn_delta_pct=txn_delta_pct,
                           retention_after_30d_pct=None, note=note or "measured by Business Saathi")
        self.ds.actions.append(rec)
        an, on = f"action:{new_id}", f"outcome:{new_id}"
        self.g.add_node(an, kind="action", action_type=action_type, params=params, started_days_ago=0)
        self.g.add_node(on, kind="outcome", outcome=outcome, gmv_delta_pct=gmv_delta_pct,
                       txn_delta_pct=txn_delta_pct, retention=None, note=rec.note)
        self.g.add_edge(f"merchant:{merchant_id}", an, key="took_action", rel="took_action")
        self.g.add_edge(an, on, key="produced", rel="produced")
        self.g.add_edge(an, f"action_type:{action_type}", key="of_type", rel="of_type")
        return new_id

    # ---- visualisation -------------------------------------------------
    def snapshot(self, merchant_id: str, peer_limit: int = 8) -> dict[str, Any]:
        """Small ego-graph for the UI: me, my peers, and one action->outcome chain."""
        peers = self.peers(merchant_id, limit=peer_limit)
        me = self.ds.merchant(merchant_id)
        nodes = [{"id": me.id, "kind": "merchant", "label": me.name, "self": True,
                  "sub": f"{CATEGORIES[me.category]['label']} · {LOCALITIES[me.locality]['label']}"}]
        edges = []
        for p in peers:
            pm = self.ds.merchant(p.merchant_id)
            nodes.append({"id": pm.id, "kind": "peer", "label": "similar merchant",
                          "sub": " · ".join(p.shares[:2]), "score": round(p.score, 2)})
            edges.append({"source": me.id, "target": pm.id, "rel": "similar_to", "weight": round(p.score, 2)})
        # one real chain, anonymised
        chain = next((a for a in self.ds.actions
                      if a.merchant_id in {p.merchant_id for p in peers} and a.outcome in SUCCESS), None)
        if chain:
            nodes.append({"id": f"act-{chain.id}", "kind": "action", "label": chain.action_type,
                          "sub": _params_label(chain.params)})
            nodes.append({"id": f"out-{chain.id}", "kind": "outcome", "label": chain.outcome,
                          "sub": f"{chain.gmv_delta_pct:+.0f}% GMV"})
            edges.append({"source": chain.merchant_id, "target": f"act-{chain.id}", "rel": "took_action"})
            edges.append({"source": f"act-{chain.id}", "target": f"out-{chain.id}", "rel": "produced"})
        return {"nodes": nodes, "edges": edges,
                "stats": {"merchants": self.g.number_of_nodes(), "relations": self.g.number_of_edges(),
                          "action_chains": len(self.ds.actions)}}


# --------------------------------------------------------------------------
# cognee-backed implementation (same interface)
# --------------------------------------------------------------------------


class CogneeGraph(LocalGraph):
    """Cognee backend. Falls back to the local traversal for anything Cognee
    has not been asked to index yet, so the demo never dead-ends on a network
    error. Activated when COGNEE_API_KEY is set."""

    def __init__(self, ds: Dataset | None = None) -> None:
        super().__init__(ds)
        self.api_key = os.environ.get("COGNEE_API_KEY")
        self._client = None

    @property
    def client(self):  # pragma: no cover - requires credentials
        if self._client is None:
            import cognee  # type: ignore

            self._client = cognee
        return self._client

    def ingest(self) -> None:  # pragma: no cover - requires credentials
        """Push merchant/action/outcome triples into Cognee as a reasoning layer."""
        triples = []
        for m in self.ds.merchants:
            triples.append(f"{m.name} is a {m.category} in {m.locality} with {m.pattern} customers")
        for a in self.ds.actions:
            triples.append(
                f"{a.merchant_id} ran {a.action_type} {a.params} and the outcome was "
                f"{a.outcome} ({a.gmv_delta_pct:+}% GMV). {a.note}"
            )
        self.client.add("\n".join(triples))
        self.client.cognify()


def get_graph() -> LocalGraph:
    backend = os.environ.get("SAATHI_GRAPH", "local").lower()
    if backend == "cognee" and os.environ.get("COGNEE_API_KEY"):
        return CogneeGraph()
    return LocalGraph()


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _median(xs: list[float]) -> float:
    if not xs:
        return 0.0
    n = len(xs)
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2


def _percentile(xs: list[float], q: float) -> float:
    if not xs:
        return 0.0
    i = max(0, min(len(xs) - 1, int(round(q * (len(xs) - 1)))))
    return xs[i]


def _modal_params(acts: list[Any]) -> dict[str, Any]:
    """The parameter set most peers actually used — what we recommend."""
    counts: dict[str, dict[Any, int]] = {}
    for a in acts:
        if a.outcome not in SUCCESS:
            continue
        for k, v in a.params.items():
            key = str(v)
            counts.setdefault(k, {}).setdefault(v if not isinstance(v, dict) else key, 0)
            counts[k][v if not isinstance(v, dict) else key] += 1
    if not counts:
        return dict(acts[0].params)
    return {k: max(v.items(), key=lambda kv: kv[1])[0] for k, v in counts.items()}


def _params_label(params: dict[str, Any]) -> str:
    """Label a parameter set. Where both a rupee and a percentage figure exist,
    show the percentage: it is the part that transfers between merchants."""
    bits = []
    if "discount_pct" in params:
        bits.append(f"{params['discount_pct']}% off")
    elif "discount_rupees" in params:
        bits.append(f"₹{params['discount_rupees']} off")
    if "increase_pct" in params:
        bits.append(f"+{params['increase_pct']}%")
    elif "increase_rupees" in params:
        bits.append(f"+₹{params['increase_rupees']}")
    if "window" in params:
        bits.append(str(params["window"]))
    if "days" in params:
        bits.append(f"{params['days']}d")
    return " · ".join(bits) or "—"
