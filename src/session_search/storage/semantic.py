"""Content-deduplicated vectors and bounded-memory exact search.

SQLite keeps progress durable; NumPy is imported only by semantic operations.
"""

import heapq
import fcntl
import time
from dataclasses import replace

from session_search.core.embeddings import validate_vector
from session_search.core.records import Citation, digest
from session_search.storage.catalog import Catalog
from session_search.storage import metadata_index
from session_search.storage.ranking import evidence_weight, rank_results
from session_search.storage.snippets import select_snippet

SEMANTIC_CANDIDATES = 200

SEMANTIC_SCHEMA = """
CREATE TABLE IF NOT EXISTS semantic_chunks (
 content_hash TEXT PRIMARY KEY, text TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS event_chunks (
 event_row INTEGER NOT NULL REFERENCES evidence(row_id), content_hash TEXT NOT NULL,
 chunk_start INTEGER NOT NULL, PRIMARY KEY(event_row,chunk_start)
);
CREATE INDEX IF NOT EXISTS event_chunks_by_hash ON event_chunks(content_hash,event_row,chunk_start);
CREATE TABLE IF NOT EXISTS vectors (
 identity TEXT NOT NULL, content_hash TEXT NOT NULL, vector BLOB NOT NULL,
 PRIMARY KEY(identity,content_hash)
);
CREATE TABLE IF NOT EXISTS vector_cache_state (id INTEGER PRIMARY KEY CHECK(id=1), epoch INTEGER NOT NULL);
INSERT OR IGNORE INTO vector_cache_state VALUES (1,0);
CREATE TRIGGER IF NOT EXISTS vector_cache_update AFTER UPDATE ON vectors BEGIN
 UPDATE vector_cache_state SET epoch=epoch+1 WHERE id=1;
 DELETE FROM semantic_coverage;
END;
CREATE TRIGGER IF NOT EXISTS vector_cache_delete AFTER DELETE ON vectors BEGIN
 UPDATE vector_cache_state SET epoch=epoch+1 WHERE id=1;
 DELETE FROM semantic_coverage;
END;
CREATE TABLE IF NOT EXISTS semantic_coverage (
 identity TEXT PRIMARY KEY, publication TEXT NOT NULL, indexed_events INTEGER NOT NULL,
 computed_at TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS semantic_coverage_insert AFTER INSERT ON vectors BEGIN
 DELETE FROM semantic_coverage WHERE identity=NEW.identity;
END;
CREATE TABLE IF NOT EXISTS embedding_failures (
 identity TEXT NOT NULL, content_hash TEXT NOT NULL, attempts INTEGER NOT NULL,
 next_attempt REAL NOT NULL, error TEXT NOT NULL,
 PRIMARY KEY(identity,content_hash)
);
"""


def prepare_chunks(catalog: Catalog):
    if catalog.readonly:
        raise PermissionError("standby cannot prepare chunks")
    catalog.db.executescript(SEMANTIC_SCHEMA)
    cursor = catalog.db.execute(
        "SELECT e.row_id,e.text FROM events e JOIN heads h ON e.session_id=h.session_id "
        "AND e.revision=h.revision WHERE NOT EXISTS(SELECT 1 FROM event_chunks c "
        "WHERE c.event_row=e.row_id)"
    )
    while rows := cursor.fetchmany(100):
        with catalog.db:
            for row in rows:
                for start in range(0, max(1, len(row["text"])), 5700):
                    text = row["text"][start:start + 6000]
                    key = digest(text.encode())
                    catalog.db.execute("INSERT OR IGNORE INTO semantic_chunks VALUES (?,?)", (key, text))
                    catalog.db.execute("INSERT OR IGNORE INTO event_chunks VALUES (?,?,?)",
                                       (row[0], key, start))
            catalog.bump_publication()


def index_pending(catalog: Catalog, provider, *, limit: int = 100) -> dict:
    if catalog.readonly:
        raise PermissionError("standby cannot index embeddings")
    with (catalog.root / ".embedding-background.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"version": 1, "status": "coalesced", "embedded": 0, "failed": 0,
                    "identity": provider.identity.key}
        return _index_pending(catalog, provider, limit=limit)


