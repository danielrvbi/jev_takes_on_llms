from jev_bench.providers.registry import MODELS, LEGACY_MODELS
from jev_bench.providers import registry

def model_configuration(model, systemone_context=None):
    return registry.model_configuration(model, suite='hard_case', systemone_context=systemone_context)

def create_provider(model, *, audit_directory=None, systemone_context=None):
    return registry.create_provider(model, suite='hard_case', audit_directory=audit_directory, systemone_context=systemone_context)
