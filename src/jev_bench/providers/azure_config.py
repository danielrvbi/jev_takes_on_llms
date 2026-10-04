"""Eight local settings: two resource keys/endpoints and four model deployments."""
import os
from urllib.parse import urlsplit

MODEL_SETTINGS = {
    "azure-gpt-luna": ("azure-gpt", "AZURE_GPT", "LUNA_MODEL"),
    "azure-gpt-sol": ("azure-gpt", "AZURE_GPT", "SOL_MODEL"),
    "azure-claude-opus": ("azure-claude", "AZURE_CLAUDE", "OPUS_MODEL"),
    "azure-claude-sonnet": ("azure-claude", "AZURE_CLAUDE", "SONNET_MODEL"),
}
AZURE_MODELS = tuple(MODEL_SETTINGS)


def required(name):
    value = os.environ.get(name, "").strip()
    if not value or value.lower() in {"xxx", "replace-me"} or "<" in value:
        raise ValueError(f"{name} must be configured; see docs/azure_handoff.md")
    return value


def configuration(model):
    if model not in MODEL_SETTINGS:
        raise ValueError(f"Unsupported Azure alias: {model}")
    provider, prefix, model_setting = MODEL_SETTINGS[model]
    endpoint = required(f"{prefix}_BASE_URL").rstrip("/") + "/"
    url = urlsplit(endpoint)
    expected_path = "/openai/v1/" if provider == "azure-gpt" else "/anthropic/"
    if (url.scheme != "https" or not url.hostname or
            not url.hostname.endswith((".openai.azure.com", ".services.ai.azure.com")) or
            url.path != expected_path or url.username or url.password or url.query or url.fragment or url.port):
        raise ValueError(f"{prefix}_BASE_URL must be an Azure HTTPS resource endpoint ending in {expected_path}")
    deployment = required(f"{prefix}_{model_setting}")
    # Fixed experiment settings are bound into saved metadata. Deployment names
    # select the API model; resolved model identities come from each response.
    return {"provider": provider, "inference_model": model, "base_url": endpoint,
            "deployment": deployment, "model_name": deployment, "model_version": None,
            "temperature": 0, "reasoning_effort": None, "max_output_tokens": 4096,
            "timeout_seconds": 120, "structured_method": "json_schema", "automatic_retries": 0,
            "langchain_cache": False, "latency": "hosted wall time including client creation and audit"}


def api_key(model):
    if model not in MODEL_SETTINGS:
        raise ValueError(f"Unsupported Azure alias: {model}")
    return required(f"{MODEL_SETTINGS[model][1]}_API_KEY")
