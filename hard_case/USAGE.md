# Hard-case insurance benchmark

For a fresh laptop or an EY GPT / Claude coding-assistant handoff, start with
[RUN_GUIDE.md](RUN_GUIDE.md). It covers the external shared environment, OS limits,
backend setup, smoke checks and the distinction between current and future providers.

This is an independent benchmark comparing native Jev/System One candidate
probabilities with structured LLM probability estimates for one long insurance
claim packet. It follows the original benchmark's provider/runner/results
architecture, but has its own implementation, CLI, schema, tests, and outputs.
It does not import the original `benchmark` package or add a regular case.

Run commands from the repository root. Use the existing parent project's shared
environment with `uv run --no-sync`; do not create another virtual environment.
The integrations are Ollama, LangChain Ollama, and LangChain MistralAI. Other
dependencies include Pydantic, pandas, Rich, python-dotenv, and HTTPX.

## Input and judgments

The benchmark always sends `hard_case/compact/packet.md` to every model. There is
no full-packet inference mode. The loader reads these original UTF-8 files from
`hard_case/data/` only for source hash and provenance verification, in order:

1. `policy_wording.md`
2. `fnol.md`
3. `customer_emails.md`
4. `adjuster_notes.md`
5. `repair_invoices.md`
6. `police_report.md`
7. `previous_claims.md`
8. `internal_guidelines.md`
9. `timeline.md`

For the original-source hash only, each document is prefixed with
`===== filename =====` followed by a newline and joined with two newlines, preserving
source characters. This private provenance operation never returns model input.
The compact packet and its source map are validated against those originals.
`hard_case/README.md` contains designer-only information and is never read or sent.
Do not modify the source facts or use that README to construct probability targets.

Both backends receive the identical validated compact packet and the same six definitions:

| Field | Judgment |
| --- | --- |
| `requires_clarification` | Probability that material missing, contradictory, or insufficient information requires clarification before the claim can be reliably progressed. |
| `requires_human_review` | Probability that the case should be escalated to or reviewed by a human claims professional rather than handled automatically. |
| `policy_grounding_required` | Probability that the operative policy wording must be explicitly consulted to determine or support the correct handling of the claim. |
| `safety_compliance_concern` | Probability that the claim contains a material safety, regulatory, procedural, or compliance concern requiring special handling. |
| `coverage_likely` | Probability that the core collision loss is likely covered under the supplied policy, based only on the supplied case evidence. This is not a final legal determination. |
| `potential_fraud_signal` | Probability that the available evidence contains meaningful indicators warranting fraud-related scrutiny. This means signal strength, not probability that fraud actually occurred. |

These are independent probabilities; they need not sum to one. The schema requires
all six fields, forbids extra fields, and rejects strings, booleans, non-finite
values, and values outside `[0, 1]`. Probabilities are never normalized, repaired,
or converted into binary decisions. No accuracy or numeric ground-truth targets
are defined.

The prompt requires cross-document reconciliation, reported-versus-verified fact
distinctions, and consideration of contradictions and source reliability. It
prohibits invented evidence, final legal/liability determinations, explanations,
and binary decisions. System One uses six native `noul` questions in one request;
the entire packet is `state`. LLMs use
`with_structured_output(HardCaseOutput, method="json_schema", include_raw=True)`.

## Running

The model registry contains `tev1:0.8b`, `tev1:4b`, `gemma4:e4b`,
`mistral-small-latest`, and `mistral-large-latest`.

Mistral needs `MISTRAL_API_KEY` in the process environment or the repository root's
`.env` (the parent directory of `hard_case/`). The process environment takes
precedence. `hard_case/.env.example` documents the setting; place it in the parent
directory's `.env`.
Ollama uses its normal server or `OLLAMA_HOST` from the same configuration sources.

All five models on the same compact packet (includes paid Mistral requests;
create the verified Tev aliases described below first):

```bash
uv run --no-sync python -m hard_case.main \
  --all \
  --systemone-context 262144 \
  --repetitions 10 \
  --warmups 1 \
  --output-dir hard_case/results/compact_all
```

Select two models:

```bash
uv run --no-sync python -m hard_case.main \
  --models tev1:4b mistral-small-latest \
  --systemone-context 262144 \
  --repetitions 10 \
  --warmups 1 \
  --output-dir hard_case/results/compact_selected
```

