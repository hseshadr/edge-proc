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
| Embedding model | Local cache/model path | `TextEncoder` may download the configured SentenceTransformer model and may use `HF_TOKEN`; pre-provision or point at a local model for zero-egress startup | Hugging Face cache, controlled by consumer |
| Signing key | Publisher filesystem | Never transmitted by EdgeProc | Publisher deletes/rotates it |

The library has no account database, analytics endpoint, or hidden telemetry. Deleting a
local index/cache and any consumer-owned sink records removes EdgeProc's retained copies;
there is no EdgeProc server-side user record to erase. Filesystem remanence, backups,
custom runtimes, custom sinks, model caches, and CDN logs remain the host operator's
responsibility.

## Reliability and recovery contract

- **Crash-atomic activation:** the active pointer is a same-filesystem, fsynced atomic
  replace. Publisher `latest` and manifest artifacts use the same primitive. A reader
  observes the old pointer or the new pointer, never a torn pointer.
- **Fail-closed retry:** signature, hash, path, size, rollback, fetch, or lock failures do
  not promote the candidate. Verified inactive chunks may remain and are safely reused.
- **Concurrent mutation:** one cross-process lock covers fetch/verify/promote versus GC and
  makes the monotonic check/write indivisible. The default wait is 30 seconds; timeout is a
  typed `IntegrityError` and the caller should retry with jitter.
- **Resource ceilings:** defaults are a 30-second HTTP client timeout per network
  operation, 256 MiB per response, 64 MiB decompressed per chunk, 4 GiB and 100,000 files
  per sync, 256 MiB per materialized file, and a 30-second mutation lock wait. Total sync time still scales with the
  signed chunk count and origin latency. Operators should lower these limits for smaller
  catalogs and place a host-level deadline around the command when they require one.
- **Materialization:** CAS activation is atomic; writing a multi-file
  `--materialize-to` directory is not a crash-atomic directory swap. Consumers needing
  that property should materialize to a versioned staging directory, validate it, then
  atomically repoint their own symlink/directory reference.
- **Recovery:** retry sync after transport or lock failure. If the active manifest/chunks
  fail integrity, quarantine the cache, recreate it, and sync from a trusted origin/key.
  Run GC only through `FilesystemCacheStore.gc()`; it is serialized and is a no-op without
  an active pointer.

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

Fresh local evidence on 2026-07-15 (macOS 26.5 arm64, CPython 3.13.5): search
p50 0.063 ms / p95 0.077 ms; cold sync p50 58.1 ms / p95 111.0 ms; warm sync
p50 18.6 ms / p95 19.8 ms; maximum RSS 114.5 MiB. These measurements describe that
exact tree and machine, not a promise for every consumer.

## Release evidence

A release is eligible only when `uv run poe gate`, `sh examples/run_loop.sh`,
`uv run python benchmarks/benchmark.py`, full-history secret scanning, dependency audit,
and CI all pass on the exact commit. Record the immutable commit/tag and benchmark JSON;
do not infer production truth from a different local tree.
