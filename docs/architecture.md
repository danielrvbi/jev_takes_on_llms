# Architecture

`jev_bench` is one editable Python package. CLI modules load repository `.env`
and select experiments; they delegate execution to the shared engine.

Suites provide the request, output schema, response interpretation, measurement
keys, CSV columns, and metrics. Provider adapters select suite configuration while
sharing the model registry, native transport, chat transport, and hosted Jev
integration. The common boundary is `invoke(Request, InvocationContext)`.

The shared engine performs sequential scheduling, warm-ups, timing, interruption
handling, audit enforcement, history recording, and stop checks. Suite entry
modules retain preflight validation and construct experiment definitions.

Storage owns atomic writes, locks, one parameterized result store, requested
targets, archive protection, and path relocation. History is authoritative;
`raw.csv` is the latest attempt per measurement key. Successful measurements are
bound to audited provider responses before entering statistics or being skipped.

Runtime verifies execution evidence without importing suite response mappers.
Storage verifies saved probability fields against those responses. Shared and
selected-suite inference code contribute implementation fingerprints; report
renderers, documentation, and tests do not.

Every new experiment has a versioned run plan. Ordinary plans retain revisions,
including exact case targets when subsets are extended. Hosted Jev plans remain
immutable to preserve their budget and exception commitments. New audit and log
links are relative to their suite directory. Readers supply the dataset context;
historical absolute paths resolve through migration manifests.

Offline evaluations use saved snapshots and write only to separate artifact
directories. Runner summaries delegate to those renderers. `analysis/` is reserved
for future notebooks and exploratory work.
