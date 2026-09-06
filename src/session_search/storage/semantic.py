"""Content-deduplicated vectors and bounded-memory exact search.

SQLite keeps progress durable; NumPy is imported only by semantic operations.
"""

import heapq
import fcntl
import time
from dataclasses import replace

from session_search.core.embeddings import validate_vector
from session_search.core.records import Citation, digest
from session_search.storage.catalog import Catalog, excerpt, match_offset
from session_search.storage import metadata_index

SEMANTIC_SCHEMA = """
CREATE TABLE IF NOT EXISTS semantic_chunks (
 content_hash TEXT PRIMARY KEY, text TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS event_chunks (
 event_row INTEGER NOT NULL REFERENCES evidence(row_id), content_hash TEXT NOT NULL,
 chunk_start INTEGER NOT NULL, PRIMARY KEY(event_row,chunk_start)
);
CREATE TABLE IF NOT EXISTS vectors (
 identity TEXT NOT NULL, content_hash TEXT NOT NULL, vector BLOB NOT NULL,
 PRIMARY KEY(identity,content_hash)
);
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
    rows = catalog.db.execute(
        "WITH active AS (SELECT DISTINCT ec.content_hash FROM event_chunks ec "
        "JOIN events e ON e.row_id=ec.event_row JOIN heads h "
        "ON e.session_id=h.session_id AND e.revision=h.revision) "
        "SELECT c.content_hash,c.text FROM active a JOIN semantic_chunks c "
        "ON c.content_hash=a.content_hash "
        "LEFT JOIN vectors v ON v.content_hash=c.content_hash AND v.identity=? "
        "LEFT JOIN embedding_failures f ON f.content_hash=c.content_hash AND f.identity=? "
        "WHERE v.vector IS NULL AND COALESCE(f.next_attempt,0)<=? LIMIT ?",
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
    return {"version": 1, "status": "partial" if failed else "ok",
            "embedded": completed, "failed": failed, "deferred": len(rows) - completed - failed,
            "identity": identity}


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
        lexical = catalog.search(replace(query, limit=max(query.limit, 50)))
        try:
            import numpy as np
            vector = np.asarray(validate_vector(provider.embed(query.text, query=True),
                                                provider.identity.dimensions), dtype="<f4")
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
            cursor = catalog.db.execute(
                cte + f"SELECT e.row_id,v.vector,c.chunk_start FROM {source} "
                "JOIN heads h ON h.session_id=e.session_id AND h.revision=e.revision "
                "JOIN revisions r ON r.session_id=e.session_id AND r.revision=e.revision "
                "JOIN event_chunks c ON c.event_row=e.row_id "
                "JOIN vectors v ON v.content_hash=c.content_hash WHERE "
                + " AND ".join(conditions), args,
            )
            best: dict[int, tuple[float, int]] = {}
            while rows := cursor.fetchmany(256):
                matrix = np.stack([np.frombuffer(row["vector"], dtype="<f4") for row in rows])
                if matrix.shape[1] != provider.identity.dimensions or not np.isfinite(matrix).all():
                    raise ValueError("stored embedding is incompatible or corrupt")
                scores = matrix @ vector
                for row, score in zip(rows, scores):
                    key = row["row_id"]
                    candidate = (float(score), row["chunk_start"])
                    previous = best.get(key)
                    if (previous is None or candidate[0] > previous[0]
                            or (candidate[0] == previous[0] and candidate[1] < previous[1])):
                        best[key] = candidate
                if len(best) > 200:
                    # Match final ordering even when SQLite changes its scan order.
                    best = dict(heapq.nlargest(
                        100, best.items(), key=lambda item: (item[1][0], -item[0]),
                    ))
            semantic = []
            for row_id, (score, start) in sorted(
                best.items(), key=lambda pair: (-pair[1][0], pair[0])
            )[:100]:
                row = catalog.db.execute(
                    "SELECT e.*,r.project,r.title FROM events e JOIN revisions r "
                    "ON r.session_id=e.session_id AND r.revision=e.revision WHERE e.row_id=?",
                    (row_id,),
                ).fetchone()
                chunk = row["text"][start:start + 6000]
                offset = start + match_offset(chunk, query.text)
                semantic.append({
                    "citation": Citation(row["session_id"], row["revision"], row["event_id"], offset).to_dict(),
                    "excerpt": excerpt(chunk, query.text), "role": row["role"],
                    "timestamp": row["timestamp"], "origin": row["origin"],
                    "project": row["project"], "title": row["title"], "score": score,
                })
            merged, scores = {}, {}
            for candidates in (lexical["results"], semantic):
                for rank, result in enumerate(candidates, start=1):
                    cite = result["citation"]
                    key = (cite["session_id"], cite["revision"], cite["event_id"])
                    merged[key] = result
                    scores[key] = scores.get(key, 0) + 1 / (60 + rank)
            ordered = sorted(scores, key=lambda key: (-scores[key], key))
            lexical["results"] = [{**merged[key], "score": scores[key]} for key in ordered[:query.limit]]
            lexical["status"] = "ok" if ordered else "no_matches"
            lexical["mode"] = "hybrid"
            lexical["semantic_available"] = True
            lexical["embedding_identity"] = provider.identity.key
            lexical["more_matches"] = len(ordered) > query.limit
            # Coverage needs active row identities, not the large evidence text pages.
            lexical["coverage"]["semantic_indexed"] = catalog.db.execute(
                "SELECT COUNT(DISTINCT a.event_row) FROM active_events a JOIN heads h "
                "ON h.session_id=a.session_id "
                "WHERE EXISTS(SELECT 1 FROM event_chunks c WHERE c.event_row=a.event_row) "
                "AND NOT EXISTS(SELECT 1 FROM event_chunks c LEFT JOIN vectors v "
                "ON v.content_hash=c.content_hash AND v.identity=? "
                "WHERE c.event_row=a.event_row AND v.vector IS NULL)", (provider.identity.key,),
            ).fetchone()[0]
        except (OSError, ValueError, KeyError, TypeError, ImportError) as exc:
            lexical.update(mode="keyword", degraded=True, degradation=type(exc).__name__)
            lexical["results"] = lexical["results"][:query.limit]
        return lexical
    finally:
        catalog.db.rollback()
