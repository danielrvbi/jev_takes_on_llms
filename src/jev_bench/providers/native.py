"""Shared native SystemOne transport for all suites."""
import json
from jev_bench.providers.base import Request
from jev_bench.runtime.execution import AuditError
from jev_bench.storage.paths import resolve_evidence, dataset_context


class NativeProvider:
    supports_requests = True

    def __init__(self, model, *, audit_directory, questions, mapper, result_class,
                 native_call, request_validator=None):
        if audit_directory is None:
            raise ValueError("An execution audit directory is mandatory")
        self.model, self.audit_directory = model, audit_directory
        self.questions, self.mapper, self.result_class = questions, mapper, result_class
        self.native_call, self.request_validator = native_call, request_validator
        self.purpose = "measurement"

    def invoke(self, request, context=None):
        state = request.state if isinstance(request, Request) else request
        questions = request.questions if isinstance(request, Request) else self.questions()
        purpose = context.purpose if context else self.purpose
        directory = context.audit_directory if context else self.audit_directory
        if self.request_validator:
            self.request_validator(self.model, state)
        try:
            return self.mapper(self.native_call(model=self.model, state=state,
                questions=questions, directory=directory, purpose=purpose))
        except AuditError as exc:
            with dataset_context(directory.parent):
                raw = json.loads(resolve_evidence(exc.audit["audit_path"]).read_text(encoding="utf-8")).get("response")
            result = self.mapper(raw) if isinstance(raw, dict) else self.result_class()
            result.raw_response = {**(raw or {}), "execution_audit": exc.audit}
            result.error = f"Cache verification failed: {exc}"
            return result
