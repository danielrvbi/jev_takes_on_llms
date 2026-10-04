# Azure GPT and Claude: work-computer handoff

This is the complete guide for an agent receiving a fresh copy of this repository.
The provider implementations are ready. Configure Azure deployments, verify the
pilot, and use the existing experiment pipeline. No provider programming is needed.

## What this experiment runs

| Phase | Ten-question suite | Compact insurance suite | Maximum calls |
|---|---|---|---|
| Pilot | Question 1, 2 repetitions per provider | 2 repetitions per provider | 16 |
| Full | All 10 questions, 30 repetitions per provider | 30 repetitions per provider | 1,320 |

Both phases select GPT Luna 6, GPT Sol 6, Claude Opus 5.5 and Claude Sonnet 5.5
(`azure-gpt-luna`, `azure-gpt-sol`, `azure-claude-opus`, `azure-claude-sonnet`).
They use zero warm-ups, fresh clients, disabled LangChain response caching and zero
SDK retries. Output uses the existing
schemas, probability derivations and statistics. Neither suite supplies gold
labels or measures accuracy.

These are **altered-prompt prefix experiments**. Each request prepends a unique
identifier to the original system prompt. Acceptance requires explicit numeric
zero server cache-read telemetry. A prefix does not guarantee a cache miss,
especially when a service processes shared structured schemas before messages.
Missing telemetry or any cache hit stops the run with evidence saved. Do not
change prompts further, relax this rule, substitute telemetry defaults or retry
automatically to make the experiment pass. Hosted latency includes client creation,
inference, auditing and teardown; it does not measure a local cold start.

## Prerequisites and download

Use Windows, macOS or Linux with Python **3.12**, `uv`, network access to the two
Azure resource endpoints and existing GPT/Claude deployments supporting native
JSON-schema structured output. Creating Azure resources is not part of this guide.

Clone the repository at the implementation commit or download its source archive
and extract it. Retain `src/`, suite data, `pyproject.toml`, `uv.lock`, and this guide.
There is no dependency on the original computer's parent environment, private
Ollama runtime or model weights. If existing comparison datasets are desired,
also retain their complete folders under `results/experiments/`.

Install from the repository directory on any OS:

```text
uv sync --frozen --extra azure
```

Activate the environment to use the same commands below in either shell:

```powershell
# Windows PowerShell
.\.venv\Scripts\Activate.ps1
Copy-Item .env.example .env
```

```bash
# macOS/Linux
source .venv/bin/activate
cp .env.example .env
```

If activation is unavailable, replace `python` with `.venv\Scripts\python.exe`
on Windows or `.venv/bin/python` on Unix. Alternatively use
`uv run --frozen --extra azure python ...` from the repository directory.
Do not overwrite an existing `.env`; add the missing Azure fields to it instead.

## Configure local deployments

Fill the `AZURE_GPT_*` and `AZURE_CLAUDE_*` entries in the ignored `.env`, or set
them as process environment variables. Existing environment variables take
precedence over `.env`.

The template contains only these eight entries:

```dotenv
AZURE_GPT_API_KEY=<gpt-key>
AZURE_GPT_BASE_URL=https://<resource>.openai.azure.com/openai/v1/
AZURE_GPT_LUNA_MODEL=gpt-6-luna
AZURE_GPT_SOL_MODEL=gpt-6-sol

AZURE_CLAUDE_API_KEY=<claude-key>
AZURE_CLAUDE_BASE_URL=https://<resource>.services.ai.azure.com/anthropic/
AZURE_CLAUDE_OPUS_MODEL=claude-opus-5-5
AZURE_CLAUDE_SONNET_MODEL=claude-sonnet-5-5
```

The four model values are the deployment names sent as the API `model` parameter.
Replace them with your actual Foundry deployment names if different; the template
names do not establish availability in your Azure resource. Each pair uses its
family's shared key and endpoint. Resolved model identities are saved from responses.
Use resource inference endpoints, not project endpoints or complete operation URIs.
Azure's `services.ai.azure.com/openai/v1/` resource format is also accepted for GPT.

GPT uses `langchain-openai.ChatOpenAI` with Azure's v1 Chat Completions API;
Claude uses `langchain-anthropic.ChatAnthropic` with Foundry's Anthropic Messages
API and Azure resource key. The Claude integration owns its HTTP client per call
instead of using LangChain's shared default client. The tested SDK versions and
their HTTP dependencies are pinned by the lockfile.

Experiment settings are fixed in code: temperature zero, 4,096 output tokens,
120-second timeout, no explicit reasoning effort, no automatic retries and no
LangChain caching. No other environment entries are required. A deployment must
support these settings and native structured output to pass the pilot. Any needed
code or model change requires a fresh pilot and a new full dataset.

Never print or commit keys. The offline check displays only non-secret settings:

