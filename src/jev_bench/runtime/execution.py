"""Audited execution, with an explicit hosted Jev cache-evidence exception."""
from datetime import datetime, timezone
from functools import lru_cache
import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import time
import uuid

import httpx
import ollama

from jev_bench.storage.paths import PROJECT_ROOT, PACKAGE_ROOT, resolve_evidence, ensure_writable

ROOT = PROJECT_ROOT
PRIVATE = ROOT / '.runtime'
COMMIT = 'cc4069396f3ad2c370c53eed2e4a42ac13adab84'
POLICY = {
    'version': 2, 'mode': 'mandatory_cache_free',
    'local_boundary': 'fresh private server, worker and client for every call',
    'local_verification': 'every prompt including scoring retries: zero initial cache, full evaluation, no restore or truncation',
    'within_request_prefix_reuse': False,
    'hosted_verification': 'fresh prompt_cache_key AND explicit numeric cached_tokens=0',
    'langchain_cache': False, 'automatic_request_retries': 0,
    'latency': 'cold wall time includes startup, loading, verification and teardown',
    'ollama_commit': COMMIT,
}
AUDIT_COLUMNS = ['call_id', 'cache_verified', 'failure_kind', 'request_sha256',
                 'runtime_sha256', 'model_sha256', 'audit_path', 'audit_sha256']
JEV_CACHE_EXCEPTION = {'version': 1, 'model': 'jev-1.13.0',
                       'server_caching': 'unverified', 'accept_valid_responses': True}


def now():
    return datetime.now(timezone.utc).isoformat()


