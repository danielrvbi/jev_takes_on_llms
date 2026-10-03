# Refactor and migration validation run

Planned on 3 October 2026. This is a run plan; no live inference has been
launched as part of preparing it.
Existing measured datasets and offline checks provide validation evidence for
the refactor and migration PR. This additional 660-measurement experiment is a
follow-up, not a prerequisite for that PR.

## Scope and targets

Run 10 fresh repetitions of every benchmark question (IDs 1–10) and 10 of the
compact insurance hard case for each registered model. Use zero warm-ups.

| Model | Question measurements | Hard-case measurements | Total |
|---|---:|---:|---:|
| `tev1:0.8b` | 100 | 10 | 110 |
| `tev1:4b` | 100 | 10 | 110 |
| `gemma4:e4b` | 100 | 10 | 110 |
| `jev-1.13.0` | 100 | 10 | 110 |
| `mistral-small-latest` | 100 | 10 | 110 |
| `mistral-large-latest` | 100 | 10 | 110 |
| **Total** | **600** | **60** | **660** |

“More” means additional independent measurements. Migrated experiments are
archived, so use new roots below, with repetition IDs 1–10 in each. Do not try to
extend archived 30-repetition runs to 40 or copy their rows into new runs.

## Offline preflight already completed

- All 138 existing tests passed, including migration, archive protection,
  request boundaries, resume behavior, evidence verification, and reporting.
- Migration dry-run returned `status: complete`.
- Current schemas, prompts, all ten question inputs, and the compact packet
  hash match the saved definitions in `tev_30`, `gemma_30`, `jev_api_30`, and
  `baseline`, for both suites.
- The migration journal retains known missing historical evidence. These are
  existing limitations; new experiments must have complete audit evidence.

Before inference, check that the existing parent environment remains installed,
the private no-cache runtime and local model weights are available, and
`MISTRAL_API_KEY` and `TYPESAFE_API` are configured. Do not print credentials.
Prepare the runtime or create aliases only if needed; see [usage](usage.md).

Capture the committed Git revision, package versions, and runtime/model identities
with the run records. If any working-tree changes are present when running,
capture their hash/snapshot as well; the Git revision alone is then insufficient.

## Execution

Run from the repository root using the existing parent environment:

```bash
source ../.venv/bin/activate
```

Run sequentially, with one fresh experiment root per model. The paths below are
reserved names for this validation batch; if any already contain an unrelated
experiment, choose a new batch prefix consistently.

### Local models

First run one repetition of all ten questions and the hard case for each local
model, then extend those same roots to ten. This first cycle is included in the
660 target measurements. Successful repetitions are skipped on extension.

```bash
for reps in 1 10; do
  for model in tev1:0.8b tev1:4b gemma4:e4b; do
    tag="${model//:/_}"
    run_dir="results/experiments/refactor_validation_20261003_${tag}"
    python -m jev_bench.run benchmark \
      --models "$model" --case 1 2 3 4 5 6 7 8 9 10 \
      --repetitions "$reps" --warmups 0 --max-new-calls "$((10 * reps))" \
      --output-dir "$run_dir" || break 2
    python -m jev_bench.run hard-case \
      --models "$model" --input-profile compact --systemone-context 262144 \
      --repetitions "$reps" --warmups 0 --max-new-calls "$reps" \
      --output-dir "$run_dir" || break 2
  done
done
```

The hard-case context flag selects the previously used Tev aliases
`tev1-hard:0.8b-ctx262144` and `tev1-hard:4b-ctx262144`. Gemma retains its existing
configuration; the Tev context override does not apply to it.

### Hosted Jev

Use the dedicated launcher with its persistent shared ledger and existing cache
exception. Its manifest is immutable: set the full ten-repetition target at the
start rather than extending a one-repetition launcher run.

```bash
python -m jev_bench.run jev-api \
  --output-dir results/experiments/refactor_validation_20261003_jev-1.13.0 \
  --repetitions 10 --max-new-calls 110 --budget-usd 0.10 \
  --allow-unverified-server-cache
```

The $0.10 value is the existing configured spending ceiling, not a cost estimate.
Keep the ledger, reservations, and rejected attempts. Server caching must stay
labeled unverified, matching the archived Jev experiment. A saved rejection,
interruption, or unresolved reservation blocks automatic resume.

