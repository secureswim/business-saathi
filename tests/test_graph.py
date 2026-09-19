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


# ---------------------------------------------------------------------------
# The cohort must be decided by what a shop DOES, not by what it was labelled.
#
# These pin the fix for a flaw that made the onboarding form decide everything:
# category carried 35% of a score with a 0.85 bar, so a different label capped
# the total at 0.65 and could never qualify, while two unlike businesses that
# ticked the same box were pooled and quoted back at each other.
# ---------------------------------------------------------------------------
from backend.data import db                              # noqa: E402
from backend.graph import behaviour, similarity          # noqa: E402


def _rows():
    return {r["id"]: r for r in repo.all_merchants()}


def test_a_cafe_is_not_a_peer_of_a_chai_stall_despite_the_same_label():
    """Same category, same locality, same volume band -- different trade."""
    rows = _rows()
    cafes = [r for r in rows.values()
             if r["name"].startswith("Cafe")
             and r["locality"] == rows[config.DEMO_MERCHANT]["locality"]]
    assert cafes, "the generator must seed cafes under the food_stall label"

    peers = SqliteGraph().peers(config.DEMO_MERCHANT)["value"]["peer_ids"]
    for cafe in cafes:
        assert cafe["category"] == rows[config.DEMO_MERCHANT]["category"]
        assert cafe["id"] not in peers, (
            f"{cafe['id']} shares the label and the locality but trades "
            f"differently; it must not be quoted as a shop 'like yours'")


def test_the_label_is_a_prior_not_a_gate():
    """A different label costs 0.10, not 0.35 against a 0.85 bar.

    The old weighting made a cross-label peer arithmetically impossible: the
    best a different category could score was 0.65."""
    assert config.SIMILARITY_WEIGHTS["category"] <= 0.15
    behavioural = sum(config.SIMILARITY_WEIGHTS[k] for k in
                      ("hour_shape", "weekday_shape", "ticket", "scale"))
    assert behavioural >= 0.70, "measured behaviour must outweigh the form"
    # a shop identical in every measured way but differently labelled
    ceiling = 1.0 - config.SIMILARITY_WEIGHTS["category"]
    assert ceiling >= config.SIMILARITY_THRESHOLD, (
        "a mislabelled shop can never reach the bar; the label is a gate again")


def test_the_day_shape_comparison_actually_discriminates():
    """Cosine on two all-positive vectors mostly measures their shared mean.

    Across the whole population it spanned 0.58-1.00 and rated a mobile
    accessories shop 0.87 against a chai stall. A centred correlation spans
    roughly -0.23 to 1.00 on the same data."""
    rows = _rows()
    me = rows[config.DEMO_MERCHANT]
    profiles = behaviour.load_many(list(rows))
    scores = [similarity.shape_score(profiles[me["id"]].get("hour_shape"),
                                     profiles[r["id"]].get("hour_shape"))
              for r in rows.values()
              if r["id"] != me["id"] and profiles.get(r["id"], {}).get("hour_shape")]
    assert max(scores) - min(scores) > 0.35, (
        "the day-shape score barely separates anyone; it is carrying no "
        "information and the label is deciding the cohort by default")


def test_ticket_and_scale_are_absolute_not_category_relative():
    """The old volume band was measured against each category's own mean, so a
    wrong label produced a wrong band on top of it and the errors compounded."""
    profile = behaviour.load(config.DEMO_MERCHANT)
    assert profile["available"]
    assert profile["avg_ticket"] > 0
    assert profile["txns_per_day"] > 0
    # a 3x ticket difference must score zero regardless of any label
    assert similarity.ratio_score(40.0, 150.0, similarity.TICKET_TOLERANCE) == 0.0
    assert similarity.ratio_score(40.0, 42.0, similarity.TICKET_TOLERANCE) > 0.9


