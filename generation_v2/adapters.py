"""Single-dispatch, non-streaming adapters. Scheduling belongs to the caller."""
from __future__ import annotations

import asyncio
import copy
import inspect
import time
from dataclasses import dataclass, field
from typing import Any

from .contracts import effective_timeout


class CapabilityError(RuntimeError):
    pass


class EmptyOutputError(ValueError):
    pass


async def dispatch_once(provider, kwargs, timeout):
    """Shared legacy/V2 boundary. Does not retry, spawn loops or mutate clients."""
    async with asyncio.timeout(effective_timeout(timeout, 180)):
        result = provider.text_chat(**kwargs)
        return await result if inspect.isawaitable(result) else result


@dataclass(frozen=True)
class Failure:
    kind: str
    http_status: int | None = None
    retry_after: float | None = None
    possibly_billed: bool = False


def classify_error(exc: BaseException) -> Failure:
    # Never persist str(exc): upstream exceptions may contain keys or prompts.
    status = getattr(exc, 'status_code', getattr(exc, 'status', None))
    status = status if isinstance(status, int) else None
    response = getattr(exc, 'response', None)
    headers = getattr(response, 'headers', {}) or {}
    try:
        retry_after = max(0.0, min(86400.0, float(headers.get('retry-after'))))
    except (ValueError, TypeError):
        retry_after = None
    name = type(exc).__name__.lower()
    from .transport import ResponseFailure
    if isinstance(exc, ResponseFailure):
        return Failure(exc.kind, possibly_billed=exc.kind != 'context_limit')
    if isinstance(exc, asyncio.CancelledError):
        kind = 'cancelled'
    elif isinstance(exc, (TimeoutError, asyncio.TimeoutError)) or 'timeout' in name:
        kind = 'timeout'
    elif status in (401, 403):
        kind = 'authentication'
    elif status == 404:
        kind = 'route_configuration'
    elif status == 429:
        kind = 'rate_limit'
    elif status is not None and status >= 500:
        kind = 'overload'
    elif status is not None and status >= 400:
        kind = 'invalid_request'
    elif isinstance(exc, CapabilityError):
        kind = 'capability'
    elif isinstance(exc, EmptyOutputError) or 'emptyfinal' in name:
        kind = 'empty_output'
    elif isinstance(exc, ConnectionError) or any(n in name for n in ('connection', 'connector', 'disconnect', 'clientpayload')):
        kind = 'transport'
    else:
        kind = 'task_error'
    return Failure(kind, status, retry_after, kind in {'timeout', 'transport', 'cancelled', 'empty_output'})


def explicit_parameter(fn: Any, name: str) -> bool:
    try:
        p = inspect.signature(fn).parameters.get(name)
        return p is not None and p.kind in (p.KEYWORD_ONLY, p.POSITIONAL_OR_KEYWORD)
    except (TypeError, ValueError):
        return False


@dataclass
class CallResult:
    raw: Any
    text: str
    elapsed_ms: float
    effective_timeout: float
    capabilities: dict
    usage: dict = field(default_factory=dict)


class AstrAdapter:
    """**kwargs is not evidence that a provider actually disables retries."""

    def __init__(self, provider: Any, *, strict: bool = True, policies=None):
        self.provider = provider
        self.strict = strict
        self.explicit_policy_tasks = set(policies or {})
        self.policies = copy.deepcopy(policies)
        if policies is not None:
            import hashlib, json
            from ..model_tasks import FAMILIES
            from ..call_policy import resolve
            self.policies = {task: resolve(policies, task) for task in FAMILIES}
            self.policy_digest = hashlib.sha256(json.dumps(self.policies, sort_keys=True).encode()).hexdigest()

    def capabilities(self) -> dict:
        fn = self.provider.text_chat
        timeout_key = next((k for k in ('request_timeout', 'timeout') if explicit_parameter(fn, k)), None)
        return {'timeout_parameter': timeout_key,
                'retry_control_exposed': explicit_parameter(fn, 'request_max_retries'),
                'retry_semantics': 'provider_dependent; verified Astr OpenAI uses total attempts',
                'first_token_ms': None, 'stream_idle_ms': None, 'streaming': False}

    async def call(self, *, prompt: str, contexts: list | None = None,
                   system_prompt: str = '', timeout: float = 180,
                   max_tokens: int | None = None, task_label: str = '') -> CallResult:
        timeout = effective_timeout(timeout, 180)
        if self.policies is not None:
            from ..call_policy import resolve
            from .transport import invoke
            policy = resolve(self.policies, task_label)
            started = time.monotonic()
            raw = await invoke(self.provider, {'prompt': prompt, 'contexts': contexts or [],
                'system_prompt': system_prompt}, timeout, policy)
            usage = vars(raw.raw_completion.usage) if getattr(getattr(raw, 'raw_completion', None), 'usage', None) else {}
            return CallResult(raw, raw.completion_text, (time.monotonic()-started)*1000,
                timeout, {'streaming': policy['stream'], 'request_policy': policy,
                          'diagnostics': raw.call_diagnostics}, usage)
        caps = self.capabilities()
        if self.strict and (not caps['retry_control_exposed'] or not caps['timeout_parameter']):
            raise CapabilityError('provider lacks explicit single-attempt/timeout controls')
        kwargs = {'prompt': prompt, 'contexts': copy.deepcopy(contexts or []),
                  'system_prompt': system_prompt}
        if caps['retry_control_exposed']:
            kwargs['request_max_retries'] = 0
        if caps['timeout_parameter']:
            kwargs[caps['timeout_parameter']] = timeout
        for key, value in (('max_tokens', max_tokens), ('task_label', task_label)):
            if value is not None and explicit_parameter(self.provider.text_chat, key):
                kwargs[key] = value
        started = time.monotonic()
        raw = await dispatch_once(self.provider, kwargs, timeout)
        text = str(getattr(raw, 'completion_text', '') or '')
        if not text.strip():
            raise EmptyOutputError('provider returned no final text')
        raw_usage = getattr(getattr(raw, 'raw_completion', None), 'usage', None)
        usage = {k: v for k in ('prompt_tokens', 'completion_tokens', 'total_tokens')
                 if isinstance(v := getattr(raw_usage, k, None), int)}
        return CallResult(raw, text, (time.monotonic() - started) * 1000, timeout, caps, usage)


class DirectAdapter(AstrAdapter):
    """Wrap the plugin DirectLLMClient, not the retrying external model pool."""

    def capabilities(self) -> dict:
        result = super().capabilities()
        result['retry_semantics'] = 'DirectLLMClient: 0 disables internal retry'
        return result


def production_route(route, profile):
    """New workflows only; explicit task controls and legacy route contracts win."""
    if not route or profile == 'legacy' or not isinstance(route.adapter, AstrAdapter):
        return route
    if profile != 'balanced-v1':
        raise ValueError('unknown production call profile')
    from dataclasses import replace
    from ..call_policy import defaults
    adapter = route.adapter
    if adapter.policies is None:
        return route  # Do not invent transport capabilities for third-party providers.
    policies = copy.deepcopy(adapter.policies)
    for task, budget in (('episode_extract', 8192), ('narrative_plan', 4096),
                         ('diary_write', 12288), ('diary_review', 8192)):
        if task not in adapter.explicit_policy_tasks:
            policies[task] = {**defaults(task), 'thinking': 'disabled', 'max_tokens': budget}
    return replace(route, adapter=type(adapter)(adapter.provider, strict=adapter.strict, policies=policies))