def _index_pending(catalog: Catalog, provider, *, limit: int) -> dict:
    import numpy as np
    if not 1 <= limit <= 10000:
        raise ValueError("embedding limit must be between 1 and 10000")
    prepare_chunks(catalog)
    identity = provider.identity.key
    # Preserve sequential event traversal: the reverse hash index serves ranked
    # retrieval but would turn this background scan into random evidence reads.
    rows = catalog.db.execute(
        "WITH active AS (SELECT DISTINCT ec.content_hash FROM event_chunks ec NOT INDEXED "
        "JOIN events e ON e.row_id=ec.event_row JOIN heads h "
        "ON e.session_id=h.session_id AND e.revision=h.revision) "
        "SELECT c.content_hash,c.text FROM active a JOIN semantic_chunks c "
        "ON c.content_hash=a.content_hash "
        "LEFT JOIN vectors v ON v.content_hash=c.content_hash AND v.identity=? "
        "LEFT JOIN embedding_failures f ON f.content_hash=c.content_hash AND f.identity=? "
        "WHERE v.content_hash IS NULL AND COALESCE(f.next_attempt,0)<=? LIMIT ?",
        (identity, identity, time.time(), limit),
    ).fetchall()
    completed = failed = 0
    consecutive_failures = 0
    for row in rows:
        try:
            vector = validate_vector(provider.embed(row["text"]), provider.identity.dimensions)
            data = np.asarray(vector, dtype="<f4").tobytes()
            with catalog.db:
                catalog.db.execute("INSERT OR IGNORE INTO vectors VALUES (?,?,?)",
                                   (identity, row["content_hash"], data))
                catalog.db.execute("DELETE FROM embedding_failures WHERE identity=? AND content_hash=?",
                                   (identity, row["content_hash"]))
                catalog.bump_publication()
            completed += 1
            consecutive_failures = 0
        except (OSError, ValueError, KeyError, TypeError) as exc:
            with catalog.db:
                catalog.db.execute(
                    "INSERT INTO embedding_failures VALUES (?,?,1,?,?) "
                    "ON CONFLICT(identity,content_hash) DO UPDATE SET attempts=attempts+1,"
                    "next_attempt=excluded.next_attempt,error=excluded.error",
                    (identity, row["content_hash"], time.time() + 60, type(exc).__name__),
                )
                catalog.bump_publication()
            failed += 1
            consecutive_failures += 1
            if consecutive_failures >= 3:
                break  # Do not hammer an unavailable provider with the entire backlog.
    refresh_coverage(catalog, identity)
    from session_search.storage.vector_cache import build
    cache = build(catalog, provider.identity)
    return {"version": 1, "status": "partial" if failed else "ok",
            "embedded": completed, "failed": failed, "deferred": len(rows) - completed - failed,
            "identity": identity, "vector_cache": cache}


def hybrid_search(catalog: Catalog, query, provider=None) -> dict:
    if provider is None or query.literal:
        return catalog.search(query)
    # Large vector/evidence joins otherwise thrash SQLite's small default page
    # cache. Keep the allowance scoped to this read and restore caller settings.
    previous_cache = catalog.db.execute("PRAGMA cache_size").fetchone()[0]
    catalog.db.execute("PRAGMA cache_size=-16384")
    try:
        return _hybrid_search(catalog, query, provider)
    finally:
        catalog.db.execute(f"PRAGMA cache_size={int(previous_cache)}")


