# Tev takes on LLMs

Two structured judgment benchmarks use one mandatory, audited cache-free execution policy. The ten-question suite compares Tev 0.8B, Tev 4B, Gemma 4 E4B, Mistral Small and Mistral Large. The insurance suite evaluates six independent probabilities from the reviewed compact claim packet. These experiments measure output distributions and repeatability; they have no gold labels or accuracy claims.

All new measurements share one results root:

```text
results/
├── benchmark/          # Ten questions: separate CSV schema, summaries and plots
├── hard_case/          # Insurance packet: separate CSV schema, summaries and plots
└── validation.json     # Complete only when all 50 + 5 repetition-one keys pass
```

`results_contaminated_dont_use/` is quarantined. Historical datasets are never imported or used to resume these runs. Both entry points interpret `--output-dir` as the common root and append their suite directory. The default is this repository's `results` directory.

## Prepare the private runtime

Use the existing parent uv project/environment. Local model tags must already be installed in Ollama. Set `MISTRAL_API_KEY` in the environment or this repository's ignored `.env`.

```bash
uv run --no-sync python runtime/prepare.py
```

The preparation script pins Ollama 0.35.0 at commit `cc4069396f3ad2c370c53eed2e4a42ac13adab84`, downloads checksum-pinned Go 1.26.0 into ignored `.runtime/`, applies the checked-in `runtime/no-cache.patch`, runs mocked backend tests and builds a private server. On macOS arm64 it copies and fingerprints the matching installed 0.35.0 native payload. It never modifies the installed Ollama application. Source, compiler, binaries and native payload remain in `.runtime/`; the patch, tests and recipe remain in Git. Run preparation again after changing the patch.

For the insurance suite, create the checked-in aliases once using the existing CLI:

```bash
ollama create tev1-hard:0.8b-ctx262144 -f hard_case/ollama/tev0.8b.Modelfile
ollama create tev1-hard:4b-ctx262144 -f hard_case/ollama/tev4b.Modelfile
```

The aliases preserve weights, templates and model settings while selecting context 262144. The runner verifies them before inference. See [hard_case/USAGE.md](hard_case/USAGE.md) for packet budgets and context selection.

## Run

```bash
uv run --no-sync python main.py --all --repetitions 1 --warmups 0 --output-dir results
uv run --no-sync python -m hard_case.main --all --repetitions 1 --warmups 0 --systemone-context 262144 --output-dir results
```

The same commands extend an experiment by increasing `--repetitions`. Select models with `--models tev1:0.8b tev1:4b`; select original-suite questions with `--case 1 7`. Defaults remain 30 repetitions and two warm-ups. Each warm-up has its own audited cache-free call and is excluded from measured CSVs. There is one execution policy and no mode switch.

Each local call starts a private loopback Ollama server and a fresh model worker, makes one inference request, terminates its process group and verifies teardown. Tev keeps its complete multi-question schema. The runtime removes shared-prefix priming and sends `cache_prompt=false` for every scoring request, including internal bias retries, and for chat/completion requests. Every nonempty native prompt task must show zero initial cached tokens and evaluation of its entire prompt, with no restored checkpoint or truncation. Backend dispatch markers account for all tasks. An empty schema-to-grammar conversion evaluates zero tokens and performs no inference computation.

LangChain response caching is explicitly disabled. Chat clients are created and closed for every call, previous answers are never sent and automatic request retries remain zero. Mistral calls use unique `prompt_cache_key` values without changing messages. Only explicit numeric `cached_tokens=0` is accepted; positive, missing or malformed telemetry rejects the result. A fresh key alone is insufficient evidence. Hosted evidence is limited to Mistral's explicit telemetry. Rejected responses are preserved and never silently retried or repaired.

Model weights on disk and state used to generate one response are permissible. Inference computation reused from another prompt, including another Tev judgment within one request, is forbidden. Independent execution can still produce identical answers at the preserved deterministic settings.

