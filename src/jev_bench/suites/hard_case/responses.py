from jev_bench.suites.hard_case.schemas import PROBABILITY_FIELDS

from jev_bench.suites.hard_case.schemas import HardCaseOutput


from jev_bench.providers.base import ProviderResult, Provider


def validate_result(values, raw_response, input_tokens=None, output_tokens=None, error=""):
    result = ProviderResult(values=values, raw_response=raw_response,
                            input_tokens=input_tokens, output_tokens=output_tokens, error=error)
    if not error:
        try:
            result.output = HardCaseOutput.model_validate(values)
        except Exception as exc:
            result.error = f"{type(exc).__name__}: {exc}"
    return result


from jev_bench.providers.parsing import token_usage, raw_values


def structured_result(response):
    raw = response.get("raw")
    parsed = response.get("parsed")
    error = response.get("parsing_error")
    if isinstance(parsed, HardCaseOutput):
        values = parsed.model_dump()
    elif isinstance(parsed, dict):
        values = parsed
    else:
        values = raw_values(raw)
    if error is not None:
        error = f"{type(error).__name__}: {error}"
    elif parsed is None:
        error = "Structured output contained no parsed hard-case judgment"
    inputs, outputs = token_usage(raw)
    raw_data = raw.model_dump(mode="json") if hasattr(raw, "model_dump") else raw
    return validate_result(values, raw_data, inputs, outputs, error or "")


def exception_result(exc):
    """Retain HTTP rejection evidence when no normal provider response is returned."""
    raw = {"exception_type": type(exc).__name__, "message": str(exc)}
    status = getattr(exc, "status_code", None)
    response = getattr(exc, "response", None)
    if response is not None:
        status = status or getattr(response, "status_code", None)
        try:
            raw["body"] = response.json()
        except (ValueError, AttributeError):
            raw["body"] = getattr(response, "text", None)
    if status is not None:
        raw["status_code"] = status
    if getattr(exc, "error", None) is not None:
        raw["body"] = exc.error
    return ProviderResult(raw_response=raw, error=f"{type(exc).__name__}: {exc}")


def map_response(response):
    raw = response.model_dump(mode="json") if hasattr(response, "model_dump") else response
    usage = raw.get("usage") if isinstance(raw, dict) else None
    usage = usage if isinstance(usage, dict) else {}
    answers = raw.get("answers") if isinstance(raw, dict) else None
    values = {}
    if isinstance(answers, dict):
        for name in PROBABILITY_FIELDS:
            answer = answers.get(name)
            if isinstance(answer, dict) and "noul" in answer:
                values[name] = answer["noul"]
    error = "Invalid System One response: answers must be an object" if not isinstance(answers, dict) else ""
    return validate_result(values, raw, usage.get("input_tokens"), usage.get("output_tokens"), error)
