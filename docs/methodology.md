# Preserved methodology

The ten-question suite evaluates information needs, safety, route distributions,
and freshness distributions. The insurance suite evaluates six independent,
unnormalized probabilities from the reviewed compact evidence packet. Neither
experiment has gold labels or an accuracy claim.

Prompts, output schemas, model identifiers, temperatures, seeds, context settings,
probability derivations, standard deviations, percentile calculations, and
failure treatment are preserved from before the refactor.

Each local inference uses a fresh private server, model worker, and client.
Every scoring prompt, including internal retries, must begin with zero cached
tokens and evaluate its complete prompt. Shared-prefix priming, restore, and
truncation are rejected. Cold latency includes startup/loading, inference,
verification, and teardown; on-disk weights remain permissible.

LangChain response caching is disabled. Clients have no previous answers and
automatic request retries remain zero. Mistral receives a unique cache key and
requires explicit numeric `cached_tokens=0`. The prefix pilot additionally changes
the first system message with a unique identifier and is never pooled with
original-prompt experiments.

Jev defaults to strict cache rejection. Its explicit exception permits otherwise
valid responses while retaining `cache_verified=False` and the server caching
label **unverified**. This makes no cache-free latency claim and does not relax
other backends. Budgets reserve before sends and retain charges when usage is
unknown.

Accepted probability statistics use successful latest repetitions. Existing
latency, usage, failure-history, and warm-up treatment are preserved. Independent
deterministic calls can still yield identical outputs.

Original requests, responses, evidence hashes, logs, budget ledgers, and metadata
remain immutable during migration. Archived results cannot be extended by the
new implementation.

Azure GPT and Claude use a separate altered-prompt prefix experiment. Each call
adds a fresh UUID identifier before the original system message; inputs and output
probability semantics are otherwise preserved. Zero cache-read telemetry is
mandatory, with Claude cache-creation tokens recorded independently. Missing
telemetry or nonzero reads stop execution; a unique prefix alone is not accepted
as evidence. Azure hosted wall time includes client creation and teardown, without
a local cold-start claim. Prefix results are shown separately from original-prompt
measurements and the Mistral pilot. The 16-call Azure pilot must pass before
the 1,320-measurement full experiment can begin.
