"""Hosted Jev execution. Typed results never bypass the strict cache gate."""
import asyncio
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
import fcntl
import json
import os
from pathlib import Path
import time

import httpx2
from langchain_typesafe import Choice, Noul, NoulCriteria, Score, TypeSafeClassifier
from langsmith import tracing_context

from execution import (atomic_write, disable_client_cache, fingerprint, new_audit,
                       save_audit, verify_request_binding)
from run_control import RunStopped

JEV_MODEL = 'jev-1.13.0'
BASE_URL = 'https://api.typesafe.ai'
ENDPOINT = BASE_URL + '/v1/systemone'
TIMEOUT = 20
INPUT_PRICE = Decimal('0.042') / 1_000_000
MAX_INPUT_TOKENS = 65_536
MAX_REQUEST_CHARGE = MAX_INPUT_TOKENS * INPUT_PRICE
CACHE_REJECTION = 'TypeSafe has no supported cache-disable and explicit zero-cache evidence contract'


def configuration():
    return {'provider': 'typesafe', 'inference_model': JEV_MODEL,
            'temperature': None, 'seed': None, 'structured_method': 'native',
            'automatic_retries': 0, 'timeout_seconds': TIMEOUT, 'base_url': BASE_URL,
            'context_override': None, 'truncation_policy': 'reject'}


def typed_questions(questions):
    result = {}
    for name, question in questions.items():
        arguments = {'instructions': question['instructions']}
        if question['type'] == 'noul':
            if 'criteria' in question:
                arguments['criteria'] = NoulCriteria(**question['criteria'])
            result[name] = Noul(**arguments)
        else:
            result[name] = {'choice': Choice, 'score': Score}[question['type']](
                **arguments, criteria=question['criteria'])
    return result


def count(value):
    return value if type(value) is int and 0 <= value <= MAX_INPUT_TOKENS else None


class JevBudget:
    """A locked ledger shared across suites; unresolved reservations remain charged."""
    def __init__(self, root, budget_usd='0.10', max_calls=330):
        self.root = Path(root).resolve()
        try:
            self.limit = Decimal(str(budget_usd))
        except InvalidOperation:
            raise ValueError('Invalid Jev spending limit') from None
        if not self.limit.is_finite() or not 0 < self.limit <= 5:
            raise ValueError('Jev spending limit must be positive and at most $5')
        if type(max_calls) is not int or not 1 <= max_calls <= 330:
            raise ValueError('Jev call limit must be between 1 and 330')
        self.max_calls = max_calls
        self.path = self.root / 'jev_budget.json'

    @classmethod
    def for_root(cls, root):
        path = Path(root) / 'jev_budget.json'
        if path.exists():
            data = json.loads(path.read_text())
            return cls(root, data['budget_usd'], data['max_calls'])
        return cls(root)

    @contextmanager
    def locked(self):
        self.root.mkdir(parents=True, exist_ok=True)
        with (self.root / '.jev-budget.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            data = json.loads(self.path.read_text()) if self.path.exists() else {
                'version': 1, 'budget_usd': str(self.limit), 'max_calls': self.max_calls,
                'input_price_usd': str(INPUT_PRICE), 'reservations': [], 'blocked_reason': None}
            if (data.get('version') != 1 or Decimal(data['budget_usd']) != self.limit
                    or data.get('max_calls') != self.max_calls
                    or data.get('input_price_usd') != str(INPUT_PRICE)):
                raise RunStopped('Jev budget configuration changed; refusing resume')
            yield data

    def save(self, data):
        atomic_write(self.path, lambda handle: json.dump(data, handle, indent=2))

    def check(self):
        with self.locked() as data:
            if data['blocked_reason']:
                raise RunStopped('Jev already has a rejected attempt: ' + data['blocked_reason'])
            if any(item['state'] == 'reserved' for item in data['reservations']):
                raise RunStopped('Unresolved Jev reservation; automatic retry is forbidden')

    def reserve(self, call_id):
        with self.locked() as data:
            if data['blocked_reason'] or any(r['state'] == 'reserved' for r in data['reservations']):
                raise RunStopped('Jev rejection or unresolved reservation blocks further calls')
            committed = sum((Decimal(item['charge_usd']) for item in data['reservations']), Decimal(0))
            if len(data['reservations']) >= self.max_calls:
                raise RunStopped('Persistent Jev call budget exhausted')
            if committed + MAX_REQUEST_CHARGE > self.limit:
                raise RunStopped('Jev spending reservation would exceed budget')
            data['reservations'].append({'call_id': call_id, 'charge_usd': str(MAX_REQUEST_CHARGE),
                                         'state': 'reserved'})
            self.save(data)

    def finish(self, call_id, input_tokens, reason):
        with self.locked() as data:
            item = next(r for r in data['reservations'] if r['call_id'] == call_id)
            tokens = count(input_tokens)
            if tokens is not None:
                item['charge_usd'] = str(tokens * INPUT_PRICE)
                item['input_tokens'] = tokens
            item['state'] = 'rejected' if reason else 'completed'
            if reason:
                data['blocked_reason'] = reason
            self.save(data)

    def block(self, reason):
        with self.locked() as data:
            data['blocked_reason'] = reason
            self.save(data)


