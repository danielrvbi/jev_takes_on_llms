"""Offline audit fixtures. Imported exclusively by tests, never by a runner."""
import copy
from pathlib import Path
import tempfile
from unittest.mock import patch

from jev_bench.runtime.execution import POLICY, hosted_call, request_recorder

_FIXTURES = tempfile.TemporaryDirectory(prefix='benchmark-offline-audits-')


def audited_result(result, model="offline-fixture", message=None):
    result = copy.deepcopy(result)
    raw = result.raw_response if isinstance(result.raw_response, dict) else {'response': result.raw_response}
    raw.pop('execution_audit', None)
    import json
    raw["content"] = json.dumps(result.values)
    raw['response_metadata'] = {'token_usage': {'prompt_tokens_details': {'cached_tokens': 0}}}
    def invoke(audit):
        audit['requests'] = [{'prompt_cache_key': audit['prompt_cache_key'], 'model': model, 'state': message}]
        from jev_bench.runtime.execution import fingerprint
        audit['request_sha256'] = fingerprint(audit['requests'][0])
        return raw
    _, link = hosted_call(model, {'model': model, 'state': message}, _FIXTURES.name, invoke)
    result.raw_response = {**raw, 'execution_audit': link}
    return result


class HardCaseChatFixture:
    """Run the production message/schema construction against a mock LLM."""
    def __init__(self, llm):
        from jev_bench.providers.chat import AuditedChatProvider
        from jev_bench.suites.hard_case.prompts import SYSTEM_PROMPT
        from jev_bench.suites.hard_case.schemas import HardCaseOutput
        from jev_bench.providers.suites.hard_case.base import structured_result, ProviderResult
        llm.cache = False
        self.llm = llm
        self.provider = AuditedChatProvider('gemma4:e4b', _FIXTURES.name, lambda **kwargs: llm,
                                            HardCaseOutput, SYSTEM_PROMPT, structured_result, ProviderResult)

    def invoke(self, packet):
        with patch('jev_bench.providers.chat.local_call', side_effect=lambda m, p, d, invoke, **k: (invoke('http://fixture', {}), {})), \
             patch('jev_bench.providers.chat.close_llm'):
            return self.provider.invoke(packet)
