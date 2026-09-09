# Retrieval evaluation

These 50 fictional engineering memories pair a natural-language question with one designated relevant session. They exercise paraphrases, similar topics, and prior implementation lessons. They contain no native session history. The labels are deliberately limited to one expected result per query; they are not exhaustive relevance judgments for every plausible answer.

Run `uv run python benchmarks/evaluate_retrieval.py` with a new `--data-dir` and `--output` outside the checkout. The default run measures keyword retrieval. To compare hybrid retrieval, supply `--embedding-config`; a remote provider additionally requires `--allow-remote-embeddings`. Model files and credentials remain outside the repository. Remote evaluation sends only the selected case texts and queries to the explicitly configured provider.

The runner refuses existing data/output paths, captures the dataset digest and model identity, and reports recall@10, MRR@10, top-one accuracy, and median/p95 query latency. Configured embedding runs also measure a vector-only cosine baseline to distinguish model ranking from hybrid rank merging. That diagnostic loads the small synthetic vector set into memory; it is not the production query path. With one designated relevant session, recall@10 equals the fraction of queries that found it in the first ten results. A degraded hybrid run fails rather than reporting keyword fallback as model quality.

Use the same dataset digest when comparing runs. The catalog has only 50 sessions, so latency is not representative of a production corpus. These checks complement whole-corpus latency tests and private relevance evaluations; they do not establish either. Keep machine-specific measurements and private evaluation sets outside the repository.


## CI regression gate

CI passes `--baseline benchmarks/keyword-baseline.json` and retains the JSON
report for 14 days, including ranking failures. Each previously retrieved case
must remain at its recorded rank or better; improvements on other cases cannot
hide a regression. Empty results therefore fail the gate.

The initial baseline retrieves 20 of 50 cases (recall@10 of 0.40). It records
existing behavior, not an acceptable quality target. Cases currently missed may
improve freely. Dataset changes or intentional ranking tradeoffs require reviewing
the case-level results and updating the baseline explicitly; CI never regenerates
it automatically. This gate does not assess semantic retrieval quality.

## Capture append evaluation

`compare_capture.py` compares the incremental parser engine with an independent
full parse after each of two synthetic appends. Supply a stable complete-record
Codex JSONL file, a scratch directory with room for a separate source copy plus
2 GiB, and a new output path outside the checkout:

```sh
uv run python benchmarks/compare_capture.py SESSION_JSONL --scratch SCRATCH_DIRECTORY --output REPORT_JSON
```

For a content-addressed raw object, pass `--filename` with its original rollout
basename so ownership detection uses the correct session identity. The runner
writes only to an independent temporary copy, checks exact normalized revisions,
and verifies the original source hash after both comparisons. It reports timings,
event reuse, scan offsets, and source identity without transcript excerpts.

The default measures an in-memory checkpoint. Add `--persistent-cache` to save
that checkpoint, discard it from memory, and load and atomically save a checkpoint
for each append. That mode includes cache I/O, serialization, and complete prefix
verification in `incremental_seconds`; separate load, parser, save, and cache-byte
fields show the costs. Failed cache saves or loads stop the benchmark instead of
silently measuring a full-parse fallback.

Both modes exclude capture staging, raw archival, and upload. They run within one
process, with the incremental path before the reference full parse, so OS page
cache and ordering can affect timings. They do not establish end-to-end capture
latency, cold-start performance, or performance on other machines. Keep
private-source reports outside the repo.
