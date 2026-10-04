# Analysis starter

Open `starter.ipynb` and select this repository's Python 3.12 `.venv` kernel.
It loads saved results and leaves the analysis to you. No model calls are made.

```bash
uv sync --frozen --extra analysis
```

If your notebook editor cannot find the environment, register it explicitly:

```bash
.venv/bin/python -m ipykernel install --user --name jev-bench --display-name "Jev bench (Python 3.12)"
```

## Data

`df_benchmark10` contains the ten-question suite; `df_hardcase` contains the
compact insurance case. The notebook uses the existing audited reader and
`pd.concat` to combine older sources and the 3 October 2026 validation batch
**in memory**. Source files and original repetition IDs stay unchanged.

Old sources are `tev_30`, `gemma_30`, `jev_api_30`, and `baseline` (Mistral only).
New sources are the six explicit `refactor_validation_20261003_*` roots.
Historical variants, pilots, generated reports, and quarantine are excluded.
The matching old/new prompts, schemas, inputs, and model configurations were
checked when preparing this starter. Hosted `latest` aliases can still change.

`source_dataset` and `cohort` preserve provenance. Unique keys are
`(source_dataset, model, case_id, repetition)` for the benchmark and
`(source_dataset, model, repetition)` for the hard case. Overlapping repetition
numbers across sources are separate calls. `cohort="old"` does not imply 30
accepted calls for every model.

Jev, both Tev models, and Gemma have 40 accepted repetitions per benchmark
question and 40 for the hard case. Mistral is incomplete; use the displayed
observed/accepted counts, not an assumed 40. Missing Mistral hard-case CSVs in
the new batch are skipped, without generating or inventing measurements.

Keep Mistral in the DataFrames, but exclude it from the primary stability
comparison: accepted repetitions per question are too sparse. Accepted Mistral
rows passed the existing cache checks; analyze those only as exploratory results.
Never include rejected cached/cache-unverified rows in probability or variance
tests. The notebook reports cache and other failures separately.

Rejected latest rows remain visible. Use only `validation_success == True`
for probability/classification analysis; validation success is not correctness.
The reader also verifies attempt history. `raw.csv` contains latest attempts,
so resource totals including retries need the reader's `history` frame as well;
do not add latest-row totals to history totals and double-count calls.

## Metrics to define yourself (not implemented)

| Metric | Definition |
|---|---|
| Classification accuracy (%) | Percentage matching your reference labels. For binary fields: `p >= 0.50` gives 1, otherwise 0. This is thresholding. Route and freshness need their own reference definitions. |
| Decision repeatability | Fraction agreeing with the most common classification for a fixed input/field. A consistently wrong model is still repeatable. Correct repetitions / accepted repetitions is per-question accuracy. |
| Absolute probability error | Your “distance to accuracy”: `abs(p - y)`, with reference label `y` equal to 0 or 1. Smaller means closer to that label. |
| Probability stability | Spread (e.g. SD) across repeated probabilities for the same input and field; smaller spread means greater repeatability. |
| Probability decisiveness | Distance from 0.5. This describes stated certainty, not correctness or calibrated confidence. |
| Resource efficiency | Compare latency and input/output tokens separately, alongside accuracy. Tokens/time per correct result includes resources spent on failures, retries, and incorrect results; zero correct results makes the ratio undefined. |

Neither suite supplies gold labels. Add your own references before accuracy or
probability-error analysis. For ambiguous questions, document the reference
rationale or report repeatability without accuracy claims. The hard case's six
probabilities are independent fields: do not normalize them to sum to one.
Benchmark categorical distributions are not six independent binary tasks.
Do not count a call's latency/tokens multiple times because it returns many fields.
Token counts also depend on each model's tokenizer; fewer tokens alone do not
establish lower cost.

## Where the tests fit

