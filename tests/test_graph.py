"""Similarity, cohorts and collective patterns."""
import config
from backend.graph import similarity
from backend.graph.sqlite_store import SqliteGraph
from backend.data import repository as repo

g = SqliteGraph()
M = config.DEMO_MERCHANT


def test_similarity_has_four_components_matching_the_weights():
    a = repo.merchant_row(M)
    b = repo.merchant_row("M002")
    s = similarity.score(a, b)
    assert set(s["components"]) == set(config.SIMILARITY_WEIGHTS)
    assert 0 <= s["total"] <= 1


def test_identical_cell_scores_higher_than_a_different_locality():
    me = repo.merchant_row(M)
    same = similarity.score(me, repo.merchant_row("M002"))["total"]
    other = similarity.score(me, repo.merchant_row("M009"))["total"]   # different locality
    assert same > other


def test_cohort_is_tight_and_has_an_extended_ring():
    v = g.peers(M)["value"]
    assert v["cohort_size"] >= config.MIN_COHORT_SIZE
    assert v["extended_size"] > 0
    assert M not in v["peer_ids"]


def test_playbook_counts_are_consistent():
    v = g.peers(M)["value"]
    pb = g.peer_playbook("evening_decline", v["peer_ids"])["value"]
    best = pb["best"]
    assert best["worked"] <= best["tried"]
    assert 0 <= best["success_rate"] <= 100


def test_failed_plays_bucket_by_parameter_range():
    v = g.peers(M)["value"]
    f = g.failed_plays("discount", v["peer_ids"] + v["extended_ids"])["value"]
    ranges = {b["range"] for b in f["buckets"]}
    assert "25%+" in ranges
    assert f["safest_bucket"]["failure_rate"] <= max(b["failure_rate"] for b in f["buckets"])


def test_cohort_profile_serves_a_merchant_with_no_history():
    v = g.cohort_profile("food_stall", "Sector 62")["value"]
    assert v["cohort_size"] >= config.MIN_COHORT_SIZE
    assert v["expected_daily_high"] > v["expected_daily_low"] > 0


# ------------------------------------------------------------------- latency
def test_cognee_retrieval_never_blocks_an_answer():
    """A slow tenant must cost provenance, never seconds.

    Retrieval used to run inline in five different graph methods, so one
    merchant question serialized five round trips and took 20+ seconds to
    answer. Every number comes from the SQLite ledger regardless, so this
    asserts the shape that keeps a voice reply fast: schedule, don't wait.
    """
    import time
    from backend.graph import cognee_store as cs

    class SlowClient:
        def search(self, query, search_type=None, timeout=None):
            time.sleep(3)
            return [{"search_result": [1, 2, 3]}]

    cs.cache_clear()
    graph = cs.CogneeGraph.__new__(cs.CogneeGraph)
    graph.client = SlowClient()

    t0 = time.time()
    result = graph._augment({"basis": {}}, "a cold cohort question")
    assert time.time() - t0 < 0.5, "retrieval blocked the answer"
    assert "in flight" in result["basis"]["store"]
    assert result["basis"]["counts_from"].startswith("sqlite ledger")

    # once it lands, the same question is answered from cache with real counts
    time.sleep(3.4)
    cached = graph._augment({"basis": {}}, "a cold cohort question")
    assert cached["basis"]["store"] == "cognee"
    assert cached["basis"]["cognee_hits"] == 3


def test_cognee_schedules_each_cold_query_only_once():
    import time
    from backend.graph import cognee_store as cs

    calls = []

    class CountingClient:
        def search(self, query, search_type=None, timeout=None):
            calls.append(query)
            time.sleep(0.4)
            return [{"search_result": [1]}]

    cs.cache_clear()
    graph = cs.CogneeGraph.__new__(cs.CogneeGraph)
    graph.client = CountingClient()
    for _ in range(5):
        graph._augment({"basis": {}}, "same question five times")
    time.sleep(0.8)
    assert len(calls) == 1, calls


def test_cognee_writeback_does_not_block_the_outcome():
    """The mirror is queued; the merchant is not kept waiting for the tenant.

    record_outcome used to call add_text and cognify inline. On a dataset of
    751 texts that took long enough to time out /api/admin/measure, which is
    the exact moment a demo shows the learning loop closing.
    """
    import time
    from backend.graph import cognee_store as cs

    class SlowClient:
        def add_text(self, texts):
            time.sleep(3)

        def cognify(self, background=True, with_model=True):
            time.sleep(3)

    class Reference:
        def record_outcome(self, *a, **k):
            return {"pattern": {"tried": 5, "worked": 3, "median_delta": 12.0}}

    graph = cs.CogneeGraph.__new__(cs.CogneeGraph)
    graph.client = SlowClient()
    graph._reference = Reference()

    t0 = time.time()
    result = graph.record_outcome(
        action_id="A1", metric="evening_gmv", before=100.0, after=128.0,
        verdict="recovered", measured_on="2026-01-01",
        cohort_key="food_stall|college_area|mid",
        situation_kind="evening_decline", action_type="evening_offer")
    assert time.time() - t0 < 0.5, "the Cognee mirror blocked the outcome"
    assert result["degraded"] is False
    assert result["mirror"] == "queued"
