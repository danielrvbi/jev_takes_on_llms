"""Spending controls shared by both suites; never relax cache acceptance."""
from pathlib import Path

PREFIX_STRATEGY = {'version': 1, 'template': 'Request identifier: <uuid4 hex>\n\n',
                   'position': 'start of first system message', 'experimental': True}


class RunStopped(RuntimeError):
    """A saved failure or exhausted call budget prevents further inference."""


class CallGuard:
    def __init__(self, maximum=None, prefix_experiment=False):
        if maximum is not None and (isinstance(maximum, bool) or not isinstance(maximum, int) or maximum < 1):
            raise ValueError('max_new_calls must be a positive integer')
        self.maximum, self.used, self.prefix_experiment = maximum, 0, prefix_experiment

    def before_call(self):
        if self.maximum is not None and self.used >= self.maximum:
            raise RunStopped(f'Call budget exhausted ({self.used}/{self.maximum}); no further inference')
        self.used += 1

    def after_result(self, model, result, console):
        if model.startswith('azure-'):
            usage = (result.raw_response or {}).get('execution_audit', {}).get('usage', {})
            console.print(f'{model}: input={result.input_tokens}, output={result.output_tokens}, '
                          f'cached={usage.get("cached_tokens", "missing")}; new calls={self.used}', markup=False)
        if model == 'jev-1.13.0':
            console.print(f'{model}: input={result.input_tokens}, output={result.output_tokens}; '
                          f'{"server caching unverified" if (result.raw_response or {}).get("execution_audit", {}).get("cache_exception") else "cache verification required"}; new calls={self.used}', markup=False)
        if model.startswith('mistral'):
            raw = result.raw_response or {}
            usage = raw.get('response_metadata', {}).get('token_usage', {})
            cached = usage.get('prompt_tokens_details', {}).get('cached_tokens', 'missing')
            console.print(f'{model}: input={result.input_tokens}, output={result.output_tokens}, cached={cached}; new calls={self.used}', markup=False)
        if result.error and (model == 'jev-1.13.0' or self.prefix_experiment or
                (model.startswith('mistral') and result.error.startswith('Cache verification failed:'))):
            raise RunStopped('Stopped after saved failure: ' + result.error)


def experiment_settings(root, models, prefix_experiment, max_new_calls):
    guard = CallGuard(max_new_calls, prefix_experiment)
    root = Path(root)
    if any(m.startswith('azure-') for m in models):
        import json
        from jev_bench.providers.azure_config import AZURE_MODELS
        if not all(m in AZURE_MODELS for m in models) or not prefix_experiment or max_new_calls is None:
            raise ValueError('Azure requires a separate, capped prefix experiment')
        plan = root / 'run_plan.json'
        if not plan.exists() or json.loads(plan.read_text()).get('kind') not in ('azure_prefix_pilot', 'azure_prefix_full'):
            raise ValueError('Use python -m jev_bench.run azure to initialize both suite targets')
        return root, guard
    if prefix_experiment:
        if any(not m.startswith('mistral') for m in models):
            raise ValueError('The prefix experiment supports only Mistral models')
        if max_new_calls is None:
            raise ValueError('The prefix experiment requires --max-new-calls')
        import json
        plan = root / 'run_plan.json'
        if not plan.exists() or json.loads(plan.read_text()).get('kind') != 'prefix_pilot':
            root = root / 'prefix_pilot'
    return root, guard
