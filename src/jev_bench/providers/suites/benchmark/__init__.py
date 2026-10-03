from jev_bench.providers.registry import MODELS, LEGACY_MODELS
from jev_bench.providers import registry

def model_configuration(model):
    return registry.model_configuration(model, suite='benchmark')

def create_provider(model, *, audit_directory=None):
    return registry.create_provider(model, suite='benchmark', audit_directory=audit_directory)
