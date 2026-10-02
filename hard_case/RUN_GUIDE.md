# Run the hard insurance benchmark on another laptop

Use this guide from the repository root, on branch `v1` (or the commit containing
these changes). All hard-case code, source data, tests and outputs live under
`hard_case/`. The original root benchmark is separate.
This suite is compact-only: every backend receives the same validated compact
packet. `--input-profile compact` remains accepted for existing commands, but is
unnecessary. `--input-profile full` is rejected and no full-packet inference API exists.

## What currently runs

| Logical model label | Backend | Required access |
| --- | --- | --- |
| `tev1:0.8b`, `tev1:4b` | Native Ollama System One | Ollama with `/v1/systemone` and the Tev models |
| `gemma4:e4b` | Ollama chat / LangChain structured output | Ollama and the Gemma model |
| `mistral-small-latest`, `mistral-large-latest` | Mistral API / LangChain structured output | Mistral API key |

**EY GPT and Claude are not registered inference providers in this version.**
If you mean using EY GPT or Claude as your coding assistant, give it the handoff
prompt below to set up and run the existing benchmark. If you mean benchmarking
models served by EY GPT or Claude, an adapter and registry entries are needed
first. A chat UI login is not an API endpoint or credential. Do not substitute a
public endpoint for the intended enterprise endpoint.

## 1. Operating system and checkout

Python 3.12 is the tested interpreter. The runner currently imports `fcntl` for
file locking, so use **macOS or Linux**. On Windows, use WSL2/Linux if available on
your laptop; native Windows Python is not supported by the current runner.
If Ollama runs on the Windows host, `OLLAMA_HOST` must point to the server reachable
from WSL; do not assume that WSL `localhost` reaches the host in every network mode.

Clone/pull the repository and check out the intended commit. Run all commands from
its root, where both `main.py` and `hard_case/` exist:

```bash
git status --short
git branch --show-current
python --version
```

The nine originals are under `hard_case/data/`, used only for provenance/hash
verification and never sent to models. Do not change them. The designer
`hard_case/README.md` must not be read for model input, summary creation, prompt
construction or expected answers. Use this guide, `USAGE.md` and `compact/REVIEW.md`
instead. Do not regenerate the compact packet during setup.

## 2. Python environment: important for a fresh clone

This repository has **no root `pyproject.toml`, lockfile or `.venv`**. The development
machine uses a shared parent project/environment that is outside this Git checkout;
pushing this repository does not transfer that environment.

If the EY workspace already provides a parent uv project with the dependencies,
use its existing environment and the normal command prefix:

```bash
uv run --no-sync python -m hard_case.main --help
```

For a different managed Python environment, install the checked-in tested snapshot
into that approved environment (not into a new environment in this repository):

```bash
# Replace this with the existing environment's absolute Python executable.
BENCH_PYTHON=/absolute/path/to/shared/environment/bin/python
uv pip install --python "$BENCH_PYTHON" -r hard_case/requirements-run.txt
"$BENCH_PYTHON" -m hard_case.main --help
```

Use `"$BENCH_PYTHON"` in place of `uv run --no-sync python` in subsequent commands
if there is no parent uv project. On a fresh laptop with no environment yet, first
provision an approved Python 3.12 shared environment outside the checkout, then
install there. This guide does not install or modify the development environment.
The snapshot pins direct runtime dependencies plus packages needed by the original
tests; it is not a complete transitive lockfile. If your package mirror cannot supply
a pinned release, report that limitation before choosing replacements. Changed
dependency versions require a new experiment directory.

For native Tev runs, verify the installed SDK exposes the actual interface, not just
a matching version number:

```bash
uv run --no-sync python -c "import ollama; print('System One SDK:', hasattr(ollama, 'systemone')); print('Request type:', hasattr(ollama, 'SystemOneRequest'))"
```

Both must be `True`. If not, the installed distribution cannot run this native
pipeline; obtain the appropriate approved SDK distribution rather than emulating
System One with chat generation.

## 3. Credentials and backend checks

The hard-case CLI loads **`.env` at the repository root**, the parent directory of
`hard_case/`. Existing process environment variables take precedence. It does not
load `hard_case/.env`.

For Mistral, put this setting in that root `.env` or set it in the process environment:

```dotenv
MISTRAL_API_KEY=your-key
```

Optional remote Ollama setting, in the same place:

```dotenv
OLLAMA_HOST=http://your-ollama-host:11434
```

Do not commit credentials. `.env` and generated `results/` directories are already
ignored. Verify backend credentials locally without printing them into chat/logs.

