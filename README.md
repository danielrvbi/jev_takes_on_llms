# Jev takes on LLMs

Audited structured judgment and repeatability experiments for Tev, Gemma, hosted
Jev, and Mistral. Two suites measure output distributions; neither supplies gold
labels or makes accuracy claims.

## Azure work-computer handoff

Read [the complete Azure handoff guide](docs/azure_handoff.md). The GPT and Claude
LangChain adapters are implemented: configure the local deployments and API keys,
run the 16-call prefix pilot for the four configured models, then run both existing
suites with 30 repetitions.
Results use the same storage and evaluations, in a separate Azure prefix dataset.

## Install from a fresh clone

Python 3.12 and `uv` are required. Dependencies and their lockfile live in this repo.

```bash
uv sync --frozen --extra azure
uv run --frozen --extra azure python -m jev_bench.run azure --help
```

Extras `local`, `mistral`, and `jev` install the respective integrations when needed.
For all offline tests: `uv sync --frozen --extra azure --group test`, then run
`.venv/bin/python -m unittest discover -s tests -p 'test_*.py'` (Unix).

The prior parent environment is still usable:

```bash
uv pip install --python ../.venv/bin/python --no-deps -e .
source ../.venv/bin/activate
python -m jev_bench.run --help
python -m jev_bench.run_evaluations --help
```

Commands and packaged suite inputs work from other working directories. Root
`.env` loading preserves existing environment variables.

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
