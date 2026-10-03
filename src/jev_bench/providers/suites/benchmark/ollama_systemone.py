from jev_bench.providers.native import NativeProvider
from jev_bench.runtime.execution import systemone_call
from jev_bench.suites.benchmark.prompts import systemone_questions
from jev_bench.suites.benchmark.responses import map_response, ProviderResult

class SystemOneProvider(NativeProvider):
    def __init__(self, model, *, audit_directory=None):
        super().__init__(model, audit_directory=audit_directory, questions=systemone_questions,
                         mapper=map_response, result_class=ProviderResult,
                         native_call=lambda **kwargs: systemone_call(**kwargs))
