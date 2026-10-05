"""One physical request, bounded streaming, safe response diagnostics."""
import asyncio
import copy
import json
import inspect
import time
from types import SimpleNamespace


class ResponseFailure(RuntimeError):
    def __init__(self, kind, diagnostics):
        self.kind = kind
        self.diagnostics = dict(diagnostics)
        self.finish_reason = diagnostics.get('finish_reason')
        self.completion_tokens = diagnostics.get('completion_tokens')
        self.reasoning_tokens = diagnostics.get('reasoning_tokens')
        super().__init__(kind)


class Collector:
    def __init__(self, policy):
        self.started = time.monotonic()
        self.parts = []
        self.meta = {'finish_reason': None, 'reasoning_chars': 0, 'content_chars': 0,
                     'first_event_ms': None, 'first_content_ms': None,
                     'max_tokens': policy['max_tokens'], 'stream': policy['stream']}
        self.usage = {}

    def feed(self, data, stream):
        if self.meta['first_event_ms'] is None:
            self.meta['first_event_ms'] = round((time.monotonic()-self.started)*1000)
        usage = data.get('usage') or {}
        for k in ('prompt_tokens', 'completion_tokens', 'total_tokens'):
            if isinstance(usage.get(k), int): self.usage[k] = usage[k]
        details = usage.get('completion_tokens_details') or {}
        if isinstance(details.get('reasoning_tokens'), int):
            self.usage['reasoning_tokens'] = details['reasoning_tokens']
        choices = data.get('choices') or []
        if not choices: return
        choice = choices[0]
        if choice.get('finish_reason'): self.meta['finish_reason'] = choice['finish_reason']
        msg = choice.get('delta' if stream else 'message') or {}
        reasoning = msg.get('reasoning_content') or ''
        self.meta['reasoning_chars'] += len(reasoning) if isinstance(reasoning, str) else 0
        content = msg.get('content') or ''
        if isinstance(content, list):
            content = ''.join(x.get('text', '') for x in content if isinstance(x, dict) and isinstance(x.get('text', ''), str))
        if not isinstance(content, str): raise ResponseFailure('invalid_response', self.meta)
        if content:
            if self.meta['first_content_ms'] is None:
                self.meta['first_content_ms'] = round((time.monotonic()-self.started)*1000)
            self.parts.append(content)
            self.meta['content_chars'] += len(content)
        if self.meta['content_chars'] + self.meta['reasoning_chars'] > 4_000_000:
            raise ResponseFailure('response_too_large', self.meta)

    def finish(self):
        self.meta.update(self.usage)
        reason = self.meta['finish_reason']
        if reason != 'stop':
            raise ResponseFailure('output_truncated' if reason == 'length' else 'incomplete_response', self.meta)
        text = ''.join(self.parts).strip()
        if not text: raise ResponseFailure('empty_output', self.meta)
        return SimpleNamespace(completion_text=text, raw_completion=SimpleNamespace(
            usage=SimpleNamespace(**self.usage)), call_diagnostics=dict(self.meta))


def request_options(policy, model):
    # Vendor-only reasoning fields must not leak into unrelated model requests.
    result = {'max_tokens': policy['max_tokens']}
    if 'deepseek' in model.lower() and policy['thinking'] != 'inherit':
        result['thinking'] = {'type': policy['thinking']}
        if policy['thinking'] == 'enabled': result['reasoning_effort'] = policy['reasoning_effort']
    return result


class ProgressGate:
    """First useful output deadline, then silence since meaningful text only."""
    def __init__(self, deadline, idle, collector):
        self.deadline, self.idle, self.collector = deadline, idle, collector
        self.seen = False

    def feed(self, data, stream=True):
        self.collector.feed(data, stream)
        choices = data.get('choices') or []
        message = (choices[0].get('delta' if stream else 'message') or {}) if choices else {}
        useful = any(isinstance(message.get(k), str) and message[k].strip()
                     for k in ('content', 'reasoning_content'))
        if isinstance(message.get('content'), list):
            useful |= any(isinstance(x, dict) and isinstance(x.get('text'), str) and x['text'].strip()
                          for x in message['content'])
        if stream and useful:
            self.seen = True
            self.deadline.reschedule(asyncio.get_running_loop().time() + self.idle)
            self.collector.meta['timeout_phase'] = 'progress_idle'
            self.collector.meta['idle_timeout_seconds'] = self.idle


def check_context(messages, policy, collector, model_limit=0):
    limits = [v for v in (policy.get('context_window_tokens', 0), model_limit) if v > 0]
    limit = min(limits) if limits else 0
    # Conservative byte-based estimate, explicitly not a vendor tokenizer.
    estimated = len(json.dumps(messages, ensure_ascii=False).encode('utf-8')) + 64
    collector.meta.update(context_window_tokens=limit, input_token_upper_estimate=estimated,
                          context_estimate_method='utf8_bytes_plus_64')
    if limit and estimated + policy['max_tokens'] > limit:
        raise ResponseFailure('context_limit', collector.meta)


