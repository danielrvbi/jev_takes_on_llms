LEGACY_MODELS = (
    "tev1:0.8b", "tev1:4b", "gemma4:e4b",
    "mistral-small-latest", "mistral-large-latest",
)
MODELS = (*LEGACY_MODELS[:3], 'jev-1.13.0', *LEGACY_MODELS[3:])


def model_configuration(model):
    if model == 'jev-1.13.0':
        from jev_execution import configuration
        return configuration()
    native = model.startswith('tev1:')
    return {'provider': 'systemone' if native else 'langchain',
            'temperature': None if native else 0,
            'structured_method': 'native' if native else 'json_schema',
            'seed': None, 'automatic_retries': 0}


def create_provider(model, *, audit_directory=None):
    if model not in MODELS:
        raise ValueError(f"Unsupported model: {model}")
    if model == 'jev-1.13.0':
        from .jev import JevProvider
        return JevProvider(model, audit_directory=audit_directory)
    if model.startswith("tev1:"):
        from .ollama_systemone import SystemOneProvider
        return SystemOneProvider(model, audit_directory=audit_directory)
    if model.startswith("gemma4:"):
        from .ollama_chat import OllamaChatProvider
        return OllamaChatProvider(model, audit_directory=audit_directory)
    from .mistral import MistralProvider
    return MistralProvider(model, audit_directory=audit_directory)
