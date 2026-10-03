"""Reconcile completed suite/Mistral runs into an evaluation-only dataset.

Suite is authoritative for Tev1 and Gemma; the separate Mistral directory is
authoritative for both Mistral models. Sources and independent repetition IDs
are never overwritten, renumbered, or treated as retries across directories.
"""

import argparse
import csv
import hashlib
import json
import math
import os
import shutil
import sys
import tempfile
from contextlib import ExitStack
from pathlib import Path

from rich.console import Console
from rich.table import Table

from benchmark.metrics import (
    FRESHNESS_COLUMNS, PROBABILITY_COLUMNS, RAW_COLUMNS, ROUTE_COLUMNS,
    derive, results_frame, summarize,
)
from benchmark.plots import plot_results
from benchmark.runner import row_key, utc_now, write_csv
from benchmark.schemas import ROUTES, DecisionOutput
from show_evaluations import build_report, saved_snapshot


SUITE_MODELS = ("tev1:0.8b", "tev1:4b", "gemma4:e4b")
MISTRAL_MODELS = ("mistral-small-latest", "mistral-large-latest")
SOURCE_FILES = ("raw.csv", "attempt_history.csv", "metadata.json")


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def definition_fingerprint(definition):
    return hashlib.sha256(json.dumps(definition, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def read_rows(path, definition):
    """Validate the CSV while retaining the exact string value of every cell."""
    cases = {case["case_id"]: case["message"] for case in definition["cases"]}
    models = definition["model_configuration"]
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != RAW_COLUMNS:
            raise ValueError(f"Unexpected benchmark CSV columns: {path}")
        rows = list(reader)
    numeric = [*PROBABILITY_COLUMNS, "route_sum_error", "freshness_sum_error", "expected_freshness",
               "route_entropy_bits", "freshness_entropy_bits", "latency_ms", "input_tokens", "output_tokens"]
    for row in rows:
        if None in row or any(value is None for value in row.values()):
            raise ValueError(f"Incomplete CSV row in {path}")
        key = row_key(row)
        if row["model"] not in models or cases.get(key[1]) != row["message"]:
            raise ValueError(f"Message/model does not match source metadata: {path}, {key}")
        if min(key[1], key[2], int(row["attempt"])) < 1:
            raise ValueError(f"Invalid repetition identifiers: {path}, {key}")
        if row["validation_success"] not in {"True", "False"}:
            raise ValueError(f"Invalid validation_success: {path}, {key}")
        for column in numeric:
            if row[column] != "":
                float(row[column])
        if not math.isfinite(float(row["latency_ms"])) or float(row["latency_ms"]) < 0:
            raise ValueError(f"Invalid latency: {path}, {key}")
        for column in ["input_tokens", "output_tokens"]:
            if row[column] and int(row[column]) < 0:
                raise ValueError(f"Invalid token usage: {path}, {key}")
        json.loads(row["raw_response_json"])
        if row["validation_success"] == "True":
            if row["error"]:
                raise ValueError(f"Successful row has an error: {path}, {key}")
            output = DecisionOutput.model_validate({
                "requires_web_probability": float(row["requires_web_probability"]),
                "is_safe_probability": float(row["is_safe_probability"]),
                "route_probabilities": {route: float(row[column]) for route, column in zip(ROUTES, ROUTE_COLUMNS)},
                "freshness_probabilities": {str(level): float(row[column]) for level, column in enumerate(FRESHNESS_COLUMNS)},
            })
            for column, expected in derive(output).items():
                actual = row[column]
                if isinstance(expected, bool):
                    matches = actual == str(expected)
                elif isinstance(expected, str):
                    matches = actual == expected
                else:
                    matches = actual != "" and math.isclose(float(actual), expected, rel_tol=1e-9, abs_tol=1e-10)
                if not matches:
                    raise ValueError(f"Inconsistent stored decision {column}: {path}, {key}")
        else:
            if not row["error"]:
                raise ValueError(f"Failed row lacks its error: {path}, {key}")
            if any(row[column] for column in ["requires_web_decision", "is_safe_decision", "derived_route",
                                              "expected_freshness", "route_entropy_bits", "freshness_entropy_bits"]):
                raise ValueError(f"Failed row has derived decisions: {path}, {key}")
    return rows


def load_source(directory):
    if any("contaminated" in part.lower() for part in Path(directory).resolve().parts):
        raise ValueError("Quarantined results cannot be imported")
    metadata = json.loads((directory / "metadata.json").read_text(encoding="utf-8"))
    if metadata.get("kind") == "reconciled_evaluation":
        raise ValueError(f"Select an original benchmark source, not a reconciled directory: {directory}")
    definition = metadata["experiment"]
    if metadata.get("fingerprint") != definition_fingerprint(definition):
        raise ValueError(f"Source metadata fingerprint does not match its experiment: {directory}")
    raw = read_rows(directory / "raw.csv", definition)
    history = read_rows(directory / "attempt_history.csv", definition)
    latest = {}
    for row in raw:
        key = row_key(row)
        if key in latest:
            raise ValueError(f"Duplicate repetition key in {directory}/raw.csv: {key}")
        latest[key] = row
    historical_latest = {}
    for row in history:
        key = row_key(row)
        previous = historical_latest.get(key)
        if int(row["attempt"]) != (int(previous["attempt"]) + 1 if previous else 1):
            raise ValueError(f"Invalid attempt sequence in {directory}: {key}")
        if previous and previous["validation_success"] == "True":
            raise ValueError(f"Completed repetition was attempted again in {directory}: {key}")
        historical_latest[key] = row
    if latest != historical_latest:
        raise ValueError(f"raw.csv and attempt_history.csv disagree in {directory}; resume the original run to recover it")
    return {"directory": directory, "metadata": metadata, "raw": raw, "history": history,
            "file_sha256": {name: sha256_file(directory / name) for name in SOURCE_FILES}}


def merged_definition(suite, mistral):
    original = suite["metadata"]["experiment"]
    separate = mistral["metadata"]["experiment"]
    if ({key: value for key, value in original.items() if key != "model_configuration"}
            != {key: value for key, value in separate.items() if key != "model_configuration"}):
        raise ValueError("Source methodology or environment metadata differs; refusing to reconcile")
    configurations = {}
    for model in (*SUITE_MODELS, *MISTRAL_MODELS):
        first = original["model_configuration"].get(model)
        second = separate["model_configuration"].get(model)
        if first is None or first != second:
            raise ValueError(f"Source model configurations differ or are missing: {model}")
        configurations[model] = first
    return {**original, "model_configuration": configurations}


def select_sources(suite, mistral):
    raw, history, provenance, assignments = [], [], [], {}
    for source, selected_models in [(suite, SUITE_MODELS), (mistral, MISTRAL_MODELS)]:
        directory = source["directory"]
        missing = set(selected_models) - {row["model"] for row in source["raw"]}
        if missing:
            raise ValueError(f"Required models have no measurements in {directory}: {', '.join(sorted(missing))}")
        selected_raw = [row for row in source["raw"] if row["model"] in selected_models]
        selected_history = [row for row in source["history"] if row["model"] in selected_models]
        excluded = [row for row in source["raw"] if row["model"] not in selected_models]
        raw.extend(selected_raw)
        history.extend(selected_history)
        assignments.update({model: str(directory) for model in selected_models})
        provenance.append({
            "path": str(directory), "file_sha256": source["file_sha256"],
            "original_metadata": source["metadata"], "selected_models": list(selected_models),
            "selected_latest_rows": len(selected_raw), "selected_history_attempts": len(selected_history),
            "excluded_latest_rows": len(excluded),
            "excluded_history_attempts": len(source["history"]) - len(selected_history),
            "excluded_measurements": [
                {key: row[key] for key in ["model", "case_id", "repetition", "attempt", "timestamp_utc"]}
                for row in excluded
            ],
            "exclusion_reason": "Only this source's assigned models are included; other rows remain in the original directory.",
        })
    if len({row_key(row) for row in raw}) != len(raw):
        raise ValueError("Selected sources contain duplicate repetition keys")
    return raw, history, provenance, assignments


def ensure_destination(output, sources):
    for source in sources:
        if output.is_relative_to(source) or source.is_relative_to(output):
            raise ValueError("Output must be separate from, not inside or above, either source directory")
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise ValueError(f"Output directory is not empty: {output}; choose a new --output-dir")


def reconcile(suite_dir, mistral_dir, output_dir):
    suite_dir, mistral_dir, output = (Path(path).resolve() for path in [suite_dir, mistral_dir, output_dir])
    if suite_dir == mistral_dir:
        raise ValueError("Select two distinct original result directories")
    ensure_destination(output, [suite_dir, mistral_dir])
    with ExitStack() as stack:
        for directory in sorted([suite_dir, mistral_dir]):
            stack.enter_context(saved_snapshot(directory))
        suite, mistral = load_source(suite_dir), load_source(mistral_dir)
        definition = merged_definition(suite, mistral)
        raw, history, sources, assignments = select_sources(suite, mistral)
        frame, historical_frame = results_frame(raw), results_frame(history)
        summary = summarize(frame, historical_frame)
        metadata = {
            "kind": "reconciled_evaluation", "created_at_utc": utc_now(),
            "fingerprint": definition_fingerprint(definition), "experiment": definition,
            "reconciliation": {
                "version": 1,
                "policy": "Suite supplies Tev1/Gemma; separate Mistral supplies both Mistral models. No smoke rows, renumbering, or cross-run retries.",
                "model_sources": assignments, "sources": sources,
                "latest_rows": len(raw), "validation_failures": int((~frame.validation_success).sum()),
                "history_attempts": len(history), "historical_failures": int((~historical_frame.validation_success).sum()),
            },
        }
        output.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=f".{output.name}.", dir=output.parent))
        try:
            for name, rows in [("raw.csv", raw), ("attempt_history.csv", history)]:
                with (staging / name).open("w", encoding="utf-8", newline="") as handle:
                    write_csv(handle, rows)
            (staging / "metadata.json").write_text(json.dumps(metadata, indent=2, ensure_ascii=False), encoding="utf-8")
            summary.to_csv(staging / "summary.csv", index=False)
            plot_results(frame, summary, staging)
            report = build_report(output, frame, historical_frame, metadata, plot_directory=staging / "plots")
            (staging / "evaluation.md").write_text(report, encoding="utf-8")
            # Check staged cell values and histories, not just counts, before publishing.
            staged = load_source_for_verification(staging, definition)
            if staged != (raw, history):
                raise ValueError("Staged CSV values differ from selected source values")
            for source in [suite, mistral]:
                for name, fingerprint in source["file_sha256"].items():
                    if sha256_file(source["directory"] / name) != fingerprint:
                        raise ValueError(f"Source changed during reconciliation: {source['directory'] / name}")
            ensure_destination(output, [suite_dir, mistral_dir])
            # POSIX rename atomically publishes the directory and cannot replace a nonempty destination.
            os.rename(staging, output)
        finally:
            if staging.exists():
                shutil.rmtree(staging)
    return metadata, summary


def load_source_for_verification(directory, definition):
    return (read_rows(directory / "raw.csv", definition),
            read_rows(directory / "attempt_history.csv", definition))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite-dir", type=Path, required=True)
    parser.add_argument("--mistral-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    console = Console()
    try:
        metadata, summary = reconcile(args.suite_dir, args.mistral_dir, args.output_dir)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        console.print(f"Reconciliation error: {exc}", markup=False)
        return 1
    table = Table(title="Reconciled evaluation (sources preserved)")
    for column in ["Model", "Latest rows", "Valid", "Failed", "Attempts"]:
        table.add_column(column, justify="left" if column == "Model" else "right")
    for model, group in summary.groupby("model", sort=False):
        table.add_row(model, str(int(group.attempted_repetitions.sum())),
                      str(int(group.successful_repetitions.sum())), str(int(group.validation_failures.sum())),
                      str(int(group.total_attempts.sum())))
    console.print(table)
    console.print(f"{metadata['reconciliation']['latest_rows']} latest rows; "
                  f"{metadata['reconciliation']['validation_failures']} latest failures; "
                  f"{metadata['reconciliation']['historical_failures']} historical failures.")
    console.print(f"Evaluation-only output: {args.output_dir.resolve()}", markup=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
