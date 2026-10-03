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