## Outputs and resume

Each suite retains append-only `attempt_history.csv`, atomic `raw.csv` and `summary.csv` snapshots, `metadata.json`, plots and `execution_audit/`. Raw rows include call ID, cache status, failure class, request/runtime/model hashes and the audit path/hash. Every attempt's sidecar retains its request, response, evidence and failure reason. Local calls also retain complete server logs, per-task token evidence and separate startup, inference and teardown timings. Reported `latency_ms` is cold wall time, including startup/loading and teardown.

Successful repetitions are skipped only after rechecking metadata, evidence hashes, all prompt/cache evidence, actual model/runtime fingerprints and agreement between the response and saved probabilities. Old metadata is rejected. Failed repetitions get one new attempt when explicitly rerunning a command; historical failures remain recorded. Writer locks prevent concurrent writes to one suite directory.

`validation.json` checks all 55 requested repetition-one keys and audit links for all measured attempts. Any rejected measurement or invalid evidence leaves validation incomplete. Partial/model-filtered experiments also remain incomplete. Audit evidence is an integrity record, not a cryptographic attestation by the hosted provider.

```bash
uv run --no-sync python show_evaluations.py --input-dir results --output results/evaluation.md
uv run --no-sync python show_evaluations.py --input-dir results --suite hard_case --include-raw
uv run --no-sync python validation.py --output-dir results
```

Reports distinguish cache-verification failures from schema failures. `reconcile_results.py` remains an explicit evaluation-only utility for compatible, audited original-suite source directories; all three paths must be supplied. Normal runs already consolidate all models in `results/benchmark` and require no reconciliation.

## Offline tests

```bash
uv run --no-sync python -m unittest discover -s tests -p 'test_*.py'
uv run --no-sync python -m unittest discover -s hard_case/tests -p 'test_*.py'
```

The preparation script separately runs the pinned Go tests against mocked native requests, checking the absence of a prefix primer and explicit `cache_prompt=false` on scoring retries and chat/completion requests. Offline Python tests cover every-prompt verification, hosted telemetry, fresh keys/clients, disabled response caches, cleanup after failure/interruption, unchanged prompts, locking, resume integrity and schema separation.

## Capped Mistral prefix pilot

Hosted cache-verification failures now stop the invocation immediately after saving the rejected response and audit. `--max-new-calls N` caps new calls, including warm-ups. The limit is per invocation and exhaustion stops with a nonzero exit; it never counts skipped successful repetitions.

Run the separate, globally capped pilot:

```bash
uv run --no-sync python run_prefix_pilot.py --output-dir results
```

It tries at most eight calls: two repetitions of question 1 and two of the compact hard case for each Mistral model, sequentially, with zero warm-ups. It stops the entire pilot on its first cache, schema or request failure and refuses to retry a saved rejection. Interrupted successful partial pilots can resume after evidence revalidation. It never launches a bulk run.

Each call prepends `Request identifier: <fresh UUID>\n\n` to the first system message and still transmits a fresh cache key. The rest of the prompts, schemas and settings remain unchanged. This is an altered-prompt experiment, not interchangeable with the original benchmark. Mistral may cache content preceding the user-controlled prefix; only explicit numeric zero telemetry passes.

Pilot outputs live under `results/prefix_pilot/benchmark/` and `results/prefix_pilot/hard_case/`, with `pilot.json` and an eight-measurement `validation.json` at the pilot root. Audits retain raw usage; rejected CSV rows retain input/output counts; console output and the manifest include cached-token counts. Original results and quarantined data are not imported or overwritten.

Both entry points expose `--prefix-experiment --max-new-calls N` for bounded diagnostics, appending `prefix_pilot` to the selected common root. This option requires Mistral-only selection and an explicit budget. Use the orchestrator above for the agreed eight-call experiment. Original and prefix metadata are incompatible by design. Source fingerprints also prevent silently resuming pre-change methodology.
