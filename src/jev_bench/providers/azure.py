"""Fresh LangChain clients for Azure GPT and Foundry Claude, with HTTP audits."""
from functools import cached_property
from importlib import import_module
import asyncio
import json
import time
import uuid

import httpx

from jev_bench.providers.azure_config import api_key, configuration
from jev_bench.providers.base import ProviderResult
from jev_bench.run.control import PREFIX_STRATEGY
from jev_bench.runtime.azure import AZURE_POLICY, verify_request, verify_response, usage
from jev_bench.runtime.execution import (disable_client_cache, fingerprint, new_audit,
                                         raw_response, save_audit, serial)


def create_chat(config, key, client, async_client=None):
    """Keep provider SDK details isolated and pin the tested LangChain versions."""
    settings = dict(model=config["deployment"], api_key=key, base_url=config["base_url"],
                    temperature=config["temperature"], max_tokens=config["max_output_tokens"],
                    timeout=config["timeout_seconds"], max_retries=0, cache=False)
    if config["provider"] == "azure-gpt":
        from langchain_openai import ChatOpenAI
        return ChatOpenAI(**settings, http_client=client, http_async_client=async_client, use_responses_api=False,
                          reasoning_effort=config["reasoning_effort"])
    import anthropic
    from langchain_anthropic import ChatAnthropic

    # ChatAnthropic's default HTTP clients are shared globally. Override its lazy
    # SDK client to use this invocation's owned, audited HTTP client instead.
    class FreshChatAnthropic(ChatAnthropic):
        @cached_property
        def _client(self):
            return anthropic.Anthropic(**self._client_params, http_client=client)

    return FreshChatAnthropic(**settings)


def wire_format(model, schema):
    if model == "azure-gpt":
        from langchain_core.utils.function_calling import convert_to_openai_tool
        function = convert_to_openai_tool(schema.model_json_schema(), strict=True)["function"]
        return {"type": "json_schema", "json_schema": {
            "name": function["name"], "description": function["description"],
            "schema": function["parameters"], "strict": True}}
    from anthropic import transform_schema
    return {"type": "json_schema", "schema": transform_schema(schema)}


class AzureProvider:
    supports_requests = True

    def __init__(self, model, *, suite="benchmark", audit_directory=None, transport=None):
        if audit_directory is None:
            raise ValueError("An execution audit directory is mandatory")
        self.model, self.suite, self.audit_directory = model, suite, audit_directory
        self.config, self.key = configuration(model), api_key(model)
        self.transport = transport  # MockTransport injection for offline verification.
        self.prefix_experiment = True

    def invoke(self, request, context):
        if not context.prefix_experiment:
            raise ValueError("Azure providers require the separate prefix experiment")
        started = time.perf_counter()
        system = f"Request identifier: {uuid.uuid4().hex}\n\n" + request.system_prompt
        messages = [("system", system), ("human", request.state)]
        payload = {"model": self.model, "messages": messages, "configuration": self.config,
                   "schema": request.schema.model_json_schema(), "wire_format": wire_format(self.config["provider"], request.schema),
                   "prefix_strategy": PREFIX_STRATEGY, "original_system_prompt": request.system_prompt}
        audit = new_audit(self.model, payload, context.audit_directory, self.config["provider"], context.purpose)
        audit["policy"] = AZURE_POLICY
        result = ProviderResult()
        interrupted = None
        async_client = None

        def redact(value):
            if isinstance(value, str):
                return value.replace(self.key, '[redacted]')
            if isinstance(value, dict):
                return {k: redact(v) for k, v in value.items()}
            if isinstance(value, list):
                return [redact(v) for v in value]
            return value

        def record_request(sent):
            expected = self.config["base_url"] + ("chat/completions" if self.config["provider"] == "azure-gpt" else "v1/messages")
            if str(sent.url) != expected:
                raise ValueError("Azure SDK attempted an unexpected endpoint")
            body = json.loads(sent.content)
            audit.setdefault("requests", []).append(body)
            audit["request_sha256"] = fingerprint(body)
            verify_request(audit)

        def record_response(response):
            response.read()
            try:
                body = response.json()
            except ValueError:
                body = response.text.replace(self.key, "[redacted]")
            if response.status_code >= 400:
                body = redact(body)
            audit["http_response"] = {"status_code": response.status_code, "body": body,
                "request_ids": {k: v for k, v in response.headers.items()
                    if k in {"request-id", "apim-request-id", "x-request-id"}}}
            audit["http_response_sha256"] = fingerprint(audit["http_response"])

        try:
            disable_client_cache()
            from langsmith import tracing_context
            if self.config["provider"] == "azure-claude":
                import httpx2 as http_transport
            else:
                http_transport = httpx
                async_client = httpx.AsyncClient(timeout=self.config['timeout_seconds'])
            with http_transport.Client(timeout=self.config["timeout_seconds"], transport=self.transport,
                              event_hooks={"request": [record_request], "response": [record_response]}) as client, \
                    tracing_context(enabled=False):
                llm = create_chat(self.config, self.key, client, async_client)
                if llm.cache is not False or llm.max_retries != 0:
                    raise ValueError("Azure caching and automatic retries must be disabled")
                audit["langchain_cache"] = False
                options = {"method": "json_schema", "include_raw": True}
                if self.config["provider"] == "azure-gpt":
                    options["strict"] = True
                schema = request.schema.model_json_schema() if self.config["provider"] == "azure-gpt" else request.schema
                value = llm.with_structured_output(schema, **options).invoke(messages)
                audit["response"] = raw_response(value)
                result = import_module(f"jev_bench.suites.{self.suite}.responses").structured_result(value)
                audit['usage'] = usage(audit)
                counts = verify_response(audit)
                result.input_tokens, result.output_tokens = counts["input_tokens"], counts["output_tokens"]
                audit["usage"] = counts
                audit["model_identity"] = {k: self.config[k] for k in ("deployment", "model_name", "model_version")}
                audit["model_identity"].update(resolved_model=audit["http_response"]["body"].get("model"),
                                               response_id=audit["http_response"]["body"].get("id"))
                audit["model_sha256"] = fingerprint(audit["model_identity"])
                audit["runtime"] = {"provider": self.config["provider"], "base_url": self.config["base_url"], "policy": AZURE_POLICY}
                audit["runtime_sha256"] = fingerprint(audit["runtime"])
                audit["verified"] = True
        except BaseException as exc:
            audit["error"] = f"{type(exc).__name__}: {exc}".replace(self.key, "[redacted]")
            result.error = audit["error"]
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                interrupted = exc
                audit['interrupted'] = True
        finally:
            if async_client is not None:
                asyncio.run(async_client.aclose())
            if audit.get("response") is None:
                audit["response"] = {"error": audit.get("error"), "http_response": audit.get("http_response")}
            audit["hosted_latency_ms"] = (time.perf_counter() - started) * 1000
            linked = save_audit(audit)
            result.raw_response = {**serial(audit["response"]), "execution_audit": linked}
        if interrupted:
            interrupted.audit = linked
            raise interrupted
        return result
