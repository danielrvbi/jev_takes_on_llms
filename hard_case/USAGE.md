# Insurance suite usage

Run from the repository root using the existing parent uv environment. This entry point uses the same mandatory cache-free infrastructure as the ten-question benchmark. There is one run policy.

```bash
uv run --no-sync python runtime/prepare.py
ollama create tev1-hard:0.8b-ctx262144 -f hard_case/ollama/tev0.8b.Modelfile
ollama create tev1-hard:4b-ctx262144 -f hard_case/ollama/tev4b.Modelfile
uv run --no-sync python -m hard_case.main --all --repetitions 1 --warmups 0 --systemone-context 262144 --output-dir results
```

Local tags are `tev1:0.8b`, `tev1:4b` and `gemma4:e4b`; hosted tags are `mistral-small-latest` and `mistral-large-latest`. Mistral requires `MISTRAL_API_KEY` in the environment or repository `.env`. `--models` selects a subset. Defaults remain 30 repetitions and two warm-ups; use zero warm-ups for validation. Every warm-up gets its own audited cache-free call and stays out of measured CSVs.

`--output-dir` selects a common results root. Insurance outputs always go in its `hard_case/` subdirectory; original-suite outputs go in `benchmark/`. The default root is `<repository>/results`, regardless of current working directory. `validation.json` at that root checks the requested 50 original-suite keys and five insurance keys. A partial run or any schema/cache failure leaves it incomplete.

The loader always uses the reviewed `compact/packet.md` and verifies `compact/source_map.json` against the original evidence documents. `--input-profile compact` remains an optional compatibility flag. The compact packet must stay within 50 KiB after JSON encoding; the complete native System One request must stay within 60 KiB. The designer README is never an inference input. Do not regenerate compact records or change claim evidence as part of a benchmark run.

`--systemone-context 262144` selects the checked-in stock Tev aliases and verifies that only context differs from the original tag. Without this option, native tags retain their stock context and may reject the long packet. Gemma receives `truncate=false` at the HTTP boundary. Context overflow is retained as failure evidence rather than silently dropping input.

The same complete packet and suite-specific schema reach each model. Six probabilities remain independent and unnormalized. Prompts, temperatures, seeds and scoring calculations are preserved. Private local calls evaluate each complete scoring prompt independently, including candidate-bias retries. Response caching is explicitly disabled; each chat client is fresh and has no previous answers. A fresh Mistral key is mandatory, and explicit numeric `cached_tokens=0` is required for acceptance.

`results/hard_case/` holds separate `raw.csv`, `attempt_history.csv`, `summary.csv`, `metadata.json`, plots and `execution_audit/`. Each local audit links the private runtime/model fingerprints, complete logs, per-prompt checks, process teardown and cold startup/inference/teardown timings. Cached, missing-telemetry, malformed and truncated responses are retained with failure reasons. Local CSV latency includes startup/loading and teardown.

Rerun the same command to resume. Successful repetitions are skipped only after revalidating their linked evidence and saved probabilities; incompatible metadata is rejected. Failed repetitions receive one new attempt on an explicit rerun, with history retained. The quarantined folder and historical `hard_case/results/` are never read for resume.

```bash
uv run --no-sync python show_evaluations.py --input-dir results --suite hard_case --output results/hard_case/evaluation.md
uv run --no-sync python -m unittest discover -s hard_case/tests -p 'test_*.py'
```

The [repository guide](../README.md) documents runtime preparation, the precise cache boundary and the original suite. Hosted cache evidence is limited to Mistral's explicit telemetry; identical independently computed outputs remain possible.

For the capped Mistral prefix experiment, use `uv run --no-sync python run_prefix_pilot.py --output-dir results` from the repository root. See the root README for its eight-call budget, stop-on-first-failure behavior, distinct output directories and altered-prompt interpretation. `--max-new-calls` includes warm-ups; hosted cache failures stop immediately after audit persistence.
