from langchain_mistralai import ChatMistralAI

from jev_bench.providers.chat import AuditedChatProvider
from jev_bench.suites.hard_case.prompts import SYSTEM_PROMPT
from jev_bench.suites.hard_case.schemas import HardCaseOutput
from .base import ProviderResult, structured_result


class MistralProvider(AuditedChatProvider):
    def __init__(self, model, *, audit_directory=None):
        super().__init__(model, audit_directory, ChatMistralAI, HardCaseOutput, SYSTEM_PROMPT,
                         structured_result, ProviderResult)