For local models, start the normal Ollama application/service, then check:

```bash
ollama --version
ollama list
ollama ps
```

The source machine was tested with Ollama **0.35.0** and native System One support.
A version string alone does not confirm that another distribution exposes the
endpoint. Verify support with the actual smoke run. If the local models are absent,
obtain them through the normal model registry available on that laptop:

```bash
ollama pull tev1:0.8b
ollama pull tev1:4b
ollama pull gemma4:e4b
```

These commands need registry/network access and download weights if not cached.
Mistral-only runs do not require running Ollama, but the Python Ollama package is
still part of the suite's imports.

## 4. Check the packet and run tests before inference

Every backend always receives exactly the same
reviewed compact packet and the same six probabilistic judgments. No numeric targets,
accuracy metrics, binary decisions, explanations or final legal decisions are added.

```bash
uv run --no-sync python -c "from hard_case.loader import load_claim_packet; print(load_claim_packet().metadata())"
uv run --no-sync python -m unittest discover -s tests -t . -v
uv run --no-sync python -m unittest discover -s hard_case/tests -t . -v
```

Current verification: **38 original tests and 47 hard-case tests passed**. Compact
loading checks source hashes, ordered sections, source-map provenance and the 50 KiB
JSON packet budget. Native requests are additionally checked against 60 KiB. If a
source/map/hash/budget check fails, stop and report it; do not truncate or regenerate.
Original concatenation exceeds the tested endpoint's 64 KiB limit and is used only
for provenance hashing. Full-packet inference has been removed.

## 5. Native Tev: create stock aliases, then smoke

Only needed for Tev. These Modelfiles reuse original weights and change only context;
they do not overwrite the original model definitions:

```bash
ollama create tev1-hard:0.8b-ctx262144 -f hard_case/ollama/tev0.8b.Modelfile
ollama create tev1-hard:4b-ctx262144 -f hard_case/ollama/tev4b.Modelfile
uv run --no-sync python -c "from hard_case.providers.context import validate_alias; print(validate_alias('tev1:0.8b', 262144)); print(validate_alias('tev1:4b', 262144))"
```

Run one measured repetition each, without warm-ups, in a **new laptop-specific output
directory**. Do not copy/resume the other machine's results; platform/dependency/model
metadata is fingerprinted.

```bash
uv run --no-sync python -m hard_case.main \
  --models tev1:0.8b tev1:4b \
  --systemone-context 262144 \
  --repetitions 1 --warmups 0 \
  --output-dir hard_case/results/ey_compact_tev_262k
```

In another terminal, use `ollama ps` during each model's call to verify **262144**
allocated context. Inspect `raw.csv` for `validation_success=True`, blank errors,
token usage and the actual alias in `raw_response_json`. Alias parameters alone
are not proof of allocated context. The source machine allocated about 5.8 GB for
0.8b and 14 GB for 4b, both on GPU; other hardware can differ. A laptop without enough
memory may fail. Do not silently lower context, change model settings, truncate or
patch the server. Report failures and stop before extension.

After both smoke calls and context checks succeed, repeat the same command with
`--repetitions 10` (or `50`). Successful first repetitions are reused, so ten each
means **20 total measured calls**, not 22. Keep the same output directory and settings.

## 6. Gemma compact, retained for two hours

```bash
uv run --no-sync python -m hard_case.main \
  --models gemma4:e4b \
  --repetitions 1 --warmups 0 \
  --output-dir hard_case/results/ey_compact_gemma_2h
```

After inspecting a successful smoke result, repeat with `--repetitions 10` or `50`.
The provider sets `keep_alive="2h"` after each call. Calls run sequentially and reuse
the loaded model; keep-alive refreshes expiry rather than creating one model instance
per repetition. Memory pressure or loading another model can still evict it.
Gemma uses its configured context, not the Tev aliases; requests reject truncation.
A historical full-packet call on the original laptop took ~85 seconds; that is not a
compact estimate or a timing guarantee for the EY laptop. Full-mode runs cannot resume.

## 7. Mistral compact

After configuring the root `.env`:

```bash
uv run --no-sync python -m hard_case.main \
  --models mistral-small-latest mistral-large-latest \
  --repetitions 1 --warmups 0 \
  --output-dir hard_case/results/ey_compact_mistral
```

