from hard_case.prompts import systemone_questions
from hard_case.schemas import PROBABILITY_FIELDS
from .base import ProviderResult, validate_result
from .context import inference_model, validate_alias, validate_request_budget
from execution import AuditError, systemone_call


def map_response(response):
    raw = response.model_dump(mode="json") if hasattr(response, "model_dump") else response
    usage = raw.get("usage") if isinstance(raw, dict) else None
    usage = usage if isinstance(usage, dict) else {}
    answers = raw.get("answers") if isinstance(raw, dict) else None
    values = {}
    if isinstance(answers, dict):
        for name in PROBABILITY_FIELDS:
            answer = answers.get(name)
            if isinstance(answer, dict) and "noul" in answer:
                values[name] = answer["noul"]
    error = "Invalid System One response: answers must be an object" if not isinstance(answers, dict) else ""
    return validate_result(values, raw, usage.get("input_tokens"), usage.get("output_tokens"), error)


class SystemOneProvider:
    def __init__(self, model, *, systemone_context=None, audit_directory=None):
        self.model = inference_model(model, systemone_context)
        if audit_directory is None:
            raise ValueError("An execution audit directory is mandatory")
        self.audit_directory = audit_directory
        self.purpose = "measurement"
        if systemone_context is not None:
            validate_alias(model, systemone_context)

    def invoke(self, packet):
        validate_request_budget(self.model, packet)
        try:
            return map_response(systemone_call(
                model=self.model, state=packet, questions=systemone_questions(),
                directory=self.audit_directory, purpose=self.purpose))
        except AuditError as exc:
            import json
            from pathlib import Path
            raw = json.loads(Path(exc.audit['audit_path']).read_text()).get("response")
            result = map_response(raw) if isinstance(raw, dict) else ProviderResult()
            result.raw_response = {**(raw or {}), "execution_audit": exc.audit}
            result.error = f"Cache verification failed: {exc}"
            return result
