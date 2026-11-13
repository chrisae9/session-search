import json

import pytest

from session_search.core.embeddings import EmbeddingIdentity
from session_search.core.providers import load_providers
from session_search.core.records import Event, SearchQuery, SessionRevision
from session_search.interfaces.capture_status import capture_status
from session_search.interfaces.cli import main
from session_search.storage.catalog import Catalog
from session_search.storage.semantic import hybrid_search, index_pending


class SyntheticProvider:
    identity = EmbeddingIdentity("synthetic", dimensions=2)

    def embed(self, text, *, query=False):
        return [1, 0]


def test_capture_opt_in_is_independent_of_catalog_search_policy(tmp_path, capsys):
    config = tmp_path / "providers.toml"
    root = tmp_path / "catalog"
    assert load_providers(None)["hermes"]["capture_enabled"] is True
    config.write_text("[providers.hermes]\nsearch_by_default = true\n")
    assert load_providers(config)["hermes"]["capture_enabled"] is True

    def ids(catalog, **filters):
        return {hit["citation"]["session_id"] for hit in
                catalog.search(SearchQuery("recovery", **filters))["results"]}

    def semantic_ids(catalog, **filters):
        result = hybrid_search(catalog, SearchQuery("synonym", **filters), SyntheticProvider())
        assert result["semantic_available"] is True
        return {hit["citation"]["session_id"] for hit in result["results"]}

    with Catalog(root) as catalog:
        for source in ("codex", "hermes"):
            catalog.ingest(SessionRevision(source, (Event("e", "user", "recovery evidence"),),
                                          source=source), producer="device", request_id=source)
        assert catalog.source_defaults()["hermes"] is True
        assert ids(catalog) == {"codex", "hermes"}
        assert {"source": "hermes", "sessions": 1, "events": 1} in catalog.status()["sources"]
        citation = catalog.search(SearchQuery("recovery", sources=["hermes"]))["results"][0]["citation"]
        index_pending(catalog, SyntheticProvider())
        assert semantic_ids(catalog) == {"codex", "hermes"}

    def configure(text):
        config.write_text(text)
        assert main(["--data-dir", str(root), "--providers-config", str(config),
                     "configure-providers"]) == 0
        return json.loads(capsys.readouterr().out)["source_defaults"]

    assert configure("[providers.hermes]\ncapture_enabled = true\nsearch_by_default = false\n")["hermes"] is False
    assert load_providers(config)["hermes"]["capture_enabled"] is True
    # Applying a partial config does not undo an existing exclusion.
    assert configure("[providers.codex]\ncapture_enabled = true\n")["hermes"] is False
    with Catalog(root, readonly=True) as catalog:
        assert ids(catalog) == {"codex"}
        assert ids(catalog, literal=True) == {"codex"}
        assert semantic_ids(catalog) == {"codex"}
        assert ids(catalog, sources=["hermes"]) == {"hermes"}
        assert semantic_ids(catalog, sources=["hermes"]) == {"hermes"}
        assert ids(catalog, all_sources=True) == {"codex", "hermes"}
        assert catalog.context([citation])["results"][0]["status"] == "ok"
    assert configure("[providers.hermes]\ncapture_enabled = false\nsearch_by_default = true\n")["hermes"] is True
    with Catalog(root, readonly=True) as catalog:
        assert ids(catalog) == {"codex", "hermes"}


@pytest.mark.parametrize("settings", ['capture_enabled = "yes"', 'profiles = ["default"]',
                                      'search_by_default = 1'])
def test_hermes_provider_rejects_invalid_settings(tmp_path, settings):
    config = tmp_path / "providers.toml"
    config.write_text(f"[providers.hermes]\n{settings}\n")
    with pytest.raises(ValueError):
        load_providers(config)


def test_hermes_sync_receipt_reports_only_safe_counts(tmp_path):
    provider = {"status": "partial", "discovered": 3, "counts": {"captured": 2,
                "deferred": 1, "unexpected": "private"}, "errors": [{"detail": "private"}]}
    receipt = {"version": 1, "status": "partial", "completed_at": "2026-01-01T00:00:00Z",
               "capture": {"providers": {"hermes": provider}}}
    path = tmp_path / "sync-status.json"
    path.write_text(json.dumps(receipt))
    assert capture_status(tmp_path)["providers"] == {"hermes": {"status": "partial",
        "discovered": 3, "counts": {"captured": 2, "deferred": 1}, "capture_error_count": 1}}
    for key, invalid in [("status", "private"), ("discovered", True),
                         ("counts", {"captured": -1}), ("errors", "private")]:
        receipt["capture"]["providers"]["hermes"] = {**provider, key: invalid}
        path.write_text(json.dumps(receipt))
        assert capture_status(tmp_path) == {"status": "unavailable"}