def test_the_learned_pattern_key_carries_no_category():
    """Outcomes used to be pooled by category|locality|band, so a wrong label
    walked straight into every 'worked for X of Y' figure."""
    key = behaviour.cohort_key(config.DEMO_MERCHANT)
    rhythm, band, locality_type = key.split("|")
    assert rhythm in ("morning", "midday", "evening", "spread", "unknown")
    assert band in ("micro", "small", "mid", "large", "unknown")
    categories = {r["category"] for r in repo.all_merchants()}
    assert not (categories & set(key.split("|"))), (
        "the cohort key still contains a category label")
    members = behaviour.members_of(key)
    assert config.DEMO_MERCHANT in members


def test_peer_components_are_exposed_so_ops_can_explain_the_match():
    v = SqliteGraph().peers(config.DEMO_MERCHANT)
    peers = v["basis"].get("peers") or v["value"].get("peers") or []
    sample = SqliteGraph().peers(config.DEMO_MERCHANT)["value"]
    assert sample["cohort_size"] >= config.MIN_COHORT_SIZE
    rows = _rows()
    line = similarity.explain(rows[config.DEMO_MERCHANT],
                              rows[sorted(rows)[1]])
    for part in ("day", "week", "ticket", "scale", "locality", "label"):
        assert part in line


def test_a_mislabelled_merchant_still_finds_its_real_peers():
    """The decisive case, and the one a judge will reach for.

    Under the old weighting a wrong category was unrecoverable: it scored 0 on
    35% of the total, capping the merchant at 0.65 against a 0.85 bar, so the
    cohort emptied out completely. Behaviour now carries the score, so the
    label being wrong costs one component, not the answer."""
    store = SqliteGraph()
    before = set(store.peers(config.DEMO_MERCHANT)["value"]["peer_ids"])
    assert len(before) >= config.MIN_COHORT_SIZE

    original = repo.merchant_row(config.DEMO_MERCHANT)["category"]
    db.write("UPDATE merchants SET category=? WHERE id=?",
             ("pharmacy", config.DEMO_MERCHANT))
    try:
        after = set(store.peers(config.DEMO_MERCHANT)["value"]["peer_ids"])
    finally:
        db.write("UPDATE merchants SET category=? WHERE id=?",
                 (original, config.DEMO_MERCHANT))

    assert len(after) >= config.MIN_COHORT_SIZE, (
        "a single wrong label emptied the cohort; the form is deciding again")
    kept = before & after
    assert len(kept) >= len(before) * 0.6, (
        f"only {len(kept)} of {len(before)} peers survived a relabel")


# --------------------------------------------------------------------------
# Cognee cards must describe the cohort they are actually keyed on.
# --------------------------------------------------------------------------
from backend.graph import cards                            # noqa: E402


def test_cards_describe_the_measured_cohort_in_readable_english():
    """The key changed from category|locality|band to rhythm|ticket|locality.

    The card writers still split it three ways and called the parts category,
    locality and band, which produced retrievable documents reading "Among
    middays in micro locations with college_area volume" -- meaningless text
    that Cognee would happily embed and return as evidence."""
    payload = cards.all_cards()
    for group in ("profiles", "experiences", "patterns"):
        assert payload[group], group
        for card in payload[group][:40]:
            text = card["text"]
            for nonsense in ("middays in", "micro locations", "college_area volume",
                             "evenings in", "with unknown volume"):
                assert nonsense not in text, (group, text)
            assert "|" not in text, f"a raw cohort key leaked into prose: {text}"


def test_card_metadata_carries_the_behavioural_key():
    payload = cards.all_cards()
    categories = {r["category"] for r in repo.all_merchants()}
    for group in ("profiles", "experiences", "patterns"):
        for card in payload[group][:40]:
            key = card["metadata"].get("cohort_key")
            assert key and len(key.split("|")) == 3, (group, key)
            assert not (set(key.split("|")) & categories), (
                f"{group} card is still keyed on a category label: {key}")


def test_the_fingerprint_changes_when_the_data_changes():
    """Cognee's add_text only appends, so a re-ingest after a regeneration
    leaves the previous version's cards in the graph. The fingerprint is the
    only way to notice."""
    payload = cards.all_cards()
    first = cards.fingerprint(payload)
    assert first == cards.fingerprint(payload), "fingerprint is not stable"
    mutated = {**payload, "patterns": payload["patterns"][:-1]}
    assert cards.fingerprint(mutated) != first
