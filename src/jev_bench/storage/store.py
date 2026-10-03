"""Suite-parameterized append-only history and atomic latest snapshots."""
import csv
import json
import os
from pathlib import Path
from jev_bench.runtime.execution import POLICY, now, fingerprint
from jev_bench.storage.io import atomic_write
from jev_bench.storage.paths import dataset_context
from jev_bench.storage.verification import revalidate_row

def is_success(value):
    return str(value).lower() == "true"

def write_csv(handle, rows, columns):
    writer = csv.DictWriter(handle, fieldnames=columns)
    writer.writeheader()
    writer.writerows(rows)

class ResultStore:
    """Append-only history is authoritative; raw.csv contains latest repetitions."""

    def __init__(self, directory, definition, suite, merge_definition=None):
        from jev_bench.suites import get_suite
        from jev_bench.storage.paths import ensure_writable
        ensure_writable(directory)
        spec = get_suite(suite)
        self.suite, self.columns, self.key = suite, spec.columns, spec.row_key
        self.validate_values = spec.validate_row
        if definition.get("execution_policy") != POLICY:
            raise ValueError("Incompatible resume: mandatory cache-free metadata required")
        if suite == "hard_case" and definition.get("case_input", {}).get("profile") != "compact":
            raise ValueError("Only compact experiment metadata is supported")
        self.definition = definition
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        manifest = self.directory / "metadata.json"
        if manifest.exists():
            metadata = json.loads(manifest.read_text(encoding="utf-8"))
            previous = metadata.get("experiment")
            if ((suite == "hard_case" and metadata.get("kind") != "hard_case_benchmark") or not isinstance(previous, dict)
                    or metadata.get("fingerprint") != fingerprint(previous)):
                raise ValueError("Invalid hard-case metadata or fingerprint")
            if metadata.get("kind") == "reconciled_evaluation":
                raise ValueError("This is an evaluation-only reconciled dataset; resume in the original source directories")
            if suite == "hard_case" and previous.get("case_input", {}).get("profile") != "compact":
                raise ValueError("Incompatible resume: historical non-compact results cannot resume")
            definition = merge_definition(previous, definition) if merge_definition else definition
            if merge_definition is None and previous != definition:
                raise ValueError("Incompatible resume metadata; use another --output-dir")
            changed = metadata["fingerprint"] != fingerprint(definition)
        else:
            if any((self.directory / name).exists() for name in ["raw.csv", "attempt_history.csv"]):
                raise ValueError("Existing results have no metadata; use another --output-dir")
            metadata = {"created_at_utc": now()}
            if suite == "hard_case":
                metadata["kind"] = "hard_case_benchmark"
            changed = True

        self.history = []
        self.latest = {}
        self.history_path = self.directory / "attempt_history.csv"
        if self.history_path.exists():
            with self.history_path.open(newline="", encoding="utf-8") as handle:
                reader = csv.DictReader(handle)
                if reader.fieldnames != self.columns:
                    raise ValueError("Incompatible attempt history CSV columns")
                for row in reader:
                    if None in row or any(value is None for value in row.values()):
                        raise ValueError("Incomplete attempt history row; inspect CSV before resuming")
                    key = self.key(row)
                    prior = self.latest.get(key)
                    if (int(row["attempt"]) != (int(prior["attempt"]) + 1 if prior else 1)
                            or (prior and is_success(prior["validation_success"]))):
                        raise ValueError("Invalid attempt sequence in history")
                    if row["validation_success"].lower() not in {"true", "false"}:
                        raise ValueError("Invalid validation status in history")
                    if is_success(row["validation_success"]):
                        if row["error"]:
                            raise ValueError("Successful history row contains an error")
                        self.validate_values(row)
                    if is_success(row["validation_success"]):
                        self.revalidate(row)
                    self.history.append(row)
                    self.latest[key] = row
        elif (self.directory / "raw.csv").exists():
            raise ValueError("Attempt history is missing; refusing to overwrite existing raw.csv")

        if changed:
            metadata.update(fingerprint=fingerprint(definition), experiment=definition)
            atomic_write(manifest, lambda handle: json.dump(metadata, handle, indent=2, ensure_ascii=False))
        if not self.history_path.exists():
            atomic_write(self.history_path, lambda handle: write_csv(handle, [], self.columns))
        self.save_raw()

    def save_raw(self):
        atomic_write(self.directory / "raw.csv", lambda handle: write_csv(handle, self.latest.values(), self.columns))

    def pending(self, *key):
        row = self.latest.get(tuple(key))
        return row is None or not is_success(row["validation_success"])

    def record(self, row):
        key = self.key(row)
        prior = self.latest.get(key)
        if prior and is_success(prior["validation_success"]):
            raise ValueError("Cannot duplicate a completed repetition")
        if is_success(row["validation_success"]):
            self.revalidate(row)
        row = {column: row.get(column) for column in self.columns}
        row["attempt"] = int(prior["attempt"]) + 1 if prior else 1
        needs_header = not self.history_path.exists() or self.history_path.stat().st_size == 0
        with self.history_path.open("a", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=self.columns)
            if needs_header:
                writer.writeheader()
            writer.writerow(row)
            handle.flush()
            os.fsync(handle.fileno())
        self.latest[key] = row
        self.history.append(row)
        self.save_raw()


    def revalidate(self, row):
        with dataset_context(self.directory):
            return revalidate_row(row, self.definition)
