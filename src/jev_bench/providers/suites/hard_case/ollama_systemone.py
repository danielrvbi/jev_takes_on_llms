from jev_bench.providers.native import NativeProvider
from jev_bench.runtime.execution import systemone_call
from jev_bench.suites.hard_case.prompts import systemone_questions
from jev_bench.suites.hard_case.responses import map_response, ProviderResult
from .context import inference_model, validate_alias, validate_request_budget

class SystemOneProvider(NativeProvider):
    def __init__(self, model, *, audit_directory=None, systemone_context=None):
        if systemone_context is not None:
            validate_alias(model, systemone_context)
        model = inference_model(model, systemone_context)
        super().__init__(model, audit_directory=audit_directory, questions=systemone_questions,
                         mapper=map_response, result_class=ProviderResult,
                         native_call=lambda **kwargs: systemone_call(**kwargs), request_validator=validate_request_budget)
