"""Fresh LangChain clients with mandatory local or hosted audit verification."""
import json
import os
import uuid

from jev_bench.run.control import PREFIX_STRATEGY

import httpx

from jev_bench.runtime.execution import (AuditError, close_llm, disable_client_cache, hosted_call,
                       local_call, request_recorder)


def reject_truncation(request):
    if request.url.path != '/api/chat':
        return
    payload = json.loads(request.content)
    payload['truncate'] = False
    body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    request.stream = httpx.ByteStream(body)
    request._content = body
    request.headers['Content-Length'] = str(len(body))


class AuditedChatProvider:
    supports_requests = True
    def __init__(self, model, audit_directory, factory, schema, system_prompt, result_mapper, result_class,
                 *, keep_alive=None):
        if audit_directory is None:
            raise ValueError('An execution audit directory is mandatory')
        if model.startswith('mistral') and not os.environ.get('MISTRAL_API_KEY'):
            raise ValueError('MISTRAL_API_KEY is required for Mistral models')
        self.model = model
        self.audit_directory = audit_directory
        self.factory = factory
        self.schema = schema
        self.system_prompt = system_prompt
        self.result_mapper = result_mapper
        self.result_class = result_class
        self.keep_alive = keep_alive
        self.prefix_experiment = False
        self.purpose = 'measurement'

    def invoke(self, message, context=None):
        from jev_bench.providers.base import Request
        if isinstance(message, Request):
            self.schema, self.system_prompt = message.schema, message.system_prompt
            message = message.state
        if context is not None:
            self.audit_directory, self.purpose = context.audit_directory, context.purpose
            self.prefix_experiment = context.prefix_experiment
        system = self.system_prompt
        if self.prefix_experiment:
            if not self.model.startswith('mistral'):
                raise ValueError('Prefix experiment is Mistral-only')
            system = f'Request identifier: {uuid.uuid4().hex}\n\n' + system
        messages = [('system', system), ('human', message)]
        payload = {'model': self.model, 'messages': messages, 'schema': self.schema.model_json_schema(), 'temperature': 0}
        if self.prefix_experiment:
            payload['prefix_strategy'] = PREFIX_STRATEGY
            payload['original_system_prompt'] = self.system_prompt
        disable_client_cache()
        def call(audit, host=None):
            settings = dict(model=self.model, temperature=0, cache=False)
            if host:
                settings.update(base_url=host, client_kwargs={'timeout': 120, 'trust_env': False,
                                'event_hooks': {'request': [request_recorder(audit, '/api/chat', reject_truncation)]}})
                if self.keep_alive is not None:
                    settings['keep_alive'] = self.keep_alive
            else:
                settings.update(max_retries=0, timeout=120, model_kwargs={"prompt_cache_key": audit["prompt_cache_key"]})
            llm = self.factory(**settings)
            try:
                if llm.cache is not False:
                    raise ValueError('LangChain response caching must be explicitly disabled')
                audit['langchain_cache'] = False
                if not host:
                    llm.client.event_hooks['request'].append(request_recorder(audit, '/v1/chat/completions'))
                    # HTTPX resolves this path relative to /v1; other configured endpoints
                    # may omit /v1, so match the actual client's base URL path.
                    path = llm.client.base_url.path.rstrip('/') + '/chat/completions'
                    llm.client.event_hooks['request'] = [request_recorder(audit, path)]
                structured = llm.with_structured_output(self.schema, method='json_schema', include_raw=True)
                return structured.invoke(messages)
            finally:
                close_llm(llm)
        try:
            if self.model.startswith('gemma4:'):
                value, audit = local_call(self.model, payload, self.audit_directory,
                                         lambda host, audit: call(audit, host), minimum_tasks=1,
                                         endpoint='/api/chat', purpose=self.purpose)
            else:
                value, audit = hosted_call(self.model, payload, self.audit_directory, call, self.purpose)
            result = self.result_mapper(value)
            result.raw_response = {**(result.raw_response or {}), 'execution_audit': audit}
            return result
        except AuditError as exc:
            # The sidecar holds the complete response even when cache verification rejects it.
            from pathlib import Path
            from jev_bench.storage.paths import resolve_evidence
            raw = json.loads(resolve_evidence(exc.audit['audit_path']).read_text(encoding="utf-8")).get('response')
            usage = (raw or {}).get('response_metadata', {}).get('token_usage', {}) if isinstance(raw, dict) else {}
            return self.result_class(input_tokens=usage.get('prompt_tokens'), output_tokens=usage.get('completion_tokens'), raw_response={**(raw if isinstance(raw, dict) else {'response': raw}),
                                                    'execution_audit': exc.audit},
                                     error=f'Cache verification failed: {exc}')
