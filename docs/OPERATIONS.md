# EdgeProc operating contract

TL;DR: EdgeProc keeps task execution local by default and treats bundle delivery as
untrusted input. Production consumers must pin a public key plus bundle identity/channel,
use monotonic sequences, retain the default resource ceilings, and own the SLA of the host
application. The library supplies fail-closed integrity, bounded fetches, crash-atomic CAS
promotion, and a repeatable performance gate; it is not a hosted service.

## Threat model and trust boundaries

The protected assets are the signing key, pinned verification key, active bundle pointer,
content-addressed cache, local task payloads, index contents, and result envelopes. The
main adversaries are a malicious or compromised CDN/origin, an on-path attacker, a stale
but valid signed release, malformed manifests/chunks, decompression bombs, path traversal,
and concurrent local writers.

The private signing key is trusted and stays only on the publisher. Consumers receive the
public key out of band. The CDN and all downloaded bytes are untrusted. A production
consumer should publish with `bundle_id`, `channel`, and `sequence`, then sync with
`expected_bundle_id` and `expected_channel`; legacy unbound pointers remain compatible but
do not provide cross-bundle identity protection.

Promotion is fail-closed on freshness, not merely on the absence of disproof. Replacing the
active pointer requires PROOF that the incoming one is fresher: a strictly-greater monotonic
`sequence`, or a strictly-greater PEP 440 `version`. Either comparison proving staleness
refuses the promote, and so does neither comparison being able to speak. Two cases where
nothing can speak: a version PEP 440 cannot parse, and a version EQUAL to the active one —
two different bundles can wear one label, so an equal version says nothing about which is
newer. A signature proves who published a pointer and never how recent it is, so a replayed
pointer is validly signed by construction. Publishers who re-ship under one label, or whose
version scheme PEP 440 cannot parse (date strings, build labels), must bind `--sequence` to
keep shipping. Re-promoting the byte-identical active pointer is a no-op and needs no proof.

The guard's own input is held to the same bar. `active` missing entirely means nothing has
been promoted and the first promote needs no proof; an `active` that exists but cannot be
read as a pointer is refused as `[bundle.integrity_failed]`, never treated as "nothing is
active" — that answer would skip the freshness check altogether.

Before promotion, EdgeProc verifies the pointer signature, pinned identity, manifest hash,
manifest identity, every chunk hash, and complete file reassembly. Paths are contained
under the selected root. HTTP bodies, decompressed chunks, aggregate sync bytes,
materialized file bytes, file counts, and lock waits have hard ceilings. A shared filesystem mutation lock serializes
publish, sync, promote, garbage collection, and CLI materialization across cooperating
threads and processes, so a stale last writer cannot bypass the rollback check.

Out of scope: compromise of the publisher's private key, a local attacker who can already
write the consumer's cache or process memory, vulnerabilities inside a consumer-supplied
runtime/telemetry sink, and availability of a consumer-selected CDN or model registry.

## Privacy and data flow

| Data | Default destination | Network behavior | Retention/owner |
| --- | --- | --- | --- |
| `Task.payload` and query text | Selected in-process runtime | `LocalVecRuntime` accepts only `local_only` and sends no query request | Host process; consumer owns deletion |
| `ResultEnvelope` | Default `NullSink` | No telemetry egress by default; a custom sink is consumer code and may egress | Consumer-defined |
| Signed bundle bytes | Consumer-selected filesystem or HTTP origin | `GET /latest`, `/manifest/<hash>`, and `/chunk/<hash>`; the origin sees ordinary HTTP metadata, never task payloads | Local CAS until consumer deletion/GC |
| Embedding model | Local directory named by `EDGEPROC_MODEL_PATH` — normally what `sync --materialize-to` wrote | No egress by default. With no local model configured `TextEncoder` refuses (`config.missing`) before any loader is built; it does not fall back to a download. A fetch happens only when `EDGEPROC_ALLOW_MODEL_DOWNLOAD` is set — a build-machine setting — and `HF_TOKEN` is used only on that path. `EDGEPROC_MODEL_DIGEST` pins a model that did not arrive in the signed bundle; a mismatch is refused as `bundle.integrity_failed` | Consumer-owned model directory; a Hugging Face cache exists only if a download was permitted |
| Signing key | Publisher filesystem | Never transmitted by EdgeProc | Publisher deletes/rotates it |

