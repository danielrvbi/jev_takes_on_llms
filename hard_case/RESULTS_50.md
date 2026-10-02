# Compact hard-case results: 50 repetitions per model

All five providers completed 50 successful measured repetitions each: **250 successful repetitions**, zero latest validation failures. Every experiment records the same compact packet, original-source hash, source-map hash, schema and judgments. These are separate provider-group experiment directories on the original macOS laptop; they are not a merged resumable experiment.

Measured timestamps (UTC): `2026-10-02T12:55:24.162730+00:00` through `2026-10-02T14:31:05.350970+00:00`.
Compact packet SHA256: `f33ea0b301fe514830f3e79cc02f8c25c2d3193b2fa3dd87ee1418417055c881`.
Original-source SHA256: `93f2e05df4e5c882f8bd7f4e0a263391f6020be646a84f68e1ccd3a46da9d261`.
Source-map SHA256: `3958163a9779936ff9abf9fe3fca438f9a218b117dcaa109e2d5fc88960a0fe3`.

## Reliability, latency and usage

| Model | Successes | Historical failures | p50 ms | p95 ms | Mean input tokens | Mean output tokens |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `tev1:0.8b` | 50/50 | 0 | 429.09 | 4772.79 | 72732.0 | 7.0 |
| `tev1:4b` | 50/50 | 0 | 890.20 | 18941.13 | 72732.0 | 7.0 |
| `mistral-small-latest` | 50/50 | 10 | 846.31 | 1294.63 | 11679.0 | 75.2 |
| `mistral-large-latest` | 50/50 | 10 | 1598.69 | 2302.02 | 11679.0 | 84.7 |
| `gemma4:e4b` | 50/50 | 0 | 46484.14 | 50336.42 | 11530.0 | 1795.0 |

Mistral attempt history retains ten earlier failed attempts per model (20 total). Those attempts failed during initialization because the API key was missing; subsequent retries succeeded. They are not erased by the successful latest rows. Warm-ups are excluded from measurements.

## Six independent judgments

Each cell is **mean ± sample standard deviation** over the 50 successful repetitions. Displayed zero standard deviations include numerical noise below the displayed precision. No normalization or accuracy target is applied.

| Judgment | Tev 0.8b | Tev 4b | Mistral Small | Mistral Large | Gemma e4b |
| --- | ---: | ---: | ---: | ---: | ---: |
| `requires_clarification` | 0.9421 ± 0.0000 | 0.9671 ± 0.0000 | 0.9410 ± 0.0314 | 0.9090 ± 0.0219 | 0.9000 ± 0.0000 |
| `requires_human_review` | 0.9601 ± 0.0000 | 0.9705 ± 0.0000 | 0.9366 ± 0.0375 | 0.9560 ± 0.0121 | 0.9500 ± 0.0000 |
| `policy_grounding_required` | 0.9613 ± 0.0000 | 0.6830 ± 0.0000 | 0.9810 ± 0.0224 | 0.8540 ± 0.0170 | 0.9000 ± 0.0000 |
| `safety_compliance_concern` | 0.8159 ± 0.0000 | 0.6221 ± 0.0000 | 0.3920 ± 0.1952 | 0.7000 ± 0.0175 | 0.7000 ± 0.0000 |
| `coverage_likely` | 0.9838 ± 0.0000 | 0.9193 ± 0.0000 | 0.5440 ± 0.1146 | 0.6660 ± 0.0710 | 0.7500 ± 0.0000 |
| `potential_fraud_signal` | 0.8246 ± 0.0000 | 0.1859 ± 0.0000 | 0.2390 ± 0.0649 | 0.3140 ± 0.0351 | 0.2000 ± 0.0000 |

Exact means, sample standard deviations, minima and maxima remain in each `summary.csv`; individual values, raw responses and usage remain in `raw.csv` and full attempt history.

## Configuration and interpretation

