# Unpatched optional inference dependency

Keep [GHSA-w8v5-vhqr-4h9v](https://github.com/advisories/GHSA-w8v5-vhqr-4h9v)
open. The dependency remains vulnerable; the application call-path assessment
below is not a package fix or an alert dismissal.

Assessed on 2026-11-05: the lock contains `llama-cpp-python 0.3.36` and
`diskcache 5.6.3`. PyPI lists those as their latest releases, and the advisory
lists no patched DiskCache version. The
[maintainer discussion](https://github.com/grantjenks/python-diskcache/issues/357)
acknowledges the risk of lower-trust writers poisoning a cache later read by a
higher-trust process, without committing to a release timeline.

## Dependency and call path

`diskcache` is required by `llama-cpp-python` in the optional `local` extra.
Importing its high-level `Llama` API imports `llama_cache`, which imports
`diskcache` unconditionally. Removing the package or excluding it from the lock
would break that import, even when no disk cache is used. Core, MCP-client and
remote-embedding installations do not need the `local` extra.

`LocalEmbedder` in `src/session_search/core/embeddings.py` loads an explicitly
provisioned, checksum-verified model and calls `Llama.embed()`. The assessed
backend initializes `Llama.cache` to `None`; Session Search never calls
`set_cache()` or constructs `LlamaDiskCache`. The backend's embedding method does
not use that cache. Its native in-memory KV state is separate from DiskCache's
pickle-backed storage.

This limits the current application's exposure. It does not make DiskCache safe
for other callers, shared cache directories, or future backend changes. Do not
enable persistent inference caches or reuse cache data from lower-trust writers.
Application and model storage should remain owned by the intended user and
unwritable by other users; permissions alone are not a fix for unsafe pickle.

## Verification and follow-up

The native job in `.github/workflows/qualification.yml` runs
`tests/check_local_inference.py` with OS networking denied. It exercises actual
model embedding, synthetic indexing, semantic retrieval and immutable citation
expansion while making `diskcache.Cache` construction fail. It also requires the
model's cache to remain disabled and reports zero constructor calls. This is a
qualification guard, not a production sandbox or a complete dependency audit.

Continue GitHub-managed Dependabot and manual review. Keep this advisory tracked
until a published fix or a validated removal resolves it. Recheck:

- DiskCache's [upstream issue](https://github.com/grantjenks/python-diskcache/issues/357)
  and [proposed serialization change](https://github.com/grantjenks/python-diskcache/pull/364),
  which was unmerged at assessment time and proposes a breaking 6.0.0 change.
- Whether llama-cpp-python makes DiskCache optional or removes its mandatory import.
- Before accepting either change, review the actual serialization/import behavior
  and rerun the full CI matrix, offline MCP checks and native qualification.

Do not suppress the alert through dependency overrides, a fake version bump, or
an unreviewed fork. Replacing the inference backend requires separate functional,
retrieval-quality and performance validation while preserving local-only mode.