def _hybrid_search(catalog: Catalog, query, provider) -> dict:
    # Hold a coherent read snapshot for lexical candidates, vector candidates,
    # and their evidence while background publication advances the current head.
    catalog.db.execute("BEGIN")
    try:
        started = time.monotonic()
        lexical = catalog.search(replace(query, limit=max(query.limit, 50)), raw_candidates=True)
        timings = {"lexical_ms": round((time.monotonic() - started) * 1000, 2)}
        try:
            import numpy as np
            stage = time.monotonic()
            vector = np.asarray(validate_vector(provider.embed(query.text, query=True),
                                                provider.identity.dimensions), dtype="<f4")
            timings["embedding_ms"] = round((time.monotonic() - stage) * 1000, 2)
            has_vectors = catalog.db.execute(
                "SELECT 1 FROM sqlite_master WHERE name='vectors'"
            ).fetchone()
            if not has_vectors:
                raise ValueError("semantic index is not initialized")
            cte, conditions, args = catalog.scope(query)
            # Identity is checked in SQL before a vector is ever decoded.
            conditions.append("v.identity=?")
            args.append(provider.identity.key)
            # Candidate filtering only needs metadata; load transcript text for
            # the final semantic hits below. Older catalogs retain the view path.
            source = metadata_index.event_source() if metadata_index.ready(catalog.db) else "events e"
            from session_search.storage.vector_cache import scores as cache_scores
            stage = time.monotonic()
            cached = cache_scores(catalog, provider.identity, vector, deadline=started + 20)
            timings["vector_cache_ms"] = round((time.monotonic() - stage) * 1000, 2)
            vector_column = "v.vector" if cached is None else "v.rowid AS vector_row"
            ranked = (cached is not None and not any((query.role, query.after, query.before,
                      query.project, query.session_id, query.producer, query.sources))
                      and catalog.db.execute("SELECT 1 FROM sqlite_master WHERE name='event_chunks_by_hash'").fetchone())
            if ranked:
                batches = ranked_batches(catalog, cached, source, cte, conditions, args)
            else:
                cursor = catalog.db.execute(
                    cte + f"SELECT e.row_id,{vector_column},c.chunk_start FROM {source} "
                    "JOIN heads h ON h.session_id=e.session_id AND h.revision=e.revision "
                    "JOIN revisions r ON r.session_id=e.session_id AND r.revision=e.revision "
                    "JOIN event_chunks c ON c.event_row=e.row_id "
                    "JOIN vectors v ON v.content_hash=c.content_hash WHERE "
                    + " AND ".join(conditions), args,
                )
                batches = ((rows, None) for rows in iter(lambda: cursor.fetchmany(256), []))
            stage = time.monotonic()
            best: dict[int, tuple[float, int]] = {}
            semantic_overflow = False
            for rows, frontier in batches:
                if not rows:
                    if semantic_overflow and len(best) >= SEMANTIC_CANDIDATES:
                        cutoff = heapq.nlargest(SEMANTIC_CANDIDATES, (v[0] for v in best.values()))[-1]
                        if frontier < cutoff:
                            break
                    continue
                if time.monotonic() > started + 20:
                    from session_search.core.deadlines import SearchDeadline
                    raise SearchDeadline()
                if cached is None:
                    matrix = np.stack([np.frombuffer(row["vector"], dtype="<f4") for row in rows])
                    if matrix.shape[1] != provider.identity.dimensions or not np.isfinite(matrix).all():
                        raise ValueError("stored embedding is incompatible or corrupt")
                    scores = matrix @ vector
                else:
                    ids, values = cached
                    wanted = np.asarray([row["vector_row"] for row in rows], dtype='<i8')
                    positions = np.searchsorted(ids, wanted)
                    valid = positions < len(ids)
                    valid[valid] &= ids[positions[valid]] == wanted[valid]
                    scores = np.empty(len(rows), dtype='<f4')
                    scores[valid] = values[positions[valid]]
                    # A missing segment cannot hide evidence: resolve any missing
                    # IDs from the authoritative snapshot instead.
                    for index in np.flatnonzero(~valid):
                        data = catalog.db.execute('SELECT vector FROM vectors WHERE rowid=?',
                                                  (int(wanted[index]),)).fetchone()[0]
                        stored = np.frombuffer(data, dtype='<f4')
                        if len(stored) != provider.identity.dimensions or not np.isfinite(stored).all():
                            raise ValueError("stored embedding is incompatible or corrupt")
                        scores[index] = stored @ vector
                for row, score in zip(rows, scores):
                    key = row["row_id"]
                    candidate = (float(score), row["chunk_start"])
                    previous = best.get(key)
                    if (previous is None or candidate[0] > previous[0]
                            or (candidate[0] == previous[0] and candidate[1] < previous[1])):
                        best[key] = candidate
                semantic_overflow = semantic_overflow or len(best) > SEMANTIC_CANDIDATES
                if len(best) > 2 * SEMANTIC_CANDIDATES:
                    # Match final ordering even when SQLite changes its scan order.
                    best = dict(heapq.nlargest(
                        SEMANTIC_CANDIDATES, best.items(),
                        key=lambda item: (item[1][0], -item[0]),
                    ))
            timings["vector_candidates_ms"] = round((time.monotonic() - stage) * 1000, 2)
            lexical["vector_engine"] = "sqlite" if cached is None else "exact_mmap_ranked" if ranked else "exact_mmap"
            semantic = []
            for row_id, (score, start) in sorted(
                best.items(), key=lambda pair: (-pair[1][0], pair[0])
            )[:SEMANTIC_CANDIDATES]:
                row = catalog.db.execute(
                    "SELECT e.*,r.project,r.title FROM events e JOIN revisions r "
                    "ON r.session_id=e.session_id AND r.revision=e.revision WHERE e.row_id=?",
                    (row_id,),
                ).fetchone()
                chunk = row["text"][start:start + 6000]
                position, preview = select_snippet(chunk, query.text)
                offset = start + position
                semantic.append({
                    "citation": Citation(row["session_id"], row["revision"], row["event_id"], offset).to_dict(),
                    "excerpt": preview, "role": row["role"],
                    "timestamp": row["timestamp"], "origin": row["origin"],
                    "project": row["project"], "title": row["title"], "score": score,
                    "_evidence_weight": evidence_weight(row["text"], row["role"], query),
                })
            ordered = rank_results(query, lexical["results"], semantic)
            lexical["results"] = ordered[:query.limit]
            lexical["status"] = "ok" if ordered else "no_matches"
            lexical["mode"] = "hybrid"
            lexical["semantic_available"] = True
            lexical["embedding_identity"] = provider.identity.key
            lexical["more_matches"] = (lexical["more_matches"] or semantic_overflow
                                       or len(ordered) > query.limit)
            # Global coverage is maintained by the background worker, not scanned
            # across the entire corpus as part of an interactive request.
            lexical["coverage"].update(cached_coverage(catalog, provider.identity.key))
        except (OSError, ValueError, KeyError, TypeError, ImportError) as exc:
            lexical.update(mode="keyword", degraded=True, degradation="TimeoutError" if isinstance(exc, TimeoutError) else type(exc).__name__)
            reason = getattr(exc, 'reason', None)
            if reason in {'local_inference_busy', 'local_inference_execution_limit',
                          'local_startup_timeout', 'local_query_timeout', 'search_deadline'}:
                lexical['degradation_reason'] = reason
            lexical["more_matches"] = (lexical["more_matches"]
                                       or len(lexical["results"]) > query.limit)
            lexical["results"] = rank_results(query, lexical["results"])[:query.limit]
        timings["total_ms"] = round((time.monotonic() - started) * 1000, 2)
        lexical["timings_ms"] = timings
        return lexical
    finally:
        catalog.db.rollback()


