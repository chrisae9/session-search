import sqlite3

import pytest

from session_search.core.output import bounded_response
from session_search.core.records import Citation, Event, SearchQuery, SessionRevision, canonical_json
from session_search.storage.catalog import Catalog


def session(text="backup restore", **kwargs):
    return SessionRevision("session-1", (Event("e1", "user", text, "2026-01-01Z"),), **kwargs)


def test_revision_retry_never_rewinds_head_or_changes_old_citation(tmp_path):
    with Catalog(tmp_path) as catalog:
        first = session()
        second = session("new migration solution")
        catalog.ingest(first, producer="a", request_id="1")
        cite = Citation(**catalog.search(SearchQuery("backup"))["results"][0]["citation"])
        catalog.ingest(second, producer="a", request_id="2")
        catalog.ingest(first, producer="a", request_id="1")
        catalog.ingest(first, producer="b", request_id="import-old")
        assert catalog.search(SearchQuery("backup"))["status"] == "no_matches"
        assert catalog.context([cite])["results"][0]["events"][0]["text"] == "backup restore"
        assert catalog.export_revision(first.session_id, first.revision)["events"][0]["text"]


def test_transaction_failure_rolls_back_entire_revision(tmp_path):
    with Catalog(tmp_path) as catalog:
        catalog.db.execute("CREATE TRIGGER reject_event BEFORE INSERT ON evidence BEGIN "
                           "SELECT RAISE(ABORT, 'injected crash'); END")
        with pytest.raises(sqlite3.IntegrityError):
            catalog.ingest(session(), producer="a", request_id="1")
        assert catalog.status()["sessions"] == 0
        assert catalog.db.execute("SELECT COUNT(*) FROM revisions").fetchone()[0] == 0
        assert catalog.db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 0


def test_conflicting_idempotency_key_rejected(tmp_path):
    with Catalog(tmp_path) as catalog:
        catalog.ingest(session(), producer="a", request_id="1")
        with pytest.raises(ValueError, match="idempotency"):
            catalog.ingest(session("different"), producer="a", request_id="1")


def test_filters_are_applied_to_matching_event_and_do_not_relax(tmp_path):
    events = (
        Event("old", "user", "exact disk failure", "2025-01-01T12:00:00Z"),
        Event("new", "user", "successful restore", "2026-01-01T12:00:00Z"),
        Event("notice", "user", "exact disk failure", "2026-01-01T12:00:00Z", "notification"),
        Event("answer", "assistant", "exact disk failure", "2026-01-01T12:00:00Z"),
    )
    with Catalog(tmp_path) as catalog:
        catalog.ingest(SessionRevision("a", events, project="demo"), producer="device", request_id="1")
        assert not catalog.search(SearchQuery("disk", role="user", after="2026-01-01"))["results"]
        assert not catalog.search(SearchQuery("disk exact", literal=True))["results"]
        assert not catalog.search(SearchQuery("disk", project="absent"))["results"]
        assert len(catalog.search(SearchQuery('"exact disk"', role="assistant"))["results"]) == 1
        assert len(catalog.search(SearchQuery("restore", producer="device"))["results"]) == 1
        assert not catalog.search(SearchQuery("restore", producer="other"))["results"]


def test_current_tree_exclusion_is_recursive(tmp_path):
    with Catalog(tmp_path) as catalog:
        for sid, parent in [("a", None), ("b", "a"), ("c", "b")]:
            catalog.ingest(SessionRevision(sid, (Event("e", "user", "match"),),
                                           parent_session_id=parent), producer="d", request_id=sid)
        assert not catalog.search(SearchQuery("match", exclude_sessions=("a",)))["results"]


def test_context_budget_keeps_matched_event_and_reports_omissions(tmp_path):
    with Catalog(tmp_path) as catalog:
        revision = SessionRevision("a", tuple(Event(str(i), "user", "é" * 15000) for i in range(5)))
        catalog.ingest(revision, producer="d", request_id="1")
        cite = Citation("a", revision.revision, "2")
        response = bounded_response(catalog.context([cite]), 2048)
        assert len(canonical_json(response).encode()) <= 2048
        assert response["truncated"]
        assert response["omitted_events"] == 4
        assert response["results"][0]["events"][0]["event_id"] == "2"
        assert response["results"][0]["citation"] == cite.to_dict()


def test_readonly_catalog_rejects_writes_and_absent_revision_is_explicit(tmp_path):
    with Catalog(tmp_path) as catalog:
        catalog.ingest(session(), producer="a", request_id="1")
    with Catalog(tmp_path, readonly=True) as catalog:
        with pytest.raises(PermissionError):
            catalog.ingest(session(), producer="a", request_id="2")
        response = catalog.context([Citation("session-1", "missing", "e1")])
        assert response["status"] == "partial"
        assert response["results"][0]["status"] == "unavailable"


def test_growing_sessions_share_unchanged_evidence_between_revisions(tmp_path):
    events = []
    with Catalog(tmp_path) as catalog:
        for index in range(50):
            events.append(Event(str(index), "user", f"evidence {index} " + "context " * 100))
            current = SessionRevision("growing", tuple(events))
            catalog.ingest(current, producer="d", request_id=str(index))
        assert catalog.db.execute("SELECT COUNT(*) FROM revisions").fetchone()[0] == 50
        assert catalog.db.execute("SELECT COUNT(*) FROM evidence").fetchone()[0] == 50
        assert catalog.db.execute("SELECT COUNT(*) FROM evidence_fts").fetchone()[0] == 50
        assert catalog.db.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 50


def test_older_catalog_lookup_index_upgrade_preserves_history_and_readonly_access(tmp_path):
    with Catalog(tmp_path) as catalog:
        catalog.ingest(session(), producer="device", request_id="first")
        citation = catalog.search(SearchQuery("restore"))["results"][0]["citation"]
        publication = catalog.publication()
        catalog.db.execute("DROP INDEX active_events_by_evidence")
        catalog.db.commit()
    with Catalog(tmp_path, readonly=True) as catalog:
        assert catalog.search(SearchQuery("restore"))["results"][0]["citation"] == citation
        assert not catalog.db.execute("SELECT 1 FROM sqlite_master WHERE name='active_events_by_evidence'").fetchone()
    with Catalog(tmp_path) as catalog:
        assert catalog.db.execute("SELECT 1 FROM sqlite_master WHERE name='active_events_by_evidence'").fetchone()
        assert catalog.publication() == publication
        assert catalog.search(SearchQuery("restore"))["results"][0]["citation"] == citation
        catalog.ingest(session("continued work"), producer="device", request_id="second")
        assert not catalog.search(SearchQuery("restore"))["results"]
        assert catalog.context([citation])["results"][0]["events"][0]["text"] == "backup restore"
        assert catalog.db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
