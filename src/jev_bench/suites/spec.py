"""Suite behavior supplied to shared execution and storage."""
from dataclasses import dataclass
from importlib import import_module
from jev_bench.providers.base import Request


@dataclass(frozen=True)
class Suite:
    name: str

    @property
    def metrics(self):
        return import_module(f"jev_bench.suites.{self.name}.metrics")

    @property
    def columns(self):
        return self.metrics.RAW_COLUMNS

    @property
    def schema(self):
        module = import_module(f"jev_bench.suites.{self.name}.schemas")
        return module.DecisionOutput if self.name == "benchmark" else module.HardCaseOutput

    def request(self, state):
        prompts = import_module(f"jev_bench.suites.{self.name}.prompts")
        return Request(state, prompts.systemone_questions(), self.schema, prompts.SYSTEM_PROMPT)

    def row_key(self, row):
        key = (row["model"], int(row["repetition"]))
        if not key[0] or key[1] < 1:
            raise ValueError("Invalid repetition key in history")
        return (key[0], int(row["case_id"]), key[1]) if self.name == "benchmark" else key

    def validate_row(self, row):
        if self.name == "hard_case":
            return self.schema.model_validate({name: float(row[name]) for name in self.schema.model_fields})
        return self.schema.model_validate({
            "requires_web_probability": float(row["requires_web_probability"]),
            "is_safe_probability": float(row["is_safe_probability"]),
            "route_probabilities": {name.removeprefix("route_").removesuffix("_probability"): float(row[name])
                                    for name in self.metrics.ROUTE_COLUMNS},
            "freshness_probabilities": {str(i): float(row[f"freshness_{i}_probability"]) for i in range(6)}})

    def parse_native(self, raw):
        return import_module(f"jev_bench.suites.{self.name}.responses").map_response(raw)
