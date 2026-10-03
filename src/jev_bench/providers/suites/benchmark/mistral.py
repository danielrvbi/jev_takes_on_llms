from langchain_mistralai import ChatMistralAI

from jev_bench.providers.chat import AuditedChatProvider
from jev_bench.suites.benchmark.prompts import SYSTEM_PROMPT
from jev_bench.suites.benchmark.schemas import DecisionOutput
from .base import ProviderResult, structured_result


class MistralProvider(AuditedChatProvider):
    def __init__(self, model, *, audit_directory=None):
        super().__init__(model, audit_directory, ChatMistralAI, DecisionOutput, SYSTEM_PROMPT,
                         structured_result, ProviderResult)
