"""Bind saved suite measurements to immutable execution evidence."""
import hashlib
import json
from jev_bench.runtime.execution import (revalidate_audit, AUDIT_COLUMNS, JEV_CACHE_EXCEPTION)

def revalidate_row(row, definition=None, directory=None):
    if directory is not None:
        from jev_bench.storage.paths import dataset_context
        with dataset_context(directory):
            return revalidate_row(row, definition)
    raw = json.loads(row['raw_response_json'])
    exception = (definition or {}).get('jev_cache_exception') == JEV_CACHE_EXCEPTION
    audit = revalidate_audit(raw.get('execution_audit'), raw, allow_unverified_jev=exception)
    for name in AUDIT_COLUMNS:
        if name in {'cache_verified', 'failure_kind'}:
            continue
        expected = raw['execution_audit'].get(name, '')
        if str(row.get(name) or '') != str(expected or ''):
            raise ValueError('Row differs from its linked execution audit: ' + name)
    expected_cache = 'false' if audit.get('accepted') and audit['provider'] == 'typesafe' else 'true'
    if str(row.get('cache_verified')).lower() != expected_cache or row.get('failure_kind'):
        raise ValueError('Successful row has invalid cache status')
    if audit['provider'] == 'typesafe' and audit.get('accepted'):
        from jev_bench.providers.jev_contract import count
        usage = raw.get('usage') if isinstance(raw.get('usage'), dict) else {}
        for field in ('input_tokens', 'output_tokens'):
            value = row.get(field)
            if (float(value) if value not in ('', None) else None) != count(usage.get(field)):
                raise ValueError('Jev token usage differs from audited response')
    if definition is not None:
        if audit['provider'] == 'typesafe' and audit['input'].get('questions') != definition.get('prompts', {}).get('systemone_questions'):
            raise ValueError('Saved Jev questions differ from experiment metadata')
        if audit['input'].get('prefix_strategy') != definition.get('prefix_strategy'):
            raise ValueError('Saved audit belongs to a different prefix strategy')
        if definition.get('prefix_strategy') and audit['input'].get('original_system_prompt') != definition.get('prompts', {}).get('llm_system'):
            raise ValueError('Prefix experiment changed the original system prompt')
        configuration = definition.get('model_configuration', {}).get(row['model'])
        if configuration is not None:
            expected_model = configuration.get('inference_model', row['model'])
            if audit['model'] != expected_model:
                raise ValueError('Saved audit belongs to a different model/configuration')
            if audit['provider'] in ('azure-gpt', 'azure-claude'):
                if audit['input'].get('configuration') != configuration or audit['input'].get('schema') != definition.get('schema'):
                    raise ValueError('Azure settings or schema differ from experiment metadata')
                if audit['policy'] != definition.get('execution_policy'):
                    raise ValueError('Azure policy differs from experiment metadata')
                for field in ('input_tokens', 'output_tokens'):
                    if float(row[field]) != audit['usage'][field]:
                        raise ValueError('Azure token usage differs from audited response')
        if definition.get('cases'):
            case = next((case for case in definition['cases'] if case['case_id'] == int(row['case_id'])), None)
            if case is None or row['message'] != case['message']:
                raise ValueError('Saved row differs from the experiment case')
            expected_input = case['message']
        else:
            expected_input = None
        payload = audit['input']
        actual_input = payload.get('state') if 'state' in payload else payload.get('messages', [[None,None]])[-1][1]
        if expected_input is not None and actual_input != expected_input:
            raise ValueError('Saved audit belongs to a different question/input')
        input_hash = definition.get('case_input', {}).get('sha256')
        if input_hash and (not isinstance(actual_input, str) or hashlib.sha256(actual_input.encode()).hexdigest() != input_hash):
            raise ValueError('Saved audit belongs to a different compact packet')
    # Bind the accepted CSV probabilities to the actual audited provider response.
    if isinstance(raw.get("answers"), dict):
        answers = raw["answers"]
        if "case_id" in row:
            probabilities = {"requires_web_probability": answers["requires_web"]["noul"],
                             "is_safe_probability": answers["is_safe"]["noul"]}
            for field in ["route", "freshness"]:
                probabilities.update({f"{field}_{name}_probability": value
                                      for name, value in answers[field]["probabilities"].items()})
        else:
            probabilities = {name: answer["noul"] for name, answer in answers.items()}
    else:
        from jev_bench.providers.parsing import raw_values
        values = raw_values(raw)
        if not values:
            raise ValueError('Saved response has no structured probability fields')
        probabilities = {}
        for name, value in values.items():
            if isinstance(value, dict):
                probabilities.update({f"{name.removesuffix('_probabilities')}_{k}_probability": v for k, v in value.items()})
            else:
                probabilities[name] = value
    for name, value in probabilities.items():
        if name not in row or float(row[name]) != value:
            raise ValueError("Saved probabilities differ from audited response: " + name)
    return audit
