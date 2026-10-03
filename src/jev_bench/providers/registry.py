"""Single model registry with explicit suite-specific configuration."""
from importlib import import_module

LEGACY_MODELS = ("tev1:0.8b", "tev1:4b", "gemma4:e4b",
                 "mistral-small-latest", "mistral-large-latest")
MODELS = (*LEGACY_MODELS[:3], "jev-1.13.0", *LEGACY_MODELS[3:])


def model_configuration(model, *, suite="benchmark", systemone_context=None):
    if model not in MODELS:
        raise ValueError(f"Unsupported model: {model}")
    if model == "jev-1.13.0":
        from jev_bench.providers.jev import configuration
        return configuration()
    native = model.startswith("tev1:")
    config = {"provider": "systemone" if native else "langchain",
              "temperature": None if native else 0,
              "structured_method": "native" if native else "json_schema",
              "seed": None, "automatic_retries": 0}
    if suite == "hard_case":
        config.update(provider="systemone" if native else "ollama_chat" if model.startswith("gemma4:") else "mistral",
                      timeout_seconds=None if native else 120, context_override=None,
                      truncation_policy="reject")
        if native and systemone_context is not None:
            from jev_bench.providers.suites.hard_case.context import inference_model
            config.update(inference_model=inference_model(model, systemone_context),
                          context_override=systemone_context)
        if model.startswith("gemma4:"):
            config["keep_alive"] = "2h"
    return config


def create_provider(model, *, suite="benchmark", audit_directory=None, systemone_context=None):
    model_configuration(model, suite=suite, systemone_context=systemone_context)
    module, class_name = (("jev", "JevProvider") if model == "jev-1.13.0" else
                          ("ollama_systemone", "SystemOneProvider") if model.startswith("tev1:") else
                          ("ollama_chat", "OllamaChatProvider") if model.startswith("gemma4:") else
                          ("mistral", "MistralProvider"))
    backend = import_module(f"jev_bench.providers.suites.{suite}.{module}")
    settings = {"audit_directory": audit_directory}
    if suite == "hard_case" and model.startswith("tev1:"):
        settings["systemone_context"] = systemone_context
    return getattr(backend, class_name)(model, **settings)
