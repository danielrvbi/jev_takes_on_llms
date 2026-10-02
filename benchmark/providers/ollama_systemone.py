import ollama

from benchmark.prompts import systemone_questions
from .base import ProviderResult, validate_result


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
    def __init__(self, model):
        self.model = model

    def invoke(self, message):
        return map_response(ollama.systemone(
            model=self.model, state=message, questions=systemone_questions(),
        ))
