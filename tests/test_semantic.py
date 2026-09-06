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


@pytest.mark.parametrize("event_count,long_chunks", [(520, False), (1, True)])
def test_semantic_ties_preserve_candidates_and_offsets_across_scan_orders(
    tmp_path, event_count, long_chunks,
):
    with Catalog(tmp_path) as catalog:
        events = tuple(Event(str(i), "user", ("recovery " * 1500 if long_chunks
                                             else f"recovery evidence {i}"))
                       for i in range(event_count))
        catalog.ingest(SessionRevision("s", events), producer="d", request_id="ties")
        provider = FakeProvider()
        index_pending(catalog, provider, limit=1000)
        query = SearchQuery("restore", limit=100)
        forward = hybrid_search(catalog, query, provider)
        catalog.db.execute("PRAGMA reverse_unordered_selects=ON")
        reverse = hybrid_search(catalog, query, provider)
        assert forward["mode"] == reverse["mode"] == "hybrid"
        assert forward["results"] == reverse["results"]
        assert [hit["citation"]["event_id"] for hit in forward["results"]] == [
            str(i) for i in range(min(event_count, 100))
        ]
        assert all(hit["citation"]["offset"] == 0 for hit in forward["results"])


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


def test_background_admission_and_outage_bound_do_not_block_queries(tmp_path):
    import fcntl

    class Offline(FakeProvider):
        def embed(self, *args, **kwargs):
            raise OSError("offline")

    with Catalog(tmp_path) as catalog:
        catalog.ingest(SessionRevision('s', tuple(Event(str(i), 'user', f'evidence {i}')
                       for i in range(10))), producer='d', request_id='new')
        with (tmp_path / '.embedding-background.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            assert index_pending(catalog, FakeProvider())['status'] == 'coalesced'
            assert catalog.search(SearchQuery('evidence'))['results']
        result = index_pending(catalog, Offline())
        assert result['failed'] == 3 and result['deferred'] == 7
        assert catalog.search(SearchQuery('evidence'))['results']


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


def test_background_work_skips_superseded_only_chunks_but_keeps_shared_evidence(tmp_path):
    from session_search.storage.semantic import prepare_chunks

    class Recording(FakeProvider):
        def __init__(self):
            self.seen = []

        def embed(self, text, *, query=False):
            self.seen.append(text)
            return super().embed(text, query=query)

    with Catalog(tmp_path) as catalog:
        first = SessionRevision('a', (Event('old', 'user', 'superseded recovery'),
                                      Event('shared', 'user', 'shared recovery')))
        catalog.ingest(first, producer='d', request_id='first')
        citation = catalog.search(SearchQuery('superseded'))['results'][0]['citation']
        prepare_chunks(catalog)
        catalog.ingest(SessionRevision('a', (Event('new', 'user', 'current recovery'),)),
                       producer='d', request_id='second')
        catalog.ingest(SessionRevision('b', (Event('shared', 'user', 'shared recovery'),)),
                       producer='d', request_id='other')
        provider = Recording()
        assert index_pending(catalog, provider)['embedded'] == 2
        assert set(provider.seen) == {'current recovery', 'shared recovery'}
        assert index_pending(catalog, provider)['embedded'] == 0
        assert catalog.context([citation])['results'][0]['events'][0]['text'] == 'superseded recovery'
        assert catalog.db.execute('SELECT count(*) FROM semantic_chunks').fetchone()[0] == 3


def test_semantic_page_cache_is_scoped_and_restored_after_fallback(tmp_path):
    with Catalog(tmp_path) as catalog:
        populate(catalog)
        index_pending(catalog, FakeProvider())
        catalog.db.execute("PRAGMA cache_size=-1234")

        class Inspect(FakeProvider):
            fail = False

            def embed(self, text, *, query=False):
                assert catalog.db.execute("PRAGMA cache_size").fetchone()[0] == -16384
                if self.fail:
                    raise OSError("unavailable")
                return super().embed(text, query=query)

        provider = Inspect()
        result = hybrid_search(catalog, SearchQuery("recovery"), provider)
        assert result["mode"] == "hybrid"
        assert catalog.db.execute("PRAGMA cache_size").fetchone()[0] == -1234
        provider.fail = True
        assert hybrid_search(catalog, SearchQuery("recovery"), provider)["degraded"]
        assert catalog.db.execute("PRAGMA cache_size").fetchone()[0] == -1234
        assert not catalog.db.in_transaction
        assert hybrid_search(catalog, SearchQuery("recovery", literal=True), provider)["results"]
        assert catalog.db.execute("PRAGMA cache_size").fetchone()[0] == -1234


def test_coverage_counts_only_complete_current_events_for_selected_model(tmp_path):
    with Catalog(tmp_path) as catalog:
        provider = FakeProvider()
        catalog.ingest(SessionRevision('s', (
            Event('old', 'user', 'old recovery'),
            Event('shared', 'user', 'shared recovery'),
        )), producer='d', request_id='old')
        index_pending(catalog, provider)
        catalog.ingest(SessionRevision('s', (
            Event('shared', 'user', 'shared recovery'),
            Event('long', 'user', 'long recovery ' * 1000),
            Event('pending', 'user', 'pending recovery'),
        )), producer='d', request_id='new')
        # Chunk preparation without successful inference leaves new events uncovered.
        class Offline(FakeProvider):
            def embed(self, *args, **kwargs):
                raise OSError('offline')
        index_pending(catalog, Offline())
        assert hybrid_search(catalog, SearchQuery('recovery'), provider)['coverage']['semantic_indexed'] == 1
        catalog.db.execute('UPDATE embedding_failures SET next_attempt=0')
        catalog.db.commit()
        index_pending(catalog, provider)
        assert hybrid_search(catalog, SearchQuery('recovery'), provider)['coverage']['semantic_indexed'] == 3
        # One missing chunk makes the entire long event incomplete, even though
        # another model has that chunk and this model retains its other chunks.
        key = catalog.db.execute(
            "SELECT c.content_hash FROM event_chunks c JOIN evidence e ON e.row_id=c.event_row "
            "WHERE e.event_id='long' ORDER BY c.chunk_start LIMIT 1"
        ).fetchone()[0]
        catalog.db.execute('UPDATE vectors SET identity=? WHERE identity=? AND content_hash=?',
                           ('different-model', provider.identity.key, key))
        catalog.db.commit()
        assert hybrid_search(catalog, SearchQuery('recovery'), provider)['coverage']['semantic_indexed'] == 2


def test_remote_query_timeout_falls_back_without_shortening_background_requests(tmp_path):
    import threading
    import time
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from session_search.core.embeddings import RemoteEmbedder
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers['Content-Length']))
            time.sleep(0.15)
            body = json.dumps({'model': 'test', 'data': [{'embedding': [1, 0]}]}).encode()
            try:
                self.send_response(200)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        provider = RemoteEmbedder(f'http://127.0.0.1:{server.server_port}', FakeProvider.identity,
                                  model='test', response_model='test', timeout=1, query_timeout=0.03)
        with Catalog(tmp_path) as catalog:
            populate(catalog)
            # Background requests finish despite exceeding the query allowance.
            assert index_pending(catalog, provider)['embedded'] == 2
            start = time.monotonic()
            result = hybrid_search(catalog, SearchQuery('recovery'), provider)
            assert time.monotonic() - start < 0.5
            assert result['mode'] == 'keyword' and result['degraded']
            assert result['results'][0]['excerpt'] == 'disaster recovery'
        assert provider.timeout == 1 and provider.query_timeout == 0.03
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_remote_timeout_configuration_is_explicit_and_validated(tmp_path):
    config = tmp_path / 'embedding.json'
    value = {'mode': 'remote', 'endpoint': 'http://127.0.0.1:1234',
             'identity': {'artifact': 'test', 'dimensions': 2}, 'model': 'test', 'response_model': 'test',
             'timeout': 0.5, 'query_timeout': 0.1}
    config.write_text(json.dumps(value))
    provider = load_provider(config, allow_remote=True)
    assert provider.timeout == 0.5 and provider.query_timeout == 0.1
    for invalid in (0, -1, 61, True, '2'):
        config.write_text(json.dumps({**value, 'query_timeout': invalid}))
        with pytest.raises(ValueError, match='timeouts'):
            load_provider(config, allow_remote=True)
