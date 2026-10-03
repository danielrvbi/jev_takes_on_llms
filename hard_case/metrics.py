from execution import AUDIT_COLUMNS
import math

import pandas as pd

from hard_case.schemas import PROBABILITY_FIELDS


RAW_COLUMNS = [
    "model", "repetition", "attempt", "timestamp_utc",
    *PROBABILITY_FIELDS,
    "latency_ms", "input_tokens", "output_tokens", "validation_success", "error", "raw_response_json", *AUDIT_COLUMNS,
]


def is_success(value):
    return str(value).lower() == "true"


def flatten_values(values):
    return {
        name: value if isinstance(value, (int, float)) and not isinstance(value, bool)
                       and math.isfinite(value) else None
        for name in PROBABILITY_FIELDS
        for value in [values.get(name)]
    }


def results_frame(rows):
    frame = pd.DataFrame(rows, columns=RAW_COLUMNS)
    for column in ["repetition", "attempt", *PROBABILITY_FIELDS,
                   "latency_ms", "input_tokens", "output_tokens"]:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame["validation_success"] = frame["validation_success"].map(is_success)
    return frame


def summarize(frame, history=None):
    records = []
    for model, group in frame.groupby("model", sort=False):
        valid = group[group.validation_success]
        attempts = group if history is None else history[history.model == model]
        record = {
            "model": model,
            "attempted_repetitions": len(group),
            "successful_repetitions": len(valid),
            "validation_failures": len(group) - len(valid),
            "total_attempts": len(attempts),
            "historical_failures": int((~attempts.validation_success).sum()),
        }
        record["cache_verification_failures"] = int((group.failure_kind == "cache_verification").sum())
        record["schema_failures"] = int((group.failure_kind == "schema").sum())
        for name in PROBABILITY_FIELDS:
            series = valid[name].dropna()
            record.update({
                f"{name}_mean": series.mean(),
                f"{name}_std": series.std(ddof=1),
                f"{name}_min": series.min(),
                f"{name}_max": series.max(),
            })
        latency = group.latency_ms.dropna()
        record["latency_ms_p50"] = latency.quantile(0.50)
        record["latency_ms_p95"] = latency.quantile(0.95)
        for name in ["input_tokens", "output_tokens"]:
            available = group[name].dropna()
            record[f"{name}_mean"] = available.mean()
            record[f"{name}_available_repetitions"] = len(available)
        records.append(record)
    return pd.DataFrame(records)
