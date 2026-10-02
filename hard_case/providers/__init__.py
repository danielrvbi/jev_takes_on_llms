MODELS = (
    "tev1:0.8b", "tev1:4b", "gemma4:e4b",
    "mistral-small-latest", "mistral-large-latest",
)


def model_configuration(model, systemone_context=None):
    if model not in MODELS:
        raise ValueError(f"Unsupported model: {model}")
    native = model.startswith("tev1:")
    config = {
        "provider": "systemone" if native else "ollama_chat" if model.startswith("gemma4:") else "mistral",
        "temperature": None if native else 0,
        "structured_method": "native" if native else "json_schema",
        "seed": None,
        "automatic_retries": 0,
        "timeout_seconds": None if native else 120,
        "context_override": None,
        "truncation_policy": "reject",
    }
    if systemone_context is not None and native:
        from .context import inference_model
        config.update(inference_model=inference_model(model, systemone_context),
                      context_override=systemone_context)
    if model.startswith("gemma4:"):
        config["keep_alive"] = "2h"
    return config


def create_provider(model, *, systemone_context=None):
    model_configuration(model, systemone_context)  # Reject unsupported models before importing integrations.
    if model.startswith("tev1:"):
        from .ollama_systemone import SystemOneProvider
        return SystemOneProvider(model, systemone_context=systemone_context)
    if model.startswith("gemma4:"):
        from .ollama_chat import OllamaChatProvider
        return OllamaChatProvider(model)
    from .mistral import MistralProvider
    return MistralProvider(model)
