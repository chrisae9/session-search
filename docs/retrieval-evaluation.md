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

## Passage lexical retrieval experiment

A separate, unshipped prototype ranked each event by its strongest lexical
passage, using the existing semantic geometry of 6,000 characters with a
5,700-character stride. Filters applied before selecting distinct events; the
candidate budget, fusion policy, embedding model and immutable citations stayed
fixed. The hypothesis was that localized evidence in long events could enter the
lexical candidate pool more reliably than with whole-event BM25.

Synthetic checks demonstrated that mechanism, but also exposed two disadvantages:
quoted phrases longer than the overlap can cross a passage boundary and lose
their lexical match, and concentrated incidental keywords can outrank useful
concepts distributed throughout an event. Passing contract checks does not turn
those cases into quality improvements. Literal search retained its original path.

The final prototype used a contentless passage index and deferred the discarded
whole-event lexical query until fallback was needed. Complete response and
fallback checks passed on synthetic fixtures, including concurrent publication.
A smaller reused workload passed timing and storage checks, but a separately
frozen synthetic workload of 60,000 events and 67,500 passages failed admission:

| Lexical phase p95 | Baseline | Candidate | Preset maximum |
| --- | ---: | ---: | ---: |
| Warm | 130.5 ms | 212.6 ms | 380.5 ms |
| Advisory cold | 220.3 ms | 1,465.0 ms | 470.3 ms |

The comparison included query validation, coverage and index-population checks,
used mirrored execution order, and retained the baseline covering metadata index.
Cold runs used fresh processes and per-file cache-eviction hints; these do not
guarantee an empty operating-system cache. Storage and per-process memory passed
their limits on this workload. Earlier inherited process-memory statistics were
insufficient to establish memory overhead; the scale run measured each executed
worker's own peak instead.

The prototype was rejected without changing the limits or tuning on fresh
questions. The larger planned scale run, full-hybrid qualification and fresh
answer-quality evaluation did not run. This is a resource rejection of the tested
implementation, not evidence that passage retrieval generally worsens accuracy.
No production behavior changed; private experiment artifacts remain outside the
repository.

## Cross-channel excerpt experiment

Another unshipped prototype compared the existing lexical and semantic excerpts
when both channels retrieved the same immutable event. It switched excerpts only
for a strict advantage under the existing snippet query-coverage objective,
preserving event scores, ordering, metadata and citation identity. Only the
displayed excerpt and its original offset could change.

Independent, sealed synthetic fixtures exposed the weakness of that proxy.
Across 14 deliberately adversarial excerpt pairs, visible supported facts fell
from 11/19 to 7/19: five fact gains across three cases, nine losses across seven
cases, and four unchanged cases. The losses included question echoes, stale
proposals, negation and code comments. All selected bodies survived the default
response budget. These constructed pairs are not a representative accuracy
estimate or evidence of their frequency in real sessions.

A separate synthetic integration check reproduced both a benefit and an echo
failure through the actual renderers and hybrid search, using deterministic test
embeddings. In the failure, the replacement offset also hid the answer when
expanding bounded context. Identical event rankings therefore did not establish
equivalent answer evidence. The selector was rejected before fresh external or
private quality evaluation; production remained unchanged.

