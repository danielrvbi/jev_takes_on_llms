MODELS = (
    "tev1:0.8b", "tev1:4b", "gemma4:e4b",
    "mistral-small-latest", "mistral-large-latest",
)


def create_provider(model):
    if model not in MODELS:
        raise ValueError(f"Unsupported model: {model}")
    if model.startswith("tev1:"):
        from .ollama_systemone import SystemOneProvider
        return SystemOneProvider(model)
    if model.startswith("gemma4:"):
        from .ollama_chat import OllamaChatProvider
        return OllamaChatProvider(model)
    from .mistral import MistralProvider
    return MistralProvider(model)