def serial(value):
    if hasattr(value, 'model_dump'):
        return value.model_dump(mode='json')
    if isinstance(value, dict):
        return {k: serial(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [serial(v) for v in value]
    if isinstance(value, BaseException):
        return f'{type(value).__name__}: {value}'
    return value


def fingerprint(value):
    return hashlib.sha256(json.dumps(serial(value), sort_keys=True, ensure_ascii=False,
                                     allow_nan=False).encode()).hexdigest()


def file_hash(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


@lru_cache(maxsize=256)
def _stable_hash(path, size, mtime_ns, ctime_ns):
    return file_hash(path)


def checked_hash(path):
    path = Path(path)
    s = path.stat()
    return _stable_hash(str(path.resolve()), s.st_size, s.st_mtime_ns, s.st_ctime_ns)


def runtime_identity():
    manifest = json.loads((PRIVATE / 'manifest.json').read_text())
    if manifest['commit'] != COMMIT or manifest['version'] != '0.35.0':
        raise ValueError('Private runtime does not match the mandatory pinned version')
    for name, key in [('runtime/no-cache.patch', 'patch_sha256'), ('runtime/no_cache_test.go', 'runtime_test_sha256')]:
        if checked_hash(PACKAGE_ROOT / name) != manifest[key]:
            raise ValueError('Runtime patch changed; rerun python -m jev_bench.runtime.prepare')
    binary = PRIVATE / manifest['binary']
    if checked_hash(binary) != manifest['binary_sha256']:
        raise ValueError('Private server binary fingerprint mismatch')
    for path, expected in manifest['native_files'].items():
        if checked_hash(PRIVATE / path) != expected:
            raise ValueError(f'Private native payload fingerprint mismatch: {path}')
    return manifest


def model_identity(model):
    # Manifests are content addressed; verify the actual referenced weights too.
    repository, _, tag = model.rpartition(':')
    parts = repository.split('/')
    if len(parts) == 1:
        parts = ['registry.ollama.ai', 'library', *parts]
    elif len(parts) == 2:
        parts = ['registry.ollama.ai', *parts]
    directory = Path(os.environ.get('OLLAMA_MODELS', Path.home() / '.ollama/models')).expanduser().resolve()
    path = directory / 'manifests' / Path(*parts) / (tag or 'latest')
    manifest = json.loads(path.read_text())
    blobs = []
    for layer in [manifest['config'], *manifest['layers']]:
        digest = layer['digest']
        blob = directory / 'blobs' / digest.replace(':', '-')
        if blob.stat().st_size != layer['size'] or checked_hash(blob) != digest.removeprefix('sha256:'):
            raise ValueError(f'Model blob fingerprint mismatch: {digest}')
        blobs.append({'digest': digest, 'size': layer['size']})
    return {'model': model, 'manifest_sha256': checked_hash(path), 'blobs': blobs}


def experiment_execution(models):
    sources = [p for name in ('run', 'providers', 'storage', 'runtime')
               for p in sorted((PACKAGE_ROOT / name).rglob('*.py'))]
    sources.extend(sorted((PACKAGE_ROOT / 'suites').glob('*.py')))
    return {'execution_policy': execution_policy(models), 'execution_implementation_sha256': fingerprint({str(p.relative_to(PACKAGE_ROOT)): checked_hash(p) for p in sources}),
            'runtime': {'provider': 'hosted', 'local_runtime_required': False}
                       if models and not any(m.startswith(('tev1:', 'gemma4:')) for m in models) else runtime_identity()}


def execution_policy(models):
    from jev_bench.providers.azure_config import AZURE_MODELS
    if any(m in AZURE_MODELS for m in models):
        if not all(m in AZURE_MODELS for m in models):
            raise ValueError('Azure prefix experiments cannot mix with other providers')
        from jev_bench.runtime.azure import AZURE_POLICY
        return AZURE_POLICY
    return POLICY


def supported_policy(policy):
    from jev_bench.runtime.azure import AZURE_POLICY
    return policy == POLICY or policy == AZURE_POLICY


from jev_bench.storage.io import atomic_write, output_lock


def suite_directory(root, suite):
    root = Path(root).resolve()
    ensure_writable(root)
    if any('contaminated' in part.lower() for part in root.parts):
        raise ValueError('Quarantined measurements cannot be used as an output or resume directory')
    if any((root / name).exists() for name in ['metadata.json', 'raw.csv', 'attempt_history.csv']):
        raise ValueError('Incompatible resume: legacy measurements at results root; select a new common root')
    return root / suite


class AuditError(RuntimeError):
    def __init__(self, message, audit):
        super().__init__(message)
        self.audit = audit


def new_audit(model, payload, directory, provider, purpose='measurement'):
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    call_id = uuid.uuid4().hex
    return {'call_id': call_id, 'policy': POLICY, 'model': model, 'provider': provider,
            'purpose': purpose, 'started_at_utc': now(), 'input_sha256': fingerprint(payload),
            'input': payload, 'audit_path': str(directory / f'{call_id}.json'),
            'verified': False, 'response': None}


def save_audit(audit):
    audit['finished_at_utc'] = now()
    path = Path(audit['audit_path'])
    suite = path.parent.parent
    plan = suite.parent / 'run_plan.json'
    if plan.exists() and json.loads(plan.read_text()).get('version') == 2:
        audit['audit_path'] = str(path.relative_to(suite))
        if audit.get('log_path'):
            audit['log_path'] = str(Path(audit['log_path']).relative_to(suite))
    with path.open('x', encoding='utf-8') as handle:
        json.dump(serial(audit), handle, indent=2, ensure_ascii=False)
        handle.flush()
        os.fsync(handle.fileno())
    return {**{k: v for k, v in audit.items() if k not in {'input', 'response'}},
            'audit_sha256': file_hash(path)}


def failed_audit(model, payload, directory, error, purpose="measurement"):
    audit = new_audit(model, payload, directory, 'not_executed', purpose)
    audit['error'] = str(error)
    return save_audit(audit)


def raw_response(value):
    return serial(value.get('raw')) if isinstance(value, dict) and 'raw' in value else serial(value)


def request_recorder(audit, endpoint, mutate=None):
    def record(request):
        if request.url.path != endpoint:
            return
        if mutate:
            mutate(request)
        body = json.loads(request.content)
        audit.setdefault('requests', []).append(body)
        audit['request_sha256'] = fingerprint(body)
    return record


def verify_request_binding(audit):
    if audit.get('provider') in ('azure-gpt', 'azure-claude'):
        from jev_bench.runtime.azure import verify_request
        return verify_request(audit)
    payload = audit['input']
    request = audit['requests'][0]
    if 'state' in payload:
        if request.get('state') != payload['state'] or request.get('model') != payload.get('model'):
            raise ValueError('Actual native request differs from the intended model/input')
        if 'questions' in payload and request.get('questions') != payload['questions']:
            raise ValueError('Actual native request changed the complete question schema')
    elif 'messages' in payload:
        if payload.get('prefix_strategy'):
            from jev_bench.run.control import PREFIX_STRATEGY
            system = payload['messages'][0][1]
            if payload['prefix_strategy'] != PREFIX_STRATEGY or not re.fullmatch(
                    r'Request identifier: [0-9a-f]{32}\n\n' + re.escape(payload['original_system_prompt']), system):
                raise ValueError('Invalid prefix experiment request')
        expected = [('user' if role == 'human' else role, content) for role, content in payload['messages']]
        observed = [(message.get('role'), message.get('content')) for message in request.get('messages', [])]
        if observed != expected or request.get('model') != payload['model']:
            raise ValueError('Actual chat request changed messages or included previous answers')


def verify_cold_log(log, *, minimum_tasks=1, endpoint='/v1/systemone'):
    """Verify all prompt tasks by (slot,task), not just the first timing line.

    Later `cached n_tokens` messages within a task describe its advancing KV
    state. Only its initial value may reflect a prior prompt; full eval is checked.
    """
    if re.search(r'restored context checkpoint|truncated\s*=\s*(?:1|true)|truncating.*prompt', log, re.I):
        raise ValueError('Prompt restored previous context or was truncated')
    tasks = {}
    for line in log.splitlines():
        match = re.search(r'id\s+(\d+)\s*\|\s*task\s+(\d+)\s*\|\s*(.*)', line)
        if not match:
            continue
        key, message = (int(match[1]), int(match[2])), match[3]
        start = re.search(r'new prompt,.*task\.n_tokens\s*=\s*(\d+)', message)
        if start:
            if key in tasks:
                raise ValueError('Duplicate prompt task evidence')
            tasks[key] = {'slot': key[0], 'task': key[1], 'prompt_tokens': int(start[1]), 'initial_cached_tokens': None, 'evaluated_tokens': None}
        cached = re.search(r'cached n_tokens\s*=\s*(\d+)', message)
        evaluated = re.search(r'prompt eval time\s*=\s*[\d.]+ ms /\s*(\d+) tokens', message)
        if cached or evaluated:
            if key not in tasks:
                raise ValueError('Cache/evaluation evidence has no corresponding prompt task')
            task = tasks[key]
            if cached and task['initial_cached_tokens'] is None:
                task['initial_cached_tokens'] = int(cached[1])
            if evaluated:
                if task['evaluated_tokens'] is not None:
                    raise ValueError('Duplicate evaluation evidence')
                task['evaluated_tokens'] = int(evaluated[1])
    dispatches = re.findall(r'msg="experiment prompt request"[^\n]*', log)
    nonempty_tasks = [task for task in tasks.values() if task['prompt_tokens'] > 0]
    empty_dispatches = sum('kind=grammar' in d for d in dispatches)
    if len(dispatches) != len(nonempty_tasks) + empty_dispatches or any('cache_prompt=false' not in d for d in dispatches):
        raise ValueError('Missing or cached backend dispatch evidence, including retries')
    score_lengths = [int(m[1]) for d in dispatches if (m := re.search(r'prompt_tokens=(\d+)', d))]
    if score_lengths and endpoint == '/v1/systemone' and score_lengths != [t['prompt_tokens'] for t in tasks.values()]:
        raise ValueError('Scoring dispatch/task counts or prompt lengths differ')
    if sum(task['prompt_tokens'] > 0 for task in tasks.values()) < minimum_tasks:
        raise ValueError('Missing prompt tasks, including internal scoring retries')
    for task in tasks.values():
        # The backend may create an empty schema-to-grammar conversion task.
        # Zero evaluated tokens proves it performs no prompt inference computation.
        if endpoint == '/api/chat' and task['prompt_tokens'] == 0 and task['evaluated_tokens'] == 0 and task['initial_cached_tokens'] in (None, 0):
            task.update(initial_cached_tokens=0, empty_schema_conversion=True)
            continue
        if task['initial_cached_tokens'] != 0:
            raise ValueError('Prompt task reused cached inference state or lacks initial cache evidence')
        if task['prompt_tokens'] <= 0 or task['evaluated_tokens'] != task['prompt_tokens']:
            raise ValueError('Prompt task did not evaluate the entire prompt')
    requests = re.findall(r'\[GIN\].*POST\s+"' + re.escape(endpoint) + r'"', log)
    if len(requests) != 1:
        raise ValueError('Private server must process exactly one inference API request')
    workers = re.findall(r'msg="llama-server stopped" pid=(\d+)', log)
    if len(workers) != 1:
        raise ValueError('Missing confirmation that the private model worker stopped')
    return {'prompt_tasks': list(tasks.values()), 'inference_requests': len(requests), 'stopped_worker_pid': int(workers[0])}


def stop_process_tree(process):
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=10)
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    deadline = time.monotonic() + 5
    while True:
        try:
            os.killpg(process.pid, 0)
        except ProcessLookupError:
            return True
        if time.monotonic() >= deadline:
            raise ValueError('Private process group remains after teardown')
        time.sleep(0.05)


def disable_client_cache():
    from langchain_core.globals import set_llm_cache
    set_llm_cache(None)


def close_llm(llm):
    for name in ['client', '_client']:
        client = getattr(llm, name, None)
        if client is not None:
            underlying = getattr(client, '_client', client)
            if hasattr(underlying, 'close'):
                underlying.close()
    client = getattr(llm, '_async_client', None) or getattr(llm, 'async_client', None)
    if client is not None:
        underlying = getattr(client, '_client', client)
        if hasattr(underlying, 'aclose'):
            asyncio.run(underlying.aclose())


def local_call(model, payload, directory, invoke, *, minimum_tasks, endpoint, purpose='measurement'):
    audit = new_audit(model, payload, directory, 'ollama', purpose)
    log_path = Path(directory).resolve() / f"{audit['call_id']}.log"
    audit.update(log_path=str(log_path), endpoint=endpoint, minimum_tasks=minimum_tasks)
    process = None
    failure = None
    value = None
    started = time.perf_counter()
    try:
        runtime = runtime_identity()
        audit.update(runtime=runtime, runtime_sha256=fingerprint(runtime), model_identity=model_identity(model))
        audit['model_sha256'] = fingerprint(audit['model_identity'])
        with socket.socket() as reservation:
            reservation.bind(('127.0.0.1', 0))
            port = reservation.getsockname()[1]
        host = f'http://127.0.0.1:{port}'
        environment = {**os.environ, 'OLLAMA_HOST': host, 'OLLAMA_DEBUG': '1',
                       'OLLAMA_DEBUG_LOG_REQUESTS': 'false', 'OLLAMA_NO_CLOUD': '1',
                       'OLLAMA_NOPRUNE': '1', 'OLLAMA_NUM_PARALLEL': '1'}
        audit['host'] = host
        with log_path.open('wb') as log_handle:
            process = subprocess.Popen([str(PRIVATE / runtime['binary']), 'serve'], env=environment,
                                       cwd=PRIVATE, stdout=log_handle, stderr=subprocess.STDOUT, start_new_session=True)
            audit['server_pid'] = process.pid
            try:
                with httpx.Client(base_url=host, timeout=2, trust_env=False) as http:
                    deadline = time.monotonic() + 30
                    while True:
                        if process.poll() is not None:
                            raise ValueError('Private Ollama server exited before becoming ready')
                        try:
                            response = http.get('/api/version')
                            response.raise_for_status()
                            audit['server_version'] = response.json()['version']
                            if audit['server_version'] != '0.35.0':
                                raise ValueError('Unexpected private server version')
                            break
                        except httpx.TransportError:
                            if time.monotonic() >= deadline:
                                raise TimeoutError('Private Ollama startup timed out')
                            time.sleep(0.1)
                    response = http.get('/api/ps')
                    response.raise_for_status()
                    audit['models_before'] = response.json()['models']
                    if audit['models_before']:
                        raise ValueError('Private server already has a loaded model')
                if process.poll() is not None or f'Listening on 127.0.0.1:{port} ' not in log_path.read_text(errors='replace'):
                    raise ValueError('Could not verify private listener ownership')
                audit['startup_ms'] = (time.perf_counter() - started) * 1000
                invoked = time.perf_counter()
                value = invoke(host, audit)
                audit['inference_ms'] = (time.perf_counter() - invoked) * 1000
                audit['response'] = raw_response(value)
                with ollama.Client(host=host, timeout=5, trust_env=False) as client:
                    audit['models_after'] = client.ps().model_dump(mode='json')['models']
                if len(audit['models_after']) != 1 or audit['models_after'][0]['model'] != model:
                    raise ValueError('Private server loaded an unexpected model')
            finally:
                teardown = time.perf_counter()
                audit['process_tree_stopped'] = stop_process_tree(process)
                audit['server_returncode'] = process.returncode
                audit['teardown_ms'] = (time.perf_counter() - teardown) * 1000
        audit['log_sha256'] = file_hash(log_path)
        audit.update(verify_cold_log(log_path.read_text(errors='replace'), minimum_tasks=minimum_tasks, endpoint=endpoint))
        if len(audit.get('requests', [])) != 1:
            raise ValueError('Missing or duplicate HTTP request evidence')
        verify_request_binding(audit)
        audit['verified'] = True
    except BaseException as exc:
        failure = exc
        audit['error'] = f'{type(exc).__name__}: {exc}'
        if audit['response'] is None:
            if getattr(exc, 'error', None) is not None:
                audit['response'] = {'body': exc.error, 'status_code': getattr(exc, 'status_code', None)}
            elif getattr(exc, 'response', None) is not None:
                audit['response'] = {'body': exc.response.text, 'status_code': exc.response.status_code}
    finally:
        if log_path.exists():
            audit['log_sha256'] = file_hash(log_path)
        audit['cold_latency_ms'] = (time.perf_counter() - started) * 1000
        linked = save_audit(audit)
    if failure is not None:
        if isinstance(failure, (KeyboardInterrupt, SystemExit)):
            failure.audit = linked
            raise failure
        raise AuditError(str(failure), linked) from failure
    return value, linked


def systemone_call(model, state, questions, directory, purpose='measurement'):
    payload = {'model': model, 'state': state, 'questions': questions}
    def invoke(host, audit):
        with ollama.Client(host=host, timeout=600, trust_env=False,
                           event_hooks={'request': [request_recorder(audit, '/v1/systemone')]}) as client:
            return client.systemone(**payload).model_dump(mode='json')
    response, audit = local_call(model, payload, directory, invoke, minimum_tasks=len(questions), endpoint='/v1/systemone', purpose=purpose)
    return {**response, 'execution_audit': audit}


def verify_mistral_cache(raw):
    try:
        cached = raw['response_metadata']['token_usage']['prompt_tokens_details']['cached_tokens']
    except (KeyError, TypeError):
        raise ValueError('Mistral cached_tokens telemetry is missing') from None
    if isinstance(cached, bool) or not isinstance(cached, (int, float)) or cached != 0:
        raise ValueError(f'Mistral cached_tokens must be explicitly numeric zero; received {cached!r}')
    return cached


def hosted_call(model, payload, directory, invoke, purpose='measurement'):
    audit = new_audit(model, payload, directory, 'mistral', purpose)
    audit['prompt_cache_key'] = uuid.uuid4().hex
    value = None
    failure = None
    started = time.perf_counter()
    try:
        value = invoke(audit)
        audit['response'] = raw_response(value)
        audit['cached_tokens'] = verify_mistral_cache(audit['response'])
        requests = audit.get('requests', [])
        if len(requests) != 1 or requests[0].get('prompt_cache_key') != audit['prompt_cache_key']:
            raise ValueError('Missing fresh Mistral key/request evidence')
        audit['model_identity'] = {'requested_model': model, 'resolved_model': audit['response'].get('response_metadata', {}).get('model'),
                                   'response_id': audit['response'].get('id')}
        audit['model_sha256'] = fingerprint(audit['model_identity'])
        audit['runtime'] = {'provider': 'Mistral hosted', 'evidence_boundary': 'explicit cache telemetry'}
        audit['runtime_sha256'] = fingerprint(audit['runtime'])
        verify_request_binding(audit)
        audit['verified'] = True
    except BaseException as exc:
        failure = exc
        audit['error'] = f'{type(exc).__name__}: {exc}'
        response = getattr(exc, 'response', None)
        if response is not None:
            audit['response'] = {'status_code': response.status_code, 'body': response.text}
    finally:
        audit['cold_latency_ms'] = (time.perf_counter() - started) * 1000
        linked = save_audit(audit)
    if failure is not None:
        if isinstance(failure, (KeyboardInterrupt, SystemExit)):
            failure.audit = linked
            raise failure
        raise AuditError(str(failure), linked) from failure
    return value, linked


def validate_native_answers(response, questions):
    """Check native response completeness against its recorded question contract."""
    import math
    answers = response.get('answers')
    if not isinstance(answers, dict):
        raise ValueError('Accepted Jev response has invalid probabilities')
    for name, question in questions.items():
        answer = answers.get(name)
        if not isinstance(answer, dict):
            raise ValueError('Accepted Jev response has invalid probabilities')
        if question['type'] == 'noul':
            values = [answer.get('noul')]
        else:
            probabilities = answer.get('probabilities')
            criteria = question['criteria']
            keys = set(criteria) if isinstance(criteria, dict) else {str(i) for i in range(len(criteria))}
            if not isinstance(probabilities, dict) or set(probabilities) != keys:
                raise ValueError('Accepted Jev response has invalid probabilities')
            values = list(probabilities.values())
        if any(type(v) not in (int, float) or not math.isfinite(v) or not 0 <= v <= 1 for v in values):
            raise ValueError('Accepted Jev response has invalid probabilities')
        if question['type'] != 'noul' and sum(values) <= 0:
            raise ValueError('Accepted Jev response has invalid probabilities')


def revalidate_audit(link, raw=None, *, require_verified=True, allow_unverified_jev=False, directory=None):
    if directory is not None:
        from jev_bench.storage.paths import dataset_context
        with dataset_context(directory):
            return revalidate_audit(link, raw, require_verified=require_verified,
                                    allow_unverified_jev=allow_unverified_jev)
    if not isinstance(link, dict) or not link.get('audit_path'):
        raise ValueError('Missing linked execution audit')
    path = resolve_evidence(link['audit_path'])
    if file_hash(path) != link.get('audit_sha256'):
        raise ValueError('Execution audit fingerprint mismatch')
    audit = json.loads(path.read_text())
    expected_policy = POLICY
    if audit.get('provider') in ('azure-gpt', 'azure-claude'):
        from jev_bench.runtime.azure import AZURE_POLICY
        expected_policy = AZURE_POLICY
    if audit.get('policy') != expected_policy or audit.get('call_id') != link.get('call_id'):
        raise ValueError('Old or inconsistent execution audit')
    jev_exception = (allow_unverified_jev and audit.get('provider') == 'typesafe'
                     and audit.get('cache_exception') == JEV_CACHE_EXCEPTION
                     and audit.get('accepted') is True)
    if require_verified and not audit.get('verified') and not jev_exception:
        raise ValueError('Cache verification failed: ' + audit.get('error', 'unverified audit'))
    if fingerprint(audit['input']) != audit['input_sha256']:
        raise ValueError('Input evidence fingerprint mismatch')
    if audit.get('requests'):
        if len(audit['requests']) != 1 or fingerprint(audit['requests'][0]) != audit.get('request_sha256'):
            raise ValueError('Request evidence fingerprint mismatch')
        verify_request_binding(audit)
    if raw is not None and audit.get('verified'):
        if serial({k: v for k, v in raw.items() if k != 'execution_audit'}) != audit['response']:
            raise ValueError('Saved response differs from audited response')
    if audit['provider'] in ('azure-gpt', 'azure-claude'):
        from jev_bench.runtime.azure import revalidate
        revalidate(audit)
        return audit
    if audit['provider'] == 'typesafe':
        if raw is not None and serial({k: v for k, v in raw.items() if k != 'execution_audit'}) != audit['response']:
            raise ValueError('Saved response differs from audited Jev response')
        for name in ('runtime', 'model_identity'):
            if fingerprint(audit[name]) != audit['runtime_sha256' if name == 'runtime' else 'model_sha256']:
                raise ValueError('Jev identity fingerprint mismatch')
        if audit.get('verified'):
            raise ValueError('Jev cannot claim verified cache-free execution')
        if audit.get('cache_exception') is not None and audit['cache_exception'] != JEV_CACHE_EXCEPTION:
            raise ValueError('Invalid Jev cache exception')
        if audit.get('accepted'):
            if (audit.get('cache_exception') != JEV_CACHE_EXCEPTION
                    or audit['runtime'].get('cache_exception') != JEV_CACHE_EXCEPTION
                    or audit.get('cache_status') != 'unverified'
                    or audit['model'] != JEV_CACHE_EXCEPTION['model']
                    or audit.get('purpose') != 'measurement'
                    or len(audit.get('requests', [])) != 1
                    or audit.get('http_status') != 200
                    or audit.get('provider_error') or audit.get('budget_error') or audit.get('error')
                    or audit['response'].get('model') != audit['model']):
                raise ValueError('Invalid accepted Jev execution')
            validate_native_answers(audit['response'], audit['input']['questions'])
        return audit
    if audit['provider'] == 'ollama':
        if audit.get('log_sha256'):
            if file_hash(resolve_evidence(audit['log_path'])) != audit['log_sha256']:
                raise ValueError('Execution log fingerprint mismatch')
        elif audit.get('server_pid') or audit.get('verified'):
            raise ValueError('Missing execution log evidence')
        if audit.get('verified'):
            evidence = verify_cold_log(resolve_evidence(audit['log_path']).read_text(errors='replace'), minimum_tasks=audit['minimum_tasks'], endpoint=audit['endpoint'])
            if any(audit.get(k) != v for k, v in evidence.items()) or not audit.get('process_tree_stopped') or audit.get('models_before') != []:
                raise ValueError('Invalid local process/cache evidence')
            if audit['runtime'] != runtime_identity() or audit['model_identity'] != model_identity(audit['model']):
                raise ValueError('Runtime/model changed since measurement')
    elif audit['provider'] == 'mistral' and audit.get('verified'):
        verify_mistral_cache(audit['response'])
        if audit['requests'][0].get('prompt_cache_key') != audit['prompt_cache_key']:
            raise ValueError('Mistral cache key evidence differs')
    elif require_verified:
        raise ValueError('Unaudited provider')
    return audit


def enforce_result(result, model, payload, directory, *, allow_unverified_jev=False):
    raw = result.raw_response if isinstance(result.raw_response, dict) else {'response': serial(result.raw_response)}
    if not raw.get('execution_audit'):
        raw['execution_audit'] = failed_audit(model, payload, directory, result.error or 'Provider returned no audited execution')
        result.raw_response = raw
    try:
        revalidate_audit(raw['execution_audit'], raw, allow_unverified_jev=allow_unverified_jev)
    except (ValueError, OSError, KeyError) as exc:
        result.error = f'Cache verification failed: {exc}' + (f'; {result.error}' if result.error else '')
    return result


def audit_fields(result):
    audit = (result.raw_response or {}).get('execution_audit', {}) if isinstance(result.raw_response, dict) else {}
    verified = bool(audit.get('verified', False)) and not result.error.startswith('Cache verification failed:')
    exception_jev = (audit.get('provider') == 'typesafe'
                    and audit.get('cache_exception') == JEV_CACHE_EXCEPTION
                    and not result.error.startswith('Cache verification failed:'))
    return {**{name: audit.get(name) for name in AUDIT_COLUMNS}, 'cache_verified': verified,
            'failure_kind': ('cache_verification' if not verified and not exception_jev else 'schema' if result.error else '')}


def revalidate_row(row, definition=None, directory=None):
    from jev_bench.storage.verification import revalidate_row as verify
    return verify(row, definition, directory)
