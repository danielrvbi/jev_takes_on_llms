
from jev_bench.suites.benchmark.schemas import DecisionOutput


from jev_bench.providers.base import ProviderResult, Provider


def validate_result(values, raw_response, input_tokens=None, output_tokens=None, error=""):
    result = ProviderResult(values=values, raw_response=raw_response,
                            input_tokens=input_tokens, output_tokens=output_tokens, error=error)
    if not error:
        try:
            result.output = DecisionOutput.model_validate(values)
        except Exception as exc:
            result.error = f"{type(exc).__name__}: {exc}"
    return result


from jev_bench.providers.parsing import token_usage, raw_values


def structured_result(response):
    raw = response.get("raw")
    parsed = response.get("parsed")
    error = response.get("parsing_error")
    values = parsed.model_dump(by_alias=True) if isinstance(parsed, DecisionOutput) else raw_values(raw)
    if isinstance(parsed, dict):
        values = parsed
    if error is not None:
        error = f"{type(error).__name__}: {error}"
    elif parsed is None:
        error = "Structured output contained no parsed decision"
    inputs, outputs = token_usage(raw)
    raw_data = raw.model_dump(mode="json") if hasattr(raw, "model_dump") else raw
    return validate_result(values, raw_data, inputs, outputs, error or "")


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
