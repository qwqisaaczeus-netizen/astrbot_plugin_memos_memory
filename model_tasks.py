"""Stable task names shared by runtime routing and the scheduling workbench."""
TASKS = (
    ('episode_extract', '原文证据抽取', 'memory_generation'),
    ('narrative_plan', '叙事规划', 'memory_generation'),
    ('diary_write', '日记写作与修订', 'memory_generation'),
    ('diary_review', '日记审稿', 'memory_generation'),
    ('compress_llm', '兼容压缩', 'memory_generation'),
    ('semantic_state', '滚动状态', 'semantic_state'),
    ('profile', '长期画像', 'profile'),
    ('query_plan', '检索意图规划', 'query_plan'),
    ('xinchao_live', '心潮即时感知', 'xinchao_live'),
    ('xinchao_post', '心潮后台感知', 'xinchao_post'),
    ('xinchao_dream', '心潮梦境', 'creative'),
    ('xinchao_proactive', '心潮主动内容', 'creative'),
    ('xinchao_daytime', '心潮白天浮现', 'creative'),
    ('time_insight', '时间洞察', 'time_insight'),
    ('thread_consistency', '一致性检查', 'thread_consistency'),
    ('thread_arbitration', '记忆脉络裁定', 'thread_arbitration'),
)
FAMILIES = {key:family for key,_,family in TASKS}


def task_key(label):
    value=str(label)
    if value.startswith('diary_render'): return 'diary_write'
    if value.startswith('profile_llm'): return 'profile'
    for key,_,_ in TASKS:
        if value==key or value.startswith(key+'_') or value.startswith(key+':'): return key
    return value


def is_model_setting(key):
    return (key.endswith('_provider_id') or key.startswith('llm_runtime_') or
            key in {'compress_llm_timeout','episode_extraction_timeout','diary_render_timeout',
                    'semantic_state_timeout','profile_llm_timeout','time_insight_llm_timeout',
                    'thread_llm_timeout','consistency_timeout','generation_v2_job_cap','generation_v2_call_profile'})