```text
python -m jev_bench.run azure --phase pilot --check-config
python -m jev_bench.run azure --help
python -m unittest tests.test_azure tests.test_portable_locks
```

The configuration check makes no network calls and writes no result files.
The tests use mocked HTTP transports; no Azure credentials or paid calls are
needed. Run them without enabling remote tracing. Real experiment invocations
disable LangSmith tracing regardless of ambient tracing settings.

## Run the pilot, then the full experiment

These commands make paid requests to your configured Azure resources:

```text
python -m jev_bench.run azure --phase pilot --output-dir results/experiments/azure_prefix_pilot
python -m jev_bench.run azure --phase full --pilot-dir results/experiments/azure_prefix_pilot --output-dir results/experiments/azure_prefix_30
```

The launcher records both suite targets before inference. The full command
revalidates the pilot evidence and requires identical deployment settings,
suite definitions, environment and implementation. A successful pilot contains
16 valid measurements and `complete: true` in `validation.json`.

The default call cap is 16 for the pilot and 1,320 for the full run. You may set
a smaller `--max-new-calls` to stop after a bounded batch. The cap counts attempted
calls across both suites **per invocation**, including calls with unknown outcomes;
it is not a dollar budget or a lifetime cap. Verified successes are skipped on
resume. An interrupted or failed request can still have been billed by Azure.

Repeat the printed resume command after a call-cap stop or interruption. It
preserves successful repetitions and their audit evidence. Resume after a
non-interruption rejection is blocked: inspect its evidence, correct configuration
if needed, and use a new output directory. Pilot and full folders stay separate;
pilot measurements are never copied into full results.

## Inspect failures

Inspect `validation.json`, the suite's `attempt_history.csv`, and its linked
`execution_audit/*.json`. Audits retain transmitted JSON, HTTP status/body,
request IDs, raw LangChain output, configured/resolved model identities, hashes,
token usage and latency. Headers containing authentication are never recorded.

- **401/403:** check the resource key and corporate access to the endpoint.
- **404:** check the resource URL and deployment name; deployment names need not
  equal model names.
- **400:** inspect deployment support for native structured output and configured
  sampling/reasoning parameters. No method fallback or automatic retry is used.
- **429/timeout:** preserve the rejection and inspect quota/connectivity. A new
  reviewed run is required; do not launch an automatic retry loop.
- **Cache rejection:** inspect raw cache-read telemetry. GPT requires
  `usage.prompt_tokens_details.cached_tokens=0`; Claude requires
  `usage.cache_read_input_tokens=0`. Missing values are not zero. Claude
  `cache_creation_input_tokens` is separately retained, and token totals include
  reported cache creation/read tokens.
- **Schema rejection/truncated response:** preserve the response and choose a
  supported model or change the fixed output budget in code, then run a new pilot.
- **Resume incompatibility:** select a new directory; never edit stored metadata
  or evidence to bypass checks.

## Validate, report and compare

Use existing offline evaluation commands; they make no model calls:

```text
python -m jev_bench.run_evaluations validate --input-dir results/experiments/azure_prefix_30
python -m jev_bench.run_evaluations report --input-dir results/experiments/azure_prefix_30
python -m jev_bench.run_evaluations compare --input-dirs results/experiments/tev_30 results/experiments/gemma_30 results/experiments/jev_api_30 results/experiments/azure_prefix_30
```

Include only comparison folders actually present on this computer. Reports and
plots go under `results/reports/`; source measurements remain unchanged. Comparisons
label Azure prefix results separately from original requests and the Mistral
prefix pilot. They do not pool these variants.

Full completion requires 1,200 valid ten-question measurements and 120 valid
insurance measurements. Histories remain authoritative and raw CSVs contain the
latest attempt per measurement key. Repeated deterministic answers are valid;
the experiment measures distributions and repeatability, not guaranteed variation.

## Bring results back

Copy the **entire** new experiment folder, including `run_plan.json`, metadata,
validation, raw/history CSVs and every execution audit. Relative audit paths work
after moving the folder. Keep the same source revision and dependency lock to
resume. Offline reports need no keys, Azure connectivity, Jev SDK, private runtime
or model weights. Never copy `.env` with results.

The receiving agent should finish by naming the output folder, counts of valid
and failed measurements, validation status and report location. This guide does
not authorize publishing, pushing results or transmitting credentials.

## References

- [LangChain Azure GPT v1 integration](https://docs.langchain.com/oss/python/integrations/chat/azure_chat_openai)
- [Claude in Microsoft Foundry: endpoints and API keys](https://platform.claude.com/docs/en/build-with-claude/claude-in-microsoft-foundry)
- [LangChain Claude structured output](https://docs.langchain.com/oss/python/integrations/chat/anthropic#structured-output)
- [Azure OpenAI prompt caching](https://learn.microsoft.com/en-us/azure/ai-services/openai/how-to/prompt-caching)
