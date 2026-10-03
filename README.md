# Jev takes on LLMs

Audited structured judgment and repeatability experiments for Tev, Gemma, hosted
Jev, and Mistral. Two suites measure output distributions; neither supplies gold
labels or makes accuracy claims.

## Install in the existing environment

```bash
uv pip install --python ../.venv/bin/python --no-deps -e .
source ../.venv/bin/activate
python -m jev_bench.run --help
python -m jev_bench.run_evaluations --help
```

The parent environment supplies dependencies. The editable installation adds only
this package; there is no separate environment or lockfile. Commands and packaged
suite inputs work from other working directories. Root `.env` loading preserves
existing environment variables.

## Repository layout

| Location | Responsibility |
|---|---|
| `src/jev_bench/run/` | Inference commands and shared scheduling |
| `src/jev_bench/providers/` | Model registry and backend integrations |
| `src/jev_bench/suites/` | Inputs, prompts, schemas, response interpretation, metrics |
| `src/jev_bench/storage/` | History, snapshots, plans, locks, relocation |
| `src/jev_bench/runtime/` | Private runtime preparation and execution auditing |
| `src/jev_bench/run_evaluations/` | Offline validation, reports, comparisons, plots |
| `results/experiments/` | Independent experiment datasets |
| `results/reports/` | Generated evaluation artifacts |
| `results/historical/`, `results/quarantine/` | Historical and excluded datasets |
| `docs/`, `tests/`, `analysis/` | Documentation, offline tests, future exploration |

## Commands

```bash
python -m jev_bench.run benchmark --models tev1:0.8b tev1:4b --repetitions 30 --warmups 0
python -m jev_bench.run hard-case --models gemma4:e4b --repetitions 30 --warmups 0
python -m jev_bench.run_evaluations compare \
  --input-dirs results/experiments/tev_30 results/experiments/gemma_30 results/experiments/jev_api_30
python -m unittest discover -s tests -p 'test_*.py'
```

Inference commands perform model calls. Without `--output-dir`, each creates a
fresh experiment and prints its resume command. Migrated datasets are archived
and reportable only. Hosted Jev requires explicit controls and retains its
unverified-cache exception as an opt-in; see [usage](docs/usage.md).

Read [architecture](docs/architecture.md), [methodology](docs/methodology.md),
[migration](docs/migration.md), and the [insurance case](docs/hard_case/README.md).
