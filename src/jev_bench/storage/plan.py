"""Versioned requested measurement targets, independent of model registries."""
import json
from copy import deepcopy
from jev_bench.storage.io import atomic_write, output_lock
from jev_bench.storage.paths import ensure_writable


def record_selection(root, suite, models, cases, repetitions, warmups, definition,
                     maximum=None, prefix=False):
    ensure_writable(root)
    with output_lock(root / "plan_lock"):
        path = root / "run_plan.json"
        plan = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {
            "version": 2, "kind": "prefix_pilot" if prefix else "benchmark",
            "targets": {}, "revisions": []}
        # Version-one hosted plans are immutable and remain supported for legacy fixtures.
        if plan["version"] == 1:
            return
        if plan.get("kind") == "jev_api":
            return
        if plan.get("kind") in ("azure_prefix_pilot", "azure_prefix_full"):
            target = plan['targets'].get(suite, {})
            for model in models:
                expected = {"case_ids": sorted(cases), "repetitions": repetitions, "warmups": warmups,
                            "configuration": definition['model_configuration'][model]}
                if target.get(model) != expected:
                    raise ValueError('Azure targets are immutable; use another --output-dir')
            return
        before = deepcopy(plan.get("targets", {}))
        target = plan.setdefault("targets", {}).setdefault(suite, {})
        for model in models:
            selection = {"case_ids": sorted(cases), "repetitions": repetitions,
                         "warmups": warmups, "configuration": definition.get("model_configuration", {}).get(model, {})}
            previous = target.get(model)
            if previous:
                if previous.get("configuration") is not None and previous["configuration"] != selection["configuration"]:
                    raise ValueError("Run-plan model configuration changed")
                selection["case_ids"] = sorted(set(previous["case_ids"]) | set(cases))
                selection["repetitions"] = max(previous["repetitions"], repetitions)
            if suite == "benchmark":
                per_case = dict((previous or {}).get("case_repetitions", {}))
                if previous and not per_case:
                    per_case = {str(c): previous["repetitions"] for c in previous["case_ids"]}
                for case in cases:
                    per_case[str(case)] = max(per_case.get(str(case), 0), repetitions)
                selection["case_repetitions"] = per_case
            target[model] = selection
        plan["suites"] = sorted(plan["targets"])
        plan["max_new_calls"] = maximum
        if definition.get("jev_cache_exception"):
            plan["jev_cache_exception"] = definition["jev_cache_exception"]
        if before != plan["targets"]:
            plan.setdefault("revisions", []).append({"targets": deepcopy(plan["targets"])})
        atomic_write(path, lambda handle: json.dump(plan, handle, indent=2))


def expected_keys(plan):
    if plan.get("version") != 2 or not isinstance(plan.get("targets"), dict) or not plan["targets"]:
        raise ValueError("Invalid experiment target manifest")
    expected = {}
    for suite, models in plan["targets"].items():
        if suite not in {"benchmark", "hard_case"} or not models:
            raise ValueError("Invalid run-plan suite selection")
        keys = set()
        for model, target in models.items():
            reps = target["repetitions"]
            cases = target["case_ids"]
            if type(reps) is not int or reps < 1 or not isinstance(model, str) or not model:
                raise ValueError("Invalid run-plan repetitions/model")
            if suite == "benchmark" and (not cases or any(type(c) is not int or not 1 <= c <= 10 for c in cases)):
                raise ValueError("Invalid run-plan cases")
            if any(type(n) is not int or n < 1 for n in target.get('case_repetitions', {}).values()):
                raise ValueError('Invalid per-case repetition target')
            keys.update((model, case, rep) for case in cases for rep in range(1, target.get('case_repetitions', {}).get(str(case), reps) + 1)) if suite == "benchmark" else keys.update(
                (model, rep) for rep in range(1, reps + 1))
        expected[suite] = keys
    return expected
