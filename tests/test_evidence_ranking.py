from session_search.core.embeddings import EmbeddingIdentity
from session_search.core.records import Event, SearchQuery, SessionRevision
from session_search.storage.catalog import Catalog
from session_search.storage.semantic import hybrid_search, index_pending


class Provider:
    identity = EmbeddingIdentity("ranking-fixture", dimensions=2)

    def embed(self, text, *, query=False):
        return [1.0, 0.0]


def ingest(catalog, name, events):
    catalog.ingest(SessionRevision(name, tuple(events)), producer="fixture", request_id=name)


def test_answers_outrank_repeated_search_commands_and_keep_exact_context(tmp_path):
    question = "why did the orchard service lose its disk mounts"
    with Catalog(tmp_path) as catalog:
        ingest(catalog, "benchmark", [
            Event(str(i), "tool", f'const query = "{question}";', kind="tool_use")
            for i in range(12)
        ])
        answer = "The orchard service started before its disk mounts; mount dependencies fixed it."
        ingest(catalog, "incident", [Event("answer", "assistant", answer)])
        index_pending(catalog, Provider())
        for provider in (None, Provider()):
            result = hybrid_search(catalog, SearchQuery(question, limit=2), provider)
            hit = result["results"][0]
            assert hit["citation"]["session_id"] == "incident"
            assert "_evidence_weight" not in hit
            context = catalog.context([hit["citation"]], neighbors=0)
            assert context["results"][0]["events"][0]["text"] == answer
        exact = hybrid_search(catalog, SearchQuery(question, literal=True), Provider())
        assert all(r["role"] == "tool" for r in exact["results"])


def test_broad_search_diversifies_but_scoped_search_retains_passages(tmp_path):
    with Catalog(tmp_path) as catalog:
        ingest(catalog, "a", [Event(str(i), "assistant", "archive recovery " * (i + 1))
                              for i in range(8)])
        ingest(catalog, "b", [Event("answer", "assistant", "archive recovery")])
        broad = catalog.search(SearchQuery("archive recovery", limit=3))
        assert {r["citation"]["session_id"] for r in broad["results"]} == {"a", "b"}
        scoped = catalog.search(SearchQuery("archive recovery", session_id="a", limit=3))
        assert len(scoped["results"]) == 3
        assert {r["citation"]["session_id"] for r in scoped["results"]} == {"a"}


def test_complementary_answer_can_outrank_weak_distinct_conversations():
    from session_search.storage.ranking import rank_results

    def hit(session, event, offset=0):
        return {"citation": {"session_id": session, "revision": "r",
                             "event_id": event, "offset": offset}, "excerpt": event}

    lexical = [hit("incident", "diagnosis"), hit("incident", "adopted-fix")]
    lexical.extend(hit(f"other-{i}", "mention") for i in range(8))
    semantic = lexical[:2]
    result = rank_results(SearchQuery("recovery decision"), lexical, semantic)
    assert [x["citation"]["event_id"] for x in result[:2]] == ["diagnosis", "adopted-fix"]
    assert result[2]["citation"]["session_id"] != "incident"


def test_stronger_channel_keeps_its_passage_and_immutable_offset():
    from session_search.storage.ranking import rank_results

    lexical = {"citation": {"session_id": "incident", "revision": "r",
                            "event_id": "answer", "offset": 10000},
               "excerpt": "the exact recovery decision"}
    weak_semantic = {**lexical, "citation": {**lexical["citation"], "offset": 0},
                     "excerpt": "unrelated introduction"}
    others = [{"citation": {"session_id": str(i), "revision": "r", "event_id": "e"}}
              for i in range(99)]
    results = rank_results(SearchQuery("recovery"), [lexical], [*others, weak_semantic])
    hit = next(x for x in results if x["citation"]["session_id"] == "incident")
    assert hit["citation"]["offset"] == 10000
    assert hit["excerpt"] == lexical["excerpt"]


def test_answer_survives_more_than_one_hundred_search_transcripts(tmp_path):
    class RankedProvider(Provider):
        def embed(self, text, *, query=False):
            if text.startswith("Volumes"):
                return [0.9, (1 - 0.9 ** 2) ** 0.5]
            return [1.0, 0.0]

    question = "what prevented the repeated startup failure"
    with Catalog(tmp_path) as catalog:
        ingest(catalog, "noise", [
            Event(str(i), "tool", f'benchmark invocation: search("{question}")')
            for i in range(150)
        ])
        answer = "Volumes must become available before launching the daemon."
        ingest(catalog, "incident", [Event("answer", "assistant", answer)])
        index_pending(catalog, RankedProvider(), limit=200)
        result = hybrid_search(catalog, SearchQuery(question), RankedProvider())
        assert result["results"][0]["citation"]["session_id"] == "incident"
        expanded = catalog.context([result["results"][0]["citation"]], neighbors=0)
        assert expanded["results"][0]["events"][0]["text"] == answer


def test_outage_ranking_and_role_time_filters(tmp_path):
    class Offline(Provider):
        def embed(self, *args, **kwargs):
            raise OSError("offline")

    with Catalog(tmp_path) as catalog:
        ingest(catalog, "discussion", [
            Event("intent", "user", "retain recovery history", "2025-01-01"),
            Event("answer", "assistant", "retain recovery history", "2025-01-02"),
            Event("later", "assistant", "retain recovery history", "2025-01-03"),
        ])
        query = SearchQuery("recovery", role="assistant", before="2025-01-03", limit=5)
        result = hybrid_search(catalog, query, Offline())
        assert result["degraded"] and result["mode"] == "keyword"
        assert [r["citation"]["event_id"] for r in result["results"]] == ["answer"]
        assert all("_evidence_weight" not in r for r in result["results"])
        user = catalog.search(SearchQuery("recovery", role="user"))
        assert [r["citation"]["event_id"] for r in user["results"]] == ["intent"]


def test_planning_and_evaluation_are_retrievable_when_requested(tmp_path):
    from session_search.storage.ranking import evidence_weight

    plan = "I'll verify the recovery plan before restarting the service."
    assert evidence_weight(plan, "assistant", SearchQuery("what was the recovery plan")) == 1
    assert evidence_weight(plan, "assistant", SearchQuery("what fixed recovery")) < 1
    report = "The benchmark showed recall@10 improved."
    assert evidence_weight(report, "assistant", SearchQuery("benchmark recall")) == 1
    assert evidence_weight(report, "assistant", SearchQuery("recover an incident")) < 1
    envelope = '<subagent_notification>{"status":"recovery"}</subagent_notification>'
    assert evidence_weight(envelope, "user", SearchQuery("recovery", role="user")) < 1


def test_exact_session_identity_wins_over_mentions_in_other_sessions(tmp_path):
    with Catalog(tmp_path) as catalog:
        ingest(catalog, "other", [Event("mention", "assistant", "target-id " * 20)])
        ingest(catalog, "target-id", [Event("answer", "assistant", "target-id original decision")])
        result = catalog.search(SearchQuery("target-id", limit=1))
        assert result["results"][0]["citation"]["session_id"] == "target-id"