The library has no account database, analytics endpoint, or hidden telemetry. Deleting a
local index/cache and any consumer-owned sink records removes EdgeProc's retained copies;
there is no EdgeProc server-side user record to erase. Filesystem remanence, backups,
custom runtimes, custom sinks, model caches, and CDN logs remain the host operator's
responsibility.

## Reliability and recovery contract

- **Crash-atomic activation:** the active pointer is a same-filesystem, fsynced atomic
  replace. Publisher `latest` and manifest artifacts use the same primitive. A reader
  observes the old pointer or the new pointer, never a torn pointer. The flat published
  `chunk/` and `manifest/` directories must be real directories; symlinks are refused.
  Object and pointer leaves are held to the same rule, including same-root symlinks.
- **Fail-closed retry:** signature, hash, path, size, rollback, fetch, or lock failures do
  not promote the candidate. Verified inactive chunks may remain and are safely reused.
- **Concurrent mutation:** one cross-process lock covers fetch/verify/promote versus GC and
  makes the monotonic check/write indivisible. The default wait is 30 seconds; timeout is a
  typed `IntegrityError` and the caller should retry with jitter.
- **Local vector state:** `FaissVectorIndex` serializes insert, rebuild, delete, search,
  and statistics per instance. Persistence is also cross-process: save, writable load,
  legacy migration, and snapshot GC share one bounded file lock. A save writes
  generation-addressed FAISS and state files, flushes them, then publishes
  one atomic manifest commit and durable parent-directory links. Digests stream in fixed-size
  blocks, so verification does not copy a multi-gigabyte FAISS file into Python memory. Stable
  read-only loads do not take that lock: they enumerate a stable manifest set, pin open
  descriptors for the selected generation, verify both digests from those handles, and retry
  if a concurrent writer or GC changes the set. If the directory is an immutable legacy 0.4.0
  pair, a structurally valid pair loads directly without creating `snapshots/`, migrating,
  deleting, or otherwise writing. Every generation load accepts only a digest-matched complete
  commit. The read-only path makes three attempts to observe a stable manifest set, then fails
  closed with an operational `ValueError` if churn continues; it never returns a hybrid
  generation. An instance loaded from a generation also compare-and-swaps its
  selected generation during save: if another process committed first, save raises
  `SnapshotConflictError`; reload, reapply the intended mutation, and retry. It
  recovers the previous complete generation when the newest commit is corrupt, retains only
  those two generations, and migrates a valid 0.4.0 two-file directory on first load. A
  post-commit cleanup failure emits a warning log but does not misreport the already-active
  save as failed; the next save retries cleanup. Snapshot roots and internal directories must
  be real directories—symlinks and non-directories are refused before any snapshot write.
  Committed manifest, FAISS, and state leaves must be regular files; symlinks are never read.
- **Resource ceilings:** defaults are a 30-second HTTP client timeout per network
  operation, 256 MiB per response, 64 MiB decompressed per chunk, 4 GiB and 100,000 files
  per sync, 256 MiB per materialized file, and 30-second mutation and vector-snapshot lock
  waits. Total sync time still scales with the
  signed chunk count and origin latency. Operators should lower these limits for smaller
  catalogs and place a host-level deadline around the command when they require one.
- **Materialization:** CAS activation is atomic; writing a multi-file
  `--materialize-to` directory is not a crash-atomic directory swap. Consumers needing
  that property should materialize to a versioned staging directory, validate it, then
  atomically repoint their own symlink/directory reference.
- **Recovery:** retry sync after transport or lock failure. If the active manifest/chunks
  fail integrity, quarantine the cache, recreate it, and sync from a trusted origin/key.
  Reclaim disk with `edgeproc gc --cache-dir <cache>` (or `FilesystemCacheStore.gc()` when
  embedding the library). Either path takes the store's mutation lock, so GC is serialized
  against a concurrent sync rather than racing it, and a store with nothing promoted is
  left untouched — a no-op, never a wipe. Never delete objects out of a cache by hand.

