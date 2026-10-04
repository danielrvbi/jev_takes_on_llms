# Additional ten-repetition results

Batch status: **completed_with_stops**. Accepted new measurements: **448 / 660**.

| Model | Accepted questions / 100 | Accepted hard case / 10 | Full target |
|---|---:|---:|---|
| tev1:0.8b | 100 | 10 | complete |
| tev1:4b | 100 | 10 | complete |
| gemma4:e4b | 100 | 10 | complete |
| jev-1.13.0 | 100 | 10 | complete |
| mistral-small-latest | 1 | 0 | incomplete |
| mistral-large-latest | 7 | 0 | incomplete |

The first cycle is included in these ten repetitions. Archived data remains unchanged. Original and additional cohorts are compared separately; overlapping repetition IDs are never pooled.

[Full batch manifest](batch_manifest.json) · [Combined new-cohort report](combined/evaluation.md) · [Old/new probability statistics](cohort_deltas.csv) · [Methodology checks](methodology_comparison.json)

## Validation findings

- Both Tev models and Gemma completed 100 benchmark measurements (ten per question) and ten hard-case measurements each. Every new probability value matches the earlier repetition-one reference exactly; aggregate differences are floating-point roundoff.
- Hosted Jev completed the same 110-measurement target using the existing unverified-server-cache exception. The largest change in a mean probability versus the old cohort is 0.010667. Its spending ledger contains 110 completed calls and records $0.008641080 against the $0.10 ceiling.
- All four completed experiments pass offline validation and reporting. All seven resume checks left attempt histories unchanged; no new inference calls were added.
- All 450 attempts have distinct call IDs and retained audited evidence. Old experiment and historical inventories remain unchanged. Schemas, prompts, inputs, and model configuration match for every executed suite.

## Mistral stops

| Model | Accepted benchmark measurements | Rejected question | Cached tokens | Hard-case measurements |
|---|---:|---:|---:|---:|
| mistral-small-latest | 1 (question 1) | 2 | 348 | 0 |
| mistral-large-latest | 7 (questions 1–7) | 8 | 296 | 0 |

Both stops reproduce the existing class of backend cache-verification failures. No failed invocation was retried, no cache check was relaxed, and neither model proceeded to its hard case. The full six-model target is incomplete: 448 of 660 intended successful measurements are saved, with two rejected attempts.

Mistral Large's accepted question 2 response shifted freshness probability mass from level 4 to level 3 (a 0.7 change). The audited old/new inputs and request fields are identical except for the intentionally unique cache key. Each cohort has only one accepted sample for this question, and the returned model metadata identifies only the `latest` alias. This does not establish repeatability or a software regression; the Mistral comparison remains inconclusive.
