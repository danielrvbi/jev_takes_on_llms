from benchmark.prompts import systemone_questions
from benchmark.providers.ollama_systemone import map_response
from jev_execution import JevProvider as HostedJevProvider


class JevProvider(HostedJevProvider):
    def __init__(self, model, *, audit_directory=None, **kwargs):
        super().__init__(model, audit_directory=audit_directory,
                         questions=systemone_questions, mapper=map_response, **kwargs)