- Tev logical labels resolve to `tev1-hard:0.8b-ctx262144` and `tev1-hard:4b-ctx262144`, using original cached weights with only `num_ctx=262144` changed. Native scoring uses six `noul` questions. The earlier allocation verification observed 262144 context for both aliases; its local `verification.json` describes an earlier 20-call stage and is not included in this final-results snapshot.
- Gemma chat requests use two-hour keep-alive and reject truncation. All chat providers use temperature zero, no supplied seed and strict LangChain structured output; Mistral automatic retries are disabled. Exact model settings, discoverable local digests, prompts, schema, dependencies and environment are recorded in each metadata file.
- Compact rewriting can affect judgment through wording and presentation even when evidence is preserved. These results do not establish accuracy, calibration, legal correctness or fraud occurrence. Designer ground truth was not used and no expected probability targets were defined.
- Latency includes loading/prefill occurring inside a measured call. Repeated local calls may benefit from caching; p50/p95 are not controlled cold-start comparisons, and models use different inference mechanisms and hardware/services.
- Native token usage aggregates six scoring questions; chat token usage reports a generation request. Token totals and output counts are not directly equivalent units of inference effort. Gemma reports substantially more output tokens despite the same six-field output schema.
- Hosted `latest` aliases can change without exposing immutable weights. New machines, dependencies, inputs or configuration changes require new experiment directories. Read these preserved outputs for comparison rather than resuming them on another laptop.

## Preserved experiment files

The following ignored result files are explicitly committed as the completed snapshot. Other outputs, locks, credentials and caches remain excluded. SHA256 values identify the exact published measurements.

| File | SHA256 |
| --- | --- |
| [compact_tev_262k/raw.csv](results/compact_tev_262k/raw.csv) | `90a897d25e62a87f7289c65b3f8a5b0848982f727f11376df48cbeb36cb888b3` |
| [compact_tev_262k/attempt_history.csv](results/compact_tev_262k/attempt_history.csv) | `90a897d25e62a87f7289c65b3f8a5b0848982f727f11376df48cbeb36cb888b3` |
| [compact_tev_262k/summary.csv](results/compact_tev_262k/summary.csv) | `70340dc68fe3e218f10d333eb996459d11cac6e711a20baec8637d05c2d12dbb` |
| [compact_tev_262k/metadata.json](results/compact_tev_262k/metadata.json) | `0589dfa193b3d509147b4bf2b7ee985fa5c406adebeaae3e1fd48892ffc83cc8` |
| [compact_mistral/raw.csv](results/compact_mistral/raw.csv) | `a560a3824161ad4cfc8fb0369f8348e6da9e7c99faae3c1e3cdbd60ed6c82c63` |
| [compact_mistral/attempt_history.csv](results/compact_mistral/attempt_history.csv) | `0f1282a1b2e8108e25f34251c3ae98eac0b3bf3d9b5568100d13ce7f1e274594` |
| [compact_mistral/summary.csv](results/compact_mistral/summary.csv) | `58252dc7844b21df585b11e870b7757e28f7900e27b2888c96a99de1454abd7d` |
| [compact_mistral/metadata.json](results/compact_mistral/metadata.json) | `58cde213c6f9efb557f8e05283cdf08d810fe7f65a495b97d4b18153308ab21c` |
| [compact_gemma_2h/raw.csv](results/compact_gemma_2h/raw.csv) | `b907099fc5a194870655ea1709d3c04bff6a431e47ed48dd585f5488a1042e71` |
| [compact_gemma_2h/attempt_history.csv](results/compact_gemma_2h/attempt_history.csv) | `b907099fc5a194870655ea1709d3c04bff6a431e47ed48dd585f5488a1042e71` |
| [compact_gemma_2h/summary.csv](results/compact_gemma_2h/summary.csv) | `c20ae0b914590921442b03d4f91f358f32373cc4f8d2e44096beaa9fc5ce3eea` |
| [compact_gemma_2h/metadata.json](results/compact_gemma_2h/metadata.json) | `2f086aee2a67971153876341c1b4831a9f6ce98818057d4d49f6a8c8e862782a` |
