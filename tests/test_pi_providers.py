import asyncio
import json

import pytest

from session_search.capture.local import capture_file, capture_providers
from session_search.capture.parsers.pi import parse_pi
from session_search.core.embeddings import EmbeddingIdentity
from session_search.core.records import Event, SearchQuery, SessionRevision
from session_search.interfaces.mcp import create_mcp
from session_search.storage.catalog import Catalog
from session_search.storage.semantic import hybrid_search, index_pending
from session_search.storage.snapshots import create_snapshot, activate_replica


def pi_file(path, entries):
    path.parent.mkdir(parents=True, exist_ok=True)
    header = {"type": "session", "version": 3, "id": "native", "cwd": "/project"}
    path.write_text("\n".join(json.dumps(e) for e in [header, *entries]) + "\n")
    return path


def message(key, parent, text, role="user"):
    return {"type": "message", "id": key, "parentId": parent,
            "timestamp": "2026-01-01T12:00:00Z", "message": {"role": role, "content": text}}


def test_pi_branch_edits_thinking_tools_and_compaction(tmp_path):
    path = pi_file(tmp_path / "pi.jsonl", [
        message("root", None, "original"), message("abandoned", "root", "wrong branch"),
        message("answer", "root", [{"type": "thinking", "thinking": "private reasoning"},
                                   {"type": "text", "text": "visible answer"},
                                   {"type": "toolCall", "name": "bash", "arguments": {"command": "uptime"}}],
                "assistant"),
        {"type": "context_edit", "id": "edit", "parentId": "answer", "targetId": "root",
         "replacement": {"content": "revised question"}},
        {"type": "compaction", "id": "compact", "parentId": "edit", "summary": "checkpoint summary"},
        message("result", "compact", "tool response", "toolResult"),
        {"type": "context_edit", "id": "remove", "parentId": "result", "targetId": "result", "replacement": None},
    ])
    session = parse_pi(path)
    text = " ".join(e.text for e in session.events)
    assert session.session_id == "pi:native" and session.source == "pi"
    assert "revised question" in text and "uptime" in text and "checkpoint summary" in text
    assert all(t not in text for t in ["original", "wrong branch", "private reasoning", "tool response"])
    assert any(e.role == "context" for e in session.events)
    assert len({e.event_id for e in session.events}) == len(session.events)


def test_pi_capture_partial_tail_idempotence_redaction_and_retained_citation(tmp_path):
    path = pi_file(tmp_path / "pi.jsonl", [message("u", None, "recovery Authorization: Bearer " + "x" * 30)])
    with path.open("a") as f:
        f.write('{"unfinished":')
    with Catalog(tmp_path / "catalog") as catalog:
        catalog.configure_sources({"pi": False})
        assert capture_file(catalog, path, "device", source="pi")["partial_tail"]
        assert capture_file(catalog, path, "device", source="pi")["status"] == "unchanged"
        assert catalog.search(SearchQuery("recovery"))["results"] == []
        hits = catalog.search(SearchQuery("recovery", sources=["pi"]))["results"]
        citation = hits[0]["citation"]
        assert "x" * 30 not in json.dumps(hits)
        pi_file(path, [message("new", None, "new branch")])
        capture_file(catalog, path, "device", source="pi")
        path.unlink()
        assert catalog.context([citation])["results"]


class Provider:
    identity = EmbeddingIdentity("synthetic", dimensions=2)

    def embed(self, text, *, query=False):
        return [1, 0]


def populate(catalog):
    catalog.configure_sources({"pi": False})
    for source in ["codex", "pi"]:
        catalog.ingest(SessionRevision(source, (Event("e", "user", "recovery evidence"),), source=source),
                       producer="machine", request_id=source)


def ids(result):
    return {r["citation"]["session_id"] for r in result["results"]}


def test_source_defaults_filter_all_retrieval_paths_and_survive_replication(tmp_path):
    with Catalog(tmp_path / "primary") as catalog:
        populate(catalog)
        index_pending(catalog, Provider())
        for literal in [False, True]:
            assert ids(hybrid_search(catalog, SearchQuery("recovery", literal=literal), Provider())) == {"codex"}
            assert ids(hybrid_search(catalog, SearchQuery("recovery", literal=literal, sources=["pi"]), Provider())) == {"pi"}
        assert ids(hybrid_search(catalog, SearchQuery("semantic synonym", sources=["pi"]), Provider())) == {"pi"}
        assert ids(catalog.search(SearchQuery("recovery", all_sources=True))) == {"pi", "codex"}
        previous = catalog.publication()
        catalog.configure_sources({"codex": False, "pi": True})
        assert catalog.publication() != previous
        assert ids(catalog.search(SearchQuery("recovery"))) == {"pi"}
        assert ids(catalog.search(SearchQuery("recovery", sources=["codex"]))) == {"codex"}
        create_snapshot(catalog, tmp_path / "snapshot", search_only=True)
    activate_replica(tmp_path / "snapshot", tmp_path / "replica")
    with Catalog(tmp_path / "replica", readonly=True) as replica:
        assert ids(replica.search(SearchQuery("recovery"))) == {"pi"}
        with pytest.raises(PermissionError):
            replica.configure_sources({"pi": False})


@pytest.mark.parametrize("values", [{"sources": []}, {"sources": "pi"},
                                   {"sources": ["pi"], "all_sources": True}, {"all_sources": "yes"}])
def test_invalid_source_overrides(values):
    with pytest.raises(ValueError):
        SearchQuery("query", **values)


