# Reviewed validation run

From the repository root, prepare the private pinned Ollama runtime and install the reviewed context aliases as described in [USAGE.md](USAGE.md). Preserve the reviewed compact packet, source map and original documents.

Run these commands sequentially after both offline test suites pass:

```bash
uv run --no-sync python main.py --all --repetitions 1 --warmups 0 --output-dir results
uv run --no-sync python -m hard_case.main --all --repetitions 1 --warmups 0 --systemone-context 262144 --output-dir results
uv run --no-sync python validation.py --output-dir results
uv run --no-sync python show_evaluations.py --input-dir results --output results/evaluation.md
```

The root must contain 50 original-suite keys in `benchmark/raw.csv` and five insurance keys in `hard_case/raw.csv`, with linked audit evidence for every measured attempt. `validation.json` is complete only if all 55 latest measurements pass schema and cache checks and all attempts retain valid links. Positive, missing or malformed Mistral cache telemetry is a recorded failure. Do not silently retry these calls, alter prompts or describe incomplete validation as cache-free success.

Increase `--repetitions` with the same settings to extend the experiment. Existing successful keys are reused only after evidence revalidation. All outputs stay in the same common root, with separate CSV schemas. Quarantined measurements remain untouched.

For the capped Mistral prefix experiment, use `uv run --no-sync python run_prefix_pilot.py --output-dir results` from the repository root. See the root README for its eight-call budget, stop-on-first-failure behavior, distinct output directories and altered-prompt interpretation. `--max-new-calls` includes warm-ups; hosted cache failures stop immediately after audit persistence.
