from langchain_ollama import ChatOllama

from jev_bench.providers.chat import AuditedChatProvider
from jev_bench.suites.hard_case.prompts import SYSTEM_PROMPT
from jev_bench.suites.hard_case.schemas import HardCaseOutput
from .base import ProviderResult, structured_result


class OllamaChatProvider(AuditedChatProvider):
    def __init__(self, model, *, audit_directory=None):
        super().__init__(model, audit_directory, ChatOllama, HardCaseOutput, SYSTEM_PROMPT,
                         structured_result, ProviderResult, keep_alive="2h")
