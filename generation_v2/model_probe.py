"""Explicit, single-model paid probes; no fallback, config writes or memory writes."""
import asyncio
import json
import re
import sqlite3
import time
from dataclasses import replace
from contextlib import closing

from ..call_policy import validate_overrides
from ..model_tasks import FAMILIES
from .bridge import ScheduledTaskProvider
from .integration import service_for
from .mind_gateway import provider_route
from .store import TaskSpec


def call_records(plugin):
    from .workbench import ledger_path
    service = getattr(plugin, '_generation_v2_service', None)
    path = service.store.path if service else ledger_path(plugin)
    if path is None or not path.is_file(): return []
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
        db.execute('PRAGMA query_only=ON')
        rows = db.execute("""SELECT t.spec,a.started,a.outcome,a.metadata FROM attempts a
            JOIN tasks t ON t.id=a.task_id WHERE t.id LIKE 'model-probe:%'
            ORDER BY a.started DESC LIMIT 50""").fetchall()
    result = []
    for spec, started, outcome, meta in rows:
        spec = json.loads(spec); meta = json.loads(meta)
        result.append({'ts': started, 'task': '单模型测试 · '+spec['category'],
            'provider': spec['scope'].removeprefix('model-probe:'),
            'outcome': 'success' if outcome == 'succeeded' else meta.get('kind', outcome),
            'provider_ms': meta.get('elapsed_ms', 0), 'job_id': spec['job_id']})
    return result


def provider_options(plugin):
    options = []
    try:
        providers = plugin.context.get_all_providers()
    except (AttributeError, TypeError):
        providers = []
    for provider in providers:
        try:
            meta = provider.meta()
            ident = str(meta.id)
            if ident and callable(getattr(provider, 'text_chat', None)):
                options.append({'value': ident, 'label': 'Astr · ' + ident})
        except (AttributeError, TypeError):
            continue
    return options


async def probe(plugin, body):
    if body.get('confirm_model_calls') is not True:
        raise ValueError('请确认本次测试可能产生费用')
    provider_id = body.get('provider_id')
    task = body.get('task', 'episode_extract')
    token = body.get('request_id', '')
    if not isinstance(provider_id, str) or not provider_id or task not in FAMILIES:
        raise ValueError('请选择具体模型和任务')
    if not isinstance(token, str) or not re.fullmatch(r'[a-zA-Z0-9_-]{8,80}', token):
        raise ValueError('invalid test request id')
    timeout = body.get('timeout', 90)
    if type(timeout) not in (int, float) or not 5 <= timeout <= 300:
        raise ValueError('首次输出等待或非流式总超时必须为 5..300 秒')
    prompt = body.get('prompt', '请只回复：连接测试成功。')
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 20000:
        raise ValueError('测试输入必须为 1..20000 字符')
    policies = validate_overrides({task: body.get('policy', {})})
    if provider_id.startswith('external:'):
        from ..external_models import ExternalSingleProvider
        model_id = provider_id.split(':', 1)[1]
        registry = plugin._external_models
        item = registry.model(model_id)
        if not item or not item['enabled'] or not registry._secret(model_id).load():
            raise ValueError('外置模型未启用或未配置密钥')
        provider = ExternalSingleProvider(registry, model_id)
    else:
        provider = plugin.context.get_provider_by_id(provider_id)
        if provider is None: raise ValueError('Astr 模型不可用')
    lock = getattr(plugin, '_model_probe_lock', None)
    if lock is None:
        lock = asyncio.Lock(); plugin._model_probe_lock = lock
    if lock.locked(): raise ValueError('已有模型测试正在执行，请等待完成')
    async with lock:
        service = await service_for(plugin)
        route = provider_route(plugin, provider, provider_id)
        adapter = type(route.adapter)(provider, policies=policies)
        route = replace(route, adapter=adapter)
        job = 'model-probe:' + token
        # Repeated submission cannot allocate a new budget for the same test.
        with service.store.connect() as db:
            existing = db.execute('SELECT 1 FROM tasks WHERE id=?', (job,)).fetchone()
        if existing: raise ValueError('该测试已提交，请查看原结果，不要重复发送')
        spec = TaskSpec(job, job, task, 'model-probe:'+provider_id, 'model-probe-v1', 'text-v1', route.digest,
                        time.time()+(1800 if policies[task]['stream'] else timeout+30), max_attempts=1)
        bound = ScheduledTaskProvider(service.scheduler, spec, route, job_cap=1)
        text = ''; success = False; failure = ''
        try:
            result = await bound.text_chat(prompt=prompt, timeout=timeout)
            text = result.completion_text; success = True
        except asyncio.CancelledError:
            await service.scheduler.cancel_job(job)
            raise
        except Exception as exc:
            failure = type(exc).__name__
        attempts = service.store.attempts_for(job)
        last = attempts[-1] if attempts else {}
        meta = json.loads(last.get('metadata', '{}'))
        diagnostics = meta.get('diagnostics', meta.get('capabilities', {}).get('diagnostics', {}))
        allowed = {'stream','controls_supported','max_tokens','finish_reason','first_event_ms',
                   'first_content_ms','reasoning_chars','content_chars','reasoning_tokens',
                   'completion_tokens','context_window_tokens','input_token_upper_estimate','context_estimate_method',
                   'timeout_phase','idle_timeout_seconds'}
        return {'success': success, 'job_id': job, 'provider_id': provider_id,
                'attempts': len(attempts), 'elapsed_ms': round(meta.get('elapsed_ms', 0)),
                'error_kind': meta.get('kind', failure), 'text': text[:12000], 'text_truncated': len(text)>12000,
                'usage': meta.get('usage', {}), 'policy': policies[task],
                'diagnostics': {k:v for k,v in diagnostics.items() if k in allowed and isinstance(v,(str,int,float,bool,type(None)))}}
