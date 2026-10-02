from langchain_ollama import ChatOllama

from .base import StructuredChatProvider


class OllamaChatProvider(StructuredChatProvider):
    def __init__(self, model):
        super().__init__(ChatOllama(model=model, temperature=0,
                                   client_kwargs={"timeout": 120}))