Inspect both smoke results, then repeat with `--repetitions 10` or `50`. Do not supply
`--systemone-context`; that option configures Tev only. These are paid API requests.
Chat temperature is zero, no seed, Mistral retries disabled, chat timeout 120 seconds.
For budgeting use actual recorded usage and current provider prices, not a fixed
per-call price copied from this guide.

## 8. Results, resume and common failures

Each output directory has `raw.csv`, append-only `attempt_history.csv`, `summary.csv`
and `metadata.json`. Summary reports successful counts, six independent probability
means/sample standard deviations/min/max, latest-attempt p50/p95 latency, token means,
validation failures and historical failures. It reports no accuracy targets.

Rerun the same command to resume. `--repetitions` is the target total per model.
Successes skip, failures retry once per invocation, increasing the total adds only
missing repetitions. Warm-ups are excluded from measurements. Different model subsets
are allowed, provided retained configuration and experiment metadata remain compatible.

- **Missing key:** set `MISTRAL_API_KEY` in root `.env` or environment; rerun to retry.
- **Native Windows `fcntl` error:** use Linux/WSL or macOS; not a model failure.
- **No parent uv environment / import error:** follow section 2 with the intended interpreter.
- **SDK lacks System One / endpoint unavailable:** verify the approved SDK/server distribution.
- **HTTP 413:** the suite always uses compact and guards request bytes; inspect the
  actual endpoint/proxy limit and recorded error without truncating or changing evidence.
- **Context/overflow/OOM:** preserve the error; do not silently lower context or truncate.
- **Timeout / provider rate limit / parsing failure:** inspect raw error/history; retry deliberately.
- **Incompatible resume metadata:** use a new output directory; do not edit saved fingerprints.
  Historical full-mode results cannot resume; compatible existing compact runs can.
- **Missing usage:** retain blanks; do not estimate tokens as if reported by the provider.

Treat a repeated native score as repeatability, not proof of correctness. Native usage
aggregates six scoring questions; chat usage counts one generation request. First calls
may include model loading/prefill, later calls can benefit from caching. Compact wording
can affect judgments even when evidence is preserved. Keep machines and
configurations in separate directories; do not mix with historical full-mode results.

Generated results are ignored by Git. If moving measurements between laptops, transfer
an entire experiment directory (all four files) separately and retain its metadata;
use it for comparison, not cross-machine resume. Push code, originals under `data/`,
compact artifacts/source map, Modelfiles, tests, requirements snapshot and documentation.
Do not stage secrets or force-add generated measurements as part of routine setup.
The completed 50-repetition experiment is an explicitly published exception:
[RESULTS_50.md](RESULTS_50.md) links its committed CSV/metadata snapshots. Treat those
directories as preserved evidence and use a new EY directory for further runs.

## Handoff prompt for EY GPT / Claude as a coding assistant

Copy this together with the repository checkout. It can inspect `RUN_GUIDE.md` and
`USAGE.md` first without opening the designer README:

```text
Set up and run the independent hard-case benchmark using hard_case/RUN_GUIDE.md.
Inspect the current branch and preserve existing work. Keep all hard-case changes
under hard_case/. Do not edit benchmark/, the root CLI, or the original tests.
Never consult hard_case/README.md: it contains designer ground truth. Do not rewrite
the nine originals in hard_case/data/, regenerate the reviewed compact packet,
introduce numeric targets or silently truncate/change context/backend settings.
Use the approved existing Python environment and enterprise credentials/endpoints.
Check OS compatibility (fcntl requires Linux/macOS) and dependencies; run the original
and hard-case suites separately. Use a fresh EY output directory. Run one repetition
with zero warm-ups per selected model; inspect validation, raw response, token usage,
latency and errors. For native Tev verify the aliases and actual 262144 context via
ollama ps. Only after successful smoke checks extend the same experiment to the
requested total, reusing successes. Report exact commands, results and limitations.

If the requested inference backend is EY GPT or Claude, explain that it is currently
unimplemented. Obtain the approved API base URL, authentication method, exact model or
deployment ID, protocol/SDK and any required API version; do not guess these or treat
an interactive chat login as API access. Implement a local provider/registry entry
under hard_case/ using the existing invoke(packet) -> ProviderResult architecture.
Send the identical compact packet and existing six definitions; use strict structured
HardCaseOutput with raw response/usage/errors preserved. Add provider configuration,
actual model/deployment identity and safe endpoint metadata to the fingerprint; never
record secrets. Test mapping, identical input delivery and incompatible resume without
paid calls, then perform the authorized smoke run. Do not use a public substitute
endpoint or claim a manual chat response is a measured API benchmark repetition.
```