The distinction agrees with the limitations discussed in
[query-aware snippet research](https://aclanthology.org/2022.emnlp-main.197/):
word overlap alone does not capture semantic or document context. That work does
not validate this prototype's policy or quantify its production impact.

## Citation-centered context experiment

An unshipped prototype preserved the cited offset when shortening context to fit
the response byte budget. It selected an exact source slice around that offset,
kept cumulative truncation metadata, and ensured the reducer made progress.
Search ordering, embeddings and source citations stayed fixed. The hypothesis
was that preserving the search anchor would retain more answer evidence than
repeatedly keeping only an event's prefix.

Independent synthetic cases demonstrated both benefits and losses: an anchor can
point to incidental text while the answer occurs elsewhere. The actual client
transport was replayed with its metadata included before applying the final
budget. Source-first rubrics and blind, independently reviewed judgments scored
the text actually delivered, with each budget reported separately.

| Complete answers | Baseline | Candidate |
| --- | ---: | ---: |
| Fresh public sample, 32,768 bytes | 14/24 | 14/24 |
| Fresh public sample, 8,192 bytes | 12/24 | 13/24 |
| Historical private regression, 32,768 bytes | 17/30 | 17/30 |
| Historical private regression, 8,192 bytes | 14/30 | 13/30 |

The public sample had one complete-answer gain and no losses at the smaller
budget. The historical regression had no complete-answer gains and one loss;
individual facts had two gains and three losses. Six controls per sample and
budget produced no misleading answers under either policy. Public histories had
source-label and missing-timestamp limitations; all selected cases were retained.
Historical questions reused previously selected citations, with no new search or
embedding calls, and are regression evidence rather than a fresh quality sample.

The candidate was rejected without adjusting the cropping geometry. The fresh
private reserve remained unread. An isolated package test run reported 335 tests
passing, and contract checks verified source slices and bounds, but those checks
do not override the answer-quality regression. Baseline reducer timeouts in
separate restrictive synthetic runs were recorded as operational failures, not
counted as answer-quality wins. No runtime change was deployed.

## Identifier decomposition experiment

A separate unshipped prototype added a low-weight lexical field containing
components of case-delimited identifiers. Original text stayed indexed; literal
search, semantic retrieval and fusion settings stayed unchanged. This tested a
representation mismatch: ordinary words can match underscore-separated names
under the existing tokenizer, but do not necessarily match camel-case names.
[Identifier preprocessing research](https://assets.ptidej.net/Publications/Documents/ICPC11b.doc.pdf)
motivated the experiment without establishing a benefit for session search.

An independent author constructed 40 answerable cases across identifier recall,
exact identifiers, ordinary prose, misleading code and multilingual/punctuation
cases, plus eight scoped no-answer controls. Source-first labels were separately
verified before retrieval. Two blind graders reviewed the delivered evidence and
cross-reviewed each other's judgments before policy unblinding.

Both policies supplied complete answers for 38/40 questions, with no paired wins
or losses and no misleading answers on the controls. All 96 isolated executions
completed. This is a synthetic lexical comparison, not a full-hybrid or real-user
accuracy estimate. One false-premise question and mixed-language category
limitations were retained and documented in the judgments.

The candidate did not meet the preset requirement for a positive quality change.
It was not tuned or deployed; full resource and fresh private confirmation runs
were not admitted. Extra component tokens can change raw-query ranking, and
phrases in the derived field can bridge identifiers separated in the source.
Passing contract tests does not establish that those costs are worthwhile.

## Adjacent conversational context experiment

An unshipped prototype embedded an assistant response together with its immediately
preceding user message when the complete pair fit the existing 6,000-character
limit. It retained raw vectors and original citations, used one semantic candidate
stream, and preserved the baseline route when disabled or when role/time filters
were present. [Conversational retrieval research](https://aclanthology.org/J19-1005/)
motivated supplying missing conversational context; it did not establish that this
particular representation would improve session retrieval.

Independent synthetic diagnostics tied at 36/36 complete answers, with no
misleading results on eight controls. An initial fixture version could not activate
the candidate because its user events had synthetic origins. That run was invalid
for candidate qualification; a uniformly corrected source-only fixture exercised
all 88 expected pairs before the valid comparison. The small diagnostic corpus
does not establish naturalistic accuracy.

A frozen public comparison used 60 answerable-designated LongMemEval questions
and 12 controls, excluding previously consumed question families. Source-first
rubrics were independently verified before retrieval. Six questions lacked a
required source-supported premise; they remained in the denominator and could not
receive complete credit. Two agents graded randomized, blinded evidence and then
cross-reviewed every case before unblinding, with no judgment changes.

| Delivered complete-answer evidence | Baseline | Candidate |
| --- | ---: | ---: |
| All 60 designated answerable questions | 47/60 | 46/60 |
| Controls with potentially misleading near matches | 8/12 | 8/12 |

The paired table contained 46 shared successes, zero candidate-only successes,
one baseline-only success and 13 shared misses. The candidate lost a previously
known constraint needed to tailor advice. The net change was −1.7 percentage
points, below the preset requirement of at least +5 points. One discordant pair
does not establish a population-level regression (exact two-sided McNemar p=1).
Control judgments concern tempting wrong-entity evidence, not observed agent
answers or an abstention success rate.

All 72 cases and 216 execution phases completed without provider errors or
semantic fallback. Both policies reused actual query vectors with the same model.
Delivered search and context evidence passed source, citation and byte-bound
checks. The measurement covers a fixed top-10 search and neighboring-context
workflow, not autonomous agent success, exhaustive filter coverage or production
performance. Public-history overlap and agent-authored judgments limit inference.

The candidate was rejected without tuning. A prospective stage-order amendment
placed public quality before the expensive full resource comparison; resource,
fresh private and deployment qualification therefore did not run and are not
claimed as passed. The private reserve remained unread and production unchanged.

## CPU reranking experiment

A private, optional cross-encoder prototype reranks the first 50 already-fused
event excerpts, then retains the remaining candidates in their original order.
It uses the original query and selected excerpt only, with stable descending
logit ordering and no score blending. Filters, source text, immutable citations,
embedding identity and the index remain unchanged. This prototype is not a
released configuration option.

The fixed candidate uses `cross-encoder/ms-marco-TinyBERT-L2-v2`, revision
`81d1926f67cb8eee2c2be17ca9f793c7c3bd20cc`, with the owner's quantized AVX2 ONNX
artifact. CPU feasibility motivated selection; target-quality outcomes did not.
Its isolated helper loads verified local bytes, admits one inference at a time,
and falls back to the original response on failure. A 500 ms warm-stage deadline
is a failure bound, not an allowance to exceed the existing end-to-end latency
gate. Full resource and transport qualification remain open.

Source-first synthetic diagnostics covered 24 answerable questions and six
controls over one shared 60-session corpus. Both policies delivered complete
required evidence for 24/24 questions. Blind grading and independent cross-review
identified one potentially misleading control for the baseline and none for the
candidate; all 30 candidate requests applied without fallback. This is a
correlated synthetic test with a ceiling on answerable cases, not evidence of
better natural-session accuracy or an observed reduction in wrong agent answers.

The planned fresh public sample required 60 answerables and 12 controls after
excluding 132 previously used question IDs and 131 base families. Only four
eligible controls remained in the pinned dataset. Selection stopped without
replacement, before producing a sample or running a public model comparison.
Public quality, resource and private confirmation gates therefore remain unrun.
Any independent replacement source requires a new prospective protocol.

The experiment also exposed a separate response-budget defect: shortening a
129-character string to 128 characters plus an ellipsis could loop indefinitely.
The core fix only shortens strings longer than 129 characters, allowing result
removal or an explicit metadata-budget error when further shortening cannot help.
It passed 13 focused tests and the 348-test repository suite. Both experimental
policies received the same fix before retrieval, so this reliability improvement
is not counted as a reranker quality gain.

### Subsequent bounded screen: rejected for deployment

A separate, prospectively frozen synthetic screen reused the unchanged TinyBERT
candidate and current hybrid baseline. It covered 48 answerable coding cases and
12 explicit unknown-answer controls across 1,268 fictional conversations. Both
policies used the same embedding vectors, top-ten search and neighboring context
under the existing response budgets. Scoring required the designated source facts
to remain visible in delivered evidence; labels were not indexed.

The baseline delivered complete evidence for 18/48 answerables; the candidate
delivered 21/48, with four wins and one loss. Both retained all 12 controls. The
frozen rule required at least three net gains **and zero lost baseline-complete
cases**. The candidate therefore failed and was not deployed. In the lost case,
reranking displaced a final deployed queue decision with unselected proposals.
All 60 candidate calls applied without fallback; citation/source checks passed
and the isolated catalog remained unchanged.

The first collector stopped after 40 completed pairs because its project-filter
check incorrectly assumed equality instead of the API's substring semantics.
Before grading, a recorded technical amendment corrected only that check and
allowed one replay of the same frozen cases with shared vectors. The incomplete
attempt was retained; the replay was not treated as a fresh holdout.

These procedural cases have correlated template variants and deliberately crowded
distractors. The paired exact p-value was 0.375 and does not establish a natural
accuracy gain. Controls measure delivery of an explicit unknown, not actual
hallucinated answers. This result rejects the tested configuration for deployment;
it does not establish that reranking in general is ineffective. No further tuning
or replacement candidate followed. The earlier experiments remain closed, and
the response-budget reliability fix remains a separate result.

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
