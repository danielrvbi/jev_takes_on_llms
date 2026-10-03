"""Extract raw structured fields and usage without repairing malformed responses."""
import json

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
    calls = (raw.get("tool_calls") if isinstance(raw, dict) else getattr(raw, "tool_calls", None)) or []
    candidate = calls[0].get("args") if calls else (raw.get("content") if isinstance(raw, dict) else getattr(raw, "content", None))
    if isinstance(candidate, list):
        texts = [block.get("text", "") for block in candidate
                 if isinstance(block, dict) and block.get("type") == "text"]
        candidate = "".join(texts) if texts else None
    if isinstance(candidate, dict):
        return candidate
    try:
        value = json.loads(candidate) if isinstance(candidate, str) else None
        return value if isinstance(value, dict) else {}
    except (ValueError, TypeError):
        return {}
