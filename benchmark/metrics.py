from execution import AUDIT_COLUMNS
import math

import numpy as np
import pandas as pd

from benchmark.schemas import ROUTES, DecisionOutput


ROUTE_COLUMNS = [f"route_{route}_probability" for route in ROUTES]
FRESHNESS_COLUMNS = [f"freshness_{level}_probability" for level in range(6)]
PROBABILITY_COLUMNS = ["requires_web_probability", "is_safe_probability", *ROUTE_COLUMNS, *FRESHNESS_COLUMNS]
DERIVED_COLUMNS = [
    "requires_web_decision", "is_safe_decision", "derived_route", "expected_freshness",
    "route_entropy_bits", "freshness_entropy_bits",
]
RAW_COLUMNS = [
    "model", "case_id", "message", "repetition", "attempt", "timestamp_utc",
    *PROBABILITY_COLUMNS, "route_sum_error", "freshness_sum_error", *DERIVED_COLUMNS,
    "latency_ms", "input_tokens", "output_tokens", "validation_success", "error", "raw_response_json", *AUDIT_COLUMNS,
]


def normalized(values):
    probabilities = np.asarray(values, dtype=float)
    total = probabilities.sum()
    if (not np.isfinite(probabilities).all() or np.any(probabilities < 0)
            or np.any(probabilities > 1) or total <= 0):
        raise ValueError("Cannot normalize an invalid or zero-total distribution")
    return probabilities / total


def entropy(probabilities):
    positive = np.asarray(probabilities)[np.asarray(probabilities) > 0]
    return float(-np.sum(positive * np.log2(positive)))


def flatten_values(values):
    """Retain scalar values even on failed validation; raw JSON is the lossless record."""
    result = {}
    for key in ["requires_web_probability", "is_safe_probability"]:
        result[key] = values.get(key)
    for name, keys, columns in [
        ("route", ROUTES, ROUTE_COLUMNS),
        ("freshness", tuple(str(i) for i in range(6)), FRESHNESS_COLUMNS),
    ]:
        distribution = values.get(f"{name}_probabilities")
        if not isinstance(distribution, dict):
            continue
        components = [distribution.get(key) for key in keys]
        result.update(zip(columns, components))
        if all(isinstance(p, (int, float)) and not isinstance(p, bool) and math.isfinite(p)
               for p in components):
            result[f"{name}_sum_error"] = sum(components) - 1
    # Don't place nested malformed values into numeric CSV columns.
    return {key: value if isinstance(value, (int, float)) and not isinstance(value, bool) else None
            for key, value in result.items()}


def derive(output: DecisionOutput):
    values = output.model_dump(by_alias=True)
    route = normalized([values["route_probabilities"][key] for key in ROUTES])
    freshness = normalized([values["freshness_probabilities"][str(i)] for i in range(6)])
    return {
        **flatten_values(values),
        "requires_web_decision": output.requires_web_probability >= 0.5,
        "is_safe_decision": output.is_safe_probability >= 0.5,
        "derived_route": ROUTES[int(np.argmax(route))],
        "expected_freshness": float(np.dot(np.arange(6), freshness)),
        "route_entropy_bits": entropy(route),
        "freshness_entropy_bits": entropy(freshness),
    }


def is_success(value):
    return str(value).lower() == "true"


def results_frame(rows):
    frame = pd.DataFrame(rows, columns=RAW_COLUMNS)
    for column in ["case_id", "repetition", "attempt", *PROBABILITY_COLUMNS,
                   "expected_freshness", "route_entropy_bits", "freshness_entropy_bits",
                   "route_sum_error", "freshness_sum_error", "latency_ms", "input_tokens", "output_tokens"]:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame["validation_success"] = frame["validation_success"].map(is_success)
    for column in ["requires_web_decision", "is_safe_decision"]:
        frame[column] = frame[column].map(
            lambda value: is_success(value) if str(value).lower() in {"true", "false"} else None)
    return frame


def consistency(values):
    counts = values.dropna().value_counts()
    return float(counts.max() / counts.sum()) if len(counts) else np.nan


def summarize(frame, history=None):
    records = []
    for (model, case_id), group in frame.groupby(["model", "case_id"], sort=False):
        valid = group[group.validation_success]
        record = {"model": model, "case_id": int(case_id),
                  "attempted_repetitions": len(group), "successful_repetitions": len(valid),
                  "validation_failures": len(group) - len(valid)}
        record["cache_verification_failures"] = int((group.failure_kind == "cache_verification").sum())
        record["schema_failures"] = int((group.failure_kind == "schema").sum())
        attempts = group if history is None else history[
            (history.model == model) & (history.case_id == case_id)]
        record["total_attempts"] = len(attempts)
        record["historical_failures"] = int((~attempts.validation_success).sum())
        for column in [*PROBABILITY_COLUMNS, "expected_freshness"]:
            series = valid[column].dropna()
            for name, value in {"mean": series.mean(), "std": series.std(ddof=1),
                                "median": series.median(), "min": series.min(), "max": series.max()}.items():
                record[f"{column}_{name}"] = value
        for column in ["requires_web_decision", "is_safe_decision", "derived_route"]:
            record[f"{column}_consistency"] = consistency(valid[column])
        record["route_consistency"] = record.pop("derived_route_consistency")
        for name in ["route", "freshness"]:
            record[f"{name}_entropy_bits_mean"] = valid[f"{name}_entropy_bits"].mean()
            record[f"{name}_sum_error_mean"] = group[f"{name}_sum_error"].mean()
            record[f"{name}_sum_error_abs_max"] = group[f"{name}_sum_error"].abs().max()
        latency = group.latency_ms.dropna()
        record.update(latency_ms_mean=latency.mean(), latency_ms_median=latency.median(),
                      latency_ms_p95=latency.quantile(0.95))
        for column in ["input_tokens", "output_tokens"]:
            tokens = group[column].dropna()
            record[f"{column}_available_repetitions"] = len(tokens)
            record[f"{column}_total"] = tokens.sum(min_count=1)
            record[f"{column}_mean"] = tokens.mean()
        records.append(record)
    return pd.DataFrame(records)
