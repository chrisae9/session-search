import json

import pytest

from session_search.core.embeddings import EmbeddingIdentity, LocalEmbedder, load_provider
from session_search.core.records import Event, SearchQuery, SessionRevision
from session_search.storage.catalog import Catalog
from session_search.storage.semantic import hybrid_search, index_pending


class FakeProvider:
    identity = EmbeddingIdentity("test-artifact", dimensions=2)

    def embed(self, text, *, query=False):
        return [1, 0] if query or "recovery" in text else [0, 1]


def populate(catalog):
    events = (Event("a", "user", "disaster recovery", "2026-01-01"),
              Event("b", "assistant", "unrelated pictures", "2026-01-01"))
    catalog.ingest(SessionRevision("s", events), producer="d", request_id="1")


def test_semantic_finds_evidence_without_keyword_overlap_and_preserves_filters(tmp_path):
    with Catalog(tmp_path) as catalog:
        populate(catalog)
        provider = FakeProvider()
        assert index_pending(catalog, provider)["embedded"] == 2
        result = hybrid_search(catalog, SearchQuery("restore", role="user"), provider)
        assert result["mode"] == "hybrid"
        assert result["results"][0]["excerpt"] == "disaster recovery"
        assert len(result["results"]) == 1
        assert result["coverage"]["semantic_indexed"] == 2
        assert not hybrid_search(catalog, SearchQuery("restore", project="absent"), provider)["results"]
        assert not hybrid_search(catalog, SearchQuery("restore", literal=True), provider)["results"]


def test_model_outage_preserves_new_lexical_evidence_and_retries_later(tmp_path):
    class Offline(FakeProvider):
        def embed(self, *args, **kwargs):
            raise OSError("offline")

    with Catalog(tmp_path) as catalog:
        populate(catalog)
        assert index_pending(catalog, Offline())["failed"] == 2
        result = hybrid_search(catalog, SearchQuery("recovery"), Offline())
        assert result["mode"] == "keyword" and result["degraded"]
        assert result["results"]
        catalog.db.execute("UPDATE embedding_failures SET next_attempt=0")
        catalog.db.commit()
        assert index_pending(catalog, FakeProvider())["embedded"] == 2
        assert index_pending(catalog, FakeProvider())["embedded"] == 0


def test_model_identity_change_never_reuses_vectors(tmp_path):
    with Catalog(tmp_path) as catalog:
        populate(catalog)
        provider = FakeProvider()
        index_pending(catalog, provider)
        provider.identity = EmbeddingIdentity("another-artifact", dimensions=2)
        result = hybrid_search(catalog, SearchQuery("recovery"), provider)
        assert result["coverage"]["semantic_indexed"] == 0
        assert result["results"]  # Lexical result survives the model transition.
        assert index_pending(catalog, provider)["embedded"] == 2


def test_local_missing_model_does_not_download_and_remote_config_is_opt_in(tmp_path):
    provider = LocalEmbedder(tmp_path / "absent.gguf", EmbeddingIdentity("sha256:missing"))
    with pytest.raises(FileNotFoundError):
        provider.embed("query", query=True)
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"mode": "remote", "identity": {"artifact": "model"},
                                  "endpoint": "https://model.example", "model": "embeddings",
                                  "response_model": "qwen"}))
    with pytest.raises(ValueError, match="authorization"):
        load_provider(config)


def test_equal_text_deduplicates_embedding_work_without_merging_citations(tmp_path):
    with Catalog(tmp_path) as catalog:
        for sid in ("a", "b"):
            catalog.ingest(SessionRevision(sid, (Event("e", "user", "same recovery"),)),
                           producer="d", request_id=sid)
        assert index_pending(catalog, FakeProvider())["embedded"] == 1
        response = hybrid_search(catalog, SearchQuery("restore"), FakeProvider())
        assert {r["citation"]["session_id"] for r in response["results"]} == {"a", "b"}