The dedicated CLI requires exactly one of `--all` or `--models`. Defaults are 30
repetitions, two warm-ups, and the absolute `hard_case/results/` directory.
There is no `--case` or `--hard-case` flag. The original CLI remains independent.
`--input-profile compact` is an optional compatibility flag for existing commands.
Omitting it still uses compact; `--input-profile full` is rejected.

Models and repetitions run sequentially. Initialization and warm-ups are excluded
from measured latency. Model loading inside the first inference call is measured.
Every warm-up uses the same compact packet and all six questions.
Chat temperature is zero, no seed is supplied, Mistral automatic retries are
disabled, and chat client timeouts are 120 seconds. Native System One uses the
SDK's default timeout and exposes no temperature or per-request context override.
The optional context selection uses stock model aliases described below.
Gemma chat requests use `keep_alive="2h"`, requesting that Ollama retain the model
for two hours after each call. This setting is fingerprinted; existing Gemma
experiments with the previous default require a fresh output directory. Ollama
can still evict a model under memory pressure or when loading another model.

## Compact packet and maximum Tev context

The only model input is the checked-in `compact/packet.md`, a reviewed
evidence rewrite with all nine sections and line provenance in
`compact/source_map.json`. See `compact/REVIEW.md` for review scope and limitations.
Every backend always gets the same compact text. Original sources are
hash-verified; any change invalidates the artifact. The JSON-encoded packet must
fit 50 KiB and the actual serialized native SDK request must fit 60 KiB. Budget
failure stops before inference; no automatic truncation occurs.

Create the aliases once using the existing Ollama installation and cached weights:

```bash
ollama create tev1-hard:0.8b-ctx262144 -f hard_case/ollama/tev0.8b.Modelfile
ollama create tev1-hard:4b-ctx262144 -f hard_case/ollama/tev4b.Modelfile
```

Each Modelfile uses the original tag as `FROM` and changes only
`PARAMETER num_ctx 262144`. `--systemone-context 262144` resolves logical benchmark
labels `tev1:0.8b` and `tev1:4b` to those aliases. Other models keep their normal
configuration. The alias must exist, reuse the original weight blob, retain other
parameters/template/directives, and specify the requested context within its
advertised maximum. The suite does not create aliases automatically or reduce the
context on failure. Originals remain unchanged. The reviewed override is 262144;
other override values are rejected.

One measured repetition per model, no warm-ups:

```bash
uv run --no-sync python -m hard_case.main \
  --models tev1:0.8b tev1:4b \
  --systemone-context 262144 \
  --repetitions 1 --warmups 0 \
  --output-dir hard_case/results/compact_tev_262k
```

Inspect validation/errors, usage, and `ollama ps` allocated context during the run.
Only after both succeed, extend the same directory to ten each:

```bash
uv run --no-sync python -m hard_case.main \
  --models tev1:0.8b tev1:4b \
  --systemone-context 262144 \
  --repetitions 10 --warmups 0 \
  --output-dir hard_case/results/compact_tev_262k
```

This reuses the first successes, for 20 total measured repetitions. Aliases require
more memory than the original definitions; allocation failure must be reported,
not repaired by silently lowering context. Compact presentation may affect model
judgments. Historical full-packet results remain separate and cannot resume.
Native scoring checks complete prompt evaluation and rejects over-context prompts;
it does not truncate the packet. Usage aggregates work across the six scored
questions rather than counting a single copy of the evidence packet.

## Results and resume

All default and example outputs are under `hard_case/results/`, which is ignored
by Git for new outputs. The completed 50-repetition snapshot was explicitly added
to Git; [RESULTS_50.md](RESULTS_50.md) records its statistics and artifact hashes.
Use a fresh directory for new measurements. Each experiment directory contains:

- `raw.csv`: latest attempt per `(model, repetition)`.
- `attempt_history.csv`: append-only record of every measured attempt.
- `summary.csv`: per-model probability, latency, usage, and validation statistics.
- `metadata.json`: fingerprinted input, schema, prompts, configuration, and environment.

Rows record model, repetition, attempt number, UTC timestamp, latency in ms,
available input/output token counts, validation success, errors, raw response
JSON, and all six probability fields. Failed responses retain available numeric
values for auditing. Parsing failures are never accepted even when raw JSON can
be recovered. Missing usage stays blank, not zero; HTTP rejections retain their
status and error body when available.

