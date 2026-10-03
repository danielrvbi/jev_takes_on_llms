from benchmark.prompts import systemone_questions
from .base import ProviderResult, validate_result
from execution import AuditError, systemone_call


def map_response(response):
    raw = response.model_dump(mode="json") if hasattr(response, "model_dump") else response
    try:
        answers = raw["answers"]
        values = {
            "requires_web_probability": answers["requires_web"]["noul"],
            "is_safe_probability": answers["is_safe"]["noul"],
            "route_probabilities": answers["route"]["probabilities"],
            "freshness_probabilities": answers["freshness"]["probabilities"],
        }
        usage = raw.get("usage") or {}
        return validate_result(values, raw, usage.get("input_tokens"), usage.get("output_tokens"))
    except (KeyError, TypeError) as exc:
        usage = (raw.get("usage") or {}) if isinstance(raw, dict) else {}
        return ProviderResult(raw_response=raw, input_tokens=usage.get("input_tokens"),
                              output_tokens=usage.get("output_tokens"),
                              error=f"Invalid System One response: {exc}")


class SystemOneProvider:
    def __init__(self, model, *, audit_directory=None):
        self.model = model
        if audit_directory is None:
            raise ValueError("An execution audit directory is mandatory")
        self.audit_directory = audit_directory
        self.purpose = "measurement"

    def invoke(self, message):
        try:
            return map_response(systemone_call(
                model=self.model, state=message, questions=systemone_questions(),
                directory=self.audit_directory, purpose=self.purpose))
        except AuditError as exc:
            import json
            from pathlib import Path
            raw = json.loads(Path(exc.audit['audit_path']).read_text()).get("response")
            result = map_response(raw) if isinstance(raw, dict) else ProviderResult()
            result.raw_response = {**(raw or {}), "execution_audit": exc.audit}
            result.error = f"Cache verification failed: {exc}"
            return result
