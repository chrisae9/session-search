# Retrieval evaluation

These 50 fictional engineering memories pair a natural-language question with one designated relevant session. They exercise paraphrases, similar topics, and prior implementation lessons. They contain no native session history. The labels are deliberately limited to one expected result per query; they are not exhaustive relevance judgments for every plausible answer.

Run `uv run python benchmarks/evaluate_retrieval.py` with a new `--data-dir` and `--output` outside the checkout. The default run measures keyword retrieval. To compare hybrid retrieval, supply `--embedding-config`; a remote provider additionally requires `--allow-remote-embeddings`. Model files and credentials remain outside the repository. Remote evaluation sends only the selected case texts and queries to the explicitly configured provider.

The runner refuses existing data/output paths, captures the dataset digest and model identity, and reports recall@10, MRR@10, top-one accuracy, and median/p95 query latency. With one designated relevant session, recall@10 equals the fraction of queries that found it in the first ten results. A degraded hybrid run fails rather than reporting keyword fallback as model quality.

Use the same dataset digest when comparing runs. The catalog has only 50 sessions, so latency is not representative of a production corpus. These checks complement whole-corpus latency tests and private relevance evaluations; they do not establish either. Keep machine-specific measurements and private evaluation sets outside the repository.