Re-run a command to resume. Successful repetitions are skipped without provider
initialization or warm-ups when that model has no pending work. Failed repetitions
are retried once per invocation, with prior attempts retained in history. Increasing
`--repetitions` changes the target total, not the number of additional calls. Adding
models or selecting a subset is allowed; already recorded models' configuration
and provenance are retained. Reports include every saved model in the directory.

History is flushed before each atomic raw snapshot; resume rebuilds stale raw
snapshots from history. A directory lock prevents concurrent writers. Missing or
malformed history and incompatible metadata fail explicitly. Interrupted measured
calls are recorded as failures when possible. Warm-up interruptions leave an empty,
resumable history. Exit statuses are `0` for all requested measurements successful,
`1` for errors or failed measurements, and `130` for interruption.

Probability mean, sample standard deviation (`ddof=1`), minimum, and maximum use
successful latest repetitions only. Standard deviation with fewer than two
successes, and probability statistics without successes, are undefined/blank.
Latency p50/p95 use all latest measured attempts, including failures, with linear
interpolation. Token means use latest attempts with usage present; availability
counts are reported separately. Latest validation failures and historical failures
remain distinct, so retries do not erase reliability evidence.

Metadata records exact ordered source filenames, packet SHA256, character count,
word count, UTF-8 byte count, both prompt forms, the output schema, requested model
settings, and dependency/Python/platform information. For selected installed local
models, read-only inspection also records digests, model parameters, templates,
Modelfiles, advertised context limits, and the server version when available.
Changes to the packet, schema, prompts, environment, or a selected model's recorded
configuration reject resume before provider calls or result replacement. Use a
fresh directory after such changes. Repetition/warm-up counts are operational
choices and are not part of the compatibility fingerprint.
Removing full-packet capability does not change existing compact fingerprints;
compatible compact runs can still resume. Historical metadata lacking the compact
profile, or explicitly recording full, is rejected without replacing result files.
Compact metadata additionally fingerprints profile, original source hash, compact
hash, source-map hash and JSON size; context overrides record actual inference
aliases/settings. Raw native responses also contain the actual alias name.

## Separate test suites and smoke test

```bash
uv run --no-sync python -m unittest discover -s tests -t . -v
uv run --no-sync python -m unittest discover -s hard_case/tests -t . -v
```

The original root `python -m unittest discover -v` command continues to discover
only the original suite. The hard-case package deliberately requires explicit
selection of `hard_case/tests/`, keeping the two suites separate.

Hard-case tests use artificial response and document fixtures, not probability
targets for the synthetic claim. They cover loading, validation, provider mapping,
packet equivalence, runtime metadata, fingerprints, persistence, resume, statistics,
CLI behavior, and independence from the original benchmark and tests.

Local smoke test; repeat the exact command to verify successful skipping and failed
attempt retention:

```bash
uv run --no-sync python -m hard_case.main \
  --models tev1:0.8b gemma4:e4b \
  --systemone-context 262144 \
  --repetitions 1 --warmups 0 \
  --output-dir hard_case/results/compact_smoke
```

## Backend limitations

The compact packet is 44,731 bytes when JSON-encoded. Native requests with the
reviewed Tev aliases are 50,083 / 50,081 bytes, below the 60 KiB guard and the
inspected Ollama 0.35.0 endpoint's 64 KiB limit. Original-source concatenation is
approximately 110,000 UTF-8 bytes but is never sent to inference. The original Tev
definitions set `num_ctx=2050`; use the verified 262144 aliases for this experiment.
The native SDK has no per-request context override, so aliases remain necessary.

Ollama chat requests explicitly set the server's `truncate=false` field so overflow
is rejected. The installed SDK does not expose that field, so a small HTTPX request
hook adds it while preserving LangChain structured output and the complete packet.
No context override is supplied. An insufficient context window must result in a
failure rather than a shortened comparison. Hosted context errors are also retained
without a fallback.

The compact packet and verified stock aliases address the original-source byte and
original-model context limits without changing the server. A larger context is a
capacity setting, not evidence that judgments are accurate.

Model aliases can change hosted weights without exposing an immutable version;
metadata pins requested aliases and discoverable local digests, not undisclosed
hosted weight revisions. Native usage can aggregate scoring work across questions,
while LLM usage describes generation: compare usage with that distinction in mind.
Results describe judgment, repeatability, variance, latency, usage, and validation
reliability; they do not establish accuracy or calibration.