| Question | Suggested analysis |
|---|---|
| Different mean error, latency, or tokens for one input? | Welch on independent comparable calls; two-sided by default. Interpret the mean difference and its 95% confidence interval. |
| Different probability or latency spread for one input/field? | Median-centered Levene (Brown–Forsythe). Its two-sided p-value detects a spread difference; observed SDs show which model varies less. |
| More accurate across the same benchmark questions? | Compare paired per-question accuracy summaries, e.g. paired permutation inference. Preserve the pairing; do not flatten all repetitions into independent questions. |

Welch does not require equal variances, so do not use Levene as a gate for choosing
Welch. Levene on binary correctness mixes variance with accuracy (`p(1-p)`);
it does not independently measure stability. Pooling raw scores from different
questions mixes question difficulty with variation across repetitions.

Assumptions and interpretation:

- Calls must be independent and conditions comparable. Forty calls on one
  question provide repeatability evidence for that question, not 40 independent
  questions. The benchmark has ten distinct questions; the hard case has one.
- Inspect distributions and outliers. Welch relies on a reasonable approximation
  for sampling means; Levene's reference distribution is approximate. There is
  no automatic “significant enough” sample-size threshold.
- Compare cohorts before pooling; inspect results within each cohort for drift.
  Local cold wall time includes loading/startup/verification/teardown; hosted
  wall time has different boundaries. Jev server caching remains unverified.
- Define comparisons before looking at outcomes. Default to alpha 0.05, use Holm
  adjustment within the declared family of comparisons, and report differences,
  spread measures, confidence intervals, and actual accepted sample sizes.
- A small adjusted p-value plus the observed direction supports a difference
  under the assumptions. Nonsignificance does not establish equivalence.
  These helpers compare models, not performance against a fixed target.

`welch(a, b)` and `levene(a, b)` accept finite 1-D samples with at least two
observations and return SciPy result objects (`.statistic`, `.pvalue`). Welch also
provides `.confidence_interval()`. Missing values must be handled explicitly
before calling. Degenerate constant samples may give undefined statistics or
p-values; do not interpret them as evidence of equality or superiority.

References: [Welch / SciPy](https://docs.scipy.org/doc/scipy/reference/generated/scipy.stats.ttest_ind.html),
[Levene / SciPy](https://docs.scipy.org/doc/scipy/reference/generated/scipy.stats.levene.html).

## Power sensitivity

`mean_power(effect_size, n=40, paired=False, alpha=0.05)` calculates the probability
of detecting an **assumed** standardized mean difference. It is not observed
significance, and it does not calculate accuracy-test or Levene-test power.

- Independent mode: `n` calls **per group**, equal group sizes, and an assumed
  common population SD. This is an equal-variance planning approximation,
  not exact Welch power. It only represents a particular input's call variation.
- Paired mode: `n` independent pairs (e.g. ten matched question summaries), with
  effect size standardized by the SD of paired differences. Its effect-size
  scale differs from independent mode.
- Use hypothetical effects meaningful to you, rather than an observed effect to
  claim post-hoc adequacy. The notebook's small table uses illustrative effects
  0.2, 0.5, and 0.8, with 40 calls/group and ten question pairs. It is not a model
  comparison. An 80% planning goal is a convention, not a significance criterion.
- Incomplete or unequal Mistral samples require a power calculation with their
  actual group sizes; this equal-size helper does not represent those comparisons.
  Multiplicity adjustment can reduce power. Variance and binary accuracy power
  need separate assumptions and calculations.

Reference: [Statsmodels independent power](https://www.statsmodels.org/stable/generated/statsmodels.stats.power.TTestIndPower.html),
[paired power](https://www.statsmodels.org/stable/generated/statsmodels.stats.power.TTestPower.html).

## Offline checks

```bash
uv sync --frozen --extra analysis --extra azure --group test
.venv/bin/python -m unittest discover -s analysis -p 'test_*.py'
.venv/bin/python -m unittest tests.test_azure tests.test_portable_locks
.venv/bin/python -m unittest discover -s tests -p 'test_*.py'
```