def cached_coverage(catalog, identity):
    if catalog.db.execute("SELECT 1 FROM sqlite_master WHERE name='semantic_coverage'").fetchone():
        row = catalog.db.execute('SELECT publication,indexed_events,computed_at FROM semantic_coverage WHERE identity=?',
                                 (identity,)).fetchone()
        if row:
            return {"semantic_indexed": row["indexed_events"],
                    "semantic_coverage_status": "current" if row["publication"] == catalog.publication() else "stale",
                    "semantic_coverage_computed_at": row["computed_at"]}
    return {"semantic_indexed": None, "semantic_coverage_status": "not_computed"}


def refresh_coverage(catalog, identity):
    if cached_coverage(catalog, identity).get('semantic_coverage_status') == 'current':
        return
    catalog.db.execute('BEGIN')
    try:
        observed = catalog.publication()
        indexed = catalog.db.execute(
                "SELECT COUNT(DISTINCT a.event_row) FROM active_events a JOIN heads h "
                "ON h.session_id=a.session_id "
                "WHERE EXISTS(SELECT 1 FROM event_chunks c WHERE c.event_row=a.event_row) "
                "AND NOT EXISTS(SELECT 1 FROM event_chunks c LEFT JOIN vectors v "
                "ON v.content_hash=c.content_hash AND v.identity=? "
                "WHERE c.event_row=a.event_row AND v.content_hash IS NULL)", (identity,),
            ).fetchone()[0]

    finally:
        catalog.db.rollback()
    catalog.db.execute('BEGIN IMMEDIATE')
    try:
        if catalog.publication() == observed:
            catalog.bump_publication()
            observed = catalog.publication()
        catalog.db.execute("INSERT OR REPLACE INTO semantic_coverage VALUES (?,?,?,strftime('%Y-%m-%dT%H:%M:%fZ','now'))",
                           (identity, observed, indexed))
        catalog.db.commit()
    except BaseException:
        catalog.db.rollback()
        raise


def ranked_batches(catalog, cached, source, cte, conditions, args):
    """Visit exact global scores in order, applying all evidence filters in SQL.

    The caller stops only below the proven top-event cutoff, including every tie.
    Selective queries retain the direct scoped-join path instead.
    """
    import numpy as np
    ids, values = cached
    order = np.argsort(-values, kind='stable')
    for start in range(0, len(order), 512):
        selected = order[start:start + 512]
        placeholders = ','.join('?' for _ in selected)
        cursor = catalog.db.execute(
            cte + f"SELECT e.row_id,v.rowid AS vector_row,c.chunk_start FROM vectors v NOT INDEXED "
            "CROSS JOIN event_chunks c INDEXED BY event_chunks_by_hash "
            f"CROSS JOIN {source} CROSS JOIN heads h CROSS JOIN revisions r WHERE "
            "c.content_hash=v.content_hash AND e.row_id=c.event_row "
            "AND h.session_id=e.session_id AND h.revision=e.revision "
            "AND r.session_id=e.session_id AND r.revision=e.revision AND "
            + ' AND '.join(conditions) + f' AND v.rowid IN ({placeholders})',
            (*args, *(int(value) for value in ids[selected])),
        )
        while rows := cursor.fetchmany(256):
            yield rows, None
        end = start + len(selected)
        yield [], float(values[order[end]]) if end < len(order) else float('-inf')
