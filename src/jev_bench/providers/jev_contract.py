"""Pure hosted Jev contract helpers, usable by offline readers without its SDK."""
import json
from pathlib import Path

from jev_bench.runtime.execution import JEV_CACHE_EXCEPTION

JEV_MODEL = 'jev-1.13.0'
MAX_INPUT_TOKENS = 65_536


def count(value):
    return value if type(value) is int and 0 <= value <= MAX_INPUT_TOKENS else None


def cache_exception_for_root(root):
    path = Path(root) / 'run_plan.json'
    if not path.exists():
        return None
    plan = json.loads(path.read_text(encoding="utf-8"))
    exception = plan.get('jev_cache_exception')
    if exception is not None and (exception != JEV_CACHE_EXCEPTION
            or plan.get('kind') != 'jev_api' or plan.get('models') != [JEV_MODEL]):
        raise ValueError('Invalid Jev cache exception in target manifest')
    return exception
