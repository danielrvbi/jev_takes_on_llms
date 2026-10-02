import json

import httpx
from langchain_ollama import ChatOllama

from .base import StructuredChatProvider


def reject_truncation(request):
    """The installed Ollama SDK omits the server's top-level truncate parameter.

    Add it at the HTTP boundary while retaining LangChain structured output and
    the original packet. The server must reject overflow instead of dropping input.
    """
    if request.url.path != "/api/chat":
        return
    payload = json.loads(request.content)
    payload["truncate"] = False
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request.stream = httpx.ByteStream(body)
    request._content = body  # Keep HTTPX's already-buffered content consistent with its stream.
    request.headers["Content-Length"] = str(len(body))


class OllamaChatProvider(StructuredChatProvider):
    def __init__(self, model):
        super().__init__(ChatOllama(
            model=model, temperature=0, keep_alive="2h",
            client_kwargs={"timeout": 120, "event_hooks": {"request": [reject_truncation]}},
        ))