### Mistral models

Use the original prompts and strict explicit numeric `cached_tokens=0` checks.
Run each model separately so one failure does not obscure the other model.

```bash
for model in mistral-small-latest mistral-large-latest; do
  run_dir="results/experiments/refactor_validation_20261003_${model}"
  for reps in 1 10; do
    python -m jev_bench.run benchmark \
      --models "$model" --case 1 2 3 4 5 6 7 8 9 10 \
      --repetitions "$reps" --warmups 0 --max-new-calls "$((10 * reps))" \
      --output-dir "$run_dir" || break
    python -m jev_bench.run hard-case \
      --models "$model" --input-profile compact \
      --repetitions "$reps" --warmups 0 --max-new-calls "$reps" \
      --output-dir "$run_dir" || break
  done
done
```

The archived Mistral benchmark already contains cache rejections. The current
runner stops after a Mistral cache failure. Review the saved evidence before
continuing; do not retry repeatedly or relax checks. If the backend prevents
completion, record this batch as incomplete for that model. A unique-prefix
experiment would change the prompt and must be a separate diagnostic dataset;
it cannot substitute for these ten original-prompt repetitions.

Call caps for direct suite commands apply per invocation, not across resumes,
and do not cap any internal native inference retries. The Jev ledger provides
the persistent cap for that backend. Do not automatically increase caps or
repeat failed invocations to force completion.

## Validation and reports

For each of the six new roots, run these offline commands, replacing the example
with that model's actual root:

```bash
python -m jev_bench.run_evaluations validate \
  --input-dir results/experiments/refactor_validation_20261003_tev1_0.8b
python -m jev_bench.run_evaluations report \
  --input-dir results/experiments/refactor_validation_20261003_tev1_0.8b
```

Reports go to separate `results/reports/` directories. Inspect the requested
targets as well as `complete`: the first-cycle manifest can validly be complete
with only 11 measurements. The final target must be 100 benchmark measurements
and 10 hard-case measurements per model, with exactly ten successful latest
rows per question and ten successful hard-case rows.

Reissue a completed direct suite command once with identical full-target
arguments to check that it reports zero pending measurements and performs no
inference. Reissue the completed Jev launcher only if its ledger is clear and
the manifest is identical. Confirm that histories and audited call IDs do not
grow. Failed-run recovery requires reviewing the failure first.

Create separate reports for the old and new cohorts. The `compare` command can
combine the six new roots because their model keys are disjoint. Do not pass an
old and new root for the same model together: repetition IDs overlap and the
tool correctly rejects duplicate measurement keys. Compare their summary CSVs
side by side instead, preserving cohort labels.

Use `tev_30`, `gemma_30`, and `jev_api_30` as the authoritative old cohorts for
their models. For Mistral, report `baseline` with a Mistral model filter; retain
its failure history and smaller accepted sample counts. Keep `prefix_pilot`,
historical inputs, quarantine, and migration backups out of the original-prompt
comparison.

## Completion criteria

1. All six full-target manifests validate, totaling 660 successful latest
   measurements, with no missing or extra measurement keys.
2. Audits and logs resolve; hashes, requests, responses, probability derivations,
   model identities, and raw/history snapshots agree. Local cache checks pass;
   Mistral reports explicit zero cached tokens; Jev retains its stated exception.
3. Schemas, prompts, question inputs, packet hash, context settings, and model
   configuration match the intended old methodology. Record resolved hosted
   model identifiers when returned: a `latest` alias can change independently
   of the refactor. Source-file hashes may change because files moved.
4. Resume skips successful work, offline reports make no model calls, archived
   data remains unchanged, and no audit or call ID is reused.
5. Review probability means, standard deviations, ranges, failure counts, latency,
   and token usage against the old cohorts. Investigate discrepancies with raw
   audited responses. Fresh stochastic/hosted output equality is not required;
   deterministic parsing and derived values for the same response are.

Report software/migration checks and live backend outcomes separately. A known
backend cache rejection is not sufficient evidence of a refactor regression,
but it also does not satisfy the requested full live run. These experiments
measure repeatability and execution integrity; they have no gold-label accuracy
claim, and ten repetitions cannot prove statistical equivalence.
