"""One scheduling owner for mind calls; never wrapped in legacy retry logic."""
import asyncio
import time
import uuid
from urllib.parse import urlsplit

from .adapters import AstrAdapter, DirectAdapter
from .bridge import ScheduledTaskProvider
from .contracts import effective_timeout
from .integration import service_for
from .scheduler import Route
from .store import TaskSpec


def provider_route(plugin, provider, fallback_identity=''):
    from ..direct_llm import OpenAICompatibleTextClient
    from ..external_models import ExternalModelPoolProvider, ExternalSingleProvider
    if isinstance(provider, ExternalModelPoolProvider):
        models=provider.registry._ordered_models(provider.preferred_id)
        if not models: raise ValueError('external model unavailable')
        provider=ExternalSingleProvider(provider.registry,str(models[0]['id']))
    direct=isinstance(provider,(OpenAICompatibleTextClient,ExternalSingleProvider))
    meta=provider.meta() if callable(getattr(provider,'meta',None)) else None
    identity=str(getattr(provider,'provider_id','') or getattr(meta,'id','') or fallback_identity)
    if isinstance(provider,ExternalSingleProvider):
        url=(provider.registry.model(provider.model_id) or {}).get('base_url','')
    else:
        url=getattr(provider,'base_url','') or getattr(provider,'provider_config',{}).get('api_base','')
    parsed=urlsplit(str(url)); host=parsed.hostname or 'unknown-provider-endpoint'
    endpoint=host+(':'+str(parsed.port) if parsed.port else '')
    policies = getattr(getattr(plugin, '_external_models', None), 'task_call_policies', {})
    return Route(identity,'direct' if direct else 'astr',endpoint,host,
                 DirectAdapter(provider, policies=policies) if direct else AstrAdapter(provider, policies=policies))


async def call(plugin,provider,*,prompt,contexts,system_prompt,timeout,label,allow_followup=True):
    timeout=effective_timeout(timeout,180)
    service=await service_for(plugin)
    primary=provider_route(plugin,provider); backup=None
    registry=getattr(plugin,'_external_models',None)
    if allow_followup and primary.kind=='astr' and registry:
        selected=registry.followup_provider(label)
        if selected is not None: backup=provider_route(plugin,selected)
    job='mind:'+uuid.uuid4().hex
    # Independent calls cannot share a result simply because their text happens to match.
    spec=TaskSpec(job,job,label,'mind-task:'+job,'mind-rc3','text-v1','single-owner-v1',
                  time.time()+timeout*3+30,priority=5)
    bound=ScheduledTaskProvider(service.scheduler,spec,primary,backup,job_cap=3)
    log=getattr(plugin,'_log_event',None)
    try:
        result=await bound.text_chat(prompt=prompt,contexts=contexts,system_prompt=system_prompt,timeout=timeout)
        if callable(log): log('xinchao','统一调度完成',{'task':label,'job_id':job})
        return result
    except asyncio.CancelledError:
        await service.scheduler.cancel_job(job)
        raise
    except Exception:
        if callable(log): log('xinchao','统一调度失败，记录已保留',{'task':label,'job_id':job})
        raise
