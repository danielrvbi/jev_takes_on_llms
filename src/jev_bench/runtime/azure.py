"""Azure HTTP evidence verification, independent of LangChain and credentials."""
import re

from jev_bench.run.control import PREFIX_STRATEGY
from jev_bench.runtime.execution import fingerprint

AZURE_POLICY = {
    "version": 3, "mode": "mandatory_cache_free", "provider_scope": "azure-prefix",
    "hosted_verification": "unique system prefix AND explicit numeric zero cache-read tokens",
    "langchain_cache": False, "automatic_request_retries": 0,
    "latency": "hosted wall time including fresh client creation, inference, audit and teardown",
}


def text_content(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list) and all(isinstance(b, dict) and b.get("type") == "text" for b in content):
        return "".join(b["text"] for b in content)
    raise ValueError("Expected only text content")


def verify_request(audit):
    payload = audit["input"]
    config = payload["configuration"]
    if audit["model"] != config["inference_model"] or audit["provider"] != config["provider"]:
        raise ValueError("Azure alias differs from configuration")
    system = payload["messages"][0][1]
    if payload.get("prefix_strategy") != PREFIX_STRATEGY or not re.fullmatch(
            r"Request identifier: [0-9a-f]{32}\n\n" + re.escape(payload["original_system_prompt"]), system):
        raise ValueError("Invalid Azure unique prefix")
    if len(audit.get("requests", [])) != 1:
        raise ValueError("Exactly one Azure HTTP request is required; retries are forbidden")
    sent = audit["requests"][0]
    if sent.get("model") != config["deployment"]:
        raise ValueError("Azure request deployment differs from configuration")
    expected_messages = [("system", system), ("user", payload["messages"][1][1])]
    observed = [(m.get("role"), text_content(m.get("content"))) for m in sent.get("messages", [])]
    if audit["provider"] == "azure-claude":
        observed.insert(0, ("system", text_content(sent.get("system"))))
        observed_format = sent.get("output_config", {}).get("format")
        limit = sent.get("max_tokens")
    else:
        observed_format = sent.get("response_format")
        limit = sent.get("max_completion_tokens", sent.get("max_tokens"))
    if observed != expected_messages:
        raise ValueError("Azure request changed messages or included previous answers")
    if observed_format != payload["wire_format"]:
        raise ValueError("Azure request changed the structured output schema")
    if sent.get("temperature") != config["temperature"] or limit != config["max_output_tokens"]:
        raise ValueError("Azure request changed sampling settings or output limit")
    if sent.get("reasoning_effort") != config["reasoning_effort"]:
        raise ValueError("Azure request changed reasoning effort")
    if any(sent.get(k) for k in ("tools", "stream", "previous_response_id", "thinking", "cache_control")):
        raise ValueError("Unexpected Azure conversation, tool, streaming or cache settings")


def usage(audit):
    wire = audit["http_response"]["body"]
    counts = wire.get("usage") or {}
    if audit["provider"] == "azure-gpt":
        cached = counts.get("prompt_tokens_details", {}).get("cached_tokens")
        inputs, outputs = counts.get("prompt_tokens"), counts.get("completion_tokens")
        created = None
    else:
        cached = counts.get("cache_read_input_tokens")
        created = counts.get("cache_creation_input_tokens")
        inputs, outputs = counts.get("input_tokens"), counts.get("output_tokens")
        if type(inputs) is int:
            inputs += created if type(created) is int else 0
            inputs += cached if type(cached) is int else 0
    return {"input_tokens": inputs, "output_tokens": outputs,
            "cached_tokens": cached, "cache_creation_input_tokens": created}


def verify_response(audit):
    if audit.get("http_response", {}).get("status_code") != 200:
        raise ValueError("Azure inference did not return HTTP 200")
    counts = usage(audit)
    cached = counts["cached_tokens"]
    if type(cached) not in (int, float) or cached != 0:
        raise ValueError(f"Azure cache-read telemetry must be explicitly numeric zero; received {cached!r}")
    for field in ("input_tokens", "output_tokens"):
        if type(counts[field]) is not int or counts[field] < 0:
            raise ValueError("Azure token usage is missing or invalid")
    created = counts["cache_creation_input_tokens"]
    if created is not None and (type(created) is not int or created < 0):
        raise ValueError("Invalid Claude cache-creation telemetry")
    wire, raw = audit["http_response"]["body"], audit["response"]
    if audit["provider"] == "azure-gpt":
        choices = wire.get("choices") or []
        if len(choices) != 1 or choices[0].get("finish_reason") != "stop":
            raise ValueError("Azure GPT output was incomplete or rejected")
        expected_content = choices[0]["message"].get("content")
    else:
        if wire.get("stop_reason") != "end_turn":
            raise ValueError("Azure Claude output was incomplete or rejected")
        expected_content = wire.get("content")
    if text_content(expected_content) != text_content(raw.get("content")):
        raise ValueError("LangChain output differs from the Azure HTTP response")
    return counts


def revalidate(audit):
    if audit.get("policy") != AZURE_POLICY:
        raise ValueError("Invalid Azure execution policy")
    if audit.get("requests"):
        verify_request(audit)
    if not audit.get("verified"):
        return
    if audit.get("langchain_cache") is not False:
        raise ValueError("Azure LangChain cache must be disabled")
    counts = verify_response(audit)
    if audit.get("usage") != counts:
        raise ValueError("Azure usage differs from HTTP evidence")
    for name in ("runtime", "model_identity", "http_response"):
        if fingerprint(audit[name]) != audit.get({"model_identity": "model_sha256"}.get(name, name + "_sha256")):
            raise ValueError("Azure evidence fingerprint mismatch: " + name)
    config = audit["input"]["configuration"]
    if audit["runtime"] != {"provider": audit["provider"], "base_url": config["base_url"], "policy": AZURE_POLICY}:
        raise ValueError("Azure runtime differs from configuration")
    identity = {k: config[k] for k in ("deployment", "model_name", "model_version")}
    identity.update(resolved_model=audit["http_response"]["body"].get("model"),
                    response_id=audit["http_response"]["body"].get("id"))
    if audit["model_identity"] != identity:
        raise ValueError("Azure model identity differs from response/configuration")
