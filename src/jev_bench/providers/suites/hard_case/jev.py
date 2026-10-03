from jev_bench.suites.hard_case.prompts import systemone_questions
from jev_bench.providers.suites.hard_case.ollama_systemone import map_response
from jev_bench.providers.jev import JevProvider as HostedJevProvider


class JevProvider(HostedJevProvider):
    def __init__(self, model, *, audit_directory=None, **kwargs):
        super().__init__(model, audit_directory=audit_directory,
                         questions=systemone_questions, mapper=map_response, **kwargs)