def validate_selection(root, models, warmups, max_new_calls):
    if JEV_MODEL not in models:
        return
    if warmups != 0 or max_new_calls is None:
        raise ValueError('Jev requires --warmups 0 and an explicit --max-new-calls limit')
    root = Path(root)
    # Neither direct entrypoint may silently retry a rejected run from another suite.
    for suite in ('benchmark', 'hard_case'):
        history = root / suite / 'attempt_history.csv'
        if history.exists():
            import csv
            with history.open(newline='') as handle:
                if any(row['model'] == JEV_MODEL and row['validation_success'].lower() != 'true'
                       for row in csv.DictReader(handle)):
                    raise RunStopped('Jev already has a rejected attempt; no automatic retry')
    JevBudget.for_root(root).check()


def redact(value, key):
    if isinstance(value, str):
        return value.replace(key, '[REDACTED]')
    if isinstance(value, dict):
        return {redact(k, key): redact(v, key) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v, key) for v in value]
    return value


class JevProvider:
    def __init__(self, model, *, audit_directory, questions, mapper,
                 budget=None, transport_factory=None):
        if model != JEV_MODEL or audit_directory is None:
            raise ValueError('A supported Jev model and audit directory are required')
        self._key = os.environ.get('TYPESAFE_API', '').strip()
        if not self._key:
            raise ValueError('TYPESAFE_API is required for hosted Jev')
        self.model = model
        self.audit_directory = Path(audit_directory)
        self.questions = questions
        self.mapper = mapper
        self.budget = budget or JevBudget.for_root(self.audit_directory.parent.parent)
        self.transport_factory = transport_factory or (
            lambda: httpx2.HTTPTransport(retries=0, trust_env=False))
        self.purpose = 'measurement'

    def invoke(self, state):
        if self.purpose != 'measurement':
            raise ValueError('Hosted Jev does not support warm-up calls')
        self.budget.check()
        questions = self.questions()
        request = {'state': state, 'questions': typed_questions(questions)}
        payload = {'model': self.model, 'state': state, 'questions': questions}
        # This is a byte-size guard, not a token count. TypeSafe enforces its token
        # context limits; any overflow is a retained failure, never truncation.
        if len(json.dumps(payload, ensure_ascii=False).encode()) > 60 * 1024:
            raise ValueError('Jev request exceeds the reviewed 60 KiB payload budget')
        audit = new_audit(self.model, payload, self.audit_directory, 'typesafe', self.purpose)
        audit['langchain_cache'] = False
        audit['runtime'] = {'provider': 'TypeSafe hosted', 'integration': 'langchain-typesafe',
                            'configuration': configuration(), 'evidence_boundary': CACHE_REJECTION}
        audit['runtime_sha256'] = fingerprint(audit['runtime'])
        started = time.perf_counter()
        reservation = False
        classifier = None

        def record_request(actual):
            if audit.get('requests'):
                raise RunStopped('Jev permits only one HTTP send per measurement')
            if actual.method != 'POST' or str(actual.url) != ENDPOINT:
                raise ValueError('Unexpected Jev endpoint')
            body = json.loads(actual.content)
            if len(actual.content) > 60 * 1024:
                raise ValueError('Jev request exceeds the 60 KiB payload limit')
            audit['requests'] = [body]
            audit['request_sha256'] = fingerprint(body)
            verify_request_binding(audit)
            self.budget.reserve(audit['call_id'])
            nonlocal reservation
            reservation = True

        def record_response(response):
            response.read()
            audit['http_status'] = response.status_code
            audit['request_id'] = response.headers.get('x-typesafe-request-id')
            try:
                audit['response'] = redact(response.json(), self._key)
            except ValueError:
                audit['response'] = {'body': redact(response.text, self._key)}

        try:
            disable_client_cache()
            with httpx2.Client(transport=self.transport_factory(), timeout=TIMEOUT,
                               follow_redirects=False, trust_env=False,
                               event_hooks={'request': [record_request], 'response': [record_response]}) as client:
                classifier = TypeSafeClassifier(model=self.model, api_key=self._key,
                    base_url=BASE_URL, timeout=TIMEOUT, client=client)
                with tracing_context(enabled=False):
                    classifier.invoke(request, config={'callbacks': []})
        except BaseException as error:
            audit['provider_error'] = type(error).__name__
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                audit['interrupted'] = True
        finally:
            if classifier is not None and classifier.async_client is not None:
                try:
                    asyncio.run(classifier.async_client.aclose())
                except Exception as error:
                    audit['provider_error'] = type(error).__name__
            raw = audit.get('response')
            raw = raw if isinstance(raw, dict) else {'response': raw}
            usage = raw.get('usage') if isinstance(raw.get('usage'), dict) else {}
            audit['model_identity'] = {'requested_model': self.model,
                'resolved_model': raw.get('model'), 'request_id': audit.get('request_id')}
            audit['model_sha256'] = fingerprint(audit['model_identity'])
            audit['error'] = CACHE_REJECTION
            audit['cold_latency_ms'] = (time.perf_counter() - started) * 1000
            audit['response'] = raw
            if reservation:
                self.budget.finish(audit['call_id'], usage.get('input_tokens'), CACHE_REJECTION)
            else:
                self.budget.block(CACHE_REJECTION)
            linked = save_audit(audit)
        result = self.mapper({**raw, 'usage': {
            'input_tokens': count(usage.get('input_tokens')),
            'output_tokens': count(usage.get('output_tokens'))}})
        result.raw_response = {**raw, 'execution_audit': linked}
        result.error = 'Cache verification failed: ' + CACHE_REJECTION + (
            '; provider error: ' + audit['provider_error'] if audit.get('provider_error') else '') + (
            '; ' + result.error if result.error else '')
        if audit.get('interrupted'):
            error = KeyboardInterrupt()
            error.audit = linked
            raise error
        return result
