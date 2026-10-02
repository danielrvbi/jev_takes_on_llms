import os

from langchain_mistralai import ChatMistralAI

from .base import StructuredChatProvider


class MistralProvider(StructuredChatProvider):
    def __init__(self, model):
        if not os.environ.get("MISTRAL_API_KEY"):
            raise ValueError("MISTRAL_API_KEY is required for Mistral models")
        super().__init__(ChatMistralAI(model=model, temperature=0, max_retries=0, timeout=120))
