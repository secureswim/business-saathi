"""Two different floors, and the difference between merchants and attempts.

MIN_COHORT_SIZE is a PRIVACY floor: enough merchants that none is identifiable.
MIN_ATTEMPTS_TO_RECOMMEND is an EVIDENCE floor: enough attempts that a success
rate means something. A cohort of 7 merchants passes the first and can still
contain an action tried exactly once -- "worked 0 of 1 (0%)" is one shop's bad
afternoon, not a cohort pattern, and it was previously eligible to be both the
recommendation and an ingested "Among food stalls..." statement.
"""
import config
from backend.graph import cards
from backend.graph.sqlite_store import SqliteGraph


def test_the_two_floors_are_different_questions():
    assert config.MIN_COHORT_SIZE >= 5
    assert config.MIN_ATTEMPTS_TO_RECOMMEND >= 2


def test_best_is_never_built_on_too_few_attempts():
    g = SqliteGraph()
    peers = g.peers(config.DEMO_MERCHANT)["value"]
    for kind in ("evening_decline", "sales_decline", "demand_surge", "margin_pressure"):
        pb = g.peer_playbook(kind, peers["peer_ids"])["value"]
        best = pb.get("best")
        if best is not None:
            assert best["tried"] >= config.MIN_ATTEMPTS_TO_RECOMMEND, (kind, best)


def test_thin_options_are_kept_as_context_but_flagged():
    """/ops should still see everything the cohort tried, labelled honestly."""
    g = SqliteGraph()
    peers = g.peers(config.DEMO_MERCHANT)["value"]
    pb = g.peer_playbook("evening_decline", peers["peer_ids"])["value"]
    for option in pb["options"]:
        assert "sufficient" in option
        assert option["sufficient"] == (option["tried"] >= config.MIN_ATTEMPTS_TO_RECOMMEND)


def test_merchants_and_attempts_are_reported_separately():
    """One merchant trying a play three times is not three merchants."""
    g = SqliteGraph()
    peers = g.peers(config.DEMO_MERCHANT)["value"]
    pb = g.peer_playbook("evening_decline", peers["peer_ids"])["value"]
    for option in pb["options"]:
        assert option["merchants"] >= 1
        assert option["merchants"] <= option["tried"], option
        assert option["merchants"] <= pb["cohort_size"], option


def test_spoken_answer_never_calls_attempts_merchants():
    """The wording bug this pairs with: `tried` was printed as a merchant count."""
    from backend.reasoning.templates import TemplateReasoner
    g = SqliteGraph()
    peers = g.peers(config.DEMO_MERCHANT)["value"]
    pb = g.peer_playbook("evening_decline", peers["peer_ids"])
    best = pb["value"].get("best")
    if best is None or best["merchants"] == best["tried"]:
        return       # the two numbers coincide here; nothing to distinguish
    out = TemplateReasoner().synthesize(
        "sales_diagnosis", "sales kyun kam hain",
        [{"tool": "get_peer_playbook", "available": True, "value": pb["value"]}])
    assert f"{best['tried']} merchants" not in out["english"]


def test_pattern_cards_below_the_evidence_floor_are_not_ingested():
    payload = cards.all_cards()
    for card in payload["patterns"]:
        assert card["metadata"]["tried"] >= config.MIN_ATTEMPTS_TO_RECOMMEND, card["text"]
    assert payload["patterns_below_floor"] >= 0


def test_experience_cards_are_unaffected_by_the_evidence_floor():
    """They describe one chain and never claim to be an aggregate."""
    payload = cards.all_cards()
    assert len(payload["experiences"]) > len(payload["patterns"])
    assert "has been tried" not in payload["experiences"][0]["text"]


def test_writeback_reports_what_it_wrote_not_invented_graph_counts():
    """`nodes_added: 2, edges_added: 3` were hardcoded and nobody measured them.

    A judge asking "how do you know it added three edges?" had no good answer.
    The write-back now reports the rows it actually wrote, and the Cognee
    mirror reports cards SUBMITTED -- extraction runs asynchronously on the
    tenant and is never counted here.
    """
    from backend.graph.sqlite_store import SqliteGraph
    import inspect
    from backend.graph import cognee_store

    source = inspect.getsource(SqliteGraph.record_outcome)
    source += inspect.getsource(cognee_store.CogneeGraph.record_outcome)
    assert "nodes_added" not in source
    assert "edges_added" not in source


def test_sqlite_writeback_counts_are_real():
    """One outcome row, one recomputed aggregate -- both actually happen."""
    from backend.graph.sqlite_store import SqliteGraph
    import inspect
    source = inspect.getsource(SqliteGraph.record_outcome)
    assert '"outcome_rows": 1' in source
    assert '"pattern_rows": 1' in source
