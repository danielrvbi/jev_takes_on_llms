import json
from dataclasses import dataclass, field
from typing import Any, Protocol

from benchmark.prompts import SYSTEM_PROMPT
from benchmark.schemas import DecisionOutput


@dataclass
class ProviderResult:
    output: DecisionOutput | None = None
    values: dict[str, Any] = field(default_factory=dict)
    raw_response: Any = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    error: str = ""


class Provider(Protocol):
    def invoke(self, message: str) -> ProviderResult: ...


def validate_result(values, raw_response, input_tokens=None, output_tokens=None, error=""):
    result = ProviderResult(values=values, raw_response=raw_response,
                            input_tokens=input_tokens, output_tokens=output_tokens, error=error)
    if not error:
        try:
            result.output = DecisionOutput.model_validate(values)
        except Exception as exc:
            result.error = f"{type(exc).__name__}: {exc}"
    return result


def token_usage(raw):
    usage = getattr(raw, "usage_metadata", None) or {}
    metadata = getattr(raw, "response_metadata", None) or {}
    fallback = metadata.get("token_usage") or metadata.get("usage") or {}
    input_tokens = usage.get("input_tokens")
    output_tokens = usage.get("output_tokens")
    if input_tokens is None:
        input_tokens = fallback.get("prompt_tokens", metadata.get("prompt_eval_count"))
    if output_tokens is None:
        output_tokens = fallback.get("completion_tokens", metadata.get("eval_count"))
    return input_tokens, output_tokens


def raw_values(raw):
    """Recover JSON fields for auditing only; never repair or accept parsing failures."""
    if raw is None:
        return {}
    calls = getattr(raw, "tool_calls", None) or []
    candidate = calls[0].get("args") if calls else getattr(raw, "content", None)
    if isinstance(candidate, dict):
        return candidate
    try:
        value = json.loads(candidate) if isinstance(candidate, str) else None
        return value if isinstance(value, dict) else {}
    except (ValueError, TypeError):
        return {}


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
