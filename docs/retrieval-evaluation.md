# Retrieval evaluation

`benchmarks/evaluate_retrieval.py` creates an isolated synthetic catalog and
measures keyword, production hybrid, and diagnostic vector-only retrieval. It
records the dataset checksum, embedding identity, recall and reciprocal rank at
10, first-result accuracy, and latency. A degraded hybrid run fails instead of
being reported as model quality. Use new catalog and report paths outside the
checkout; the evaluator never reads native session directories.

`benchmarks/retrieval-cases.json` contains 50 engineering-memory questions.
`benchmarks/retrieval-holdout.json` adds 12 questions: six conceptual questions and
six exact identifiers with deliberately similar error codes, paths, and commits.
Each case designates one relevant session. This is a synthetic regression corpus,
not a human relevance judgment set or evidence of production latency.

To investigate ranking without changing runtime defaults, run
`benchmarks/compare_fusion.py` against a catalog previously built by the evaluator,
with the same `--cases` and `--embedding-config`. It rejects catalogs whose current
revisions do not exactly match the synthetic input, requires complete matching
vectors, and opens the database read-only. Set `--allow-remote-embeddings` only for
an explicitly configured remote provider and choose a new `--output` report path.

The comparison reuses each query vector across rank constants, semantic weights,
and lexical candidate windows. Its `equal_60` baseline should agree with production
hybrid on this one-event-per-session corpus. Analyze identifier cases separately
from conceptual questions; a better aggregate score can hide identifier regressions.
Keep parameters fixed before examining newly added cases, and expand relevance
judgments before choosing a runtime default. These diagnostic policies are not
user-facing configuration options.

The fusion controls follow the reciprocal-rank formulation described in
[Elastic's RRF reference](https://www.elastic.co/docs/reference/elasticsearch/rest-apis/reciprocal-rank-fusion).
That reference explains the rank constant and candidate window; it does not
establish which values work best for coding-session evidence.

## Literal substring experiment

`benchmarks/compare_literal.py SOURCE_DATA NEW_EXPERIMENT_DIRECTORY` copies the
catalog, builds a contentless FTS5 trigram candidate index in that copy, and compares
four literal queries against the scan plan. The existing substring predicate,
filters, and evidence ordering remain the final authority. No production catalog
or search behavior is changed by this benchmark. The experiment copy contains
private history if the source does; keep it outside the repository.

The pilot index added about 115 MB. Repeated scan queries took roughly 0.9–1.1
seconds; the trigram candidate plans took about 6–62 ms, with the same ordered
evidence for the four queries. These timings describe the initial experiment, not whole-corpus parity.
The opt-in index has since been implemented and deployed on the pilot primary. SQLite documents the
[trigram tokenizer](https://www.sqlite.org/fts5.html#the_trigram_tokenizer) and its
short-query limitations.

The reusable benchmark limits candidate terms to eight distinct printable ASCII
triples and retains the exact substring check. Queries without such a triple use
the scan path. Synthetic checks cover mixed Unicode, quotes, wildcard
characters, embedded NULs, and control characters. Production construction is transactional, checks capacity, and maintains new
evidence on ingestion. Filter and ordering tests are implemented; broader workload
qualification and standby rollout remain open. An extra search index also consumes replica and
backup capacity, which matters on a constrained standby.
