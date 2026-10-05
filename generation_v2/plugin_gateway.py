"""Explicit WebUI draft entry on the Astr event loop, without legacy retries."""
from pathlib import Path

from .adapters import AstrAdapter, DirectAdapter
from .coordinator import Coordinator
from .literary import DiaryStore, WritingPolicy
from .preflight import readonly
from .scheduler import Route, Scheduler
from .workbench import ledger_path


async def start_draft(plugin, request):
    if getattr(plugin, '_terminating', False):
        raise ValueError('plugin is stopping')
    if request.get('confirm_model_calls') is not True:
        raise ValueError('explicit model-call confirmation required')
    batch_id = request.get('batch_id')
    provider_id = request.get('provider_id')
    cap = request.get('job_cap', 8)
    if not isinstance(batch_id, str) or not batch_id or not isinstance(provider_id, str) or not provider_id:
        raise ValueError('explicit batch and provider required')
    if type(cap) is not int or not 5 <= cap <= 20:
        raise ValueError('attempt cap must be 5..20')
    character = str(getattr(plugin, 'character_name', '') or '').strip()
    if not character:
        raise ValueError('character identity must be configured before writing')
    if getattr(plugin,'_episodes',None) is None:
        initialize=getattr(plugin,'_ensure_init',None)
        if not callable(initialize) or not await initialize() or getattr(plugin,'_episodes',None) is None:
            raise ValueError('source archive not initialized; no model call attempted')
    archive = Path(plugin._episodes.db_path)
    db = readonly(archive)
    try:
        row = db.execute('SELECT session_id FROM source_batches WHERE batch_id=?', (batch_id,)).fetchone()
        if row is None:
            raise ValueError('original source batch not found')
        scope = row[0]
    finally:
        db.close()
    if provider_id.startswith('external:'):
        from ..external_models import ExternalSingleProvider
        registry = plugin._external_models
        model_id = provider_id.split(':', 1)[1]
        model = registry.model(model_id)
        if not model or not registry.enabled:
            raise ValueError('external model unavailable')
        provider = ExternalSingleProvider(registry, model_id)
        adapter, kind = DirectAdapter(provider), 'direct'
    else:
        provider = plugin.context.get_provider_by_id(provider_id)
        if provider is None:
            raise ValueError('Astr provider unavailable')
        adapter, kind = AstrAdapter(provider), 'astr'
    from .mind_gateway import provider_route
    route = provider_route(plugin, provider, provider_id)
    from .integration import service_for
    service = await service_for(plugin)
    # Character and current time policy are inherited; credentials are never persisted here.
    policy = WritingPolicy(character=character,
                           timezone=str(getattr(plugin, 'rp_time_timezone', 'Asia/Shanghai') or 'Asia/Shanghai'))
    from .integration import route_for, stage_routes_for
    backup = None
    registry = getattr(plugin, '_external_models', None)
    if route.kind == 'astr' and registry is not None:
        followup = registry.followup_provider('memory_generation')
        if followup is not None: backup = provider_route(plugin, followup)
    from .integration import new_production_options
    job = await service.start(batch_id, scope, route, backup, stage_routes=stage_routes_for(plugin, scope),
                              policy=policy, job_cap=cap, max_diaries=3,
                              **new_production_options(plugin,service.store,batch_id,scope))
    return {'job_id': job, 'status': service.status(job), 'memos_writes': 0,
            'automatic_publication': False}
