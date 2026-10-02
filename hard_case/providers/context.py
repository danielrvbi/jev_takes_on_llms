"""Stock Ollama aliases: logical labels stay stable, inference names are explicit."""
import re
import shlex
from pathlib import Path

import httpx
import ollama

from hard_case.prompts import systemone_questions


SYSTEMONE_REQUEST_BUDGET = 60 * 1024
SUPPORTED_CONTEXT = 262144


def inference_model(model, context=None):
    if context is None or not model.startswith("tev1:"):
        return model
    if context != SUPPORTED_CONTEXT:
        raise ValueError("Only the reviewed stock alias context 262144 is supported")
    return f"tev1-hard:{model.split(':', 1)[1]}-ctx{context}"


def serialized_request(model, packet):
    """Use the installed SDK request type and HTTPX's actual JSON encoder."""
    request = ollama.SystemOneRequest(model=model, state=packet, questions=systemone_questions())
    return httpx.Request("POST", "http://localhost:11434/v1/systemone",
                         json=request.model_dump(exclude_none=True)).content


def validate_request_budget(model, packet):
    size = len(serialized_request(model, packet))
    if size > SYSTEMONE_REQUEST_BUDGET:
        raise ValueError(f"System One request {size} bytes exceeds 60 KiB compact budget; revise packet")
    return size


def _weight_blob(info):
    for line in (info.modelfile or "").splitlines():
        tokens = shlex.split(line)
        if tokens and tokens[0].upper() == "FROM" and len(tokens) == 2:
            blob = Path(tokens[1]).name
            if re.fullmatch(r"sha256-[0-9a-f]{64}", blob):
                return blob
    raise ValueError("Ollama model inspection did not identify a cached weight blob")


def _parameters(info):
    result = {}
    for line in (info.parameters or "").splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2:
            result.setdefault(parts[0], []).append(parts[1])
    return result


def validate_alias(model, context, client=None):
    actual = inference_model(model, context)
    client = client or ollama.Client(timeout=5)
    try:
        original, alias = client.show(model), client.show(actual)
    except (ollama.ResponseError, httpx.HTTPError, ConnectionError) as exc:
        raise ValueError(f"Cannot inspect required alias {actual}; create it with its Modelfile: {exc}") from exc
    original_blob, alias_blob = _weight_blob(original), _weight_blob(alias)
    if original_blob != alias_blob:
        raise ValueError(f"Alias {actual} does not reference the original weight blob")
    original_parameters, alias_parameters = _parameters(original), _parameters(alias)
    if alias_parameters.pop("num_ctx", []) != [str(context)]:
        raise ValueError(f"Alias {actual} must specify num_ctx {context}")
    original_parameters.pop("num_ctx", None)
    if (original_parameters != alias_parameters or original.template != alias.template
            or original.capabilities != alias.capabilities):
        raise ValueError(f"Alias {actual} changes more than num_ctx")
    # Preserve other Modelfile directives (SYSTEM, ADAPTER, MESSAGE, license, etc.).
    def directives(info):
        return [line for line in (info.modelfile or "").splitlines()
                if not line.startswith(("#", "FROM ", "PARAMETER num_ctx")) and line.strip()]
    if directives(original) != directives(alias):
        raise ValueError(f"Alias {actual} changes original model directives")
    limits = [value for key, value in (original.modelinfo or {}).items() if key.endswith("context_length")]
    if not limits or context > max(limits):
        raise ValueError(f"Requested context {context} exceeds the advertised model context")
    return {"inference_model": actual, "weight_blob": alias_blob, "num_ctx": context}