async def invoke(provider, request, timeout, policy):
    from ..direct_llm import OpenAICompatibleTextClient, DirectRequestError, DirectHTTPError
    from ..external_models import ExternalSingleProvider, ExternalModelPoolProvider
    collector = Collector(policy)
    model_limit = 0
    if isinstance(provider, ExternalModelPoolProvider):
        models = provider.registry._ordered_models(provider.preferred_id)
        if not models: raise ValueError('external model unavailable')
        provider = ExternalSingleProvider(provider.registry, models[0]['id'])
    if isinstance(provider, ExternalSingleProvider):
        item = provider.registry.model(provider.model_id)
        model_limit = item.get('context_window_tokens', 0)
        policy = {**policy, 'max_tokens': min(policy['max_tokens'], item['max_output_tokens'])}
        collector.meta['max_tokens'] = policy['max_tokens']
        provider = provider.registry._client(item)
    try:
        async with asyncio.timeout(timeout) as deadline:
            gate = ProgressGate(deadline, policy['idle_timeout'], collector)
            collector.meta['timeout_phase'] = 'first_progress' if policy['stream'] else 'total'
            if isinstance(provider, OpenAICompatibleTextClient):
                messages = copy.deepcopy(request.get('contexts') or [])
                if request.get('system_prompt'): messages.insert(0, {'role': 'system', 'content': request['system_prompt']})
                messages.append({'role': 'user', 'content': request['prompt']})
                payload = {'model': provider.model, 'messages': messages,
                           'temperature': provider.temperature, 'stream': policy['stream'],
                           **request_options(policy, provider.model)}
                if policy['stream']: payload['stream_options'] = {'include_usage': True}
                check_context(messages, policy, collector, model_limit)
                import aiohttp
                session = await provider._session()
                async with session.post(provider._chat_url(), json=payload,
                        headers={'Authorization': 'Bearer '+provider.api_key, 'Content-Type': 'application/json'},
                        timeout=aiohttp.ClientTimeout(total=None if policy['stream'] else timeout,
                            connect=min(timeout, 30), sock_read=None if policy['stream'] else policy['idle_timeout']),
                        allow_redirects=False) as response:
                    if response.status >= 400:
                        if response.status == 429 or response.status >= 500:
                            raise DirectHTTPError(response.status, response.headers.get('Retry-After'))
                        raise DirectRequestError(response.status)
                    if not policy['stream']:
                        collector.feed(await response.json(), False)
                    else:
                        event = []; done = False
                        async for line in response.content:
                            if len(line) > 2_000_000: raise ResponseFailure('response_too_large', collector.meta)
                            line = line.decode('utf-8').rstrip('\r\n')
                            if line.startswith('data:'): event.append(line[5:].lstrip())
                            elif not line and event:
                                raw = '\n'.join(event); event = []
                                if raw == '[DONE]': done = True; break
                                data = json.loads(raw)
                                if 'error' in data: raise ResponseFailure('stream_error', collector.meta)
                                gate.feed(data)
                        if not done: raise ResponseFailure('incomplete_stream', collector.meta)
            else:
                # Reuse Astr's request assembly and configured SDK client, without
                # mutating shared provider settings or using its nested retry loop.
                if not all(hasattr(provider, k) for k in ('client', '_prepare_chat_payload', 'provider_config')):
                    from .adapters import dispatch_once, EmptyOutputError
                    params = inspect.signature(provider.text_chat).parameters
                    variadic = any(p.kind == p.VAR_KEYWORD for p in params.values())
                    kwargs = dict(request)
                    check_context({'contexts': request.get('contexts', []),
                                   'system_prompt': request.get('system_prompt', ''),
                                   'prompt': request.get('prompt', '')}, policy, collector, model_limit)
                    for key, value in (('max_tokens', policy['max_tokens']), ('request_max_retries', 0)):
                        if key in params or variadic: kwargs[key] = value
                    timeout_key = 'request_timeout' if 'request_timeout' in params else 'timeout'
                    if timeout_key in params or variadic: kwargs[timeout_key] = timeout
                    raw = await dispatch_once(provider, kwargs, timeout)
                    if not str(getattr(raw, 'completion_text', '') or '').strip():
                        raise EmptyOutputError('provider returned no final text')
                    raw.call_diagnostics = {**collector.meta, 'stream': False, 'controls_supported': False,
                        'max_tokens': policy['max_tokens']}
                    return raw
                payload, _ = await provider._prepare_chat_payload(**request)
                check_context(payload['messages'], policy, collector)
                extra = copy.deepcopy(provider.provider_config.get('custom_extra_body') or {})
                options = request_options(policy, str(payload['model']))
                for key in ('max_tokens', 'max_completion_tokens', 'stream', 'stream_options'):
                    extra.pop(key, None)
                if policy['thinking'] != 'inherit' and 'deepseek' in str(payload['model']).lower():
                    extra.pop('thinking', None); extra.pop('reasoning_effort', None)
                if extra.get('top_p') == 0:
                    raise ValueError('top_p must be greater than zero')
                for key, value in options.items():
                    if key == 'max_tokens': payload[key] = value
                    else: extra[key] = value
                import httpx
                sdk_timeout = httpx.Timeout(connect=min(timeout, 30), read=None, write=30, pool=30) if policy['stream'] else timeout
                client = provider.client.with_options(timeout=sdk_timeout, max_retries=0)
                result = await client.chat.completions.create(**payload, extra_body=extra,
                    stream=policy['stream'], **({'stream_options': {'include_usage': True}} if policy['stream'] else {}))
                if not policy['stream']:
                    collector.feed(result.model_dump(), False)
                else:
                    try:
                        iterator = aiter(result)
                        while True:
                            try: chunk = await anext(iterator)
                            except StopAsyncIteration: break
                            gate.feed(chunk.model_dump())
                    finally:
                        await result.close()
        return collector.finish()
    except TimeoutError as exc:
        exc.diagnostics = {**collector.meta, **collector.usage, 'timeout_seconds': timeout}
        exc.partial_text=''.join(collector.parts)[:1000000]
        raise
    except Exception as exc:
        exc.partial_text=''.join(collector.parts)[:1000000]
        if not isinstance(exc, ResponseFailure):
            exc.diagnostics = {**collector.meta, **collector.usage}
        raise