def test_capture_config_budget_and_disable_preserves_history(tmp_path):
    config = tmp_path / "providers.toml"
    config.write_text('[providers.codex]\ncapture_enabled = false\n[providers.pi]\ncapture_enabled = true\nsearch_by_default = false\n')
    pi = tmp_path / "pi"
    pi_file(pi / "sessions/project/session.jsonl", [message("u", None, "retained recovery")])
    with Catalog(tmp_path / "catalog") as catalog:
        deferred = capture_providers(catalog, tmp_path / "absent", "device", pi_home=pi,
                                     providers_config=config, max_bytes=1)
        assert deferred["counts"] == {"deferred": 1}
        captured = capture_providers(catalog, tmp_path / "absent", "device", pi_home=pi,
                                     providers_config=config, max_bytes=10000)
        assert captured["providers"]["codex"]["status"] == "disabled"
        assert captured["providers"]["pi"]["counts"] == {"captured": 1}
        config.write_text('[providers.pi]\ncapture_enabled = false\n')
        capture_providers(catalog, tmp_path / "absent", "device", pi_home=pi, providers_config=config)
        assert ids(catalog.search(SearchQuery("recovery", sources=["pi"]))) == {"pi:native"}


def test_mcp_explicit_sources_and_context(tmp_path):
    with Catalog(tmp_path) as catalog:
        populate(catalog)

    async def run():
        server = create_mcp(tmp_path)
        result = json.loads((await server.call_tool("search", {"text": "recovery", "sources": ["pi"]}))[0].text)
        assert ids(result) == {"pi"}
        assert result["source_scope"] == {"mode": "explicit", "sources": ["pi"]}
        citation = result["results"][0]["citation"]
        context = json.loads((await server.call_tool("context", {"citations": [citation]}))[0].text)
        assert context["results"]
    asyncio.run(run())


def test_pi_remote_sync_raw_upload_http_filters_and_status(tmp_path):
    from test_raw_reconciliation import setup
    from session_search.capture.sync import sync_once
    from session_search.interfaces.capture_status import capture_status
    server, client, _, _ = setup(tmp_path)
    with Catalog(server) as catalog:
        catalog.configure_sources({"pi": False})
    config = tmp_path / "providers.toml"
    config.write_text('[providers.pi]\ncapture_enabled = true\nsearch_by_default = false\n')
    pi = tmp_path / "pi"
    pi_file(pi / "sessions/project/session.jsonl", [message("u", None, "computer recovery")])
    root = tmp_path / "client"
    result = sync_once(root, tmp_path / "absent", "d", client=client,
                       pi_home=pi, providers_config=config, max_bytes=100000,
                       max_pending_bytes=100000, reserve_bytes=0, archive_raw=True, chunk_raw=True)
    assert result["status"] == "complete"
    assert result["flush_after"]["sent"] == 1
    assert capture_status(root)["providers"]["pi"]["counts"] == {"captured": 1}
    assert not client.read("search", {"text": "recovery"})["results"]
    hits = client.read("search", {"text": "recovery", "sources": ["pi"]})
    assert ids(hits) == {"pi:native"}
    assert client.read("context", {"citations": [hits["results"][0]["citation"]]})["results"]
    with Catalog(server, readonly=True) as catalog:
        assert catalog.db.execute("SELECT COUNT(*) FROM raw_sources").fetchone()[0] == 1


def test_old_catalog_defaults_and_cli_configuration(tmp_path, capsys):
    from session_search.interfaces.cli import main
    root = tmp_path / "catalog"
    with Catalog(root) as catalog:
        populate(catalog)
        catalog.db.execute("DROP TABLE source_defaults")
        catalog.db.commit()
    with Catalog(root, readonly=True) as catalog:
        assert ids(catalog.search(SearchQuery("recovery"))) == {"codex", "pi"}
    config = tmp_path / "providers.toml"
    config.write_text('[providers.pi]\nsearch_by_default = true\n')
    assert main(["--data-dir", str(root), "--providers-config", str(config), "configure-providers"]) == 0
    capsys.readouterr()
    assert main(["--data-dir", str(root), "search", "recovery", "--source", "pi"]) == 0
    assert ids(json.loads(capsys.readouterr().out)) == {"pi"}


def test_legacy_label_alias_defaults_and_immutable_citations(tmp_path):
    from session_search.core.providers import load_providers
    config = tmp_path / "providers.toml"
    config.write_text('[providers.pi-legacy]\nsearch_by_default = true\n')
    assert load_providers(config)["pi-legacy"]["search_by_default"]
    with Catalog(tmp_path / "catalog") as catalog:
        session = SessionRevision("historical", (Event("u", "user", "legacy recovery"),), source="pi-dev")
        catalog.ingest(session, producer="old", request_id="legacy")
        index_pending(catalog, Provider())
        before = catalog.search(SearchQuery("recovery"))["results"][0]["citation"]
        assert before["revision"] == session.revision
        assert catalog.status()["sources"][0]["source"] == "pi-legacy"
        for label in ["pi-legacy", "pi-dev"]:
            assert ids(hybrid_search(catalog, SearchQuery("synonym", sources=[label]), Provider())) == {"historical"}
        catalog.configure_sources({"pi-legacy": False})
        assert not catalog.search(SearchQuery("recovery"))["results"]
        assert catalog.search(SearchQuery("recovery", sources=["pi-legacy"]))["results"]
        catalog.configure_sources({"pi-legacy": True})
        assert catalog.context([before])["results"]
        assert catalog.search(SearchQuery("recovery"))["results"][0]["citation"] == before


def test_pi_included_by_default_without_explicit_policy(tmp_path):
    with Catalog(tmp_path) as catalog:
        session = SessionRevision("pi-default", (Event("u", "user", "computer update"),), source="pi")
        catalog.ingest(session, producer="d", request_id="new-pi")
        assert ids(catalog.search(SearchQuery("update"))) == {"pi-default"}
        assert catalog.source_defaults()["pi"] is True