EdgeProc has no independent uptime SLA because it is an embedded library. The host owns
origin redundancy, retry policy, alerting, disk monitoring, model warm-up, process
supervision, and end-user SLOs. `MemoryManager` enforces the sum of declared in-flight
task reservations for one `EdgeProc` instance and releases each reservation on every exit
path. It is admission control, not a portable native-RSS limit: the host must still set a
process/container memory limit and supervise FAISS, NumPy, model loading, and other native
allocations. Share one manager across facades that share a process boundary.

## Measured performance contract

Run the fixed, offline benchmark:

```bash
uv run python benchmarks/benchmark.py
```

The fixture and budgets are set before measurement: 10,000 normalized 32-dimensional
vectors with 30 searches after warm-up; and a signed 4 MiB bundle with seven cold syncs
plus 20 no-change syncs. The gate requires vector-search p95 <= 100 ms, cold-sync p95
<= 750 ms, warm-sync p95 <= 250 ms, and process max RSS <= 512 MiB. The script prints
JSON with p50, p95, maximum, RSS, fixture sizes, Python/platform identity, and pass/fail.

These numbers cover library-owned FAISS lookup and signed filesystem sync without network
variance. They deliberately exclude model download/encoding and CDN latency, which depend
on the consumer's model, hardware, and origin and must be measured in the embedding app.

### Measured evidence

**This table is the single source for EdgeProc's performance figures.** They are stated
here and nowhere else — no other document restates them, so there is nothing to drift.

Measured 2026-08-13 on macOS 26.5 arm64 (Apple silicon laptop), CPython 3.13.5,
from the 0.4.1 release candidate after the full local gate:

| metric | p50 | p95 | gate budget |
|---|---|---|---|
| vector search | 0.065 ms | 0.084 ms | 100.0 ms |
| cold sync | 51.494 ms | 51.936 ms | 750.0 ms |
| warm sync | 14.838 ms | 17.226 ms | 250.0 ms |

Peak process RSS was 116.75 MiB against the 512 MiB budget.

Read the p95 column as a shape, not a constant. Cold sync is the noisiest metric: two
consecutive runs on this same machine can vary materially, because seven cold
syncs give the 95th percentile very few samples and each one is dominated by filesystem
behavior the library does not control. The table records the slower run deliberately —
an optimistic number is the more dangerous error. This is also why the drift test in
`tests/test_release_contract_docs.py` compares these committed figures against the
committed budgets rather than against a benchmark run at test time: a test that raced a
live measurement against a documented one would fail on machine variance, not on defects.

These measurements describe that exact tree and machine. They are not a promise for
every consumer, and the budgets above — not these figures — are what the gate enforces.

## Release evidence

A release is eligible only when `uv run poe gate`, `bash examples/run_loop.sh`,
`uv run python benchmarks/benchmark.py`, the secret scan, and dependency audit all pass on the
exact commit. The tag-triggered `publish.yml` workflow first requires the tag to identify the
exact current `main` commit with green hosted `gate`, `Secret scan / gitleaks`, and `pip-audit`
checks. It then runs all five checks itself because tag creation does not trigger `ci.yml`.
Before building, it verifies the exact tag/version/top-changelog identity; before OIDC upload,
it verifies both built artifacts' project and version metadata. Record the immutable commit/tag
and benchmark JSON; do not infer production truth from a different local tree.

Build and validation run in an unprivileged job that records the archive SHA-256 digests and
uploads one short-lived workflow artifact. The OIDC-bearing job only downloads and re-verifies
those archives with the runner standard library and coreutils before invoking the official PyPI
publisher; it does not check out source, install dependencies, or execute the build backend.
Registry propagation verification runs afterward in a third job without OIDC permission.

The local `poe gate` mirrors only the hosted `CI / gate` job. The separate hosted
`Secret scan / gitleaks` job is the shared `hseshadr/ci` brick called from `ci.yml`; it scans
the commit range of each push or PR, not the whole repository. The tag workflow independently
scans the full Git history from the tagged checkout before the OIDC-bearing job becomes
reachable. See the "no key material in the tree" invariant in `CLAUDE.md`.
