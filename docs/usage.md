# Current commands

Use the self-contained frozen environment described in the
[repository README](../README.md); the prior parent environment remains supported.
For Azure GPT/Claude, follow the complete [work-computer guide](azure_handoff.md).
Set `MISTRAL_API_KEY` and `TYPESAFE_API`
in the environment or root `.env` as applicable.

## Local execution

Prepare the private runtime only when needed:

```bash
python -m jev_bench.runtime.prepare
ollama create tev1-hard:0.8b-ctx262144 -f src/jev_bench/runtime/ollama/tev0.8b.Modelfile
ollama create tev1-hard:4b-ctx262144 -f src/jev_bench/runtime/ollama/tev4b.Modelfile
```

Preparation builds the checksum-pinned private runtime under ignored `.runtime/`.
It preserves the installed Ollama application. Alias verification checks that
only the context differs from the original Tev tag.

```bash
python -m jev_bench.run benchmark --models tev1:0.8b tev1:4b \
  --repetitions 30 --warmups 0 --output-dir results/experiments/my_local_run
python -m jev_bench.run hard-case --models tev1:0.8b tev1:4b \
  --repetitions 30 --warmups 0 --systemone-context 262144 \
  --output-dir results/experiments/my_local_run
```

`--case` selects benchmark questions. `--input-profile compact` is the insurance
suite's only supported input profile. Defaults remain 30 repetitions and two
warm-ups. Without an output directory, each command selects a fresh experiment;
use the printed command to resume it. Repeated commands skip verified successes,
and compatible extensions retain plan revisions and original provenance.

## Hosted execution

These commands perform paid inference; they are not offline checks:

```bash
python -m jev_bench.run jev-api --repetitions 30 --max-new-calls 330 \
  --budget-usd 0.10 --allow-unverified-server-cache
python -m jev_bench.run prefix-pilot
```

The Jev launcher shares one spending ledger across both suites, uses zero
warm-ups, and stops after a rejection. Omitting the explicit server-cache exception
retains strict rejection because the provider supplies no supported zero-cache
verification contract. The exception never marks server caching as verified.
Unknown usage retains the reservation; unresolved reservations and saved
rejections block automatic resume.

The Mistral prefix pilot targets eight calls: two repetitions of question 1 and
two insurance repetitions for each Mistral model. It remains a separate altered-
prompt experiment and stops at its first failure. Its output directory is the
experiment itself.

Direct Jev suite selection and `--all`, which includes Jev, require `--warmups 0`
and an explicit `--max-new-calls`. Prefer the dedicated hosted launcher.

## Saved-run evaluations

```bash
python -m jev_bench.run_evaluations validate --input-dir results/experiments/jev_api_30
python -m jev_bench.run_evaluations report --input-dir results/experiments/gemma_30
python -m jev_bench.run_evaluations compare \
  --input-dirs results/experiments/tev_30 results/experiments/gemma_30 results/experiments/jev_api_30
python -m jev_bench.run_evaluations reconcile \
  --input-dirs path/to/original/benchmark path/to/separate/mistral/benchmark \
  --output-dir results/reports/reconciliation
```

Reports and comparisons support `--models`, `--case`, `--suite`, and
`--include-raw`. `--output-dir` selects a separate artifact directory; otherwise
outputs go under repository `results/reports/`. Source files stay unchanged.
Comparisons require explicit sources and reject duplicate measurement keys.
Reconciliation retains its original two-source assignment rules and produces an
evaluation-only dataset.

Validation of new experiments uses their requested targets. Archived validation
manifests are preserved, including earlier completeness assumptions; regenerating
a report never rewrites them. Historical formats retain their original validity
limitations. Quarantine is excluded.

## Offline verification

```bash
python -m unittest discover -s tests -p 'test_*.py'
python -m jev_bench.storage.migrate --dry-run
```

See [migration](migration.md) for relocation, archived datasets, and backups.
