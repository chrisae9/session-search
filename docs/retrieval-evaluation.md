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
and lexical candidate windows. `equal_60` represents the previous fusion policy;
`equal_1` matches the current fusion constant. Neither diagnostic applies evidence
weights or conversation diversification. Analyze identifier cases separately
from conceptual questions; a better aggregate score can hide identifier regressions.
Keep parameters fixed before examining newly added cases, and expand relevance
judgments before choosing a runtime default. These diagnostic policies are not
user-facing configuration options.

The fusion controls follow the reciprocal-rank formulation described in
[Elastic's RRF reference](https://www.elastic.co/docs/reference/elasticsearch/rest-apis/reciprocal-rank-fusion).
That reference explains the rank constant and candidate window; it does not
establish which values work best for coding-session evidence.

## Evidence selection

Ordinary hybrid searches use at least 50 lexical candidates and up to 200 semantic
events. Both pools contribute equally through reciprocal rank, using a constant
of 1. Strong matches near the top
of either pool retain more influence than with the previous constant of 60.
Message evidence receives more weight than tool transcripts. Additional heuristics
downrank generated envelopes, repeated search invocations, planning preambles,
and evaluation prose when those are not the requested subject. These are ranking
hints, not relevance judgments or exclusions; they can also downrank useful text.

Broad searches discount each conversation's next hit by the number of its hits
already selected plus one. This favors diversity while allowing a strong second
answer to outrank weak hits from other conversations. The discount affects
selection; returned scores remain the underlying fusion signals.
A session-scoped search retains passage ordering so an agent can
find the answer within a promising conversation. An exact session ID receives
priority when that session is in the retrieved pool. Literal search bypasses
these heuristics. Role, project, time and source filters still constrain candidates;
immutable citations and context expansion are unchanged.

When both channels retrieve an event, its stronger reciprocal-rank contribution
selects the displayed passage; lexical evidence wins ties. A weak semantic hit
therefore cannot overwrite a strong lexical offset elsewhere in a long event.
Semantic excerpts favor a window covering distinct query terms and explicit
phrases within the selected chunk. Keyword and literal excerpt placement is
unchanged. Term coverage improves presentation, not factual confidence.

Evaluate both conversation discovery and the actual answer passage. Finding the
expected conversation does not establish that the first excerpt answers the
question. Keep repeated benchmark queries out of the judgment evidence, record
paraphrase regressions, and use unseen topics before claiming general accuracy.
The committed regression tests cover transcript noise, conversation diversity,
explicit evidence requests, filtered fallback and citation integrity.

For private history evaluation, select source evidence before writing questions
and record required answer facts and immutable citations. Freeze development
labels before tuning and use different sessions for held-out questions. Compare
policies using the same query vectors and candidate pools, then validate the
chosen implementation end to end. Blindly grade excerpts and expanded evidence
separately, accepting equivalent answers beyond the initially selected citation.
Report misses, partial answers, paired regressions, selection bias, and whether
judgments came from people or agents. Keep transcripts and evaluation artifacts
outside the checkout. A small convenience sample is not a general accuracy claim.

## Generalization checks

Freeze the hypothesis, candidate implementation, context budget and acceptance
criteria before inspecting new search results. Prior questions remain regression
checks after their outcomes have been examined. Do not add query-specific rules,
select favorable cases from retrieval output, or repeatedly tune against a
held-out set. A failed candidate may be a useful result without becoming a
runtime change.

Select private source sessions from metadata before reading their contents, then
author questions from verified evidence. Independently check that a proposed
change, a reported action and a verified outcome have not been conflated. Record
rejected sources and label repairs before running retrieval. Reserve sessions
must remain unread until a separately defined confirmation experiment needs them.
Keep ambiguous questions and questions with verified absence within an explicit
session scope separate from answerable cases; returning candidates does not itself
establish correct abstention.

A fixed comparison tested two isolated ranking hypotheses: equal-weight min-max
score fusion and removal of the planning/evaluation prose penalties. The former
was motivated by [score-fusion research](https://arxiv.org/abs/2210.11934); the latter
tested meaning-preserving invariance, following the approach in
[CheckList](https://aclanthology.org/2020.acl-main.442/). Neither candidate changed
the embedding model, candidate windows, filters or citation format. Parameters
were not selected from private evaluation results.

The external check used a deterministic, category-balanced sample from
[LongMemEval](https://github.com/xiaowu0162/LongMemEval): 24 answerable questions
and six separately reported abstention controls. All supplied history events
were searchable; answer annotations were excluded from indexed text. Each paired
comparison reused query embeddings and candidate pools.

The source was `longmemeval_s_cleaned.json` at dataset revision
`98d7416c24c778c2fee6e6f3006e7a073259d48f`. Selection took the four lowest
SHA-256 values of seed plus question ID within each answerable question type,
then the six lowest among abstention IDs. The seed was
`session-search-generalization-round3-20260907`.

| Policy | Mean labeled-evidence recall at 10 | All required labeled evidence | Paired recall wins / losses |
| --- | ---: | ---: | ---: |
| Baseline | 86.8% | 19/24 | — |
| Equal-weight min-max fusion | 75.0% | 17/24 | 0 / 5 |
| Remove prose penalties | 86.8% | 19/24 | 0 / 0 |

These measure retrieval of labeled evidence, not complete answer accuracy or the
full benchmark. Min-max fusion was rejected. Removing prose penalties improved
fixed-pool style invariance but also regressed a synthetic transcript-noise case;
the external tie did not establish better answer quality. Neither result alone
justifies deployment. Repeated use of this sample is regression testing, not fresh
confirmation.

The prose-penalty candidate subsequently failed a sealed private comparison of
30 source-authored questions across six categories, with six separate controls.
Two agents graded opaque paired rankings independently; a third adjudicated
disagreements and paired differences before the assignment key was opened.
Both original graders agreed on these complete-answer metrics:

| Bounded expanded evidence | Baseline | Remove prose penalties |
| --- | ---: | ---: |
| Complete answer across the top 10 | 17/30 | 16/30 |
| Complete answer in the first hit | 10/30 | 9/30 |
| First complete-hit reciprocal rank, mean | 0.419 | 0.401 |

There were no wins in complete-answer coverage, first-hit completeness or
first-complete-hit rank. The candidate lost one multipart answer from the top 10
and worsened first-complete-hit rank in two other cases. Neither candidate was
adopted. No query-specific exceptions or follow-up parameter search were added.

Each cited event received at most 2,800 characters of context, with no neighboring
events or follow-up searches. These are deliberately bounded retrieval judgments,
not end-to-end agent success rates. The source selection required inspecting 84
discovery transcripts to obtain 30 eligible questions; the resulting small,
agent-authored sample is not representative of every user query. The 90-source
reserve remained unread. Do not compare this sample's success rate directly with
earlier, different private question sets.

Both graders agreed on paired answer-quality losses. They differed on whether
two unchanged first hits were misleading or merely confusable; that sensitivity
does not favor either policy and remains recorded. Immutable evidence, filters,
context bounds and embedding identity passed the collection audit, with no
fallback. Historical recursive exclusion sets were not separately snapshotted,
so their exact earlier membership cannot be independently reconstructed from
later catalog state. Private evidence and full judgments remain outside the repo.

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
evidence on ingestion. A subsequent read-only qualification compared 32 production
search responses against the scan path on one frozen historical catalog. All
responses matched exactly, including ordered citations, excerpts, the `more_matches` flag, and
coverage; 21 cases returned evidence and 11 returned no matches. Cases included
identifiers, short and non-ASCII text, quotes, control characters, and role, time,
project, session, producer, subagent, and exclusion filters. An empty case verifies
agreement on absence, not successful retrieval for that filter.

Alternating which path ran first gave median times of 21 ms indexed and 818 ms
scanning across those cases. This single-pass measurement includes warm-cache
effects and queries that intentionally use the scan fallback. It does not prove
semantic relevance, exhaustive corpus parity, sustained concurrency performance,
or latency on another machine. Standby rollout and additional-machine qualification
remain open. An extra search index also consumes replica and
backup capacity, which matters on a constrained standby.

## Keyword ranking metadata

An isolated frozen-catalog comparison tested three keyword queries, including a
user-role filter, in mirrored execution order. A covering metadata index and
deferred text projection preserved complete responses: ordered citations, scores,
excerpts, filters, and coverage. Per-file cache-eviction hints were applied between
runs, and process-level physical reads were recorded.

The original path took 3.5–9.0 seconds and read about 0.34–1.13 GB per query.
The indexed path took 0.28–1.63 seconds and read about 0.07–0.11 GB. The index added
64 MB and took about 9.6 seconds to build on that host. These are keyword-only
measurements on three queries, not full hybrid latency or whole-corpus relevance
qualification. Cache hints are advisory, and other host work continued.

A separate full-hybrid comparison reused the same frozen catalog and metadata
index, changing only the vector candidate SQL to read covering metadata. Three
queries ran in baseline/candidate/candidate/baseline order with per-file cache
eviction hints. Each query reused one actual query embedding across variants.
All 12 complete responses matched, including scores, citations, and coverage.
The baseline took 19.6–23.9 seconds and read 1.48–2.11 GB; the candidate took
10.2–13.3 seconds and read 0.59–1.52 GB. This added no further index storage.
These cold-cache measurements exclude embedding-request latency and do not
establish the normal client deadline, concurrent performance, or broader relevance.
Coverage accounting and exact vector scoring still contribute to hybrid latency.

## Concurrent embedding requests

A two-minute pilot check kept one synthetic background worker continuously
embedding 6,000-character inputs while issuing one foreground query per second.
All 120 foreground queries and 809 background requests succeeded. Foreground
median latency was 86 ms, p95 was 139 ms, and maximum was 149 ms, below the
configured two-second query timeout. The five-query baseline median was 11 ms.
Normal background services were not paused.

This measures the embedding endpoint under that workload, not complete hybrid
search latency or fairness against arbitrary applications. Session Search's
background indexers share a catalog lock; it is not a deployment-wide scheduler
for unrelated producers or independent catalogs.
