"""Explicit draft publication, readiness and safe test-mode controls."""
import asyncio
import json

from .integration import (ConsumerIndex, StateConsumer, ensure_handoffs, pending,
                          resume_delivery, service_for)
from .publishing import Publisher, MemosPublisherAdapter
from .recovery import saved_workflow
from .store import ConflictError


def mode_settings(plugin, request):
    mode=request.get('mode')
    if mode not in ('automatic','compatibility'):
        raise ValueError('请选择自动新生产或兼容生产')
    if mode=='compatibility': return {'generation_v2_enable':False}
    if request.get('confirm_model_calls') is not True or request.get('confirm_delivery') is not True:
        raise ValueError('启用前必须确认模型费用和自动写入 Memos')
    if request.get('confirm_memos_contract') is not True:
        raise ValueError('必须确认 Memos 支持固定 ID 和事件日期写入')
    if getattr(plugin,'_episodes',None) is None or getattr(plugin,'_vec',None) is None or getattr(plugin,'_memos',None) is None:
        raise ValueError('原文库、索引或 Memos 客户端尚未初始化')
    if not str(getattr(plugin,'character_name','') or '').strip():
        raise ValueError('请先配置角色名称')
    return {'generation_v2_enable':True,'generation_v2_memos_capabilities_confirmed':True}


def readiness(plugin):
    enabled=bool(getattr(plugin,'generation_v2_enable',False))
    confirmed=bool(getattr(plugin,'generation_v2_memos_capabilities_confirmed',False))
    return {'mode':'automatic' if enabled and confirmed else 'blocked' if enabled else 'compatibility',
            'model_test_verified':False,'memos_contract_confirmed':confirmed,
            'archive_ready':getattr(plugin,'_episodes',None) is not None,
            'index_ready':getattr(plugin,'_vec',None) is not None,
            'mind_scheduler':bool(getattr(plugin,'generation_v2_mind_enable',False)),
            'drafts_do_not_publish':True}


async def publish_draft(plugin,request):
    if request.get('confirm_delivery') is not True: raise ValueError('delivery confirmation required')
    if (not getattr(plugin,'generation_v2_memos_capabilities_confirmed',False)
            and request.get('confirm_memos_contract') is not True):
        raise ConflictError('confirm Memos fixed-ID/event-date support first')
    if any(getattr(plugin,name,None) is None for name in ('_memos','_vec','_episodes')):
        initialize=getattr(plugin,'_ensure_init',None)
        if not callable(initialize) or not await initialize():
            raise ConflictError('publication dependencies not initialized; no remote write attempted')
        if any(getattr(plugin,name,None) is None for name in ('_memos','_vec','_episodes')):
            raise ConflictError('publication dependencies still unavailable')
    service=await service_for(plugin); job=request.get('job_id')
    workflow=await asyncio.to_thread(saved_workflow,service,job)
    if service.closed or getattr(plugin,'_terminating',False): raise ConflictError('plugin stopping')
    await asyncio.to_thread(ensure_handoffs,service.store)
    active=service.delivery_tasks.get(job)
    if active and not active.done(): return {'job_id':job,'status':'already_running'}
    if service.tasks.get(job) and not service.tasks[job].done(): raise ConflictError('draft still running')
    first=await asyncio.to_thread(pending,plugin,workflow['scope'])
    if first and first['job_id']!=job: raise ConflictError('earlier handoff owns this session')
    publisher=Publisher(service.store,MemosPublisherAdapter(plugin._memos,capabilities_verified=True),service.archive)
    # Persist and validate before returning a queued response. No network writes here.
    await asyncio.to_thread(publisher.prepare,job,workflow['scope'])
    if first:
        return await resume_delivery(plugin,request)
    async def deliver():
        lock=plugin._compress_locks.setdefault(workflow['scope'],asyncio.Lock())
        async with lock:
            try:
                owner=await asyncio.to_thread(pending,plugin,workflow['scope'])
                if owner and owner['job_id']!=job:
                    raise ConflictError('another batch now owns this session')
                service._stage(job,'publishing')
                await publisher.publish(job)
                service._stage(job,'indexing')
                await publisher.deliver_index(job,ConsumerIndex(plugin,service.index))
                await publisher.deliver_state(job,StateConsumer(plugin))
                service._stage(job,'published')
            except asyncio.CancelledError:
                service._stage(job,'awaiting_recovery','CancelledError'); raise
            except Exception as exc:
                service._stage(job,'awaiting_recovery',type(exc).__name__)
    service.delivery_tasks[job]=asyncio.create_task(deliver())
    return {'job_id':job,'status':'delivery_queued','generation_calls':0,'embedding_may_call':True}


async def operate(plugin,request):
    if request.get('action') in ('upgrade_apply','upgrade_rollback'):
        if request.get('confirm_upgrade') is not True: raise ValueError('upgrade confirmation required')
        from . import legacy_links
        service=await service_for(plugin)
        if request['action']=='upgrade_rollback':
            return await asyncio.to_thread(legacy_links.rollback,service.store,request.get('episode_id'))
        return await asyncio.to_thread(legacy_links.apply,service.store,service.archive,
            request.get('episode_id'),request.get('job_id'))
    if request.get('action')=='publish': return await publish_draft(plugin,request)
    from .recovery import control
    return await control(plugin,request)
