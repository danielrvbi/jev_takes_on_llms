from langchain_mistralai import ChatMistralAI

from chat_execution import AuditedChatProvider, reject_truncation
from benchmark.prompts import SYSTEM_PROMPT
from benchmark.schemas import DecisionOutput
from .base import ProviderResult, structured_result


class MistralProvider(AuditedChatProvider):
    def __init__(self, model, *, audit_directory=None):
        super().__init__(model, audit_directory, ChatMistralAI, DecisionOutput, SYSTEM_PROMPT,
                         structured_result, ProviderResult)
