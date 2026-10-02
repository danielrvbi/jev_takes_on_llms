import json
from dataclasses import dataclass, field
from typing import Any, Protocol

from hard_case.prompts import SYSTEM_PROMPT
from hard_case.schemas import HardCaseOutput


@dataclass
class ProviderResult:
    output: HardCaseOutput | None = None
    values: dict[str, Any] = field(default_factory=dict)
    raw_response: Any = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    error: str = ""


class Provider(Protocol):
    def invoke(self, packet: str) -> ProviderResult: ...


def validate_result(values, raw_response, input_tokens=None, output_tokens=None, error=""):
    result = ProviderResult(values=values, raw_response=raw_response,
                            input_tokens=input_tokens, output_tokens=output_tokens, error=error)
    if not error:
        try:
            result.output = HardCaseOutput.model_validate(values)
        except Exception as exc:
            result.error = f"{type(exc).__name__}: {exc}"
    return result


def token_usage(raw):
    usage = getattr(raw, "usage_metadata", None) or {}
    metadata = getattr(raw, "response_metadata", None) or {}
    fallback = metadata.get("token_usage") or metadata.get("usage") or {}
    inputs = usage.get("input_tokens")
    outputs = usage.get("output_tokens")
    if inputs is None:
        inputs = fallback.get("prompt_tokens", metadata.get("prompt_eval_count"))
    if outputs is None:
        outputs = fallback.get("completion_tokens", metadata.get("eval_count"))
    return inputs, outputs


def raw_values(raw):
    """Audit malformed output without repairing it or accepting parsing failures."""
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


class StructuredChatProvider:
    def __init__(self, llm):
        self.structured_llm = llm.with_structured_output(
            HardCaseOutput, method="json_schema", include_raw=True,
        )

    def invoke(self, packet):
        return structured_result(self.structured_llm.invoke([
            ("system", SYSTEM_PROMPT), ("human", packet),
        ]))
