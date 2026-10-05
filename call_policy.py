"""Validated task-level generation controls, independent of credentials."""
import copy
from .model_tasks import task_key, FAMILIES


def defaults(task):
    key = task_key(task)
    creative = key in {'diary_write', 'compress_llm', 'xinchao_dream', 'xinchao_proactive', 'xinchao_daytime'}
    return {'stream': True, 'thinking': 'disabled' if creative else 'enabled',
            'reasoning_effort': 'low', 'max_tokens': 16384 if creative else 32768,
            'idle_timeout': 60, 'context_window_tokens': 0}


def validate_overrides(value):
    if not isinstance(value, dict):
        raise ValueError('task_call_policies must be an object')
    out = {}
    for task, settings in value.items():
        if task not in FAMILIES or not isinstance(settings, dict):
            raise ValueError('unknown task call policy')
        clean = defaults(task)
        if set(settings) - set(clean):
            raise ValueError('unknown call policy field')
        clean.update(settings)
        if type(clean['stream']) is not bool or clean['thinking'] not in ('inherit', 'enabled', 'disabled'):
            raise ValueError('invalid stream/thinking mode')
        if clean['reasoning_effort'] not in ('low', 'high', 'max'):
            raise ValueError('invalid reasoning effort')
        if type(clean['max_tokens']) is not int or not 256 <= clean['max_tokens'] <= 131072:
            raise ValueError('output budget must be 256..131072')
        if type(clean['idle_timeout']) is not int or not 10 <= clean['idle_timeout'] <= 300:
            raise ValueError('stream idle timeout must be 10..300 seconds')
        if type(clean['context_window_tokens']) is not int or not 0 <= clean['context_window_tokens'] <= 2097152:
            raise ValueError('context window must be 0..2097152 tokens')
        out[task] = clean
    return out


def resolve(overrides, task):
    return copy.deepcopy(overrides.get(task_key(task), defaults(task)))
