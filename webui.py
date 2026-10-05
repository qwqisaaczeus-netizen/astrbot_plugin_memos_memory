"""
astrbot_plugin_memos_memory WebUI (v1.10.1)

Uses Python stdlib http.server + threading for a dependency-light local UI.
No aiohttp dependency. Runs in a daemon thread, no async event loop required.
"""
from __future__ import annotations

import json
import logging
import mimetypes
import time
import asyncio
import calendar
import builtins
import gc
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, parse_qs, unquote

logger = logging.getLogger(__name__)


def _is_preset_protected_setting(key: str) -> bool:
    """Keep presets away from identity, providers and deployment-specific values."""
    name = str(key or "").strip().lower()
    if not name:
        return True
    if name.endswith("_provider_id"):
        return True
    if name in {"character_name", "memos_mode", "rp_time_timezone"}:
        return True
    return any(
        token in name
        for token in ("token", "password", "secret", "api_key")
    ) or name.endswith((
        "_url", "_host", "_port", "_port_start", "_path", "_dir", "_exe", "_keywords",
    ))


def _safe_preset_values(values: Any) -> dict[str, Any]:
    if not isinstance(values, dict):
        return {}
    return {
        str(key): value
        for key, value in values.items()
        if not _is_preset_protected_setting(str(key))
    }


def _strip_internal_metadata(text: str) -> str:
    import re
    return re.sub(r"(?m)^\s*<!--\s*memos-memory:[\s\S]*?-->\s*$", "", text or "").strip()


def _cors_headers() -> dict[str, str]:
    return {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, POST, DELETE, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
    }


def _json_response(handler: BaseHTTPRequestHandler, status: int, data: Any) -> None:
    handler.send_response(status)
    for k, v in _cors_headers().items():
        handler.send_header(k, v)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Cache-Control", "no-store")
    body = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _html_response(handler: BaseHTTPRequestHandler, html: str) -> None:
    handler.send_response(200)
    for k, v in _cors_headers().items():
        handler.send_header(k, v)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    body = html.encode("utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _binary_response(handler: BaseHTTPRequestHandler, body: bytes, content_type: str) -> None:
    handler.send_response(200)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Cache-Control", "public, max-age=86400")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _make_handler(
    plugin: Any,
    dashboard_html: str,
    console_html: str,
    xinchao_html: str,
    production_html: str,
    access_html: str,
    forgetting_html: str,
    models_html: str,
    house_html: str = "<h1>house.html missing</h1>",
    house_assets: dict[str, bytes] | None = None,
    compensation_html: str = "<h1>compensation.html missing</h1>",
):
    """Create a request handler class bound to the plugin instance."""

    from .thread_policy import LOCK as settings_lock
    house_assets = house_assets or {}

    setting_groups = {
        "基础与连接": {
            "enable", "character_name", "memos_base_url", "memos_mode", "memos_token",
            "memos_timeout", "managed_memos_dir", "managed_memos_exe",
            "managed_memos_data_dir", "managed_memos_host", "managed_memos_port",
            "managed_memos_port_start", "vec_db_path", "emb_provider_id",
        },
        "原文档案与日记生成": {
            "compress_provider_id", "compress_llm_timeout", "enable_auto_compress",
            "compress_every_n_turns", "diary_count", "compress_batch_max_messages",
            "eod_checkpoint_enable", "eod_checkpoint_min_turns", "eod_checkpoint_max_diaries",
            "episodic_memory_enable", "episodic_db_path", "evidence_first_generation_enable",
            "raw_evidence_archive_enable", "episode_extraction_provider_id",
            "episode_extraction_timeout", "diary_render_provider_id", "diary_render_timeout",
            "episodic_auto_migrate", "episode_migration_wait_seconds",
            "raw_archive_index_chunk_chars", "raw_archive_prompt_view_max_chars",
            "scene_split_enable", "scene_split_gap_seconds", "diary_count_max_cap",
            "evidence_tier_enable", "diary_literary_mode", "diary_transcript_check_enable",
            "diary_first_person_check_enable", "diary_must_coverage_threshold",
            "diary_rewrite_preview_min_chars", "db_snapshot_keep",
        },
        "滚动当前状态": {
            "semantic_state_enable", "semantic_state_provider_id", "semantic_state_timeout",
            "semantic_state_target_chars", "semantic_state_update_policy",
            "semantic_state_batch_threshold", "semantic_state_max_wait_hours",
            "semantic_state_min_interval_hours", "semantic_state_suppress_after_hours",
            "semantic_state_noop_similarity",
            "semantic_state_significance_threshold", "semantic_state_merge_max_batches",
            "semantic_state_auto_bootstrap",
            "semantic_state_bootstrap_episode_limit", "semantic_state_replace_profile",
        },
        "融合召回": {
            "enable_auto_recall", "lean_recall_enable", "lean_recall_candidate_k",
            "lean_event_index_enable", "lean_source_evidence_enable", "lean_coverage_selection_enable",
            "lean_adaptive_evidence_enable", "passage_vector_auto_migrate",
            "source_turn_vector_auto_migrate",
            "lean_temporal_enable", "lean_story_min_inject", "lean_story_max_inject",
            "lean_story_normal_inject", "lean_relative_margin", "lean_relative_margin_broad",
            "recall_safety_net_enable", "recall_safety_net_min_selected",
            "recall_safety_net_min_top_score",
            "recall_cross_layer_consistency_enable", "recall_temporal_constraints_enable",
            "recall_intent_layer_weights_enable", "recall_observation_enable",
            "recall_auto_eval_limit",
            "lean_texture_enable", "min_similarity_to_inject", "w_relevance",
            "w_importance", "w_recency", "pin_boost", "recall_injection_min_score",
            "recall_context_query_messages", "recall_context_query_max_chars",
            "recall_dedup_window", "recall_rerank_enable", "rerank_provider_id",
            "bm25_tokenizer",
        },
        "剧情注入与原文证据": {
            "inject_order",
            "inject_char_budget", "inject_compact_chars",
            "episodic_evidence_per_memory", "episodic_full_diary_limit",
            "passage_index_enable", "passage_max_chars", "passage_overlap_chars",
            "mixed_injection_enable", "full_diary_top_n", "passage_expand_chars",
        },
        "兼容回退（不参与当前主路）": {
            "inject_format", "summary_chars", "enable_injection_style_rules",
            "raw_archive_full_assistant_text", "raw_archive_assistant_max_chars",
            "recall_top_k", "persona_top_k", "plot_top_k", "texture_top_k",
            "enable_layered_injection", "time_boost", "recall_candidate_pool",
            "recall_multi_query_enable", "recall_rrf_k", "recall_month_route_enable",
            "recall_month_route_count", "recall_month_route_candidate_k",
            "recall_month_route_inject_max", "recall_month_route_timeout",
            "recall_information_gain_enable", "recall_necessary_can_exceed_max",
            "recall_necessary_hard_cap", "recall_dynamic_count_enable",
            "recall_min_inject", "recall_max_inject", "episodic_candidate_pool",
            "episodic_default_inject", "episodic_narrative_inject", "episodic_min_card_score",
            "recall_cluster_fold_enable", "recall_cluster_fold_apply",
            "recall_cluster_similarity", "recall_cluster_base_per_group",
            "recall_cluster_allow_protected",
        },
        "长期人格与画像": {
            "enable_affiliate_profile", "affiliate_profile_max_age_days", "profile_provider_id",
            "profile_auto_update_days", "profile_recent_persona_limit", "profile_anchor_limit",
            "profile_manual_limit", "profile_feedback_limit", "profile_llm_timeout",
            "profile_target_chars", "profile_facts_target_count", "profile_min_new_evidence",
            "identity_core_inject_chars",
        },
        "上下文治理": {
            "context_governance_enable", "context_exclude_command_turns", "context_keep_recent_messages",
            "context_min_messages_before_trim", "context_preserve_system_messages",
            "context_trim_backup_enable",
            "context_archive_enable", "context_archive_keep_recent_messages",
            "context_archive_min_total_messages", "context_archive_interval_days",
            "context_archive_backup_dir",
        },
        "角色增强": set(),
        "缓存诊断": set(),
        "运行与维护": {
            "data_backup_enable", "data_backup_interval_days",
            "data_backup_keep", "data_backup_dir",
            "llm_runtime_provider_concurrency", "llm_runtime_external_concurrency",
            "llm_runtime_interactive_queue_timeout",
            "llm_runtime_foreground_lease_seconds", "llm_runtime_defer_background",
        },
        "记忆可达性与补充支路": {
            "memory_forgetting_enable", "memory_access_route_mode",
            "memory_access_decay_enable", "memory_access_deep_rescue_enable",
            "memory_access_independent_cue_rescue", "memory_access_rescue_candidate_limit",
            "memory_interference_enable", "memory_reconsolidation_enable",
            "memory_psychological_bias_enable", "memory_psychological_bias_strength",
            "memory_access_vivid_threshold", "memory_access_deep_threshold",
            "memory_access_decay_days", "memory_access_exact_cue_relief",
            "memory_access_max_neighbors", "memory_access_maintenance_hours",
            "memory_access_event_keep", "memory_access_observation_keep",
            "memory_access_observation_query_max_chars", "memory_access_export_keep",
            "memory_access_export_dir",
            "memory_access_cue_grade_b_rarity_min", "memory_access_same_day_gap_threshold",
            "memory_access_state_confirmation_runs", "memory_access_source_proxy_weight",
            "memory_access_supplement_max", "memory_access_supplement_temporal_max",
            "memory_access_supplement_rescue_max",
            "memory_access_supplement_temporal_threshold",
            "memory_access_supplement_rescue_threshold",
            "memory_access_supplement_source_bonus",
            "memory_access_supplement_allow_diary_derived",
            "memory_access_supplement_holdout_percent",
            "memory_access_supplement_breaker_threshold",
            "memory_access_eval_max_age_days",
            "memory_access_auto_eval_case_limit",
        },
        "6.1 生产交接": set(),
        "记忆脉络与一致性": set(),
        "数据备份": set(),
        "同步与网络": set(),
        "检索运行": set(),
        "界面与诊断": set(),
        "重要性规则": {
            "imp_tier5_keywords", "imp_tier4_keywords", "imp_tier3_keywords", "imp_low_keywords",
        },
    }
    time_insight_setting_keys = {
        "enable_time_insight_affiliate", "time_insight_auto_update_hours",
        "time_insight_source_memo_limit", "time_insight_recent_window_days",
        "time_insight_anniversary_window_days", "time_insight_min_importance",
        "time_insight_min_evidence_score", "time_insight_trend_min_distinct_days",
        "time_insight_trend_min_evidence", "time_insight_seasonal_min_years",
        "time_insight_ambient_max_insights", "time_insight_ambient_max_chars",
        "time_insight_repeat_cooldown_minutes", "time_insight_query_enable",
        "time_insight_query_max_insights", "time_insight_query_min_score",
        "time_insight_query_max_chars", "time_insight_query_lookup_timeout",
        "time_insight_llm_refine_enable", "time_insight_llm_provider_id",
        "time_insight_llm_timeout", "time_insight_llm_min_confidence",
        "time_insight_diagnostic_log",
    }

    preset_common_values = {
        "enable_auto_compress": True,
        "eod_checkpoint_enable": True,
        "eod_checkpoint_min_turns": 1,
        "episodic_memory_enable": True,
        "episodic_auto_migrate": True,
        "evidence_first_generation_enable": True,
        "raw_evidence_archive_enable": True,
        "scene_split_enable": True,
        "evidence_tier_enable": True,
        "diary_literary_mode": True,
        "diary_transcript_check_enable": True,
        "diary_first_person_check_enable": True,
        "semantic_state_enable": True,
        "semantic_state_update_policy": "adaptive",
        "semantic_state_auto_bootstrap": True,
        "semantic_state_replace_profile": True,
        "semantic_state_min_interval_hours": 72,
        "semantic_state_suppress_after_hours": 168,
        "semantic_state_noop_similarity": 0.94,
        "enable_affiliate_profile": True,
        "profile_auto_update_days": 30,
        "profile_min_new_evidence": 8,
        "identity_core_inject_chars": 1000,
        "profile_target_chars": 1000,
        "profile_facts_target_count": 20,
        "enable_auto_recall": True,
        "lean_recall_enable": True,
        "lean_event_index_enable": True,
        "lean_source_evidence_enable": True,
        "lean_coverage_selection_enable": True,
        "lean_adaptive_evidence_enable": True,
        "memory_forgetting_enable": True,
        "memory_forgetting_shadow_mode": False,
        "memory_access_route_mode": "supplement",
        "memory_access_decay_enable": True,
        "memory_access_deep_rescue_enable": True,
        "memory_access_observation_keep": 5000,
        "memory_access_observation_query_max_chars": 2000,
        "memory_access_export_keep": 10,
        "memory_interference_enable": True,
        "memory_reconsolidation_enable": True,
        "memory_psychological_bias_enable": True,
        "passage_vector_auto_migrate": True,
        "source_turn_vector_auto_migrate": True,
        "lean_temporal_enable": True,
        "recall_safety_net_enable": True,
        "recall_cross_layer_consistency_enable": True,
        "recall_temporal_constraints_enable": True,
        "recall_intent_layer_weights_enable": True,
        "recall_observation_enable": True,
        "lean_texture_enable": False,
        "passage_index_enable": True,
        "mixed_injection_enable": True,
        "context_governance_enable": True,
        "context_exclude_command_turns": True,
        "context_preserve_system_messages": True,
        "context_trim_backup_enable": True,
        "rp_enhancer_enable": True,
        "rp_time_enable": True,
        "rp_time_strip_default": True,
        "rp_time_show_lunar": False,
        "rp_time_show_festival": True,
        "rp_time_show_rhythm": True,
        "enable_time_insight_affiliate": True,
        "time_insight_query_enable": True,
        "time_insight_diagnostic_log": True,
        "data_backup_enable": True,
        "data_backup_interval_days": 14,
        "llm_runtime_provider_concurrency": 2,
        "llm_runtime_external_concurrency": 3,
        "llm_runtime_interactive_queue_timeout": 0.75,
        "llm_runtime_foreground_lease_seconds": 240,
        "llm_runtime_defer_background": True,
    }

    preset_definitions = {
        "deep_roleplay": {
            "name": "长线灵魂塑形",
            "summary": "年月级 RP、人格收敛和跨阶段剧情覆盖最大化；完整证据与文学日记优先。",
            "cost": "最高",
            "values": {
                **preset_common_values,
                "compress_every_n_turns": 30, "diary_count": 2, "compress_batch_max_messages": 60,
                "eod_checkpoint_enable": True, "eod_checkpoint_min_turns": 1,
                "eod_checkpoint_max_diaries": 3,
                "evidence_first_generation_enable": True, "raw_evidence_archive_enable": True,
                "diary_must_coverage_threshold": 0.94, "diary_rewrite_preview_min_chars": 2800,
                "semantic_state_enable": True, "semantic_state_target_chars": 1500,
                "semantic_state_update_policy": "adaptive", "semantic_state_batch_threshold": 4,
                "semantic_state_max_wait_hours": 168, "semantic_state_min_interval_hours": 72,
                "semantic_state_suppress_after_hours": 168,
                "semantic_state_significance_threshold": 0.70,
                "semantic_state_merge_max_batches": 12,
                "semantic_state_auto_bootstrap": True, "semantic_state_replace_profile": True,
                "lean_recall_enable": True, "lean_recall_candidate_k": 80,
                "lean_event_index_enable": True, "lean_source_evidence_enable": True,
                "lean_coverage_selection_enable": True,
                "lean_adaptive_evidence_enable": True, "passage_vector_auto_migrate": True,
                "source_turn_vector_auto_migrate": True,
                "lean_temporal_enable": True, "lean_story_min_inject": 1,
                "lean_story_max_inject": 10, "lean_story_normal_inject": 4,
                "lean_relative_margin": 0.30, "lean_relative_margin_broad": 0.46,
                "recall_safety_net_enable": True, "recall_safety_net_min_selected": 2,
                "recall_safety_net_min_top_score": 0.62,
                "inject_char_budget": 14000, "inject_compact_chars": 240,
                "lean_texture_enable": False,
                "episodic_evidence_per_memory": 4, "episodic_full_diary_limit": 2,
                "recall_context_query_messages": 5, "recall_context_query_max_chars": 1500,
                "min_similarity_to_inject": 0.48, "recall_injection_min_score": 0.58,
                "recall_dedup_window": 3, "recall_rerank_enable": True,
                "passage_index_enable": True, "passage_max_chars": 220,
                "passage_overlap_chars": 90, "mixed_injection_enable": True,
                "full_diary_top_n": 3, "passage_expand_chars": 160,
                "time_insight_auto_update_hours": 8, "time_insight_recent_window_days": 45,
                "time_insight_anniversary_window_days": 3, "time_insight_min_importance": 2,
                "time_insight_min_evidence_score": 0.62, "time_insight_query_max_insights": 3,
                "time_insight_query_min_score": 0.38, "time_insight_query_max_chars": 1100,
                "time_insight_llm_refine_enable": True, "time_insight_llm_min_confidence": 0.68,
                "context_keep_recent_messages": 60, "context_min_messages_before_trim": 120,
                "data_backup_keep": 8,
                "memory_access_supplement_max": 2,
                "memory_access_supplement_temporal_max": 1,
                "memory_access_supplement_rescue_max": 1,
                "memory_access_supplement_temporal_threshold": 0.50,
                "memory_access_supplement_rescue_threshold": 0.56,
                "memory_access_supplement_source_bonus": 0.10,
                "memory_access_supplement_allow_diary_derived": True,
                "memory_access_supplement_holdout_percent": 0,
                "memory_access_supplement_breaker_threshold": 4,
            },
        },
        "quality": {
            "name": "全维效果优先（推荐）",
            "summary": "自动兼顾日常、精确取证与长线叙事，保留较宽召回和充分注入细节。",
            "cost": "中高",
            "values": {
                **preset_common_values,
                "compress_every_n_turns": 30, "diary_count": 2, "compress_batch_max_messages": 60,
                "eod_checkpoint_enable": True, "eod_checkpoint_min_turns": 1,
                "eod_checkpoint_max_diaries": 3,
                "evidence_first_generation_enable": True, "raw_evidence_archive_enable": True,
                "diary_must_coverage_threshold": 0.92, "diary_rewrite_preview_min_chars": 2400,
                "semantic_state_enable": True, "semantic_state_target_chars": 1200,
                "semantic_state_update_policy": "adaptive", "semantic_state_batch_threshold": 4,
                "semantic_state_max_wait_hours": 168, "semantic_state_min_interval_hours": 72,
                "semantic_state_suppress_after_hours": 168,
                "semantic_state_significance_threshold": 0.72,
                "semantic_state_merge_max_batches": 12,
                "semantic_state_auto_bootstrap": True, "semantic_state_replace_profile": True,
                "lean_recall_enable": True, "lean_recall_candidate_k": 70,
                "lean_event_index_enable": True, "lean_source_evidence_enable": True,
                "lean_coverage_selection_enable": True,
                "lean_adaptive_evidence_enable": True, "passage_vector_auto_migrate": True,
                "source_turn_vector_auto_migrate": True,
                "lean_temporal_enable": True, "lean_story_min_inject": 1,
                "lean_story_max_inject": 9, "lean_story_normal_inject": 4,
                "lean_relative_margin": 0.28, "lean_relative_margin_broad": 0.42,
                "recall_safety_net_enable": True, "recall_safety_net_min_selected": 2,
                "recall_safety_net_min_top_score": 0.60,
                "inject_char_budget": 12000, "inject_compact_chars": 210,
                "lean_texture_enable": False,
                "episodic_evidence_per_memory": 3, "episodic_full_diary_limit": 2,
                "recall_context_query_messages": 4, "recall_context_query_max_chars": 1200,
                "min_similarity_to_inject": 0.50, "recall_injection_min_score": 0.60,
                "recall_dedup_window": 4, "recall_rerank_enable": True,
                "passage_index_enable": True, "passage_max_chars": 240,
                "passage_overlap_chars": 80, "mixed_injection_enable": True,
                "full_diary_top_n": 3, "passage_expand_chars": 140,
                "time_insight_auto_update_hours": 12, "time_insight_recent_window_days": 30,
                "time_insight_anniversary_window_days": 3, "time_insight_min_importance": 3,
                "time_insight_min_evidence_score": 0.65, "time_insight_query_max_insights": 3,
                "time_insight_query_min_score": 0.40, "time_insight_query_max_chars": 1000,
                "time_insight_llm_refine_enable": True, "time_insight_llm_min_confidence": 0.70,
                "context_keep_recent_messages": 48, "context_min_messages_before_trim": 96,
                "data_backup_keep": 8,
                "memory_access_supplement_max": 2,
                "memory_access_supplement_temporal_max": 1,
                "memory_access_supplement_rescue_max": 1,
                "memory_access_supplement_temporal_threshold": 0.54,
                "memory_access_supplement_rescue_threshold": 0.60,
                "memory_access_supplement_source_bonus": 0.08,
                "memory_access_supplement_allow_diary_derived": True,
                "memory_access_supplement_holdout_percent": 0,
                "memory_access_supplement_breaker_threshold": 3,
            },
        },
        "balanced": {
            "name": "均衡推荐",
            "summary": "保留 5.1 完整记忆链路，以默认规模平衡长期效果、延迟和调用成本。",
            "cost": "中等",
            "values": {
                **preset_common_values,
                "compress_every_n_turns": 30, "diary_count": 2, "compress_batch_max_messages": 60,
                "eod_checkpoint_enable": True, "eod_checkpoint_min_turns": 1,
                "eod_checkpoint_max_diaries": 3,
                "evidence_first_generation_enable": True, "raw_evidence_archive_enable": True,
                "diary_must_coverage_threshold": 0.90, "diary_rewrite_preview_min_chars": 2200,
                "semantic_state_enable": True, "semantic_state_target_chars": 1000,
                "semantic_state_update_policy": "adaptive", "semantic_state_batch_threshold": 4,
                "semantic_state_max_wait_hours": 240, "semantic_state_min_interval_hours": 96,
                "semantic_state_suppress_after_hours": 240,
                "semantic_state_significance_threshold": 0.74,
                "semantic_state_merge_max_batches": 12,
                "semantic_state_auto_bootstrap": True, "semantic_state_replace_profile": True,
                "lean_recall_enable": True, "lean_recall_candidate_k": 50,
                "lean_event_index_enable": True, "lean_source_evidence_enable": True,
                "lean_coverage_selection_enable": True,
                "lean_adaptive_evidence_enable": True, "passage_vector_auto_migrate": True,
                "source_turn_vector_auto_migrate": True,
                "lean_temporal_enable": True, "lean_story_min_inject": 1,
                "lean_story_max_inject": 7, "lean_story_normal_inject": 3,
                "lean_relative_margin": 0.24, "lean_relative_margin_broad": 0.36,
                "recall_safety_net_enable": True, "recall_safety_net_min_selected": 2,
                "recall_safety_net_min_top_score": 0.60,
                "inject_char_budget": 9000, "inject_compact_chars": 180,
                "lean_texture_enable": False,
                "episodic_evidence_per_memory": 3, "episodic_full_diary_limit": 1,
                "recall_context_query_messages": 3, "recall_context_query_max_chars": 900,
                "min_similarity_to_inject": 0.52, "recall_injection_min_score": 0.62,
                "recall_dedup_window": 6, "recall_rerank_enable": True,
                "passage_index_enable": True, "passage_max_chars": 280,
                "passage_overlap_chars": 60, "mixed_injection_enable": True,
                "full_diary_top_n": 2, "passage_expand_chars": 100,
                "time_insight_auto_update_hours": 12, "time_insight_recent_window_days": 21,
                "time_insight_anniversary_window_days": 2, "time_insight_min_importance": 3,
                "time_insight_min_evidence_score": 0.68, "time_insight_query_max_insights": 2,
                "time_insight_query_min_score": 0.42, "time_insight_query_max_chars": 900,
                "time_insight_llm_refine_enable": True, "time_insight_llm_min_confidence": 0.72,
                "context_keep_recent_messages": 40, "context_min_messages_before_trim": 80,
                "data_backup_keep": 6,
                "memory_access_supplement_max": 2,
                "memory_access_supplement_temporal_max": 1,
                "memory_access_supplement_rescue_max": 1,
                "memory_access_supplement_temporal_threshold": 0.58,
                "memory_access_supplement_rescue_threshold": 0.64,
                "memory_access_supplement_source_bonus": 0.07,
                "memory_access_supplement_allow_diary_derived": True,
                "memory_access_supplement_holdout_percent": 0,
                "memory_access_supplement_breaker_threshold": 3,
            },
        },
        "economy": {
            "name": "低成本长记忆",
            "summary": "不牺牲原文档案和证据生成，通过降低频率、候选量与 LLM 精炼节省成本。",
            "cost": "较低",
            "values": {
                **preset_common_values,
                "compress_every_n_turns": 45, "diary_count": 2, "compress_batch_max_messages": 90,
                "eod_checkpoint_enable": True, "eod_checkpoint_min_turns": 1,
                "eod_checkpoint_max_diaries": 3,
                "evidence_first_generation_enable": True, "raw_evidence_archive_enable": True,
                "diary_must_coverage_threshold": 0.88, "diary_rewrite_preview_min_chars": 1800,
                "semantic_state_enable": True, "semantic_state_target_chars": 800,
                "semantic_state_update_policy": "adaptive", "semantic_state_batch_threshold": 5,
                "semantic_state_max_wait_hours": 336, "semantic_state_min_interval_hours": 168,
                "semantic_state_suppress_after_hours": 336,
                "semantic_state_significance_threshold": 0.80,
                "semantic_state_merge_max_batches": 16,
                "semantic_state_auto_bootstrap": True, "semantic_state_replace_profile": True,
                "lean_recall_enable": True, "lean_recall_candidate_k": 40,
                "lean_event_index_enable": True, "lean_source_evidence_enable": True,
                "lean_coverage_selection_enable": True,
                "lean_adaptive_evidence_enable": True, "passage_vector_auto_migrate": True,
                "source_turn_vector_auto_migrate": True,
                "lean_temporal_enable": True, "lean_story_min_inject": 1,
                "lean_story_max_inject": 5, "lean_story_normal_inject": 2,
                "lean_relative_margin": 0.20, "lean_relative_margin_broad": 0.32,
                "recall_safety_net_enable": True, "recall_safety_net_min_selected": 1,
                "recall_safety_net_min_top_score": 0.55,
                "inject_char_budget": 6000, "inject_compact_chars": 140,
                "lean_texture_enable": False,
                "episodic_evidence_per_memory": 2, "episodic_full_diary_limit": 1,
                "recall_context_query_messages": 2, "recall_context_query_max_chars": 600,
                "min_similarity_to_inject": 0.55, "recall_injection_min_score": 0.66,
                "recall_dedup_window": 8, "recall_rerank_enable": False,
                "passage_index_enable": True, "passage_max_chars": 320,
                "passage_overlap_chars": 50, "mixed_injection_enable": True,
                "full_diary_top_n": 1, "passage_expand_chars": 80,
                "time_insight_auto_update_hours": 24, "time_insight_recent_window_days": 21,
                "time_insight_anniversary_window_days": 2, "time_insight_min_importance": 3,
                "time_insight_min_evidence_score": 0.72, "time_insight_query_max_insights": 1,
                "time_insight_query_min_score": 0.48, "time_insight_query_max_chars": 650,
                "time_insight_llm_refine_enable": False, "time_insight_llm_min_confidence": 0.76,
                "context_keep_recent_messages": 32, "context_min_messages_before_trim": 72,
                "data_backup_keep": 4,
                "memory_access_supplement_max": 1,
                "memory_access_supplement_temporal_max": 1,
                "memory_access_supplement_rescue_max": 1,
                "memory_access_supplement_temporal_threshold": 0.64,
                "memory_access_supplement_rescue_threshold": 0.70,
                "memory_access_supplement_source_bonus": 0.05,
                "memory_access_supplement_allow_diary_derived": True,
                "memory_access_supplement_holdout_percent": 0,
                "memory_access_supplement_breaker_threshold": 4,
            },
        },
    }

    # Retrieval-only presets shown in the recall lab. They never touch tokens,
    # providers, character names, service URLs, paths, ports, or generation rules.
    recall_preset_common_values = {
        "enable_auto_recall": True,
        "lean_recall_enable": True,
        "lean_event_index_enable": True,
        "lean_source_evidence_enable": True,
        "lean_coverage_selection_enable": True,
        "lean_adaptive_evidence_enable": True,
        "lean_temporal_enable": True,
        "recall_safety_net_enable": True,
        "recall_cross_layer_consistency_enable": True,
        "recall_temporal_constraints_enable": True,
        "recall_intent_layer_weights_enable": True,
        "recall_observation_enable": True,
        "lean_texture_enable": False,
    }
    recall_preset_definitions = {
        "recall_effect": {
            "name": "全场景最优（推荐）",
            "summary": "普通对话控制密度，叙事意图自动扩到长线上限；同时兼顾精确事实与跨阶段剧情。",
            "cost": "较高",
            "values": {
                **recall_preset_common_values,
                "lean_recall_candidate_k": 80, "lean_story_min_inject": 1,
                "lean_story_normal_inject": 4, "lean_story_max_inject": 10,
                "min_similarity_to_inject": 0.48, "recall_injection_min_score": 0.58,
                "lean_relative_margin": 0.30, "lean_relative_margin_broad": 0.46,
                "recall_safety_net_min_selected": 2, "recall_safety_net_min_top_score": 0.62,
                "recall_context_query_messages": 5, "recall_context_query_max_chars": 1500,
                "recall_dedup_window": 3, "recall_rerank_enable": True,
            },
        },
        "recall_precise": {
            "name": "精确取证",
            "summary": "提高资格线、缩小普通注入，优先日期、原话、承诺、边界和专名的一手证据。",
            "cost": "中等",
            "values": {
                **recall_preset_common_values,
                "lean_recall_candidate_k": 60, "lean_story_min_inject": 1,
                "lean_story_normal_inject": 3, "lean_story_max_inject": 6,
                "min_similarity_to_inject": 0.55, "recall_injection_min_score": 0.66,
                "lean_relative_margin": 0.18, "lean_relative_margin_broad": 0.28,
                "recall_safety_net_min_selected": 2, "recall_safety_net_min_top_score": 0.64,
                "recall_context_query_messages": 3, "recall_context_query_max_chars": 900,
                "recall_dedup_window": 6, "recall_rerank_enable": True,
            },
        },
        "recall_narrative": {
            "name": "长线宽召回（专项）",
            "summary": "比综合最优更宽松地覆盖跨月、多阶段和多人物剧情，适合集中回顾长篇故事。",
            "cost": "最高",
            "values": {
                **recall_preset_common_values,
                "lean_recall_candidate_k": 100, "lean_story_min_inject": 1,
                "lean_story_normal_inject": 4, "lean_story_max_inject": 10,
                "min_similarity_to_inject": 0.46, "recall_injection_min_score": 0.55,
                "lean_relative_margin": 0.28, "lean_relative_margin_broad": 0.54,
                "recall_safety_net_min_selected": 2, "recall_safety_net_min_top_score": 0.60,
                "recall_context_query_messages": 6, "recall_context_query_max_chars": 1800,
                "recall_dedup_window": 2, "recall_rerank_enable": True,
            },
        },
        "recall_balanced": {
            "name": "日常均衡",
            "summary": "保留全 MEMORY 三路与安全网，以默认候选量适配长期日常 RP。",
            "cost": "中等",
            "values": {
                **recall_preset_common_values,
                "lean_recall_candidate_k": 50, "lean_story_min_inject": 1,
                "lean_story_normal_inject": 3, "lean_story_max_inject": 7,
                "min_similarity_to_inject": 0.52, "recall_injection_min_score": 0.62,
                "lean_relative_margin": 0.24, "lean_relative_margin_broad": 0.36,
                "recall_safety_net_min_selected": 2, "recall_safety_net_min_top_score": 0.60,
                "recall_context_query_messages": 3, "recall_context_query_max_chars": 900,
                "recall_dedup_window": 6, "recall_rerank_enable": True,
            },
        },
    }

    # Dedicated 5.0 forgetting presets. They intentionally mirror the four
    # global modes, but touch only bounded ACCESS booleans/numbers. In
    # particular, no preset can enable the online source canary takeover.
    forgetting_preset_definitions = {
        "deep_roleplay": {
            "name": "长线灵魂塑形",
            "summary": "最慢自然淡出、最强精确线索穿透与更宽干扰建模，适合年月级连续 RP。",
            "cost": "维护较高",
            "values": {
                "memory_forgetting_enable": True, "memory_access_route_mode": "supplement",
                "memory_access_decay_enable": True, "memory_access_deep_rescue_enable": True,
                "memory_access_independent_cue_rescue": True,
                "memory_access_rescue_candidate_limit": 20,
                "memory_interference_enable": True, "memory_reconsolidation_enable": True,
                "memory_psychological_bias_enable": True,
                "memory_psychological_bias_strength": 0.10,
                "memory_access_vivid_threshold": 0.64, "memory_access_deep_threshold": 0.24,
                "memory_access_decay_days": 120.0, "memory_access_exact_cue_relief": 0.94,
                "memory_access_max_neighbors": 16, "memory_access_maintenance_hours": 24,
                "memory_access_event_keep": 10000,
                "memory_access_observation_keep": 12000,
                "memory_access_cue_grade_b_rarity_min": 0.30,
                "memory_access_same_day_gap_threshold": 0.18,
                "memory_access_state_confirmation_runs": 3,
                "memory_access_source_proxy_weight": 0.30,
                "memory_access_supplement_max": 2,
                "memory_access_supplement_temporal_max": 1,
                "memory_access_supplement_rescue_max": 1,
                "memory_access_supplement_temporal_threshold": 0.50,
                "memory_access_supplement_rescue_threshold": 0.56,
                "memory_access_supplement_source_bonus": 0.10,
                "memory_access_supplement_allow_diary_derived": True,
                "memory_access_supplement_holdout_percent": 0,
                "memory_access_supplement_breaker_threshold": 4,
                "memory_access_eval_max_age_days": 7,
                "memory_access_auto_eval_case_limit": 120,
            },
        },
        "quality": {
            "name": "全维效果优先（推荐）",
            "summary": "长期保留、精确救援、干扰抑制和再巩固之间的高质量默认平衡。",
            "cost": "中高",
            "values": {
                "memory_forgetting_enable": True, "memory_access_route_mode": "supplement",
                "memory_access_decay_enable": True, "memory_access_deep_rescue_enable": True,
                "memory_access_independent_cue_rescue": True,
                "memory_access_rescue_candidate_limit": 16,
                "memory_interference_enable": True, "memory_reconsolidation_enable": True,
                "memory_psychological_bias_enable": True,
                "memory_psychological_bias_strength": 0.08,
                "memory_access_vivid_threshold": 0.66, "memory_access_deep_threshold": 0.28,
                "memory_access_decay_days": 90.0, "memory_access_exact_cue_relief": 0.92,
                "memory_access_max_neighbors": 14, "memory_access_maintenance_hours": 24,
                "memory_access_event_keep": 8000,
                "memory_access_observation_keep": 9000,
                "memory_access_cue_grade_b_rarity_min": 0.32,
                "memory_access_same_day_gap_threshold": 0.16,
                "memory_access_state_confirmation_runs": 2,
                "memory_access_source_proxy_weight": 0.35,
                "memory_access_supplement_max": 2,
                "memory_access_supplement_temporal_max": 1,
                "memory_access_supplement_rescue_max": 1,
                "memory_access_supplement_temporal_threshold": 0.54,
                "memory_access_supplement_rescue_threshold": 0.60,
                "memory_access_supplement_source_bonus": 0.08,
                "memory_access_supplement_allow_diary_derived": True,
                "memory_access_supplement_holdout_percent": 0,
                "memory_access_supplement_breaker_threshold": 3,
                "memory_access_eval_max_age_days": 7,
                "memory_access_auto_eval_case_limit": 100,
            },
        },
        "balanced": {
            "name": "日常均衡",
            "summary": "保留完整七路机制，以适中的半衰期和维护规模服务长期日常聊天。",
            "cost": "中等",
            "values": {
                "memory_forgetting_enable": True, "memory_access_route_mode": "supplement",
                "memory_access_decay_enable": True, "memory_access_deep_rescue_enable": True,
                "memory_access_independent_cue_rescue": True,
                "memory_access_rescue_candidate_limit": 12,
                "memory_interference_enable": True, "memory_reconsolidation_enable": True,
                "memory_psychological_bias_enable": True,
                "memory_psychological_bias_strength": 0.06,
                "memory_access_vivid_threshold": 0.68, "memory_access_deep_threshold": 0.32,
                "memory_access_decay_days": 60.0, "memory_access_exact_cue_relief": 0.90,
                "memory_access_max_neighbors": 12, "memory_access_maintenance_hours": 24,
                "memory_access_event_keep": 5000,
                "memory_access_observation_keep": 6000,
                "memory_access_cue_grade_b_rarity_min": 0.34,
                "memory_access_same_day_gap_threshold": 0.15,
                "memory_access_state_confirmation_runs": 2,
                "memory_access_source_proxy_weight": 0.40,
                "memory_access_supplement_max": 2,
                "memory_access_supplement_temporal_max": 1,
                "memory_access_supplement_rescue_max": 1,
                "memory_access_supplement_temporal_threshold": 0.58,
                "memory_access_supplement_rescue_threshold": 0.64,
                "memory_access_supplement_source_bonus": 0.07,
                "memory_access_supplement_allow_diary_derived": True,
                "memory_access_supplement_holdout_percent": 0,
                "memory_access_supplement_breaker_threshold": 3,
                "memory_access_eval_max_age_days": 7,
                "memory_access_auto_eval_case_limit": 80,
            },
        },
        "economy": {
            "name": "低成本长记忆",
            "summary": "减少图规模和维护频率，仍保留精确救援、非破坏存储与事实安全边界。",
            "cost": "较低",
            "values": {
                "memory_forgetting_enable": True, "memory_access_route_mode": "supplement",
                "memory_access_decay_enable": True, "memory_access_deep_rescue_enable": True,
                "memory_access_independent_cue_rescue": True,
                "memory_access_rescue_candidate_limit": 8,
                "memory_interference_enable": True, "memory_reconsolidation_enable": True,
                "memory_psychological_bias_enable": False,
                "memory_psychological_bias_strength": 0.04,
                "memory_access_vivid_threshold": 0.70, "memory_access_deep_threshold": 0.34,
                "memory_access_decay_days": 45.0, "memory_access_exact_cue_relief": 0.86,
                "memory_access_max_neighbors": 8, "memory_access_maintenance_hours": 48,
                "memory_access_event_keep": 2500,
                "memory_access_observation_keep": 3000,
                "memory_access_cue_grade_b_rarity_min": 0.38,
                "memory_access_same_day_gap_threshold": 0.14,
                "memory_access_state_confirmation_runs": 2,
                "memory_access_source_proxy_weight": 0.35,
                "memory_access_supplement_max": 1,
                "memory_access_supplement_temporal_max": 1,
                "memory_access_supplement_rescue_max": 1,
                "memory_access_supplement_temporal_threshold": 0.64,
                "memory_access_supplement_rescue_threshold": 0.70,
                "memory_access_supplement_source_bonus": 0.05,
                "memory_access_supplement_allow_diary_derived": True,
                "memory_access_supplement_holdout_percent": 0,
                "memory_access_supplement_breaker_threshold": 4,
                "memory_access_eval_max_age_days": 7,
                "memory_access_auto_eval_case_limit": 60,
            },
        },
    }

    class Handler(BaseHTTPRequestHandler):
        _house_plugin = plugin

        def log_message(self, format, *args):
            pass  # suppress default stderr logging

        def do_OPTIONS(self):
            self.send_response(204)
            for k, v in _cors_headers().items():
                self.send_header(k, v)
            self.end_headers()

        def do_GET(self):
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            qs = parse_qs(parsed.query)

            if path == "/house":
                _html_response(self, house_html)
            elif path.startswith("/assets/"):
                relative = unquote(path[len("/assets/"):]).replace("\\", "/")
                if (
                    not relative
                    or relative.startswith("/")
                    or ".." in Path(relative).parts
                ):
                    return _json_response(
                        self, 400, {"ok": False, "error": "Invalid asset path"}
                    )
                asset = house_assets.get(relative)
                if asset is None:
                    _json_response(self, 404, {"ok": False, "error": "Not found"})
                else:
                    content_type = mimetypes.guess_type(relative)[0] or "application/octet-stream"
                    if content_type in {"text/javascript", "application/javascript", "application/json"}:
                        content_type += "; charset=utf-8"
                    _binary_response(self, asset, content_type)
            elif path.startswith("/api/house/"):
                from .house_webui import HouseWebUIHandlers
                if not HouseWebUIHandlers.handle_get(self, path, qs):
                    _json_response(self, 404, {"ok": False, "error": "Not found"})
            elif path == "/":
                _html_response(self, dashboard_html)
            elif path == "/console" or path == "/logs":
                _html_response(self, console_html)
            elif path == "/xinchao":
                _html_response(self, xinchao_html)
            elif path == "/models":
                _html_response(self, models_html)
            elif path == "/compensation":
                _html_response(self, compensation_html)
            elif path == "/production":
                _html_response(self, production_html)
            elif path == "/threads":
                from .thread_webui import ThreadWebUIHandlers
                ThreadWebUIHandlers()._serve_threads_page(self)
            elif path == "/api/threads/policy":
                from .thread_policy import gates, snapshot
                with settings_lock:
                    data = gates(plugin)
                    data.update(snapshot(plugin))
                    data.pop("rollback_target", None)
                    data["provider_id"] = getattr(plugin, "thread_llm_provider_id", "")
                    data["provider_options"] = self._llm_provider_options(data["provider_id"])
                self._json_ok(data)
            elif path == "/api/threads/status":
                from .thread_webui import ThreadWebUIHandlers
                ThreadWebUIHandlers()._api_threads_status(self, plugin)
            elif path == "/api/threads/edges":
                from .thread_webui import ThreadWebUIHandlers
                ThreadWebUIHandlers()._api_threads_edges(self, plugin, qs)
            elif path == "/api/threads/ambiguities":
                from .thread_webui import ThreadWebUIHandlers
                ThreadWebUIHandlers()._api_threads_ambiguities(self, plugin, qs)
            elif path == "/api/threads/list":
                from .thread_webui import ThreadWebUIHandlers
                ThreadWebUIHandlers()._api_threads_list(self, plugin, qs)
            elif path == "/api/threads/detail":
                from .thread_webui import ThreadWebUIHandlers
                ThreadWebUIHandlers()._api_threads_detail(self, plugin, qs)
            elif path == "/api/threads/operations":
                from .thread_webui import ThreadWebUIHandlers
                ThreadWebUIHandlers()._api_threads_operations(self, plugin, qs)
            elif path == "/api/threads/claims":
                from .thread_webui import ThreadWebUIHandlers
                ThreadWebUIHandlers()._api_threads_claims(self, plugin, qs)
            elif path == "/api/threads/claim-transitions":
                from .thread_webui import ThreadWebUIHandlers
                ThreadWebUIHandlers()._api_threads_claim_transitions(self, plugin, qs)
            elif path == "/api/threads/view-history":
                from .thread_webui import ThreadWebUIHandlers
                ThreadWebUIHandlers()._api_threads_view_history(self, plugin, qs)
            elif path == "/api/threads/prospective":
                from .thread_webui import ThreadWebUIHandlers
                ThreadWebUIHandlers()._api_threads_prospective(self, plugin, qs)
            elif path == "/api/threads/observations":
                from .thread_webui import ThreadWebUIHandlers
                ThreadWebUIHandlers()._api_threads_observations(self, plugin, qs)
            elif path == "/api/threads/eval-cases":
                from .thread_webui import ThreadWebUIHandlers
                ThreadWebUIHandlers()._api_threads_eval_cases(self, plugin, qs)
            elif path == "/api/threads/feedback":
                from .thread_webui import ThreadWebUIHandlers
                ThreadWebUIHandlers()._api_threads_feedback(self, plugin, qs)
            elif path == "/api/threads/consistency":
                from .thread_webui import ThreadWebUIHandlers
                ThreadWebUIHandlers()._api_threads_consistency(self, plugin, qs)
            elif path.startswith("/api/threads/consistency/detail/"):
                from .thread_webui import ThreadWebUIHandlers
                ThreadWebUIHandlers()._api_threads_consistency_detail(
                    self, plugin, unquote(path[len("/api/threads/consistency/detail/"):])
                )
            elif path == "/access":
                _html_response(self, access_html)
            elif path == "/forgetting":
                _html_response(self, forgetting_html)
            elif path == "/api/status":
                self._api_status()
            elif path == "/api/stats":
                self._api_stats()
            elif path == "/api/passages/status":
                self._api_passages_status()
            elif path == "/api/episodic/status":
                self._api_episodic_status()
            elif path == "/api/production/overview":
                self._api_production_overview(qs)
            elif path == "/api/production/v2":
                self._api_production_v2(False, qs)
            elif path == "/api/access/overview":
                self._api_access_overview()
            elif path == "/api/access/memories":
                self._api_access_memories(qs)
            elif path.startswith("/api/access/detail/"):
                self._api_access_detail(unquote(path[len("/api/access/detail/"):]))
            elif path == "/api/access/interference":
                self._api_access_interference(qs)
            elif path == "/api/access/events":
                self._api_access_events(qs)
            elif path == "/api/access/observations":
                self._api_access_observations(qs)
            elif path.startswith("/api/access/observation/"):
                self._api_access_observation_detail(
                    unquote(path[len("/api/access/observation/"):])
                )
            elif path == "/api/access/exports":
                self._api_access_exports()
            elif path == "/api/access/export/download":
                self._api_access_export_download(qs)
            elif path == "/api/access/eval_cases":
                self._api_access_eval_cases(qs)
            elif path == "/api/access/eval_run_latest":
                self._api_access_eval_run_latest()
            elif path == "/api/access/safety_gates":
                self._api_access_safety_gates()
            elif path == "/api/access/takeover_status":
                self._api_access_takeover_status()
            elif path == "/api/forgetting/overview":
                self._api_forgetting_overview()
            elif path == "/api/forgetting/presets":
                self._api_forgetting_presets()
            elif path == "/api/production/episodes":
                self._api_production_episodes(qs)
            elif path == "/api/production/snapshots":
                self._api_production_snapshots()
            elif path == "/api/production/long_diaries":
                self._api_production_long_diaries(qs)
            elif path == "/api/production/fallback_repairs":
                self._api_production_fallback_repairs(qs)
            elif path == "/api/production/rollbacks":
                self._api_production_rollbacks()
            elif path == "/api/production/previews":
                self._api_production_previews()
            elif path == "/api/episodic/memories":
                self._api_episodic_memories(qs)
            elif path.startswith("/api/episodic/detail/"):
                self._api_episodic_detail(unquote(path[len("/api/episodic/detail/"):]))
            elif path == "/api/state/status":
                self._api_semantic_state_status()
            elif path == "/api/eval/cases":
                self._api_eval_cases()
            elif path == "/api/eval/presets":
                self._json_ok({
                    "items": [
                        dict(
                            {"id": key},
                            **{
                                **value,
                                "values": _safe_preset_values(value.get("values")),
                            },
                        )
                        for key, value in recall_preset_definitions.items()
                    ]
                })
            elif path == "/api/eval/observations":
                self._api_eval_observations(qs)
            elif path == "/api/settings":
                self._api_settings()
            elif path == "/api/scheduling/settings":
                self._api_settings(scheduling=True)
            elif path == "/api/models":
                self._api_models()
            elif path == "/api/models/calls":
                self._api_model_calls()
            elif path == "/api/compensation":
                self._api_compensation()
            elif path == "/api/memories":
                self._api_memories(qs)
            elif path == "/api/timeline":
                self._api_timeline(qs)
            elif path == "/api/month-route":
                self._api_month_route(qs)
            elif path == "/api/search":
                self._api_search(qs)
            elif path == "/api/eval":
                self._api_eval(qs)
            elif path == "/api/health":
                self._api_health()
            elif path == "/api/affiliate/status":
                self._api_affiliate_status()
            elif path == "/api/time-insight/status":
                self._api_time_insight_status()
            elif path == "/api/time-insight/settings":
                self._api_time_insight_settings()
            elif path == "/api/managed-memos/status":
                self._api_managed_memos_status()
            elif path == "/api/data-backup/list":
                self._api_data_backup_list()
            elif path == "/api/data-backup/inspect":
                self._api_data_backup_inspect(qs)
            elif path == "/api/data-backup/download":
                self._api_data_backup_download(qs)
            elif path == "/api/context/history":
                self._api_context_history(qs)
            elif path == "/api/context/backup":
                self._api_context_backup(qs)
            elif path == "/api/logs":
                self._api_logs(qs)
            elif path == "/api/console/logs":
                self._api_console_logs()
            elif path == "/api/console/stats":
                self._api_console_stats()
            elif path == "/api/xinchao/overview":
                self._api_xinchao_overview(qs)
            elif path == "/api/xinchao/state":
                self._api_xinchao_state(qs)
            elif path == "/api/xinchao/settings":
                self._api_xinchao_settings()
            elif path.startswith("/api/memo/"):
                self._api_memo_detail(unquote(path[len("/api/memo/"):]))
            elif path == "/api/test/llm":
                self._api_test_llm()
            elif path == "/api/test/emb":
                self._api_test_emb()
            elif path == "/api/test/rerank":
                self._api_test_rerank()
            elif path == "/api/graph":
                self._api_graph()
            elif path == "/api/graph/clusters":
                self._api_graph_clusters()
            elif path == "/api/graph/rebuild":
                self._api_graph_rebuild()
            elif path == "/api/graph/related":
                self._api_graph_related(qs)
            elif path == "/api/feedback":
                self._api_feedback()
            elif path == "/api/keywords":
                self._api_keywords()
            elif path == "/api/debug":
                self._api_debug()
            else:
                _json_response(self, 404, {"ok": False, "error": "Not found"})

        def do_POST(self):
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            allowed = {
                "/api/settings/save", "/api/settings/preset",
                "/api/models/save", "/api/models/test", "/api/models/probe",
                "/api/compensation/restore", "/api/compensation/dismiss",
                "/api/feedback/apply",
                "/api/episodic/rebuild",
                "/api/state/rebuild", "/api/eval/case", "/api/eval/run",
                "/api/eval/auto_cases", "/api/eval/preset", "/api/eval/observation_feedback",
                "/api/xinchao/settings/save", "/api/xinchao/settings/test-api",
                "/api/xinchao/settle",
                "/api/xinchao/feedback", "/api/xinchao/thought",
                "/api/xinchao/simulate", "/api/xinchao/reset",
                "/api/time-insight/settings/save", "/api/time-insight/update",
                "/api/time-insight/preview",
                "/api/production/restore_snapshot",
                "/api/production/v2/migrate_preview",
                "/api/production/v2/draft",
                "/api/production/v2/resume_delivery",
                "/api/production/v2/control",
                "/api/production/v2/mode",
                "/api/production/long_diary_preview",
                "/api/production/long_diary_confirm",
                "/api/production/long_diary_rollback",
                "/api/production/long_diary_discard",
                "/api/production/fallback_repair_preview",
                "/api/production/fallback_repair_confirm",
                "/api/production/generation_switch",
                "/api/production/generation_rollback",
                "/api/access/rebuild", "/api/access/maintenance", "/api/access/simulate",
                "/api/threads/pause", "/api/threads/resume", "/api/threads/decide",
                "/api/threads/merge/preview", "/api/threads/merge/apply",
                "/api/threads/split/preview", "/api/threads/split/apply",
                "/api/threads/operation/revert",
                "/api/threads/claim/status", "/api/threads/prospective/status",
                "/api/threads/rebuild-derived", "/api/threads/prospective/simulate",
                "/api/threads/lab", "/api/threads/eval/run", "/api/threads/lab/feedback",
                "/api/threads/policy", "/api/threads/maintenance",
                "/api/threads/consistency/export", "/api/threads/consistency/feedback",
                "/api/access/eval_cases/generate", "/api/access/eval_cases/enabled",
                "/api/access/eval_run", "/api/access/eval_run_results",
                "/api/access/eval_cases/confirm",
                "/api/access/takeover_reset",
                "/api/access/observation_feedback", "/api/access/export",
                "/api/forgetting/preset", "/api/forgetting/feedback",
                "/api/data-backup/run",
                "/api/data-backup/restore",
                "/api/data-backup/cancel-restore",
                "/api/data-backup/delete",
                "/api/house/settings/save", "/api/house/settings/test",
                "/api/house/artifact/read", "/api/house/artifact/feedback",
                "/api/house/artifact/rewrite", "/api/house/artifact/archive",
                "/api/house/artifact/delete", "/api/house/artifact/send",
                "/api/house/generate/preview", "/api/house/generate/now",
                "/api/house/session/settle", "/api/house/character/interact",
            }
            if path not in allowed:
                return _json_response(self, 404, {"ok": False, "error": "Not found"})
            if not self._same_origin_write_allowed():
                # Drain a bounded rejected body before closing. Unread POST bytes
                # can otherwise turn the intended 403 into a TCP reset on Windows.
                old_timeout = self.connection.gettimeout()
                try:
                    rejected_length = int(self.headers.get("Content-Length") or 0)
                    if 0 < rejected_length <= 1024 * 1024:
                        self.connection.settimeout(0.25)
                        self.rfile.read(rejected_length)
                except (ValueError, OSError):
                    pass
                finally:
                    self.connection.settimeout(old_timeout)
                return self._json_err("跨来源写入已拒绝", 403)
            try:
                length = int(self.headers.get("Content-Length") or 0)
                if length <= 0 or length > 1024 * 1024:
                    return self._json_err("请求体为空或过大", 400)
                body = json.loads(self.rfile.read(length).decode("utf-8"))
                if not isinstance(body, dict):
                    return self._json_err("请求格式错误", 400)
                if path.startswith("/api/house/"):
                    from .house_webui import HouseWebUIHandlers
                    if not HouseWebUIHandlers.handle_post(self, path, body):
                        self._json_err("Not found", 404)
                elif path == "/api/settings/save":
                    self._api_settings_save(body)
                elif path.startswith("/api/models/"):
                    self._api_models_write(path, body)
                elif path.startswith("/api/compensation/"):
                    self._api_compensation_write(path, body)
                elif path == "/api/threads/policy":
                    self._api_threads_policy(body)
                elif path == "/api/threads/maintenance":
                    self._api_threads_maintenance(body)
                elif path == "/api/settings/preset":
                    self._api_settings_preset(body)
                elif path == "/api/feedback/apply":
                    self._api_feedback_apply(body)
                elif path == "/api/episodic/rebuild":
                    self._api_episodic_rebuild()
                elif path == "/api/state/rebuild":
                    self._api_semantic_state_rebuild()
                elif path == "/api/eval/case":
                    self._api_eval_case_save(body)
                elif path == "/api/eval/run":
                    self._api_eval_run(body)
                elif path == "/api/eval/auto_cases":
                    self._api_eval_auto_cases(body)
                elif path == "/api/eval/preset":
                    self._api_eval_preset(body)
                elif path == "/api/eval/observation_feedback":
                    self._api_eval_observation_feedback(body)
                elif path == "/api/production/restore_snapshot":
                    self._api_production_restore_snapshot(body)
                elif path == "/api/production/v2/migrate_preview":
                    self._api_production_v2(True)
                elif path == "/api/production/v2/draft":
                    self._api_production_v2_draft(body)
                elif path == "/api/production/v2/resume_delivery":
                    self._api_production_v2_draft(body, delivery=True)
                elif path == "/api/production/v2/control":
                    self._api_production_v2_draft(body, control=True)
                elif path == "/api/production/v2/mode":
                    self._api_production_v2_mode(body)
                elif path == "/api/production/long_diary_preview":
                    self._api_production_long_diary_preview(body)
                elif path == "/api/production/long_diary_confirm":
                    self._api_production_long_diary_confirm(body)
                elif path == "/api/production/long_diary_rollback":
                    self._api_production_long_diary_rollback(body)
                elif path == "/api/production/long_diary_discard":
                    self._api_production_long_diary_discard(body)
                elif path == "/api/production/fallback_repair_preview":
                    self._api_production_fallback_repair_preview(body)
                elif path == "/api/production/fallback_repair_confirm":
                    self._api_production_fallback_repair_confirm(body)
                elif path == "/api/production/generation_switch":
                    self._api_production_generation_switch()
                elif path == "/api/production/generation_rollback":
                    self._api_production_generation_rollback()
                elif path == "/api/access/rebuild":
                    self._api_access_rebuild()
                elif path == "/api/access/maintenance":
                    self._api_access_maintenance()
                elif path == "/api/access/simulate":
                    self._api_access_simulate(body)
                elif path == "/api/threads/pause":
                    from .thread_webui import ThreadWebUIHandlers
                    ThreadWebUIHandlers()._api_threads_pause(self, plugin)
                elif path == "/api/threads/resume":
                    from .thread_webui import ThreadWebUIHandlers
                    ThreadWebUIHandlers()._api_threads_resume(self, plugin)
                elif path == "/api/threads/decide":
                    from .thread_webui import ThreadWebUIHandlers
                    ThreadWebUIHandlers()._api_threads_decide(self, plugin, body)
                elif path == "/api/threads/merge/preview":
                    from .thread_webui import ThreadWebUIHandlers
                    ThreadWebUIHandlers()._api_threads_merge_preview(self, plugin, body)
                elif path == "/api/threads/merge/apply":
                    from .thread_webui import ThreadWebUIHandlers
                    ThreadWebUIHandlers()._api_threads_merge_apply(self, plugin, body)
                elif path == "/api/threads/split/preview":
                    from .thread_webui import ThreadWebUIHandlers
                    ThreadWebUIHandlers()._api_threads_split_preview(self, plugin, body)
                elif path == "/api/threads/split/apply":
                    from .thread_webui import ThreadWebUIHandlers
                    ThreadWebUIHandlers()._api_threads_split_apply(self, plugin, body)
                elif path == "/api/threads/operation/revert":
                    from .thread_webui import ThreadWebUIHandlers
                    ThreadWebUIHandlers()._api_threads_revert(self, plugin, body)
                elif path == "/api/threads/claim/status":
                    from .thread_webui import ThreadWebUIHandlers
                    ThreadWebUIHandlers()._api_threads_claim_status(self, plugin, body)
                elif path == "/api/threads/prospective/status":
                    from .thread_webui import ThreadWebUIHandlers
                    ThreadWebUIHandlers()._api_threads_prospective_status(self, plugin, body)
                elif path == "/api/threads/rebuild-derived":
                    from .thread_webui import ThreadWebUIHandlers
                    ThreadWebUIHandlers()._api_threads_rebuild_derived(self, plugin, body)
                elif path == "/api/threads/prospective/simulate":
                    from .thread_webui import ThreadWebUIHandlers
                    ThreadWebUIHandlers()._api_threads_prospective_simulate(self, plugin, body)
                elif path == "/api/threads/lab":
                    from .thread_webui import ThreadWebUIHandlers
                    ThreadWebUIHandlers()._api_threads_lab(self, plugin, body)
                elif path == "/api/threads/eval/run":
                    from .thread_webui import ThreadWebUIHandlers
                    ThreadWebUIHandlers()._api_threads_eval_run(self, plugin, body)
                elif path == "/api/threads/lab/feedback":
                    from .thread_webui import ThreadWebUIHandlers
                    ThreadWebUIHandlers()._api_threads_lab_feedback(self, plugin, body)
                elif path == "/api/threads/consistency/export":
                    from .thread_webui import ThreadWebUIHandlers
                    ThreadWebUIHandlers()._api_threads_consistency_export(self, plugin, body)
                elif path == "/api/threads/consistency/feedback":
                    from .thread_webui import ThreadWebUIHandlers
                    ThreadWebUIHandlers()._api_threads_consistency_feedback(self, plugin, body)
                elif path == "/api/access/eval_cases/generate":
                    self._api_access_eval_cases_generate(body)
                elif path == "/api/access/eval_cases/enabled":
                    self._api_access_eval_case_enabled(body)
                elif path == "/api/access/eval_run":
                    self._api_access_eval_run(body)
                elif path == "/api/access/eval_run_results":
                    self._api_access_eval_run_results(body)
                elif path == "/api/access/eval_cases/confirm":
                    self._api_access_eval_case_confirm(body)
                elif path == "/api/access/takeover_reset":
                    self._api_access_takeover_reset()
                elif path == "/api/access/observation_feedback":
                    self._api_access_observation_feedback(body)
                elif path == "/api/access/export":
                    self._api_access_export()
                elif path == "/api/forgetting/preset":
                    self._api_forgetting_preset(body)
                elif path == "/api/forgetting/feedback":
                    self._api_forgetting_feedback(body)
                elif path == "/api/data-backup/run":
                    self._api_data_backup_run()
                elif path == "/api/data-backup/restore":
                    self._api_data_backup_restore(body)
                elif path == "/api/data-backup/cancel-restore":
                    self._api_data_backup_cancel_restore()
                elif path == "/api/data-backup/delete":
                    self._api_data_backup_delete(body)
                elif path.startswith("/api/time-insight/"): 
                    self._api_time_insight_write(path, body)
                else:
                    self._api_xinchao_write(path, body)
            except json.JSONDecodeError:
                self._json_err("JSON 格式错误", 400)
            except Exception as e:
                self._json_err(str(e))

        def _same_origin_write_allowed(self) -> bool:
            origin = str(self.headers.get("Origin") or "").strip()
            if not origin:
                return True
            try:
                parsed = urlparse(origin)
                return parsed.scheme in {"http", "https"} and parsed.netloc == str(self.headers.get("Host") or "")
            except Exception:
                return False

        def do_DELETE(self):
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            if path == "/api/console/logs":
                plugin._log_events.clear()
                if hasattr(plugin, "_save_runtime_telemetry"):
                    plugin._telemetry_dirty = True
                    plugin._save_runtime_telemetry(force=True)
                self._json_ok({"cleared": True})
            elif path == "/api/logs":
                plugin._log_events.clear()
                if hasattr(plugin, "_save_runtime_telemetry"):
                    plugin._telemetry_dirty = True
                    plugin._save_runtime_telemetry(force=True)
                self._json_ok({"cleared": True})
            elif path.startswith("/api/feedback/"):
                if not self._same_origin_write_allowed():
                    return self._json_err("跨来源写入已拒绝", 403)
                try:
                    event_id = int(path[len("/api/feedback/"):])
                    vec = getattr(plugin, "_vec", None)
                    if vec is None:
                        return self._json_err("vec not ready", 503)
                    self._json_ok({"deleted": vec.feedback_event_delete(event_id)})
                except Exception as e:
                    self._json_err(str(e), 400)
            elif path.startswith("/api/eval/case/"):
                if not self._same_origin_write_allowed():
                    return self._json_err("跨来源写入已拒绝", 403)
                try:
                    store = getattr(plugin, "_episodes", None)
                    if store is None:
                        return self._json_err("情景记忆库未就绪", 503)
                    case_id = unquote(path[len("/api/eval/case/"):])
                    self._json_ok({"deleted": store.delete_eval_case(case_id)})
                except Exception as e:
                    self._json_err(str(e), 400)
            else:
                _json_response(self, 404, {"ok": False, "error": "Not found"})

        def _json_ok(self, data):
            """Wrap response as {ok: true, data: ...} to match dashboard/console JS."""
            _json_response(self, 200, {"ok": True, "data": data})

        def _json_err(self, msg, code=500):
            _json_response(self, code, {"ok": False, "error": msg})

        def _api_status(self):
            try:
                plg = plugin
                vec = getattr(plg, "_vec", None)
                passage_stats = vec.passage_index_stats() if vec is not None else {}
                episodes = getattr(plg, "_episodes", None)
                episode_stats = episodes.stats() if episodes is not None else {}
                session_times = list((getattr(plg, "_session_time", {}) or {}).values())
                latest_time = max(
                    (item for item in session_times if isinstance(item, dict)),
                    key=lambda item: float(item.get("snapshot_ts") or item.get("ts") or 0.0),
                    default={},
                )
                xinchao_settings = getattr(getattr(plg, "_xinchao", None), "settings", {}) or {}
                data = {
                    "plugin_version": getattr(plg, "_PLUGIN_VERSION", "?"),
                    "init_ok": getattr(plg, "_initialized", False),
                    "init_error": getattr(plg, "_init_error", ""),
                    "character": getattr(plg, "character_name", "") or "(unset)",
                    "memos_url": getattr(plg, "memos_base_url", ""),
                    "memos_mode": getattr(plg, "memos_mode", "external"),
                    "memos_diag": self._memos_diag(),
                    "managed_memos": plg._managed_memos.as_dict() if hasattr(plg, "_managed_memos") else {"mode": "external"},
                    "emb_provider_name": getattr(plg, "_emb_model_id", "") or "(unset)",
                    "emb_cache_used": len(getattr(plg, "_emb_cache", {})),
                    "emb_cache_size": getattr(plg, "emb_cache_size", 0),
                    "compress_count": getattr(plg, "_compress_count", 0),
                    "eod_checkpoint": {
                        "enabled": bool(getattr(plg, "eod_checkpoint_enable", False)),
                        "schedule": "23:45",
                        "min_turns": int(getattr(plg, "eod_checkpoint_min_turns", 1) or 1),
                        "max_diaries": int(getattr(plg, "eod_checkpoint_max_diaries", 3) or 3),
                        "last": dict(getattr(plg, "_eod_last_status", {}) or {}),
                    },
                    "data_backup": (
                        plg._data_backup_status()
                        if hasattr(plg, "_data_backup_status")
                        else {"enabled": False, "count": 0}
                    ),
                    "reconcile_total_deleted": getattr(plg, "_reconcile_stats", {}).get("deleted", 0),
                    "last_reconcile_ts": getattr(plg, "_last_reconcile_ts", 0),
                    "rerank_provider": getattr(plg, "rerank_provider_id", "") or "(off)",
                    "rerank_active": getattr(plg, "_rerank_provider", None) is not None,
                    "rp_enhancer_enable": getattr(plg, "rp_enhancer_enable", False),
                    "rp_inject_max_chars": getattr(plg, "rp_inject_max_chars", 0),
                    "rp_last_stats": list(getattr(plg, "_rp_stats", {}).values())[-5:],
                    "context_governance_enable": getattr(plg, "context_governance_enable", False),
                    "context_exclude_command_turns": getattr(plg, "context_exclude_command_turns", True),
                    "context_archive_enable": getattr(plg, "context_archive_enable", False),
                    "context_stats": getattr(plg, "_context_stats", {}),
                    "recall_architecture": {
                        "active": (
                            "lean_full_memory_fusion" if (
                                episode_stats.get("episodes")
                                and getattr(plg, "_episode_migration_ready", False)
                                and getattr(plg, "lean_recall_enable", False)
                            ) else ("episodic_cascade" if (
                                episode_stats.get("episodes") and getattr(plg, "_episode_migration_ready", False)
                            ) else "legacy_hybrid")
                        ),
                        "episodic_enabled": bool(getattr(plg, "episodic_memory_enable", False)),
                        "episodic_ready": bool(
                            episode_stats.get("episodes") and getattr(plg, "_episode_migration_ready", False)
                        ),
                        "lean_enabled": bool(getattr(plg, "lean_recall_enable", False)),
                        "event_index": bool(getattr(plg, "lean_event_index_enable", False)),
                        "source_evidence": bool(getattr(plg, "lean_source_evidence_enable", False)),
                        "coverage_selection": bool(getattr(plg, "lean_coverage_selection_enable", False)),
                        "adaptive_evidence": bool(getattr(plg, "lean_adaptive_evidence_enable", False)),
                        "candidate_k": int(getattr(plg, "lean_recall_candidate_k", 0) or 0),
                        "story_min": int(getattr(plg, "lean_story_min_inject", 0) or 0),
                        "story_max": int(getattr(plg, "lean_story_max_inject", 0) or 0),
                        "story_normal": int(getattr(plg, "lean_story_normal_inject", 0) or 0),
                        "relative_margin": float(getattr(plg, "lean_relative_margin", 0.24) or 0.24),
                        "relative_margin_broad": float(getattr(plg, "lean_relative_margin_broad", 0.34) or 0.34),
                        "safety_net": bool(getattr(plg, "recall_safety_net_enable", False)),
                        "safety_net_min_selected": int(getattr(plg, "recall_safety_net_min_selected", 0) or 0),
                        "safety_net_min_top_score": float(getattr(plg, "recall_safety_net_min_top_score", 0.0) or 0.0),
                        "inject_char_budget": int(getattr(plg, "inject_char_budget", 0) or 0),
                        "inject_compact_chars": int(getattr(plg, "inject_compact_chars", 0) or 0),
                        "temporal_on_demand": bool(getattr(plg, "lean_temporal_enable", False)),
                        "multi_query": False if getattr(plg, "lean_recall_enable", False) else bool(getattr(plg, "recall_multi_query_enable", False)),
                        "month_route": False if getattr(plg, "lean_recall_enable", False) else bool(getattr(plg, "recall_month_route_enable", False)),
                        "month_route_parallel": False,
                        "month_route_separate_quota": False,
                        "month_route_inject_max": int(getattr(plg, "recall_month_route_inject_max", 0) or 0),
                        "information_gain": False if getattr(plg, "lean_recall_enable", False) else bool(getattr(plg, "recall_information_gain_enable", False)),
                        "necessary_can_exceed_max": False if getattr(plg, "lean_recall_enable", False) else bool(getattr(plg, "recall_necessary_can_exceed_max", False)),
                        "necessary_hard_cap": int(getattr(plg, "recall_necessary_hard_cap", 0) or 0),
                        "passage_index": bool(getattr(plg, "passage_index_enable", False)),
                        "mixed_injection": bool(getattr(plg, "mixed_injection_enable", False)),
                    },
                    "passage_stats": passage_stats,
                    "episodic_stats": episode_stats,
                    "episodic_migration": dict(getattr(plg, "_episode_migration_state", {}) or {}),
                    "passage_vector_migration": dict(getattr(plg, "_passage_vector_migration_state", {}) or {}),
                    "profile": plugin._affiliate_profile_status() if hasattr(plugin, "_affiliate_profile_status") else {"enabled": False, "connected": False},
                    "semantic_state": plugin._semantic_state_status(include_pending_preview=True) if hasattr(plugin, "_semantic_state_status") else {"enabled": False, "ready": False},
                    "time_insight": plugin._time_insight_status() if hasattr(plugin, "_time_insight_status") else {"enabled": False, "connected": False},
                    "time_model": {
                        "request_snapshot": True,
                        "current_timezone": getattr(plg, "rp_time_timezone", "Asia/Shanghai"),
                        "body_timezone": xinchao_settings.get(
                            "time_zone", getattr(plg, "rp_time_timezone", "Asia/Shanghai")
                        ) if isinstance(xinchao_settings, dict) else getattr(plg, "rp_time_timezone", "Asia/Shanghai"),
                        "solar_date": latest_time.get("solar_date", ""),
                        "period": latest_time.get("period", ""),
                        "snapshot_ts": float(latest_time.get("snapshot_ts") or latest_time.get("ts") or 0.0),
                    },
                    "last_reconcile_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(getattr(plg, "_last_reconcile_ts", 0)))
                        if getattr(plg, "_last_reconcile_ts", 0) else "\u2014",
                }
                self._json_ok(data)
            except Exception as e:
                self._json_err(str(e))

        def _api_data_backup_run(self):
            runner = getattr(plugin, "_run_data_backup_once", None)
            if not callable(runner):
                return self._json_err("数据备份服务未就绪", 503)
            try:
                result = self._run_plugin_coro(
                    runner(force=True, reason="webui_manual"), timeout=180,
                )
                if not isinstance(result, dict) or not result.get("created"):
                    reason = (result or {}).get("error") or (result or {}).get("reason") or "unknown"
                    return self._json_err("备份未创建: " + str(reason), 503)
                self._json_ok(result)
            except RuntimeError as exc:
                self._json_err(str(exc), 503)

        def _api_data_backup_list(self):
            runner = getattr(plugin, "_data_backup_list", None)
            if not callable(runner):
                return self._json_err("数据备份服务未就绪", 503)
            try:
                self._json_ok(self._run_plugin_coro(runner(), timeout=30))
            except RuntimeError as exc:
                self._json_err(str(exc), 503)

        def _api_data_backup_inspect(self, qs):
            file_name = str((qs.get("file") or [""])[0]).strip()
            if not file_name:
                return self._json_err("缺少备份文件名", 400)
            runner = getattr(plugin, "_data_backup_inspect", None)
            if not callable(runner):
                return self._json_err("数据备份服务未就绪", 503)
            try:
                self._json_ok(self._run_plugin_coro(runner(file_name), timeout=60))
            except (RuntimeError, ValueError, FileNotFoundError) as exc:
                self._json_err(str(exc), 400)

        def _api_data_backup_download(self, qs):
            file_name = str((qs.get("file") or [""])[0]).strip()
            if not file_name:
                return self._json_err("缺少备份文件名", 400)
            manager = getattr(plugin, "_data_backup", None)
            if manager is None:
                return self._json_err("数据备份服务未就绪", 503)
            try:
                path = manager.archive_path(file_name)
                self.send_response(200)
                for key, value in _cors_headers().items():
                    self.send_header(key, value)
                self.send_header("Content-Type", "application/zip")
                self.send_header("Content-Disposition", f'attachment; filename="{path.name}"')
                self.send_header("Content-Length", str(path.stat().st_size))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                with path.open("rb") as stream:
                    while True:
                        chunk = stream.read(1024 * 1024)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
            except (ValueError, FileNotFoundError) as exc:
                self._json_err(str(exc), 404)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as exc:
                self._json_err(str(exc), 500)

        def _api_data_backup_restore(self, body):
            file_name = str(body.get("file") or "").strip()
            if not file_name:
                return self._json_err("缺少备份文件名", 400)
            if str(body.get("confirm") or "") != "RESTORE_ON_RELOAD":
                return self._json_err("恢复确认无效", 400)
            runner = getattr(plugin, "_prepare_data_restore", None)
            if not callable(runner):
                return self._json_err("数据恢复服务未就绪", 503)
            try:
                self._json_ok(self._run_plugin_coro(runner(file_name), timeout=240))
            except (RuntimeError, ValueError, FileNotFoundError) as exc:
                self._json_err(str(exc), 400)

        def _api_data_backup_cancel_restore(self):
            runner = getattr(plugin, "_cancel_data_restore", None)
            if not callable(runner):
                return self._json_err("数据恢复服务未就绪", 503)
            try:
                self._json_ok(self._run_plugin_coro(runner(), timeout=30))
            except RuntimeError as exc:
                self._json_err(str(exc), 503)

        def _api_data_backup_delete(self, body):
            file_name = str(body.get("file") or "").strip()
            if not file_name:
                return self._json_err("缺少备份文件名", 400)
            runner = getattr(plugin, "_delete_data_backup", None)
            if not callable(runner):
                return self._json_err("数据备份服务未就绪", 503)
            try:
                self._json_ok(self._run_plugin_coro(runner(file_name), timeout=30))
            except (RuntimeError, ValueError, FileNotFoundError) as exc:
                self._json_err(str(exc), 400)
            except Exception as exc:
                self._json_err(str(exc))

        def _api_passages_status(self):
            try:
                plg = plugin
                vec = getattr(plg, "_vec", None)
                stats = vec.passage_index_stats() if vec is not None else {
                    "memos": 0, "passages": 0, "traced_passages": 0,
                    "traced_memos": 0, "multi_passage_memos": 0, "machine_meta_memos": 0,
                }
                stats.update({
                    "enabled": bool(getattr(plg, "passage_index_enable", False)),
                    "mixed_injection": bool(getattr(plg, "mixed_injection_enable", False)),
                    "passage_max_chars": int(getattr(plg, "passage_max_chars", 0) or 0),
                    "passage_overlap_chars": int(getattr(plg, "passage_overlap_chars", 0) or 0),
                    "full_diary_top_n": int(getattr(plg, "full_diary_top_n", 0) or 0),
                    "passage_expand_chars": int(getattr(plg, "passage_expand_chars", 0) or 0),
                    "vector_strategy": vec.get_meta_value("passage_embedding_strategy") if vec is not None else "",
                    "vector_migration": dict(getattr(plg, "_passage_vector_migration_state", {}) or {}),
                    "rebuild_command": "/memos-passage-rebuild",
                    "note": "重建只更新本地派生索引，不改写 Memos 日记、查询反馈或画像。",
                })
                self._json_ok(stats)
            except Exception as e:
                self._json_err(str(e))

        @staticmethod
        def _production_list(value: Any) -> list[dict[str, Any]]:
            return [dict(item) for item in value if isinstance(item, dict)] if isinstance(value, (list, tuple)) else []

        @staticmethod
        def _public_production_preview(item: dict[str, Any]) -> dict[str, Any]:
            payload = item.get("payload") if isinstance(item.get("payload"), dict) else item
            diaries = payload.get("new_diaries") if isinstance(payload.get("new_diaries"), list) else []
            return {
                "preview_id": str(item.get("preview_id") or payload.get("preview_id") or ""),
                "episode_id": str(item.get("episode_id") or payload.get("episode_id") or ""),
                "old_memo_name": str(item.get("old_memo_name") or payload.get("old_memo_name") or ""),
                "mode": str(payload.get("mode") or ""),
                "status": str(item.get("status") or "pending"),
                "source_turns": int(payload.get("source_turns") or 0),
                "new_diaries_count": len(diaries),
                "new_diaries": [
                    {
                        "episode_key": str(d.get("episode_key") or ""),
                        "content": str(d.get("content") or ""),
                        "scene_start_turn": d.get("scene_start_turn"),
                        "scene_end_turn": d.get("scene_end_turn"),
                        "must_coverage": d.get("must_coverage", d.get("_must_coverage")),
                    }
                    for d in diaries if isinstance(d, dict)
                ],
            }

        @staticmethod
        def _public_production_rollback(item: dict[str, Any]) -> dict[str, Any]:
            return {key: item.get(key) for key in (
                "id", "memo_name", "episode_id", "note", "created_ts", "reverted_ts",
            )}

        def _production_store(self):
            return getattr(plugin, "_episodes", None)

        def _api_production_v2(self, migrate=False, qs=None):
            from .generation_v2.workbench import overview, migration_preview
            try:
                offset=int((qs or {}).get('offset',['0'])[0])
                self._json_ok(migration_preview(plugin) if migrate else overview(plugin,offset))
            except Exception as exc:
                self._json_err(type(exc).__name__ + ": V2 production operation failed", 409)

        def _api_production_v2_mode(self, body):
            from .generation_v2.operations import mode_settings, readiness
            try:
                values = mode_settings(plugin, body)
                result = self._save_settings_values(values, "production-mode")
                result['readiness'] = readiness(plugin)
                self._json_ok(result)
            except (ValueError, RuntimeError) as exc:
                self._json_err(str(exc), 409)

        def _api_production_v2_draft(self, body, delivery=False, control=False):
            from .generation_v2.plugin_gateway import start_draft
            from .generation_v2.integration import resume_delivery
            from .generation_v2.store import ConflictError
            loop = getattr(getattr(plugin, "_webui", None), "_loop", None)
            if loop is None or not loop.is_running():
                return self._json_err("Astr event loop unavailable", 503)
            from .generation_v2.operations import operate
            operation = operate if control else resume_delivery if delivery else start_draft
            future = asyncio.run_coroutine_threadsafe(operation(plugin, body), loop)
            try:
                self._json_ok(future.result(timeout=15))
            except ConflictError as exc:
                self._json_err("操作未执行：" + str(exc), 409)
            except Exception as exc:
                self._json_err(type(exc).__name__ + ": check production progress before retrying", 409)

        def _production_helper(self, name: str):
            helper = getattr(plugin, name, None)
            if not callable(helper):
                raise RuntimeError(f"记忆生产能力不可用: {name}")
            return helper

        def _access_store(self):
            return getattr(plugin, "_episodes", None)

        def _api_access_overview(self):
            store = self._access_store()
            if store is None:
                return self._json_ok({
                    "ready": False,
                    "reason": "情景记忆库尚未就绪；4.6 召回不受影响",
                    "runtime": dict(getattr(plugin, "_memory_access_state", {}) or {}),
                })
            try:
                overview = dict(store.memory_access_overview() or {})
                overview.update({
                    "ready": True,
                    "plugin_version": getattr(plugin, "_PLUGIN_VERSION", "?"),
                    "runtime": dict(getattr(plugin, "_memory_access_state", {}) or {}),
                    "settings": {
                        "enable": bool(getattr(plugin, "memory_forgetting_enable", True)),
                        "shadow_mode": bool(getattr(plugin, "memory_forgetting_shadow_mode", True)),
                        "takeover_enable": bool(getattr(plugin, "memory_access_takeover_enable", False)),
                        "decay_days": float(getattr(plugin, "memory_access_decay_days", 45.0)),
                        "vivid_threshold": float(getattr(plugin, "memory_access_vivid_threshold", 0.68)),
                        "deep_threshold": float(getattr(plugin, "memory_access_deep_threshold", 0.32)),
                        "exact_cue_relief": float(getattr(plugin, "memory_access_exact_cue_relief", 0.85)),
                    },
                    "contract": {
                        "source_of_truth": "Memos 日记 + 原文档案 + 情景卡",
                        "destructive": False,
                        "changes_live_recall": bool(getattr(plugin, "memory_access_takeover_enable", False)),
                        "policy": "默认 Shadow；接管仅在人工确认评测全绿后追加 A/B 级精确命中",
                    },
                })
                self._json_ok(overview)
            except Exception as exc:
                self._json_err(str(exc))

        def _api_access_memories(self, qs):
            store = self._access_store()
            if store is None:
                return self._json_err("情景记忆库未就绪", 503)
            try:
                state = str(qs.get("state", [""])[0] or "")
                query = str(qs.get("q", [""])[0] or "")[:100]
                limit = max(1, min(500, int(qs.get("limit", ["120"])[0] or 120)))
                offset = max(0, int(qs.get("offset", ["0"])[0] or 0))
                items = store.list_memory_access_states(
                    state=state, query=query, limit=limit, offset=offset,
                )
                self._json_ok({"items": items, "count": len(items), "state": state, "query": query})
            except Exception as exc:
                self._json_err(str(exc), 400)

        def _api_access_detail(self, memo_name: str):
            store = self._access_store()
            if store is None:
                return self._json_err("情景记忆库未就绪", 503)
            try:
                detail = store.memory_access_detail(memo_name)
                if not detail:
                    return self._json_err("未找到该记忆的可达状态", 404)
                episode = store.episode_detail(memo_name)
                self._json_ok({"access": detail, "episode": episode})
            except Exception as exc:
                self._json_err(str(exc), 400)

        def _api_access_interference(self, qs):
            store = self._access_store()
            if store is None:
                return self._json_err("情景记忆库未就绪", 503)
            try:
                memo_name = str(qs.get("memo", [""])[0] or "")
                edge_type = str(qs.get("type", [""])[0] or "")
                limit = max(1, min(1000, int(qs.get("limit", ["240"])[0] or 240)))
                items = store.list_memory_interference(
                    memo_name=memo_name, edge_type=edge_type, limit=limit,
                )
                self._json_ok({"items": items, "count": len(items)})
            except Exception as exc:
                self._json_err(str(exc), 400)

        def _api_access_events(self, qs):
            store = self._access_store()
            if store is None:
                return self._json_err("情景记忆库未就绪", 503)
            try:
                memo_name = str(qs.get("memo", [""])[0] or "")
                limit = max(1, min(1000, int(qs.get("limit", ["120"])[0] or 120)))
                items = store.list_memory_access_events(memo_name=memo_name, limit=limit)
                self._json_ok({"items": items, "count": len(items)})
            except Exception as exc:
                self._json_err(str(exc), 400)

        def _api_access_observations(self, qs):
            store = self._access_store()
            if store is None:
                return self._json_err("情景记忆库未就绪", 503)
            try:
                limit = max(1, min(500, int((qs.get("limit") or [100])[0])))
                offset = max(0, int((qs.get("offset") or [0])[0]))
                changed_only = str((qs.get("changed") or [""])[0]).lower() in {"1", "true", "yes"}
                rescued_only = str((qs.get("rescued") or [""])[0]).lower() in {"1", "true", "yes"}
                query = str((qs.get("q") or [""])[0]).strip()
                self._json_ok({
                    "items": store.list_memory_access_observations(
                        limit=limit, offset=offset, changed_only=changed_only,
                        rescued_only=rescued_only, query=query,
                    ),
                    "summary_30d": store.memory_access_observation_summary(days=30),
                    "summary_all": store.memory_access_observation_summary(days=0),
                })
            except (TypeError, ValueError) as exc:
                self._json_err(str(exc), 400)
            except Exception as exc:
                self._json_err(str(exc), 500)

        def _api_access_observation_detail(self, request_id: str):
            store = self._access_store()
            if store is None:
                return self._json_err("情景记忆库未就绪", 503)
            try:
                detail = store.memory_access_observation_detail(request_id)
                if detail is None:
                    return self._json_err("评测记录不存在", 404)
                self._json_ok(detail)
            except Exception as exc:
                self._json_err(str(exc), 500)

        def _api_access_observation_feedback(self, body):
            store = self._access_store()
            if store is None:
                return self._json_err("情景记忆库未就绪", 503)
            try:
                result = store.record_memory_access_observation_feedback(
                    request_id=str(body.get("request_id") or ""),
                    verdict=str(body.get("verdict") or ""),
                    note=str(body.get("note") or ""),
                )
                self._json_ok(result)
            except ValueError as exc:
                self._json_err(str(exc), 400)
            except Exception as exc:
                self._json_err(str(exc), 500)

        def _api_access_exports(self):
            manager = getattr(plugin, "_access_export", None)
            if manager is None:
                return self._json_err("ACCESS 导出服务未就绪", 503)
            try:
                self._json_ok({"items": manager.list_archives()})
            except Exception as exc:
                self._json_err(str(exc), 500)

        def _api_access_export(self):
            store = self._access_store()
            manager = getattr(plugin, "_access_export", None)
            if store is None or manager is None:
                return self._json_err("ACCESS 导出服务未就绪", 503)
            try:
                payload = store.memory_access_export_payload(
                    observation_limit=int(getattr(plugin, "memory_access_observation_keep", 5000)),
                )
                self._json_ok(manager.create(
                    payload, plugin_version=str(getattr(plugin, "_PLUGIN_VERSION", "?")),
                ))
            except Exception as exc:
                self._json_err(str(exc), 500)

        def _api_access_export_download(self, qs):
            file_name = str((qs.get("file") or [""])[0]).strip()
            manager = getattr(plugin, "_access_export", None)
            if not file_name:
                return self._json_err("缺少导出文件名", 400)
            if manager is None:
                return self._json_err("ACCESS 导出服务未就绪", 503)
            try:
                path = manager.archive_path(file_name)
                self.send_response(200)
                for key, value in _cors_headers().items():
                    self.send_header(key, value)
                self.send_header("Content-Type", "application/zip")
                self.send_header("Content-Disposition", f'attachment; filename="{path.name}"')
                self.send_header("Content-Length", str(path.stat().st_size))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                with path.open("rb") as stream:
                    while True:
                        chunk = stream.read(1024 * 1024)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
            except (ValueError, FileNotFoundError) as exc:
                self._json_err(str(exc), 404)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as exc:
                self._json_err(str(exc), 500)

        def _api_access_rebuild(self):
            try:
                import asyncio
                loop = getattr(getattr(plugin, "_webui", None), "_loop", None)
                if not loop or not loop.is_running():
                    return self._json_err("event loop not available", 503)
                future = asyncio.run_coroutine_threadsafe(
                    plugin._memory_access_rebuild("webui_manual"), loop,
                )
                self._json_ok(future.result(timeout=180))
            except Exception as exc:
                self._json_err(str(exc))

        def _api_access_maintenance(self):
            store = self._access_store()
            if store is None:
                return self._json_err("情景记忆库未就绪", 503)
            try:
                result = store.maintain_memory_access(
                    config=plugin._memory_access_config(), reason="webui_manual",
                )
                plugin._memory_access_state = {
                    "status": "ready", "updated_ts": time.time(),
                    "reason": "webui_manual", **result,
                }
                self._json_ok(result)
            except Exception as exc:
                self._json_err(str(exc))

        def _api_access_simulate(self, body):
            query = str(body.get("query") or "").strip()
            if not query:
                return self._json_err("请输入检索问题", 400)
            try:
                import asyncio
                loop = getattr(getattr(plugin, "_webui", None), "_loop", None)
                if not loop or not loop.is_running():
                    return self._json_err("event loop not available", 503)
                future = asyncio.run_coroutine_threadsafe(
                    plugin._eval_recall_query(query, top_k=body.get("top_k")), loop,
                )
                baseline = future.result(timeout=90)
                candidates = list(baseline.get("hits") or [])
                selected = [str(item.get("memo_name") or "") for item in candidates if item.get("selected")]
                store = self._access_store()
                if store is None:
                    return self._json_err("情景记忆库未就绪", 503)
                access = store.evaluate_memory_access(
                    query=query, candidates=candidates, selected_names=selected,
                    request_id=f"lab-{time.time_ns():x}", config=plugin._memory_access_config(),
                    psychological_bias=0.0, record=False,
                )
                self._json_ok({
                    "query": query,
                    "baseline": {
                        "architecture": baseline.get("recall_architecture"),
                        "candidate_count": baseline.get("candidate_count"),
                        "selected_count": baseline.get("selected_count"),
                        "selected": selected,
                    },
                    "access": access,
                })
            except Exception as exc:
                self._json_err(str(exc))

        def _api_access_eval_cases(self, qs):
            store = self._access_store()
            if store is None:
                return self._json_err("情景记忆库未就绪", 503)
            try:
                rows = store.list_memory_eval_cases(
                    case_type=str((qs.get("case_type") or [""])[0]),
                    enabled_only=str((qs.get("enabled_only") or ["1"])[0]) != "0",
                    limit=int((qs.get("limit") or ["2000"])[0]),
                )
                case_types = {}
                for row in rows:
                    ct = str(row.get("case_type") or "general")
                    case_types[ct] = case_types.get(ct, 0) + 1
                self._json_ok({"cases": rows, "case_types": case_types, "total": len(rows)})
            except Exception as exc:
                self._json_err(str(exc))

        def _api_access_eval_run_latest(self):
            store = self._access_store()
            if store is None:
                return self._json_err("情景记忆库未就绪", 503)
            try:
                run = store.latest_memory_eval_run()
                self._json_ok({"run": run, "available": run is not None})
            except Exception as exc:
                self._json_err(str(exc))

        def _api_access_safety_gates(self):
            store = self._access_store()
            if store is None:
                return self._json_err("情景记忆库未就绪", 503)
            try:
                gates = store.memory_access_safety_gates()
                self._json_ok(gates)
            except Exception as exc:
                self._json_err(str(exc))

        def _api_access_eval_cases_generate(self, body):
            store = self._access_store()
            if store is None:
                return self._json_err("情景记忆库未就绪", 503)
            try:
                result = store.generate_memory_eval_cases(replace=bool(body.get("replace", False)))
                self._json_ok(result)
            except Exception as exc:
                self._json_err(str(exc))

        def _api_access_eval_case_enabled(self, body):
            store = self._access_store()
            if store is None:
                return self._json_err("情景记忆库未就绪", 503)
            case_id = str(body.get("case_id") or "").strip()
            if not case_id:
                return self._json_err("case_id 必填", 400)
            enabled = bool(body.get("enabled", True))
            try:
                ok = store.set_memory_eval_case_enabled(case_id, enabled)
                self._json_ok({"case_id": case_id, "enabled": enabled, "updated": ok})
            except Exception as exc:
                self._json_err(str(exc))

        def _api_access_eval_run(self, body):
            store = self._access_store()
            if store is None:
                return self._json_err("情景记忆库未就绪", 503)
            try:
                loop = getattr(getattr(plugin, "_webui", None), "_loop", None)
                if not loop or not loop.is_running():
                    return self._json_err("event loop not available", 503)

                def recall_fn(query, _top_k):
                    future = asyncio.run_coroutine_threadsafe(
                        plugin._eval_recall_query(str(query), top_k=None), loop,
                    )
                    data = future.result(timeout=90)
                    candidates = list(data.get("hits") or [])
                    selected = [
                        str(item.get("memo_name") or "") for item in candidates
                        if item.get("selected") and str(item.get("memo_name") or "")
                    ]
                    return candidates, selected

                result = store.run_memory_eval(
                    config=plugin._memory_access_config(),
                    case_type=str(body.get("case_type") or ""),
                    recall_fn=recall_fn,
                    recalled_only=bool(body.get("confirmed_only", False)),
                    scope=str(body.get("scope") or "calibration"),
                )
                self._json_ok({
                    "run_id": result["run_id"],
                    "cases_total": result["cases_total"],
                    "cases_passed": result["cases_passed"],
                    "gates": result["gates"],
                    "algorithm_version": result["algorithm_version"],
                    "confirmed_cases": int(result["gates"].get("eligible_cases") or 0),
                    "real_recall_cases": int(result["gates"].get("real_recall_cases") or 0),
                    "calibration": result["gates"].get("calibration") or {},
                    "supervision_counts": result["gates"].get("supervision_counts") or {},
                })
            except Exception as exc:
                self._json_err(str(exc))

        def _api_access_eval_run_results(self, body):
            store = self._access_store()
            if store is None:
                return self._json_err("情景记忆库未就绪", 503)
            run_id = str(body.get("run_id") or "").strip()
            if not run_id:
                return self._json_err("run_id 必填", 400)
            try:
                rows = store.memory_eval_run_results(run_id, limit=int(body.get("limit") or 500))
                self._json_ok({"run_id": run_id, "results": rows})
            except Exception as exc:
                self._json_err(str(exc))

        def _api_access_eval_case_confirm(self, body):
            store = self._access_store()
            if store is None:
                return self._json_err("情景记忆库未就绪", 503)
            case_id = str(body.get("case_id") or "").strip()
            if not case_id:
                return self._json_err("case_id 必填", 400)
            expected_memo = str(body.get("expected_memo") or "")
            confirmed_by = str(body.get("confirmed_by") or "webui")
            if not expected_memo.strip():
                return self._json_err("expected_memo 必填", 400)
            try:
                ok = store.confirm_memory_eval_case(
                    case_id, expected_memo=expected_memo, confirmed_by=confirmed_by,
                )
                self._json_ok({"case_id": case_id, "confirmed": ok})
            except Exception as exc:
                self._json_err(str(exc))

        def _api_access_takeover_reset(self):
            store = self._access_store()
            if store is None:
                return self._json_err("情景记忆库未就绪", 503)
            try:
                ok = store.memory_access_reset_breaker()
                self._json_ok({"reset": ok})
            except Exception as exc:
                self._json_err(str(exc))

        def _api_access_takeover_status(self):
            store = self._access_store()
            if store is None:
                return self._json_err("情景记忆库未就绪", 503)
            try:
                access_config = getattr(plugin, "_memory_access_config", lambda: {})()
                breaker = store.memory_access_breaker_status(
                    config=access_config,
                )
                route_mode = str(getattr(plugin, "memory_access_route_mode", "shadow"))
                prereq = {
                    "eligible": route_mode == "supplement" and not bool(breaker.get("tripped")),
                    "policy": "access_supplement_v1",
                    "conditions": [
                        {"key": "route_mode", "passed": route_mode == "supplement",
                         "reason": f"当前路由 {route_mode}"},
                        {"key": "breaker_clear", "passed": not bool(breaker.get("tripped")),
                         "reason": "补充断路器正常" if not breaker.get("tripped") else "补充断路器已触发"},
                        {"key": "baseline_preserved", "passed": True,
                         "reason": "旧主召回完整保留，不移除、不替换"},
                    ],
                    "max_appends": int(getattr(plugin, "memory_access_supplement_max", 0)),
                    "slots": {"T": 1, "A": 1},
                }
                recent = store.list_memory_takeover_log(limit=30, include_breaker=True)
                decisions = [item for item in recent if not int(item.get("breaker_trip") or 0)]
                supplement_decisions = [
                    item for item in decisions if str(item.get("route_mode") or "") == "supplement"
                ]
                evaluated = [item for item in supplement_decisions if int(item.get("response_used", -1)) >= 0]
                used = [item for item in evaluated if int(item.get("response_used") or 0) == 1]
                self._json_ok({
                    "prerequisites": prereq,
                    "breaker": breaker,
                    "recent": recent,
                    "audit": {
                        "appended": len(supplement_decisions),
                        "pending": len(supplement_decisions) - len(evaluated),
                        "evaluated": len(evaluated),
                        "used": len(used),
                        "use_rate": round(len(used) / max(1, len(evaluated)), 4),
                        "slots": {
                            "T": sum(1 for item in supplement_decisions if item.get("slot") == "T"),
                            "A": sum(1 for item in supplement_decisions if item.get("slot") == "A"),
                        },
                        "policy": "access_supplement_v1",
                    },
                })
            except Exception as exc:
                self._json_err(str(exc))

        @staticmethod
        def _forgetting_preset_payload(definitions):
            return [
                {
                    "id": preset_id,
                    "name": definition["name"],
                    "summary": definition["summary"],
                    "cost": definition["cost"],
                    "values": _safe_preset_values(definition.get("values")),
                }
                for preset_id, definition in definitions.items()
            ]

        def _api_forgetting_presets(self):
            self._json_ok({
                "items": self._forgetting_preset_payload(forgetting_preset_definitions),
                "contract": {
                    "numeric_and_mode_only": True,
                    "stage": "supplement",
                    "baseline_recall_preserved": True,
                    "protected_fields_untouched": True,
                },
            })

        def _api_forgetting_overview(self):
            store = self._access_store()
            runtime = dict(getattr(plugin, "_memory_access_state", {}) or {})
            fail_open = dict(getattr(plugin, "_memory_access_fail_open", {}) or {})
            llm_runtime = getattr(plugin, "_llm_runtime", None)
            llm_snapshot = llm_runtime.snapshot() if llm_runtime is not None else {}
            if store is None:
                return self._json_ok({
                    "ready": False,
                    "version": getattr(plugin, "_PLUGIN_VERSION", "?"),
                    "reason": "4.6 原文库尚未就绪；正式召回保持可用",
                    "runtime": runtime,
                    "llm_runtime": llm_snapshot,
                    "fail_open": fail_open,
                    "presets": self._forgetting_preset_payload(forgetting_preset_definitions),
                })
            try:
                overview = dict(store.memory_access_overview() or {})
                access_config = getattr(plugin, "_memory_access_config", lambda: {})()
                breaker = store.memory_access_breaker_status(config=access_config)
                route_mode = str(getattr(plugin, "memory_access_route_mode", "shadow"))
                takeover = {
                    "prerequisites": {
                        "eligible": route_mode == "supplement" and not bool(breaker.get("tripped")),
                        "policy": "access_supplement_v1",
                        "route_mode": route_mode,
                        "max_appends": int(getattr(plugin, "memory_access_supplement_max", 0)),
                    },
                    "breaker": breaker,
                }
                recent_events = store.list_memory_access_events(limit=80)
                overview.update({
                    "ready": True,
                    "version": getattr(plugin, "_PLUGIN_VERSION", "?"),
                    "runtime": runtime,
                    "llm_runtime": llm_snapshot,
                    "fail_open": fail_open,
                    "takeover": takeover,
                    "recent_events": recent_events,
                    "presets": self._forgetting_preset_payload(forgetting_preset_definitions),
                    "settings": {
                        key: getattr(plugin, key, None)
                        for key in sorted(next(
                            values for name, values in setting_groups.items()
                            if name == "记忆可达性与补充支路"
                        ))
                    },
                    "contract": {
                        "destructive": False,
                        "source_of_truth_unchanged": True,
                        "baseline_recall_preserved": True,
                        "route_mode": route_mode,
                        "online_supplement": route_mode == "supplement",
                        "max_online_append": int(getattr(plugin, "memory_access_supplement_max", 0)),
                        "slots": {"T": 1, "A": 1},
                    },
                })
                self._json_ok(overview)
            except Exception as exc:
                self._json_err(str(exc))

        def _api_forgetting_preset(self, body):
            preset_id = str(body.get("preset") or "").strip()
            preset = forgetting_preset_definitions.get(preset_id)
            if not preset:
                return self._json_err("未知遗忘预设", 400)
            try:
                values = _safe_preset_values(preset.get("values"))
                result = self._save_settings_values(values, f"forgetting_preset:{preset_id}")
                result["preset"] = {"id": preset_id, "name": preset["name"]}
                result["route_mode"] = values.get("memory_access_route_mode", "supplement")
                self._json_ok(result)
            except ValueError as exc:
                self._json_err(str(exc), 400)
            except Exception as exc:
                self._json_err(str(exc))

        def _api_forgetting_feedback(self, body):
            store = self._access_store()
            if store is None:
                return self._json_err("情景记忆库未就绪", 503)
            memo_name = str(body.get("memo_name") or "").strip()
            action = str(body.get("action") or "").strip()
            if not memo_name or not action:
                return self._json_err("memo_name 与 action 必填", 400)
            try:
                result = store.record_memory_access_feedback(
                    memo_name=memo_name, action=action,
                    note=str(body.get("note") or ""),
                )
                self._json_ok(result)
            except ValueError as exc:
                self._json_err(str(exc), 400)
            except Exception as exc:
                self._json_err(str(exc))

        def _api_production_overview(self, qs):
            """Return one coherent production snapshot for the dedicated dashboard tab."""
            store = self._production_store()
            if store is None:
                return self._json_ok({
                    "ready": False,
                    "reason": "原文库未就绪或 episodic 未启用",
                    "generation_actions": {"switch_available": False, "rollback_available": False},
                })
            try:
                import sqlite3

                stats = dict(store.stats() or {}) if callable(getattr(store, "stats", None)) else {}
                identity = dict(store.identity() or {}) if callable(getattr(store, "identity", None)) else {}
                for key in ("active_generation", "pending_generation", "prev_generation", "migration_status"):
                    if not identity.get(key) and stats.get(key):
                        identity[key] = stats[key]
                traceable = dict(store.traceability() or {}) if callable(getattr(store, "traceability", None)) else {}
                source_status = dict(traceable.get("status") or {})
                if not source_status:
                    source_status = {"level": "gray", "label": "追溯状态不可用"}

                snapshots = self._production_list(
                    store.list_snapshots(limit=10) if callable(getattr(store, "list_snapshots", None)) else []
                )
                rollbacks = [self._public_production_rollback(item) for item in self._production_list(
                    store.rollback_history(limit=30) if callable(getattr(store, "rollback_history", None)) else []
                )]
                previews = [self._public_production_preview(item) for item in self._production_list(
                    store.list_diary_previews(limit=30) if callable(getattr(store, "list_diary_previews", None)) else []
                )]
                long_helper = getattr(plugin, "_long_diaries_list", None)
                long_diaries = self._production_list(long_helper() if callable(long_helper) else [])
                repair_helper = getattr(plugin, "_fallback_repair_candidates", None)
                fallback_repairs = self._production_list(
                    repair_helper() if callable(repair_helper) else []
                )

                totals = {
                    "batches": int(stats.get("source_batches") or 0),
                    "turns": int(stats.get("source_turns") or 0),
                    "episodes": int(stats.get("episodes") or 0),
                    "evidence": int(stats.get("evidence") or 0),
                    "turn_links": int(stats.get("exact_turn_links") or 0),
                }
                tier_dist: dict[str, int] = {}
                scene_stats = {"scenes": 0, "with_reasons": 0, "avg_span_turns": 0.0}
                diary_stats: dict[str, Any] = {
                    "with_diary": 0,
                    "avg_card_len": 0.0,
                    "quality_dist": {},
                    "avg_must_coverage": 0.0,
                    "avg_support_coverage": 0.0,
                    "avg_transcript_risk": 0.0,
                    "avg_compression_ratio": 0.0,
                    "render_retries": 0,
                    "render_fallbacks": 0,
                }
                trace = {
                    "grounded_evidence": 0,
                    "turn_linked_episodes": int(traceable.get("recoverable_episodes") or 0),
                    "grounded_ratio": 0.0,
                    "traceable_ratio": 0.0,
                }
                batches: list[dict[str, Any]] = []
                db_path = str(getattr(store, "db_path", "") or stats.get("db_path") or "")
                if db_path:
                    conn = sqlite3.connect(db_path, check_same_thread=False)
                    conn.row_factory = sqlite3.Row
                    try:
                        summary = conn.execute(
                            """SELECT
                                 (SELECT COUNT(*) FROM source_batches) batches,
                                 (SELECT COUNT(*) FROM source_turns) turns,
                                 (SELECT COUNT(*) FROM episodes) episodes,
                                 (SELECT COUNT(*) FROM episode_evidence) evidence,
                                 (SELECT COUNT(*) FROM episode_turn_links) turn_links,
                                 (SELECT COUNT(*) FROM episodes WHERE scene_start_turn >= 0) scenes,
                                 (SELECT COUNT(*) FROM episodes WHERE scene_boundary_reasons_json NOT IN ('', '[]')) with_reasons,
                                 (SELECT AVG(scene_end_turn-scene_start_turn) FROM episodes WHERE scene_start_turn >= 0 AND scene_end_turn >= scene_start_turn) avg_span,
                                 (SELECT COUNT(*) FROM episodes WHERE diary_content_hash != '') with_diary,
                                 (SELECT AVG(LENGTH(card_text)) FROM episodes) avg_card_len,
                                 (SELECT AVG(must_coverage) FROM episodes WHERE must_coverage>=0) avg_must,
                                 (SELECT AVG(support_coverage) FROM episodes WHERE support_coverage>=0) avg_support,
                                 (SELECT AVG(transcript_risk) FROM episodes WHERE transcript_risk>=0) avg_risk,
                                 (SELECT AVG(compression_ratio) FROM episodes WHERE compression_ratio>=0) avg_compression,
                                 (SELECT COUNT(*) FROM episodes WHERE render_retry_reason!='') render_retries,
                                 (SELECT COUNT(*) FROM episodes WHERE render_fallback=1) render_fallbacks,
                                 (SELECT COUNT(*) FROM episode_evidence WHERE grounded=1) grounded_evidence,
                                 (SELECT COUNT(DISTINCT e2.episode_id)
                                    FROM episodes e2
                                   WHERE e2.active=1 AND EXISTS (
                                     SELECT 1 FROM episode_turn_links l2
                                     JOIN source_turns s2 ON s2.batch_id=l2.batch_id AND s2.turn_index=l2.turn_index
                                     WHERE l2.episode_id=e2.episode_id
                                   )) linked_episodes"""
                        ).fetchone()
                        if summary:
                            totals = {key: int(summary[key] or 0) for key in ("batches", "turns", "episodes", "evidence", "turn_links")}
                            scene_stats = {
                                "scenes": int(summary["scenes"] or 0),
                                "with_reasons": int(summary["with_reasons"] or 0),
                                "avg_span_turns": round(float(summary["avg_span"] or 0), 1),
                            }
                            diary_stats.update({
                                "with_diary": int(summary["with_diary"] or 0),
                                "avg_card_len": round(float(summary["avg_card_len"] or 0), 1),
                                "avg_must_coverage": round(float(summary["avg_must"] or 0), 4),
                                "avg_support_coverage": round(float(summary["avg_support"] or 0), 4),
                                "avg_transcript_risk": round(float(summary["avg_risk"] or 0), 4),
                                "avg_compression_ratio": round(float(summary["avg_compression"] or 0), 4),
                                "render_retries": int(summary["render_retries"] or 0),
                                "render_fallbacks": int(summary["render_fallbacks"] or 0),
                            })
                            trace.update({
                                "grounded_evidence": int(summary["grounded_evidence"] or 0),
                                "turn_linked_episodes": int(summary["linked_episodes"] or 0),
                            })
                        tier_dist = {
                            str(row["evidence_tier"] or "supporting"): int(row["n"] or 0)
                            for row in conn.execute("SELECT evidence_tier,COUNT(*) n FROM episode_evidence GROUP BY evidence_tier")
                        }
                        diary_stats["quality_dist"] = {
                            str(row["evidence_quality"] or "unknown"): int(row["n"] or 0)
                            for row in conn.execute("SELECT evidence_quality,COUNT(*) n FROM episodes GROUP BY evidence_quality")
                        }
                        batches = [dict(row) for row in conn.execute(
                            """SELECT b.batch_id,b.source_kind,b.message_count,b.first_event_ts,b.last_event_ts,b.created_ts,
                                      (SELECT SUM(LENGTH(s.content)) FROM source_turns s WHERE s.batch_id=b.batch_id) char_count,
                                      (SELECT COUNT(*) FROM source_turns s WHERE s.batch_id=b.batch_id AND s.role='user') user_turns,
                                      (SELECT COUNT(*) FROM source_turns s WHERE s.batch_id=b.batch_id AND s.role='assistant') assistant_turns,
                                      (SELECT q.status FROM semantic_state_queue q WHERE q.batch_id=b.batch_id ORDER BY q.updated_ts DESC LIMIT 1) state_status,
                                      COUNT(DISTINCT e.episode_id) ep_cnt,
                                      COUNT(DISTINCT CASE WHEN ev.evidence_tier='must_write' THEN ev.id END) must_cnt,
                                      COUNT(DISTINCT CASE WHEN e.scene_start_turn>=0 THEN e.episode_id END) scene_cnt,
                                      COUNT(DISTINCT CASE WHEN e.diary_content_hash!='' THEN e.episode_id END) diary_cnt
                               FROM source_batches b
                               LEFT JOIN episodes e ON e.source_batch_id=b.batch_id
                               LEFT JOIN episode_evidence ev ON ev.episode_id=e.episode_id
                               GROUP BY b.batch_id ORDER BY b.created_ts DESC LIMIT 12"""
                        )]
                    finally:
                        conn.close()
                trace["grounded_ratio"] = round(trace["grounded_evidence"] / totals["evidence"], 4) if totals["evidence"] else 0.0
                trace["traceable_ratio"] = round(trace["turn_linked_episodes"] / totals["episodes"], 4) if totals["episodes"] else 0.0

                pending = str(identity.get("pending_generation") or stats.get("pending_generation") or "")
                previous = str(identity.get("prev_generation") or stats.get("prev_generation") or "")
                emb_dim = int(getattr(plugin, "_emb_dim", 0) or 0)
                generation_actions = {
                    "switch_available": bool(pending and emb_dim and callable(getattr(store, "switch_generation", None))),
                    "rollback_available": bool(previous and callable(getattr(store, "rollback_generation", None))),
                    "pending_generation": pending,
                    "prev_generation": previous,
                }
                self._json_ok({
                    "ready": True,
                    "version": getattr(plugin, "_PLUGIN_VERSION", "?"),
                    "db_path": str(identity.get("canonical_db_path") or db_path),
                    "identity": identity,
                    "source_status": source_status,
                    "totals": totals,
                    "traceable": traceable,
                    "tier_dist": tier_dist,
                    "scene_stats": scene_stats,
                    "diary_stats": diary_stats,
                    "trace": trace,
                    "linked": totals["turn_links"],
                    "batches": batches,
                    "snapshots": snapshots,
                    "snapshot_count": len(snapshots),
                    "long_diaries": long_diaries[:50],
                    "long_diaries_count": len(long_diaries),
                    "fallback_repairs": fallback_repairs[:100],
                    "fallback_repair_count": len(fallback_repairs),
                    "previews": previews,
                    "rollback_count": int(stats.get("rollbacks") or len(rollbacks)),
                    "rollbacks": rollbacks,
                    "generation_actions": generation_actions,
                })
            except Exception as exc:
                self._json_err(str(exc))

        def _api_production_episodes(self, qs):
            """Per-Episode generation audit rows for the standalone workbench."""
            store = self._production_store()
            if store is None:
                return self._json_ok({"items": [], "total": 0, "ready": False})
            try:
                import sqlite3

                query = str(qs.get("q", [""])[0] or "").strip()
                quality = str(qs.get("quality", [""])[0] or "").strip()
                limit = max(1, min(300, int(qs.get("limit", ["120"])[0] or 120)))
                clauses = ["e.active=1"]
                params: list[Any] = []
                if quality:
                    clauses.append("e.evidence_quality=?")
                    params.append(quality)
                if query:
                    like = "%" + query + "%"
                    clauses.append(
                        "(e.memo_name LIKE ? OR e.scene_anchor LIKE ? OR e.retrieval_key LIKE ? OR e.card_text LIKE ?)"
                    )
                    params.extend([like, like, like, like])
                where = " AND ".join(clauses)
                conn = sqlite3.connect(str(store.db_path), check_same_thread=False)
                conn.row_factory = sqlite3.Row
                try:
                    total = int(conn.execute(
                        f"SELECT COUNT(*) n FROM episodes e WHERE {where}", params,
                    ).fetchone()["n"])
                    rows = conn.execute(
                        f"""SELECT e.episode_id,e.memo_name,e.source_batch_id,e.source_kind,
                                   e.occurred_at,e.memory_type,e.importance,e.scene_anchor,e.retrieval_key,
                                   e.scene_start_turn,e.scene_end_turn,e.scene_boundary_reasons_json,
                                   e.evidence_quality,e.diary_render_version,e.must_coverage,
                                   e.support_coverage,e.transcript_risk,e.source_overlap_ratio,
                                   e.direct_quote_ratio,e.compression_ratio,e.render_retry_reason,
                                   e.render_fallback,LENGTH(e.card_text) card_len,
                                   CASE WHEN e.diary_content_hash!='' THEN 1 ELSE 0 END memos_written,
                                   COUNT(DISTINCT CASE WHEN ev.evidence_tier='must_write' THEN ev.id END) must_count,
                                   COUNT(DISTINCT CASE WHEN ev.evidence_tier='supporting' THEN ev.id END) supporting_count,
                                   COUNT(DISTINCT CASE WHEN ev.evidence_tier='archive_only' THEN ev.id END) archive_count,
                                   COUNT(DISTINCT l.turn_index || ':' || l.evidence_index) exact_links,
                                   COUNT(DISTINCT s.id) source_turns,
                                   (SELECT q.status FROM semantic_state_queue q
                                     WHERE q.batch_id=e.source_batch_id ORDER BY q.updated_ts DESC LIMIT 1) state_status
                              FROM episodes e
                              LEFT JOIN episode_evidence ev ON ev.episode_id=e.episode_id
                              LEFT JOIN episode_turn_links l ON l.episode_id=e.episode_id
                              LEFT JOIN source_turns s ON s.batch_id=l.batch_id AND s.turn_index=l.turn_index
                             WHERE {where}
                             GROUP BY e.id ORDER BY e.event_ts DESC,e.updated_ts DESC LIMIT ?""",
                        [*params, limit],
                    ).fetchall()
                    items = []
                    for row in rows:
                        item = dict(row)
                        try:
                            item["scene_boundary_reasons"] = json.loads(
                                str(item.pop("scene_boundary_reasons_json") or "[]")
                            )
                        except Exception:
                            item["scene_boundary_reasons"] = []
                        item["recoverable"] = bool(item.get("source_turns") and item.get("exact_links"))
                        items.append(item)
                finally:
                    conn.close()
                self._json_ok({"items": items, "total": total, "ready": True})
            except (TypeError, ValueError) as exc:
                self._json_err(str(exc), 400)
            except Exception as exc:
                self._json_err(str(exc))

        def _api_production_snapshots(self):
            store = self._production_store()
            try:
                items = store.list_snapshots(limit=50) if store is not None and callable(getattr(store, "list_snapshots", None)) else []
                self._json_ok({"snapshots": self._production_list(items), "available": store is not None})
            except Exception as exc:
                self._json_err(str(exc))

        def _api_production_long_diaries(self, qs):
            try:
                helper = getattr(plugin, "_long_diaries_list", None)
                items = helper() if callable(helper) else []
                self._json_ok({"long_diaries": self._production_list(items), "available": callable(helper)})
            except Exception as exc:
                self._json_err(str(exc))

        def _api_production_fallback_repairs(self, qs):
            try:
                limit = max(1, min(500, int(qs.get("limit", ["100"])[0] or 100)))
                helper = getattr(plugin, "_fallback_repair_candidates", None)
                items = helper(limit=limit) if callable(helper) else []
                self._json_ok({
                    "items": self._production_list(items),
                    "total": len(items),
                    "available": callable(helper),
                })
            except (TypeError, ValueError) as exc:
                self._json_err(str(exc), 400)
            except Exception as exc:
                self._json_err(str(exc))

        def _api_production_rollbacks(self):
            store = self._production_store()
            try:
                items = store.rollback_history(limit=50) if store is not None and callable(getattr(store, "rollback_history", None)) else []
                self._json_ok({"rollbacks": [self._public_production_rollback(item) for item in self._production_list(items)], "available": store is not None})
            except Exception as exc:
                self._json_err(str(exc))

        def _api_production_previews(self):
            store = self._production_store()
            try:
                items = store.list_diary_previews(limit=50) if store is not None and callable(getattr(store, "list_diary_previews", None)) else []
                self._json_ok({"previews": [self._public_production_preview(item) for item in self._production_list(items)], "available": store is not None})
            except Exception as exc:
                self._json_err(str(exc))

        def _api_production_restore_snapshot(self, body):
            store = self._production_store()
            file_name = str((body or {}).get("file_name") or "").strip()
            if not file_name:
                return self._json_err("缺少 file_name", 400)
            if store is None or not callable(getattr(store, "restore_snapshot", None)):
                return self._json_err("快照恢复能力不可用", 503)
            try:
                result = store.restore_snapshot(file_name)
                if not isinstance(result, dict) or not result.get("restored"):
                    return self._json_err("恢复失败: " + str((result or {}).get("reason") if isinstance(result, dict) else "unknown"), 409)
                self._json_ok(result)
            except Exception as exc:
                self._json_err(str(exc))

        def _api_production_long_diary_preview(self, body):
            episode_id = str((body or {}).get("episode_id") or "").strip()
            if not episode_id:
                return self._json_err("缺少 episode_id", 400)
            try:
                result = self._run_plugin_coro(self._production_helper("_create_diary_rewrite_preview")(episode_id), timeout=300)
                self._json_ok(self._public_production_preview(result))
            except RuntimeError as exc:
                self._json_err(str(exc), 503)
            except Exception as exc:
                self._json_err(str(exc))

        def _api_production_long_diary_confirm(self, body):
            preview_id = str((body or {}).get("preview_id") or "").strip()
            if not preview_id:
                return self._json_err("缺少 preview_id", 400)
            try:
                result = self._run_plugin_coro(self._production_helper("_confirm_diary_rewrite")(preview_id), timeout=300)
                self._json_ok(result)
            except RuntimeError as exc:
                self._json_err(str(exc), 503)
            except Exception as exc:
                self._json_err(str(exc))

        def _api_production_long_diary_rollback(self, body):
            memo_name = str((body or {}).get("memo_name") or "").strip()
            preview_id = str((body or {}).get("preview_id") or "").strip()
            if not memo_name and not preview_id:
                return self._json_err("缺少 memo_name 或 preview_id", 400)
            try:
                result = self._run_plugin_coro(
                    self._production_helper("_rollback_diary_rewrite")(memo_name=memo_name, preview_id=preview_id),
                    timeout=120,
                )
                self._json_ok(result)
            except RuntimeError as exc:
                self._json_err(str(exc), 503)
            except Exception as exc:
                self._json_err(str(exc))

        def _api_production_long_diary_discard(self, body):
            preview_id = str((body or {}).get("preview_id") or "").strip()
            if not preview_id:
                return self._json_err("缺少 preview_id", 400)
            try:
                result = self._run_plugin_coro(self._production_helper("_discard_diary_preview")(preview_id), timeout=30)
                self._json_ok(result)
            except RuntimeError as exc:
                self._json_err(str(exc), 503)
            except Exception as exc:
                self._json_err(str(exc))

        def _api_production_generation_switch(self):
            store = self._production_store()
            if store is None or not callable(getattr(store, "switch_generation", None)):
                return self._json_err("向量代际切换能力不可用", 503)
            dim = int(getattr(plugin, "_emb_dim", 0) or 0)
            model_id = str(getattr(plugin, "_emb_model_id", "") or "unknown")
            if dim <= 0:
                return self._json_err("Embedding 维度尚未就绪", 409)
            try:
                result = store.switch_generation(dim, model_id)
                if not result.get("switched"):
                    return self._json_err("切换未执行: " + str(result.get("reason") or "unknown"), 409)
                self._json_ok(result)
            except Exception as exc:
                self._json_err(str(exc))

        def _api_production_generation_rollback(self):
            store = self._production_store()
            if store is None or not callable(getattr(store, "rollback_generation", None)):
                return self._json_err("向量代际回滚能力不可用", 503)
            try:
                result = store.rollback_generation()
                if not result.get("rolled_back"):
                    return self._json_err("回滚未执行: " + str(result.get("reason") or "unknown"), 409)
                self._json_ok(result)
            except Exception as exc:
                self._json_err(str(exc))

        def _api_episodic_status(self):
            try:
                store = getattr(plugin, "_episodes", None)
                if store is None:
                    return self._json_ok({
                        "enabled": bool(getattr(plugin, "episodic_memory_enable", False)),
                        "ready": False,
                        "episodes": 0,
                        "rebuild_command": "/memos-episodic-rebuild",
                    })
                data = store.stats()
                data.update({
                    "enabled": bool(getattr(plugin, "episodic_memory_enable", False)),
                    "ready": True,
                    "migration_ready": bool(getattr(plugin, "_episode_migration_ready", False)),
                    "migration": dict(getattr(plugin, "_episode_migration_state", {}) or {}),
                    "evidence_first_generation": bool(getattr(plugin, "evidence_first_generation_enable", False)),
                    "raw_evidence_archive": bool(getattr(plugin, "raw_evidence_archive_enable", False)),
                    "active_architecture": "lean_full_memory_fusion" if (
                        getattr(plugin, "lean_recall_enable", False)
                        and getattr(plugin, "_episode_migration_ready", False)
                        and int(data.get("episodes") or 0) > 0
                    ) else "episodic_cascade",
                    "lean_candidate_k": int(getattr(plugin, "lean_recall_candidate_k", 0) or 0),
                    "lean_event_index": bool(getattr(plugin, "lean_event_index_enable", False)),
                    "lean_source_evidence": bool(getattr(plugin, "lean_source_evidence_enable", False)),
                    "lean_coverage_selection": bool(getattr(plugin, "lean_coverage_selection_enable", False)),
                    "lean_adaptive_evidence": bool(getattr(plugin, "lean_adaptive_evidence_enable", False)),
                    "passage_vector_migration": dict(getattr(plugin, "_passage_vector_migration_state", {}) or {}),
                    "source_turn_vector_migration": dict(getattr(plugin, "_source_turn_vector_migration_state", {}) or {}),
                    "lean_story_min": int(getattr(plugin, "lean_story_min_inject", 0) or 0),
                    "lean_story_max": int(getattr(plugin, "lean_story_max_inject", 0) or 0),
                    "lean_temporal": bool(getattr(plugin, "lean_temporal_enable", False)),
                    "lean_texture": bool(getattr(plugin, "lean_texture_enable", False)),
                    "candidate_pool": int(getattr(plugin, "episodic_candidate_pool", 0) or 0),
                    "default_inject": int(getattr(plugin, "episodic_default_inject", 0) or 0),
                    "narrative_inject": int(getattr(plugin, "episodic_narrative_inject", 0) or 0),
                    "full_diary_limit": int(getattr(plugin, "episodic_full_diary_limit", 0) or 0),
                    "rebuild_command": "/memos-episodic-rebuild",
                    "recent_batches": store.batch_status(12),
                })
                self._json_ok(data)
            except Exception as e:
                self._json_err(str(e))

        def _api_episodic_memories(self, qs):
            try:
                store = getattr(plugin, "_episodes", None)
                if store is None:
                    return self._json_ok({"items": [], "total": 0})
                query = str(qs.get("q", [""])[0]).strip()
                quality = str(qs.get("quality", [""])[0]).strip()
                limit = max(1, min(500, int(qs.get("limit", ["120"])[0] or 120)))
                items = store.list_episodes(query=query, quality=quality, limit=limit)
                self._json_ok({"items": items, "total": len(items), "query": query, "quality": quality})
            except (TypeError, ValueError) as e:
                self._json_err(str(e), 400)
            except Exception as e:
                self._json_err(str(e))

        def _api_episodic_detail(self, memo_name: str):
            try:
                store = getattr(plugin, "_episodes", None)
                if store is None:
                    return self._json_err("情景记忆库未就绪", 503)
                detail = store.episode_detail(memo_name)
                if not detail:
                    return self._json_err("未找到情景记忆", 404)
                self._json_ok(detail)
            except Exception as e:
                self._json_err(str(e))

        def _api_episodic_rebuild(self):
            try:
                if getattr(plugin, "_episodes", None) is None:
                    return self._json_err("情景记忆库未就绪", 503)
                result = self._run_plugin_coro(
                    plugin._rebuild_episodic_from_memos(force=True),
                    timeout=600,
                )
                self._json_ok(result)
            except Exception as e:
                self._json_err(str(e))

        def _api_semantic_state_status(self):
            try:
                if not hasattr(plugin, "_semantic_state_status"):
                    return self._json_err("滚动状态功能不可用", 503)
                self._json_ok(plugin._semantic_state_status(
                    include_history=True,
                    include_pending_preview=True,
                ))
            except Exception as e:
                self._json_err(str(e))

        def _api_semantic_state_rebuild(self):
            try:
                if getattr(plugin, "_episodes", None) is None:
                    return self._json_err("情景记忆库未就绪", 503)
                result = self._run_plugin_coro(plugin._bootstrap_semantic_state(force=True), timeout=600)
                self._json_ok(result)
            except Exception as e:
                self._json_err(str(e))

        def _api_eval_cases(self):
            try:
                store = getattr(plugin, "_episodes", None)
                if store is None:
                    return self._json_ok({"items": [], "total": 0})
                items = store.list_eval_cases(enabled_only=False, limit=500)
                self._json_ok({"items": items, "total": len(items)})
            except Exception as e:
                self._json_err(str(e))

        def _api_eval_case_save(self, body: dict[str, Any]):
            try:
                store = getattr(plugin, "_episodes", None)
                if store is None:
                    return self._json_err("情景记忆库未就绪", 503)
                result = store.upsert_eval_case(
                    str(body.get("query") or ""),
                    body.get("expected_memos") if isinstance(body.get("expected_memos"), list) else [],
                    case_id=str(body.get("case_id") or ""),
                    note=str(body.get("note") or ""),
                    source=str(body.get("source") or "webui"),
                    enabled=bool(body.get("enabled", True)),
                )
                self._json_ok(result)
            except ValueError as e:
                self._json_err(str(e), 400)
            except Exception as e:
                self._json_err(str(e))

        def _api_eval_run(self, body: dict[str, Any]):
            try:
                modes = body.get("modes") if isinstance(body.get("modes"), list) else None
                limit = max(1, min(200, int(body.get("case_limit") or 80)))
                result = self._run_plugin_coro(
                    plugin._run_recall_ablation(modes, case_limit=limit),
                    timeout=max(600, limit * 30),
                )
                self._json_ok(result)
            except ValueError as e:
                self._json_err(str(e), 400)
            except Exception as e:
                self._json_err(str(e))

        def _api_eval_auto_cases(self, body: dict[str, Any]):
            try:
                limit = max(20, min(200, int(body.get("limit") or getattr(plugin, "recall_auto_eval_limit", 80))))
                self._json_ok(plugin._generate_recall_eval_cases(limit))
            except ValueError as e:
                self._json_err(str(e), 400)
            except Exception as e:
                self._json_err(str(e))

        def _api_eval_observations(self, qs):
            try:
                store = getattr(plugin, "_episodes", None)
                if store is None:
                    return self._json_ok({"items": [], "summary": {}})
                limit = max(1, min(300, int(qs.get("limit", ["80"])[0])))
                safety_only = str(qs.get("safety_only", ["0"])[0]).lower() in {"1", "true", "yes"}
                self._json_ok({
                    "items": store.list_recall_observations(limit=limit, safety_only=safety_only),
                    "summary": store.recall_observation_summary(),
                })
            except Exception as e:
                self._json_err(str(e))

        def _api_eval_observation_feedback(self, body: dict[str, Any]):
            try:
                store = getattr(plugin, "_episodes", None)
                if store is None:
                    return self._json_err("情景记忆库未就绪", 503)
                request_id = str(body.get("request_id") or "").strip()
                feedback = str(body.get("feedback") or "").strip()
                if not request_id:
                    return self._json_err("缺少 request_id", 400)
                observation = store.get_recall_observation(request_id)
                if observation is None:
                    return self._json_err("观测记录不存在", 404)
                updated = store.set_recall_observation_feedback(request_id, feedback)
                linked_feedback = []
                linked_error = ""
                if feedback in {"useful", "wrong"}:
                    targets = list(dict.fromkeys(
                        str(value) for value in (observation.get("rescue_selected") or [])
                        if str(value)
                    ))
                    vec = getattr(plugin, "_vec", None)
                    query = str(observation.get("query_text") or "").strip()
                    if targets and vec is not None and query:
                        embedding = None
                        try:
                            embedding = self._run_plugin_coro(plugin._embed(query), timeout=20)
                        except Exception as exc:
                            logger.debug(
                                "[memos-memory] observation feedback embedding unavailable: %s", exc
                            )
                        action = "useful" if feedback == "useful" else "incorrect"
                        effect = 0.06 if feedback == "useful" else -0.18
                        reason = "安全网救回有帮助" if feedback == "useful" else "安全网救援错误"
                        try:
                            for memo_name in targets:
                                linked_feedback.append(vec.feedback_record(
                                    request_id=request_id,
                                    memo_name=memo_name,
                                    query_text=query,
                                    action=action,
                                    effect=effect,
                                    reason=reason,
                                    source="safety_observation",
                                    query_embedding=embedding,
                                ))
                        except Exception as exc:
                            linked_error = str(exc)
                            logger.warning(
                                "[memos-memory] safety observation feedback link failed open: %s", exc
                            )
                self._json_ok({
                    "updated": updated,
                    "request_id": request_id, "feedback": feedback,
                    "linked_memories": len(linked_feedback),
                    "linked_error": linked_error,
                })
            except ValueError as e:
                self._json_err(str(e), 400)
            except Exception as e:
                self._json_err(str(e))

        @staticmethod
        def _settings_schema() -> dict[str, Any]:
            schema = getattr(getattr(plugin, "config", None), "schema", None)
            if isinstance(schema, dict) and schema:
                return schema
            try:
                data = json.loads((Path(__file__).resolve().parent / "_conf_schema.json").read_text(encoding="utf-8"))
                return data if isinstance(data, dict) else {}
            except Exception:
                return {}

        @staticmethod
        def _is_sensitive_setting(key: str) -> bool:
            lowered = str(key or "").lower()
            return any(word in lowered for word in ("token", "password", "secret", "api_key"))

        @staticmethod
        def _setting_group(key: str) -> str:
            if key.startswith("generation_v2_"):
                return "6.1 生产交接"
            if key.startswith(("thread_", "consistency_")):
                return "记忆脉络与一致性"
            if key.startswith("data_backup_") or key == "db_snapshot_keep":
                return "数据备份"
            if key.startswith(("webui_", "diagnostic_")):
                return "界面与诊断"
            if key.startswith(("retry_", "reconcile_", "enable_auto_reconcile")):
                return "同步与网络"
            if key in {"emb_cache_size", "recall_embed_timeout", "recall_search_timeout", "rerank_timeout"}:
                return "检索运行"
            for group, keys in setting_groups.items():
                if key in keys:
                    return group
            if key.startswith("rp_"):
                return "角色增强"
            if key.startswith("cache_"):
                return "缓存诊断"
            if key.startswith(("retry_", "webui_", "reconcile_", "enable_auto_reconcile")) or key in {
                "emb_cache_size", "recall_embed_timeout", "recall_search_timeout", "rerank_timeout",
            }:
                return "运行与维护"
            return "运行与维护"

        @staticmethod
        def _llm_provider_options(current: Any = "") -> list[dict[str, str]]:
            resolver = getattr(plugin, "_chat_provider_options", None)
            if callable(resolver):
                return resolver(current)
            options: list[dict[str, str]] = [
                {"value": "", "label": "跟随当前会话模型"}
            ]
            try:
                providers = list(plugin.context.get_all_providers())
            except Exception:
                providers = []
            seen: set[str] = set()
            for provider in providers:
                try:
                    meta = provider.meta()
                    provider_id = str(getattr(meta, "id", "") or "").strip()
                    model = str(getattr(meta, "model", "") or "").strip()
                except Exception:
                    config = getattr(provider, "provider_config", {}) or {}
                    provider_id = str(config.get("id") or "").strip()
                    model = str(config.get("model") or "").strip()
                if not provider_id or provider_id in seen:
                    continue
                seen.add(provider_id)
                label = provider_id if not model or model == provider_id else f"{provider_id} · {model}"
                options.append({"value": provider_id, "label": label})
            configured = str(current or "").strip()
            if configured and configured not in seen:
                options.append({
                    "value": configured,
                    "label": f"{configured} · 当前不可用",
                })
            return options

        @staticmethod
        def _setting_options(
            key: str,
            meta: dict[str, Any] | None = None,
            current: Any = "",
        ) -> list[dict[str, str]]:
            if isinstance(meta, dict) and meta.get("_special") == "select_provider":
                return Handler._llm_provider_options(current)
            choices = {
                "memos_mode": [("external", "外部服务"), ("managed", "插件托管")],
                "inject_order": [("relevance", "相关性"), ("story_time", "事件时间"), ("insert_time", "写入时间")],
                "inject_format": [("diary", "完整日记"), ("summary", "摘要"), ("quote", "引用")],
                "bm25_tokenizer": [("jieba", "jieba"), ("bigram", "字符 bigram")],
            }
            return [{"value": value, "label": label} for value, label in choices.get(key, [])]

        def _api_settings(self, scheduling=False):
            try:
                from .model_tasks import is_model_setting
                schema = self._settings_schema()
                config = getattr(plugin, "config", {})
                items = []
                for key, meta in schema.items():
                    if not isinstance(meta, dict):
                        continue
                    if scheduling and not is_model_setting(key):
                        continue
                    if not scheduling and (key in time_insight_setting_keys or key == "time_insight_max_age_days"):
                        continue
                    sensitive = self._is_sensitive_setting(key)
                    current = config.get(key, meta.get("default")) if hasattr(config, "get") else meta.get("default")
                    items.append({
                        "key": key,
                        "type": meta.get("type", "string"),
                        "description": meta.get("description", key),
                        "hint": meta.get("hint", ""),
                        "default": "" if sensitive else meta.get("default"),
                        "value": "" if sensitive else current,
                        "configured": bool(current) if sensitive else True,
                        "sensitive": sensitive,
                        "group": self._setting_group(key),
                        "scheduling": is_model_setting(key),
                        "options": self._setting_options(key, meta, current),
                        "min": meta.get("min"),
                        "max": meta.get("max"),
                        "step": meta.get("step"),
                    })
                presets = [
                    dict(
                        {"id": preset_id},
                        **{
                            **definition,
                            "values": _safe_preset_values(definition.get("values")),
                        },
                    )
                    for preset_id, definition in preset_definitions.items()
                ]
                self._json_ok({
                    "items": items,
                    "groups": list(setting_groups.keys()),
                    "presets": presets,
                    "count": len(items),
                    "save_supported": hasattr(config, "save_config"),
                    "notice": "连接、Provider、路径、端口及后台周期类设置保存后需重载插件；其他参数会立即热应用。",
                })
            except Exception as e:
                self._json_err(str(e))

        @staticmethod
        def _coerce_setting(key: str, raw: Any, meta: dict[str, Any]) -> Any:
            kind = str(meta.get("type") or "string")
            if kind == "bool":
                if isinstance(raw, bool):
                    return raw
                if isinstance(raw, str) and raw.strip().lower() in {"true", "1", "yes", "on"}:
                    return True
                if isinstance(raw, str) and raw.strip().lower() in {"false", "0", "no", "off"}:
                    return False
                raise ValueError(f"{key} 必须是布尔值")
            if kind == "int":
                if isinstance(raw, bool):
                    raise ValueError(f"{key} 必须是整数")
                value = int(raw)
            elif kind == "float":
                if isinstance(raw, bool):
                    raise ValueError(f"{key} 必须是数字")
                value = float(raw)
            else:
                value = str(raw if raw is not None else "").strip()
                if len(value) > 20000:
                    raise ValueError(f"{key} 内容过长")
                return value
            if meta.get("min") is not None and value < meta["min"]:
                raise ValueError(f"{key} 不能小于 {meta['min']}")
            if meta.get("max") is not None and value > meta["max"]:
                raise ValueError(f"{key} 不能大于 {meta['max']}")
            return value

        def _save_settings_values(self, incoming: dict[str, Any], source: str) -> dict[str, Any]:
            if not isinstance(incoming, dict) or not incoming:
                raise ValueError("没有需要保存的设置")
            from .thread_policy import FIELDS, hot_apply, validate
            validate(incoming, plugin, manual=source == "manual")
            schema = self._settings_schema()
            config = getattr(plugin, "config", None)
            if config is None or not hasattr(config, "get"):
                raise RuntimeError("插件配置对象不可用")
            unknown = sorted(set(incoming) - set(schema))
            if unknown:
                raise ValueError("未知设置: " + ", ".join(unknown[:8]))
            changes: dict[str, dict[str, Any]] = {}
            coerced: dict[str, Any] = {}
            for key, raw in incoming.items():
                if self._is_sensitive_setting(key):
                    if raw == "__CLEAR__":
                        raw = ""
                    elif raw is None or str(raw).strip() == "":
                        continue
                value = self._coerce_setting(key, raw, schema[key])
                old = config.get(key, schema[key].get("default"))
                if old != value:
                    coerced[key] = value
                    changes[key] = {
                        "old": "已配置" if self._is_sensitive_setting(key) and old else ("未配置" if self._is_sensitive_setting(key) else old),
                        "new": "已配置" if self._is_sensitive_setting(key) and value else ("未配置" if self._is_sensitive_setting(key) else value),
                    }
            if not changes:
                return {"changed": {}, "restart_required": [], "hot_applied": [], "source": source}

            restart_exact = {
                "memos_base_url", "memos_mode", "memos_token", "memos_timeout", "vec_db_path",
                "episodic_db_path", "episodic_memory_enable", "episodic_auto_migrate",
                "passage_vector_auto_migrate", "source_turn_vector_auto_migrate",
                "emb_provider_id", "rerank_provider_id", "profile_provider_id", "rp_time_timezone",
                "webui_enable", "webui_host", "webui_port", "enable_auto_reconcile",
                "reconcile_interval", "profile_auto_update_days", "context_archive_interval_days",
                "data_backup_enable", "data_backup_interval_days", "data_backup_keep",
                "data_backup_dir", "llm_runtime_provider_concurrency",
                "llm_runtime_external_concurrency", "llm_runtime_interactive_queue_timeout",
                "llm_runtime_foreground_lease_seconds", "llm_runtime_defer_background",
            }
            restart_prefixes = ("managed_memos_",)
            restart_required = sorted(
                key for key in changes
                if key in restart_exact or key.startswith(restart_prefixes)
            )
            hot_applied = []
            with settings_lock:
                save = getattr(config, "save_config", None)
                if not callable(save):
                    raise RuntimeError("当前配置对象不支持持久化；请从 AstrBot 正常加载插件后再保存")
                previous = {key: config.get(key, schema[key].get("default")) for key in coerced}
                try:
                    for key, value in coerced.items():
                        config[key] = value
                    save()
                except Exception:
                    for key, value in previous.items():
                        config[key] = value
                    raise
                for key, value in coerced.items():
                    if key not in restart_required and hasattr(plugin, key):
                        setattr(plugin, key, value)
                        hot_applied.append(key)
                hot_apply(plugin, {key: value for key, value in coerced.items() if key in FIELDS})
                insight=getattr(plugin,'_time_insight',None)
                if insight is not None and any(k.startswith('time_insight_') for k in coerced):
                    insight.apply_settings()
                if "emb_cache_size" in hot_applied:
                    while len(getattr(plugin, "_emb_cache", {})) > max(0, int(plugin.emb_cache_size)):
                        plugin._emb_cache.popitem(last=False)
            if hasattr(plugin, "_log_event"):
                plugin._log_event("system", f"WebUI 设置已保存: {len(changes)}项", {
                    "source": source, "hot_applied": hot_applied, "restart_required": restart_required,
                })
            return {
                "changed": changes, "changed_count": len(changes), "source": source,
                "hot_applied": hot_applied, "restart_required": restart_required,
                "message": "设置已持久化" + ("；部分设置需重载插件" if restart_required else "并已热应用"),
            }

        def _api_settings_save(self, body: dict[str, Any]):
            values = body.get("values")
            if not isinstance(values, dict):
                return self._json_err("缺少 values", 400)
            try:
                self._json_ok(self._save_settings_values(values, "manual"))
            except ValueError as e:
                self._json_err(str(e), 400)
            except Exception as e:
                self._json_err(str(e))

        @staticmethod
        def _models_registry():
            registry = getattr(plugin, "_external_models", None)
            if registry is None:
                raise RuntimeError("外置模型池未加载")
            return registry

        def _api_models(self):
            try:
                data = self._models_registry().payload()
                data["plugin_version"] = getattr(plugin, "_PLUGIN_VERSION", "?")
                data["astr_fallback_active"] = not bool(data.get("enabled"))
                from .llm_compensation import TASK_OPTIONS
                from .model_tasks import TASKS
                data['task_route_options']=[{'value':key,'label':label,'family':family} for key,label,family in TASKS]
                from .generation_v2.model_probe import provider_options
                data['probe_provider_options'] = provider_options(plugin)
                data["followup_task_options"] = [
                    {"value": key, "label": label} for key, label in TASK_OPTIONS
                ]
                data["notice"] = (
                    "模型池接管与 Astr 故障跟接互相独立；主回复、Embedding 与 rerank 不变。"
                )
                self._json_ok(data)
            except Exception as exc:
                self._json_err(str(exc))

        def _api_model_calls(self):
            runtime = getattr(plugin, "_llm_runtime", None)
            from .generation_v2.model_probe import call_records
            records = list(getattr(plugin, "_llm_call_events", [])[-120:]) + call_records(plugin)
            self._json_ok({
                "calls": sorted(records, key=lambda r: float(r.get('ts', 0)))[-120:],
                "runtime": runtime.snapshot() if runtime is not None else {},
            })

        def _api_compensation(self):
            store = getattr(plugin, "_llm_compensation", None)
            self._json_ok({
                "items": store.list(150) if store is not None else [],
                "available": store is not None,
            })

        def _api_compensation_write(self, path: str, body: dict[str, Any]):
            store = getattr(plugin, "_llm_compensation", None)
            if store is None:
                return self._json_err("补偿队列未就绪", 503)
            record_id = str((body or {}).get("id") or "").strip()
            if not record_id or store.get(record_id) is None:
                return self._json_err("请求不存在", 404)
            if path == "/api/compensation/dismiss":
                row = store.get(record_id) or {}
                if str(row.get("status") or "") == "running":
                    return self._json_err("后台补偿正在运行，不能忽略", 409)
                store.update(record_id, "dismissed", increment_attempt=False)
                return self._json_ok({"id": record_id, "status": "dismissed"})
            if path == "/api/compensation/restore":
                try:
                    result = self._run_plugin_coro(
                        plugin._start_llm_compensation(record_id), timeout=15,
                    )
                    return self._json_ok(result)
                except Exception as exc:
                    return self._json_err(str(exc), self._plugin_coro_error_status(exc))
            return self._json_err("未知补偿操作", 404)

        def _api_production_fallback_repair_preview(self, body):
            episode_id = str((body or {}).get("episode_id") or "").strip()
            if not episode_id:
                return self._json_err("缺少 episode_id", 400)
            try:
                helper = self._production_helper("_create_fallback_repair_preview")
                result = self._run_plugin_coro(helper(episode_id), timeout=420)
                self._json_ok(self._public_production_preview(result))
            except RuntimeError as exc:
                self._json_err(str(exc), 503)
            except Exception as exc:
                self._json_err(str(exc))

        def _api_production_fallback_repair_confirm(self, body):
            preview_id = str((body or {}).get("preview_id") or "").strip()
            if not preview_id:
                return self._json_err("缺少 preview_id", 400)
            try:
                helper = self._production_helper("_confirm_fallback_repair")
                result = self._run_plugin_coro(helper(preview_id), timeout=420)
                self._json_ok(result)
            except RuntimeError as exc:
                self._json_err(str(exc), 503)
            except Exception as exc:
                self._json_err(str(exc))

        def _api_models_write(self, path: str, body: dict[str, Any]):
            try:
                registry = self._models_registry()
                if path == '/api/models/probe':
                    from .generation_v2.model_probe import probe
                    return self._json_ok(self._run_plugin_coro(probe(plugin, body), timeout=1840))
                if path == "/api/models/save":
                    data = registry.save(body)
                    if hasattr(plugin, "_log_event"):
                        plugin._log_event("system", "外置模型池设置已保存", {
                            "enabled": data.get("enabled"),
                            "models": len(data.get("models") or []),
                            "astr_followup_enabled": data.get("astr_followup_enabled"),
                            "astr_followup_model_id": data.get("astr_followup_model_id"),
                            "astr_followup_tasks": list(data.get("astr_followup_tasks") or []),
                            "persisted": data.get("persisted"),
                        })
                    return self._json_ok(data)
                if path == "/api/models/test":
                    model_id = str(body.get("model_id") or "").strip()
                    data = self._run_plugin_coro(
                        registry.test_model(model_id, float(body.get("timeout") or 20)),
                        timeout=50,
                    )
                    return self._json_ok(data)
                self._json_err("未知模型池操作", 404)
            except ValueError as exc:
                self._json_err(str(exc), 400)
            except Exception as exc:
                self._json_err(str(exc), self._plugin_coro_error_status(exc))

        def _api_threads_policy(self, body: dict[str, Any]):
            from .thread_policy import PRESETS, rollback, switch
            try:
                action = str(body.get("action") or "switch")
                confirmed = body.get("confirm") is True
                revision = body.get("expected_revision")
                if action == "preset":
                    name = str(body.get("preset") or "")
                    if name not in PRESETS:
                        raise ValueError("unknown preset")
                    result = switch(
                        plugin, dict(PRESETS[name]), source="preset:" + name,
                        manual=confirmed, expected_revision=revision,
                    )
                elif action == "rollback":
                    result = rollback(
                        plugin, expected_revision=revision, manual=confirmed,
                    )
                elif action == "switch":
                    result = switch(
                        plugin, body.get("values") or {}, source="manual",
                        manual=confirmed, expected_revision=revision,
                    )
                else:
                    raise ValueError("unknown action")
                self._json_ok(result)
            except ValueError as exc:
                self._json_err(str(exc), 400)
            except Exception as exc:
                self._json_err(str(exc), 503)

        def _api_threads_maintenance(self, body: dict[str, Any]):
            try:
                episodes = getattr(plugin, "_episodes", None)
                if episodes is None or episodes._threads is None:
                    raise RuntimeError("thread layer unavailable")
                action = str(body.get("action") or "")
                scope = str(getattr(plugin, "character_name", "default") or "default")
                if action == "scan":
                    from .thread_migration import ThreadMigration
                    result = ThreadMigration(
                        episodes._connect, episodes._lock, episodes._threads,
                    ).run_scan(
                        scope_id=scope, read_only=True, enqueue_existing=False,
                        batch_size=100, max_batches=1,
                    )
                elif action == "build":
                    from .thread_builder import ThreadBuilder
                    if episodes.thread_is_paused():
                        raise ValueError("resume before building")
                    result = ThreadBuilder(
                        episodes._threads, episodes._connect, episodes._lock,
                    ).process_batch(batch_size=10)
                elif action == "project":
                    from .thread_projector import ThreadProjector
                    ids = body.get("episode_ids")
                    if (
                        not isinstance(ids, list)
                        or not 1 <= len(ids) <= 50
                        or not all(isinstance(item, str) and item for item in ids)
                    ):
                        raise ValueError("select 1..50 episode nodes")
                    result = ThreadProjector(
                        episodes._threads,
                        min_confidence=float(
                            getattr(plugin, "thread_projection_min_confidence", .84)
                        ),
                    ).project(scope_id=scope, episode_ids=ids)
                else:
                    raise ValueError("unknown maintenance action")
                self._json_ok(result)
            except ValueError as exc:
                self._json_err(str(exc), 400)
            except Exception as exc:
                self._json_err(str(exc), 503)

        def _api_settings_preset(self, body: dict[str, Any]):
            preset_id = str(body.get("preset") or "").strip()
            preset = preset_definitions.get(preset_id)
            if not preset:
                return self._json_err("未知预设", 400)
            try:
                result = self._save_settings_values(
                    _safe_preset_values(preset["values"]),
                    f"preset:{preset_id}",
                )
                result["preset"] = {"id": preset_id, "name": preset["name"]}
                self._json_ok(result)
            except ValueError as e:
                self._json_err(str(e), 400)
            except Exception as e:
                self._json_err(str(e))

        def _api_eval_preset(self, body: dict[str, Any]):
            preset_id = str(body.get("preset") or "").strip()
            preset = recall_preset_definitions.get(preset_id)
            if not preset:
                return self._json_err("未知召回预设", 400)
            try:
                result = self._save_settings_values(
                    _safe_preset_values(preset["values"]),
                    f"recall_preset:{preset_id}",
                )
                result["preset"] = {"id": preset_id, "name": preset["name"]}
                self._json_ok(result)
            except ValueError as e:
                self._json_err(str(e), 400)
            except Exception as e:
                self._json_err(str(e))

        def _get_webui_db(self):
            """Get a fresh SQLite connection for WebUI thread (avoids cross-thread issue)."""
            import sqlite3
            db_path = getattr(plugin, "vec_db_path", "")
            if not db_path:
                return None
            conn = sqlite3.connect(db_path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            return conn

        def _api_stats(self):
            try:
                vec = getattr(plugin, "_vec", None)
                if vec is None:
                    return self._json_err("vec not ready", 503)
                # Use own connection to avoid thread issue
                conn = self._get_webui_db()
                if conn is None:
                    return self._json_err("db not available", 503)
                items = []
                total = 0
                try:
                    total = conn.execute("SELECT COUNT(DISTINCT memo_name) AS n FROM chunks").fetchone()["n"]
                    cur = conn.execute(
                        """SELECT memo_name, MAX(ts_text) AS ts_text, MAX(importance) AS importance,
                                  MAX(occurred_at) AS occurred_at, MAX(event_ts) AS event_ts,
                                  MAX(time_basis) AS time_basis,
                                  MAX(source_created_ts) AS source_created_ts, MAX(created_ts) AS created_ts,
                                  MAX(tags) AS tags, MAX(memory_type) AS memory_type
                           FROM chunks GROUP BY memo_name
                           ORDER BY COALESCE(NULLIF(MAX(event_ts),0), NULLIF(MAX(source_created_ts),0), MAX(created_ts)) DESC
                           LIMIT 10000""")
                    for r in cur:
                        items.append({
                            "importance": int(r["importance"] or 3),
                            "created_ts": float(r["created_ts"] or 0),
                            "occurred_at": str(r["occurred_at"] or ""),
                            "event_ts": float(r["event_ts"] or 0),
                            "source_created_ts": float(r["source_created_ts"] or 0),
                            "time_basis": r["time_basis"] or "unknown",
                            "memory_type": str(r["memory_type"] or "unknown"),
                        })
                finally:
                    conn.close()
                imp_dist = {}
                month_dist = {}
                memory_type_dist = {}
                for it in items:
                    imp = it.get("importance", 3)
                    imp_dist[imp] = imp_dist.get(imp, 0) + 1
                    memory_type = str(it.get("memory_type") or "unknown")
                    memory_type_dist[memory_type] = memory_type_dist.get(memory_type, 0) + 1
                    occurred_at = str(it.get("occurred_at") or "")
                    if len(occurred_at) >= 7 and occurred_at[4:5] == "-":
                        m = occurred_at[:7]
                        month_dist[m] = month_dist.get(m, 0) + 1
                    else:
                        ts = it.get("event_ts") or it.get("source_created_ts") or it.get("created_ts", 0)
                        if not ts:
                            continue
                        m = time.strftime("%Y-%m", time.localtime(ts))
                        month_dist[m] = month_dist.get(m, 0) + 1
                episodes = getattr(plugin, "_episodes", None)
                episode_stats = episodes.stats() if episodes is not None else {}
                episode_total = int(episode_stats.get("episodes") or 0)
                recoverable = min(
                    episode_total, int(episode_stats.get("recoverable_episodes") or 0)
                )
                data = {
                    "memories_total": total,
                    "importance_dist": [{"k": str(k), "v": v} for k, v in sorted(imp_dist.items())],
                    "memory_structure": {
                        "memory_types": [
                            {"k": k, "v": v}
                            for k, v in sorted(
                                memory_type_dist.items(), key=lambda pair: (-pair[1], pair[0])
                            )
                        ],
                        "evidence_quality": [
                            {"k": "source_grounded", "v": int(episode_stats.get("source_grounded") or 0)},
                            {"k": "mixed_user_edited", "v": int(episode_stats.get("mixed_user_edited") or 0)},
                            {"k": "diary_derived", "v": int(episode_stats.get("diary_derived") or 0)},
                        ],
                        "traceability": [
                            {"k": "recoverable", "v": recoverable},
                            {"k": "unlinked", "v": max(0, episode_total - recoverable)},
                        ],
                        "episodes_total": episode_total,
                    },
                    "by_month": [{"k": k, "v": v} for k, v in sorted(month_dist.items())],
                    "time_quality": {
                        "exact_or_explicit": sum(1 for x in items if (x.get("event_ts") or x.get("occurred_at")) and x.get("time_basis") in {"explicit", "explicit_dialogue", "conversation_now"}),
                        "inferred": sum(1 for x in items if (x.get("event_ts") or x.get("occurred_at")) and "inferred" in str(x.get("time_basis") or "")),
                        "unknown": sum(1 for x in items if not x.get("event_ts") and not x.get("occurred_at")),
                    },
                }
                self._json_ok(data)
            except Exception as e:
                self._json_err(str(e))

        def _api_memories(self, qs):
            try:
                vec = getattr(plugin, "_vec", None)
                if vec is None:
                    return self._json_err("vec not ready", 503)
                limit = min(int(qs.get("limit", ["200"])[0]), 10000)
                offset = int(qs.get("offset", ["0"])[0])
                conn = self._get_webui_db()
                if conn is None:
                    return self._json_err("db not available", 503)
                try:
                    cur = conn.execute(
                        """SELECT memo_name, MAX(ts_text) AS ts_text, MAX(occurred_at) AS occurred_at,
                                  MAX(event_ts) AS event_ts, MAX(time_basis) AS time_basis,
                                  MAX(source_created_ts) AS source_created_ts, MAX(source_updated_ts) AS source_updated_ts,
                                  MAX(indexed_ts) AS indexed_ts, MAX(importance) AS importance,
                                  MAX(created_ts) AS created_ts, MAX(manual) AS manual,
                                  MAX(tags) AS tags, MAX(memory_type) AS memory_type,
                                  MAX(long_effect) AS long_effect, MAX(trigger_hint) AS trigger_hint,
                                  MIN(chunk_text) AS chunk_text
                           FROM chunks GROUP BY memo_name
                           ORDER BY COALESCE(NULLIF(MAX(event_ts),0), NULLIF(MAX(source_created_ts),0), MAX(created_ts)) DESC
                           LIMIT ? OFFSET ?""",
                        (limit, offset))
                    items = []
                    for r in cur:
                        tags_raw = r["tags"] or ""
                        tags = [t for t in tags_raw.split(",") if t.strip()] if tags_raw else []
                        items.append({
                            "memo_name": r["memo_name"],
                            "ts_text": r["ts_text"] or "",
                            "occurred_at": r["occurred_at"] or "",
                            "event_ts": float(r["event_ts"] or 0),
                            "time_basis": r["time_basis"] or "unknown",
                            "source_created_ts": float(r["source_created_ts"] or 0),
                            "source_updated_ts": float(r["source_updated_ts"] or 0),
                            "indexed_ts": float(r["indexed_ts"] or 0),
                            "importance": int(r["importance"] or 3),
                            "tags": ",".join(tags),
                            "memory_type": r["memory_type"] or "plot_fact",
                            "long_effect": r["long_effect"] or "",
                            "trigger_hint": r["trigger_hint"] or "",
                            "preview": (r["chunk_text"] or "")[:120],
                            "chunk_text": r["chunk_text"] or "",
                        })
                    total = conn.execute("SELECT COUNT(DISTINCT memo_name) AS n FROM chunks").fetchone()["n"]
                finally:
                    conn.close()
                self._json_ok({"total": total, "items": items})
            except Exception as e:
                self._json_err(str(e))

        def _api_memo_detail(self, memo_name):
            try:
                if not memo_name:
                    return self._json_err("missing memo_name", 400)
                memos = getattr(plugin, "_memos", None)
                ts_text = "unknown"
                importance = 3
                tags = []
                full_text = ""
                memory_type = "plot_fact"
                long_effect = ""
                trigger_hint = ""
                occurred_at = ""
                event_ts = 0.0
                time_basis = "unknown"
                source_created_ts = 0.0
                source_updated_ts = 0.0
                indexed_ts = 0.0
                passages = []
                scene_anchor = ""
                retrieval_key = ""
                state_change = ""
                entities = ""

                # Try memos API first
                if memos is not None and memo_name.startswith("memos/"):
                    try:
                        memo = self._run_plugin_coro(memos.get_memo(memo_name), timeout=30)
                        if memo:
                            raw = _strip_internal_metadata((memo.get("content") or "").strip())
                            lines = raw.split("\n")
                            if lines:
                                ts_text = lines[0].strip()
                                body_lines = []
                                for ln in lines[1:]:
                                    stripped = ln.strip()
                                    if stripped.startswith("#"):
                                        continue
                                    if stripped.startswith(("长期影响:", "长期影响：")):
                                        long_effect = stripped.split(":", 1)[-1] if ":" in stripped else stripped.split("：", 1)[-1]
                                        continue
                                    if stripped.startswith(("触发线索:", "触发线索：")):
                                        trigger_hint = stripped.split(":", 1)[-1] if ":" in stripped else stripped.split("：", 1)[-1]
                                        continue
                                    body_lines.append(ln)
                                full_text = "\n".join(body_lines).strip()
                    except Exception:
                        pass

                # Local DB owns normalized temporal metadata even when full text came from Memos.
                conn = self._get_webui_db()
                if conn is not None:
                    try:
                        cur = conn.execute(
                                """SELECT id AS chunk_id, chunk_text, ts_text, importance, tags, memory_type,
                                          long_effect, trigger_hint, occurred_at, event_ts, time_basis,
                                          source_created_ts, source_updated_ts, indexed_ts,
                                          passage_index, char_start, char_end, scene_anchor,
                                          retrieval_key, state_change, entities
                                   FROM chunks WHERE memo_name=? ORDER BY passage_index, rowid""",
                                (memo_name,))
                        rows = cur.fetchall()
                        if rows:
                            ts_text = rows[0]["ts_text"] or ts_text
                            importance = int(rows[0]["importance"] or 3)
                            tags = (rows[0]["tags"] or "").split(",") if rows[0]["tags"] else []
                            memory_type = rows[0]["memory_type"] or memory_type
                            long_effect = rows[0]["long_effect"] or long_effect
                            trigger_hint = rows[0]["trigger_hint"] or trigger_hint
                            occurred_at = rows[0]["occurred_at"] or ""
                            event_ts = float(rows[0]["event_ts"] or 0)
                            time_basis = rows[0]["time_basis"] or "unknown"
                            source_created_ts = float(rows[0]["source_created_ts"] or 0)
                            source_updated_ts = float(rows[0]["source_updated_ts"] or 0)
                            indexed_ts = float(rows[0]["indexed_ts"] or 0)
                            scene_anchor = rows[0]["scene_anchor"] or ""
                            retrieval_key = rows[0]["retrieval_key"] or ""
                            state_change = rows[0]["state_change"] or ""
                            entities = rows[0]["entities"] or ""
                            passages = [{
                                "chunk_id": int(r["chunk_id"] or 0),
                                "passage_index": int(r["passage_index"] or 0),
                                "char_start": int(r["char_start"] or 0),
                                "char_end": int(r["char_end"] or 0),
                                "text": r["chunk_text"] or "",
                            } for r in rows]
                            if not full_text:
                                parts = []
                                for r in rows:
                                    ct = (r["chunk_text"] or "").strip()
                                    if not ct:
                                        continue
                                    if parts:
                                        last = parts[-1]
                                        for ol in range(30, 10, -1):
                                            if last.endswith(ct[:ol]):
                                                ct = ct[ol:]
                                                break
                                    parts.append(ct)
                                full_text = "".join(parts)
                    finally:
                        conn.close()

                if not full_text:
                    return self._json_err("not found", 404)

                self._json_ok({
                    "memo_name": memo_name,
                    "ts_text": ts_text,
                    "occurred_at": occurred_at,
                    "event_ts": event_ts,
                    "time_basis": time_basis,
                    "source_created_ts": source_created_ts,
                    "source_updated_ts": source_updated_ts,
                    "indexed_ts": indexed_ts,
                    "importance": importance,
                    "tags": tags,
                    "memory_type": memory_type,
                    "long_effect": long_effect,
                    "trigger_hint": trigger_hint,
                    "scene_anchor": scene_anchor,
                    "retrieval_key": retrieval_key,
                    "state_change": state_change,
                    "entities": entities,
                    "passages": passages,
                    "full_text": full_text,
                })
            except Exception as e:
                self._json_err(str(e))

        def _api_timeline(self, qs):
            try:
                vec = getattr(plugin, "_vec", None)
                if vec is None:
                    return self._json_err("vec not ready", 503)
                months = vec.month_index_overview()
                target = str(qs.get("month", [""])[0] or "").strip()
                if not target and months:
                    target = months[0]["year_month"]
                detail = vec.month_calendar(target) if target else {
                    "year_month": "", "memo_count": 0, "day_count": 0, "days": [], "index": {},
                }
                day_map = {int(item.get("day") or 0): item for item in detail.get("days") or []}
                weeks = []
                if target:
                    year, month = (int(value) for value in target.split("-", 1))
                    first_weekday, days_in_month = calendar.monthrange(year, month)
                    cells = [{"day": 0, "count": 0, "memos": []} for _ in range(first_weekday)]
                    for day in range(1, days_in_month + 1):
                        entry = day_map.get(day, {"day": day, "count": 0, "memos": []})
                        cells.append(entry)
                    while len(cells) % 7:
                        cells.append({"day": 0, "count": 0, "memos": []})
                    weeks = [cells[i:i + 7] for i in range(0, len(cells), 7)]
                recent = []
                for month_item in months[:3]:
                    month_detail = vec.month_calendar(month_item["year_month"])
                    for day in month_detail.get("days") or []:
                        recent.extend(day.get("memos") or [])
                recent.sort(
                    key=lambda item: float(item.get("event_ts") or item.get("source_created_ts") or item.get("created_ts") or 0),
                    reverse=True,
                )
                self._json_ok({
                    "months": months,
                    "selected": target,
                    "calendar": dict(detail, weeks=weeks),
                    "recent": recent[:8],
                    "view": {
                        "mode": "archive_only",
                        "participates_in_recall": False,
                    },
                })
            except Exception as e:
                self._json_err(str(e))

        def _api_month_route(self, qs):
            try:
                query = str(qs.get("q", [""])[0] or "").strip()
                if not query:
                    return self._json_err("missing q parameter", 400)
                data = self._run_plugin_coro(plugin._eval_month_route(query), timeout=30)
                self._json_ok(data)
            except Exception as e:
                self._json_err(str(e))

        def _api_search(self, qs):
            try:
                query = qs.get("q", [""])[0].strip()
                if not query:
                    return self._json_err("missing q parameter", 400)
                top_k = int(qs.get("top_k", ["5"])[0])
                data = self._run_plugin_coro(plugin._eval_recall_query(query, top_k=top_k), timeout=60)
                out = []
                for h in data.get("hits", []):
                    out.append({
                        "memo_name": h.get("memo_name", ""),
                        "ts_text": h.get("ts_text", ""),
                        "occurred_at": h.get("occurred_at", ""),
                        "event_ts": h.get("event_ts", 0),
                        "time_basis": h.get("time_basis", "unknown"),
                        "importance": h.get("importance", 3),
                        "memory_type": h.get("memory_type", "plot_fact"),
                        "long_effect": h.get("long_effect", ""),
                        "trigger_hint": h.get("trigger_hint", ""),
                        "score": round(h.get("score", 0), 4),
                        "relevance": round(h.get("relevance", 0), 4),
                        "preview": (h.get("preview", "") or "")[:300],
                        "selected": bool(h.get("selected")),
                        "reject_reason": h.get("reject_reason", ""),
                    })
                self._json_ok({"query": query, "hits": out, "candidate_count": data.get("candidate_count", len(out))})
            except Exception as e:
                self._json_err(str(e))

        def _api_eval(self, qs):
            try:
                query = qs.get("q", [""])[0].strip()
                if not query:
                    return self._json_err("missing q parameter", 400)
                try:
                    top_k = int(qs.get("top_k", ["0"])[0]) or None
                except Exception:
                    top_k = None
                data = self._run_plugin_coro(
                    plugin._eval_recall_query(query, top_k=top_k), timeout=60,
                )
                self._json_ok(data)
            except Exception as e:
                self._json_err(str(e))

        def _api_feedback_apply(self, body):
            try:
                memo_name = str(body.get("memo_name") or body.get("memo") or "").strip()
                action = str(body.get("action") or "").strip()
                request_id = str(body.get("request_id") or "").strip()
                query = str(body.get("query") or "").strip()
                source = str(body.get("source") or "webui").strip()[:40]
                if not memo_name or not action:
                    return self._json_err("missing memo_name/action", 400)
                mapping = {
                    "useful": (0.06, "有帮助"),
                    "key": (0.14, "关键记忆"),
                    "irrelevant": (-0.10, "与本次问题无关"),
                    "incorrect": (-0.18, "事实错误，待核验"),
                    "stale": (-0.12, "对当前状态已过时"),
                    "frequent": (-0.06, "近期出现太频繁"),
                }
                if action not in mapping:
                    return self._json_err("unknown action", 400)
                vec = getattr(plugin, "_vec", None)
                if vec is None:
                    return self._json_err("vec not ready", 503)
                if request_id and not query:
                    for stat in reversed(list(getattr(plugin, "_last_injection_stats", []) or [])):
                        if str(stat.get("request_id") or "") == request_id:
                            query = str(stat.get("query") or stat.get("query_with_context") or "").strip()
                            break
                if not query:
                    return self._json_err("feedback requires its original query", 400)
                embedding = None
                try:
                    embedding = self._run_plugin_coro(plugin._embed(query), timeout=20)
                except Exception as exc:
                    logger.debug("[memos-memory] feedback embedding unavailable: %s", exc)
                effect, default_reason = mapping[action]
                result = vec.feedback_record(
                    request_id=request_id,
                    memo_name=memo_name,
                    query_text=query,
                    action=action,
                    effect=effect,
                    reason=str(body.get("reason") or default_reason),
                    source=source,
                    query_embedding=embedding,
                )
                self._json_ok(result)
            except Exception as e:
                self._json_err(str(e))

        def _api_affiliate_status(self):
            try:
                if hasattr(plugin, "_affiliate_profile_status"):
                    self._json_ok(plugin._affiliate_profile_status())
                else:
                    self._json_ok({"enabled": False, "connected": False, "reason": "unsupported"})
            except Exception as e:
                self._json_err(str(e))

        def _api_time_insight_status(self):
            try:
                service = getattr(plugin, "_time_insight", None)
                if service is not None:
                    self._json_ok(self._run_plugin_coro(service.status(), timeout=15))
                elif hasattr(plugin, "_time_insight_status"):
                    self._json_ok(plugin._time_insight_status())
                else:
                    self._json_ok({"enabled": False, "connected": False, "reason": "unsupported"})
            except Exception as e:
                self._json_err(str(e))

        def _api_time_insight_settings(self):
            try:
                service = getattr(plugin, "_time_insight", None)
                if service is None:
                    return self._json_err("内置时间洞察未加载", 503)
                controller = self._xinchao_controller()
                self._json_ok({
                    "settings": service.settings_snapshot(),
                    "providerOptions": controller.provider_options(),
                    "status": self._run_plugin_coro(service.status(), timeout=15),
                })
            except Exception as e:
                self._json_err(str(e))

        def _api_time_insight_write(self, path: str, body: dict[str, Any]):
            try:
                service = getattr(plugin, "_time_insight", None)
                if service is None:
                    return self._json_err("内置时间洞察未加载", 503)
                if path == "/api/time-insight/settings/save":
                    incoming = body.get("settings")
                    if not isinstance(incoming, dict):
                        return self._json_err("settings 必须是对象", 400)
                    unknown = sorted(set(incoming) - time_insight_setting_keys)
                    if unknown:
                        return self._json_err("未知时间洞察设置: " + ", ".join(unknown[:8]), 400)
                    save_result = self._save_settings_values(incoming, "time-insight")
                    service.apply_settings()
                    return self._json_ok({
                        "save": save_result,
                        "settings": service.settings_snapshot(),
                    })
                if path == "/api/time-insight/update":
                    data = self._run_plugin_coro(service.update("webui"), timeout=190)
                elif path == "/api/time-insight/preview":
                    data = self._run_plugin_coro(service.preview(), timeout=60)
                else:
                    return self._json_err("未知操作", 404)
                self._json_ok(data)
            except (TypeError, ValueError) as e:
                self._json_err(str(e), 400)
            except Exception as e:
                self._json_err(str(e))

        def _api_managed_memos_status(self):
            try:
                st = plugin._managed_memos.as_dict() if hasattr(plugin, "_managed_memos") else {"mode": "external"}
                self._json_ok(st)
            except Exception as e:
                self._json_err(str(e))

        def _memos_diag(self):
            try:
                memos = getattr(plugin, "_memos", None)
                if memos is None:
                    return {"connected": False, "note": "memos 客户端未初始化"}
                return self._run_plugin_coro(memos.diagnose(), timeout=8)
            except Exception as e:
                return {"connected": False, "note": str(e)[:120]}

        def _api_health(self):
            try:
                vec = getattr(plugin, "_vec", None)
                if vec is None:
                    return self._json_err("vec not ready", 503)
                self._json_ok(vec.health_check())
            except Exception as e:
                self._json_err(str(e))

        def _api_test_llm(self):
            try:
                import asyncio
                ctx = getattr(plugin, "context", None)
                prov = None
                if ctx and hasattr(ctx, "get_using_provider"):
                    try:
                        prov = ctx.get_using_provider()
                    except Exception:
                        pass
                if prov is None:
                    return self._json_err("LLM provider not available", 503)
                name = getattr(prov, "provider_id", "unknown")
                t0 = time.time()
                caller = getattr(plugin, "_plugin_llm_text_chat", None)
                if callable(caller):
                    probe = caller(
                        prov,
                        prompt="ping",
                        contexts=[],
                        system_prompt="",
                        timeout=29,
                        label="webui_llm_probe",
                        optional=False,
                    )
                else:
                    probe = prov.text_chat(prompt="ping", contexts=[], system_prompt="")
                resp = self._run_plugin_coro(probe, timeout=30)
                cost = (time.time() - t0) * 1000
                text = getattr(resp, "completion_text", "") or ""
                self._json_ok({"provider": name, "ms": round(cost, 1), "response_len": len(text)})
            except Exception as e:
                self._json_err(str(e), self._plugin_coro_error_status(e))

        def _api_test_emb(self):
            try:
                emb = getattr(plugin, "_emb_provider", None)
                if emb is None:
                    return self._json_err("Emb provider not available", 503)
                name = getattr(emb, "provider_id", "unknown")
                t0 = time.time()
                vec = self._run_plugin_coro(emb.get_embedding("ping"), timeout=30)
                cost = (time.time() - t0) * 1000
                self._json_ok({"provider": name, "ms": round(cost, 1), "dim": len(vec), "first5": [round(x, 4) for x in vec[:5]]})
            except Exception as e:
                self._json_err(str(e), self._plugin_coro_error_status(e))

        def _api_test_rerank(self):
            try:
                rp = getattr(plugin, "_rerank_provider", None)
                if rp is None:
                    rid = getattr(plugin, "rerank_provider_id", "")
                    if not rid:
                        return self._json_err("rerank not configured", 503)
                    return self._json_err(f"rerank provider '{rid}' not available", 503)
                import time, concurrent.futures
                name = getattr(rp, "provider_id", "unknown")
                t0 = time.time()
                main_loop = getattr(getattr(plugin, "_webui", None), "_loop", None)
                if main_loop and main_loop.is_running():
                    future = asyncio.run_coroutine_threadsafe(
                        rp.rerank(query="cat", documents=["cat sat on mat", "the sky is blue", "a dog barks"], top_n=3),
                        main_loop,
                    )
                    results = future.result(timeout=15)
                else:
                    return self._json_err("event loop not available", 503)
                cost = (time.time() - t0) * 1000
                scores = [{"idx": r.index, "score": round(r.relevance_score, 4)} for r in results] if results else []
                self._json_ok({"provider": name, "ms": round(cost, 1), "results": scores})
            except Exception as e:
                self._json_err(str(e))

        def _api_graph(self):
            try:
                parsed = urlparse(self.path)
                qs = parse_qs(parsed.query)
                limit = min(max(int(qs.get("limit", ["120"])[0] or 120), 20), 400)
                query = (qs.get("q", [""])[0] or "").strip().lower()
                conn = self._get_webui_db()
                if conn is None:
                    return self._json_err("db not available", 503)
                try:
                    node_sql = """SELECT memo_name, MAX(ts_text) AS ts, MAX(importance) AS imp,
                                         MAX(tags) AS tags, MAX(memory_type) AS memory_type,
                                         MAX(long_effect) AS long_effect, MAX(trigger_hint) AS trigger_hint,
                                         MIN(chunk_text) AS preview, MAX(created_ts) AS created_ts,
                                         MAX(event_ts) AS event_ts, MAX(time_basis) AS time_basis
                                  FROM chunks GROUP BY memo_name"""
                    params: list[Any] = []
                    if query:
                        node_sql += """ HAVING lower(memo_name) LIKE ? OR lower(MAX(tags)) LIKE ?
                                             OR lower(MAX(memory_type)) LIKE ? OR lower(MIN(chunk_text)) LIKE ?
                                             OR lower(MAX(ts_text)) LIKE ?"""
                        like = f"%{query}%"
                        params.extend([like, like, like, like, like])
                    node_sql += " ORDER BY MAX(importance) DESC, COALESCE(NULLIF(MAX(event_ts),0), NULLIF(MAX(source_created_ts),0), MAX(created_ts)) DESC LIMIT ?"
                    params.append(limit)
                    memo_rows = conn.execute(node_sql, params).fetchall()
                    cluster_rows = conn.execute(
                        "SELECT memo_name, cluster_id, representative, cluster_size, max_similarity, reason FROM memo_similarity_clusters"
                    ).fetchall()
                    cluster_map = {r["memo_name"]: r for r in cluster_rows}
                    nodes = []
                    seen = set()
                    for r in memo_rows:
                        mn = r["memo_name"]
                        cr = cluster_map.get(mn)
                        seen.add(mn)
                        nodes.append({
                            "id": mn,
                            "label": mn.replace("memos/", ""),
                            "cluster_id": cr["cluster_id"] if cr else f"solo:{mn}",
                            "cluster_size": int(cr["cluster_size"] or 1) if cr else 1,
                            "representative": cr["representative"] if cr else mn,
                            "cluster_reason": cr["reason"] if cr else "unclustered",
                            "cluster_similarity": float(cr["max_similarity"] or 1.0) if cr else 1.0,
                            "imp": int(r["imp"] or 3),
                            "ts": r["ts"] or "",
                            "tags": r["tags"] or "",
                            "memory_type": r["memory_type"] or "plot_fact",
                            "long_effect": r["long_effect"] or "",
                            "trigger_hint": r["trigger_hint"] or "",
                            "preview": (r["preview"] or "")[:260],
                            "created_ts": float(r["created_ts"] or 0),
                            "event_ts": float(r["event_ts"] or 0),
                            "time_basis": r["time_basis"] or "unknown",
                        })
                    if seen:
                        placeholders = ",".join("?" for _ in seen)
                        rows = conn.execute(
                            f"""SELECT memo_a, memo_b, similarity, source, shared_tags FROM memo_similarity_edges
                                WHERE memo_a IN ({placeholders}) OR memo_b IN ({placeholders})
                                ORDER BY similarity DESC LIMIT ?""",
                            [*seen, *seen, limit * 4],
                        ).fetchall()
                    else:
                        rows = conn.execute(
                            "SELECT memo_a, memo_b, similarity, source, shared_tags FROM memo_similarity_edges ORDER BY similarity DESC LIMIT ?",
                            (limit * 4,),
                        ).fetchall()
                    edges = []
                    for r in rows:
                        a = r["memo_a"]
                        b = r["memo_b"]
                        if query and a not in seen and b not in seen:
                            continue
                        edges.append({
                            "from": a, "to": b, "sim": float(r["similarity"] or 0),
                            "source": r["source"] or "embedding",
                            "tags": r["shared_tags"] or "",
                        })
                        for mn in (a, b):
                            if mn not in seen:
                                cr = cluster_map.get(mn)
                                seen.add(mn)
                                row = conn.execute(
                                    """SELECT MAX(ts_text) AS ts, MAX(importance) AS imp, MAX(tags) AS tags,
                                              MAX(memory_type) AS memory_type, MAX(long_effect) AS long_effect,
                                              MAX(trigger_hint) AS trigger_hint, MIN(chunk_text) AS preview,
                                              MAX(created_ts) AS created_ts, MAX(event_ts) AS event_ts,
                                              MAX(time_basis) AS time_basis
                                       FROM chunks WHERE memo_name=?""",
                                    (mn,),
                                ).fetchone()
                                nodes.append({
                                    "id": mn,
                                    "label": mn.replace("memos/", ""),
                                    "cluster_id": cr["cluster_id"] if cr else f"solo:{mn}",
                                    "cluster_size": int(cr["cluster_size"] or 1) if cr else 1,
                                    "representative": cr["representative"] if cr else mn,
                                    "cluster_reason": cr["reason"] if cr else "unclustered",
                                    "cluster_similarity": float(cr["max_similarity"] or 1.0) if cr else 1.0,
                                    "imp": int(row["imp"] or 3) if row else 3,
                                    "ts": row["ts"] or "" if row else "",
                                    "tags": row["tags"] or "" if row else "",
                                    "memory_type": row["memory_type"] or "plot_fact" if row else "plot_fact",
                                    "long_effect": row["long_effect"] or "" if row else "",
                                    "trigger_hint": row["trigger_hint"] or "" if row else "",
                                    "preview": (row["preview"] or "")[:260] if row else "",
                                    "created_ts": float(row["created_ts"] or 0) if row else 0,
                                    "event_ts": float(row["event_ts"] or 0) if row else 0,
                                    "time_basis": row["time_basis"] or "unknown" if row else "unknown",
                                })
                    total = conn.execute("SELECT COUNT(DISTINCT memo_name) AS n FROM chunks").fetchone()["n"]
                    cluster_count = conn.execute("SELECT COUNT(DISTINCT cluster_id) AS n FROM memo_similarity_clusters").fetchone()["n"]
                finally:
                    conn.close()
                self._json_ok({"nodes": nodes, "edges": edges, "total_memos": total, "cluster_count": cluster_count, "query": query})
            except Exception as e:
                self._json_err(str(e))

        def _api_graph_clusters(self):
            try:
                conn = self._get_webui_db()
                if conn is None:
                    return self._json_err("db not available", 503)
                try:
                    clusters = getattr(plugin._vec, "similarity_cluster_overview")(limit=80)
                    edge_count = conn.execute("SELECT COUNT(*) AS n FROM memo_similarity_edges").fetchone()["n"]
                    memo_count = conn.execute("SELECT COUNT(DISTINCT memo_name) AS n FROM chunks").fetchone()["n"]
                finally:
                    conn.close()
                self._json_ok({"clusters": clusters, "total_memos": memo_count, "total_edges": edge_count})
            except Exception as e:
                self._json_err(str(e))

        def _api_graph_rebuild(self):
            try:
                import asyncio
                main_loop = getattr(getattr(plugin, "_webui", None), "_loop", None)
                if main_loop and main_loop.is_running():
                    future = asyncio.run_coroutine_threadsafe(
                        self._do_graph_rebuild(), main_loop)
                    result = future.result(timeout=60)
                    self._json_ok(result)
                else:
                    self._json_err("event loop not available", 503)
            except Exception as e:
                self._json_err(str(e))

        async def _do_graph_rebuild(self):
            """Rebuild similarity clusters from WebUI."""
            if not await plugin._ensure_init():
                return {"ok": False, "error": "init failed"}
            stats = plugin._build_similarity_cluster_index()
            return {"ok": True, "data": stats}

        def _api_graph_related(self, qs):
            try:
                mn = unquote(qs.get("memo", [""])[0])
                if not mn:
                    return self._json_err("missing memo param", 400)
                conn = self._get_webui_db()
                if conn is None:
                    return self._json_err("db not available", 503)
                try:
                    rows = conn.execute(
                        "SELECT memo_b AS related, similarity, source, shared_tags FROM memo_similarity_edges WHERE memo_a=? UNION ALL SELECT memo_a, similarity, source, shared_tags FROM memo_similarity_edges WHERE memo_b=? ORDER BY similarity DESC LIMIT 20",
                        (mn, mn)).fetchall()
                    related = []
                    for r in rows:
                        meta = conn.execute(
                            """SELECT MAX(ts_text) AS ts, MAX(importance) AS imp, MAX(memory_type) AS memory_type,
                                      MIN(chunk_text) AS preview FROM chunks WHERE memo_name=?""",
                            (r["related"],),
                        ).fetchone()
                        related.append({
                            "memo_name": r["related"],
                            "similarity": float(r["similarity"] or 0),
                            "source": r["source"] or "embedding",
                            "tags": r["shared_tags"] or "",
                            "ts": meta["ts"] or "" if meta else "",
                            "importance": int(meta["imp"] or 3) if meta else 3,
                            "memory_type": meta["memory_type"] or "" if meta else "",
                            "preview": (meta["preview"] or "")[:180] if meta else "",
                        })
                    if len(related) < 8:
                        base = conn.execute(
                            "SELECT MAX(tags) AS tags, MAX(memory_type) AS memory_type FROM chunks WHERE memo_name=?",
                            (mn,),
                        ).fetchone()
                        base_tags = {t.strip().lstrip("#") for t in ((base["tags"] or "") if base else "").replace(",", " ").split() if t.strip()}
                        base_type = (base["memory_type"] or "") if base else ""
                        seen_related = {x["memo_name"] for x in related}
                        candidates = conn.execute(
                            """SELECT memo_name, MAX(ts_text) AS ts, MAX(importance) AS imp, MAX(memory_type) AS memory_type,
                                      MAX(tags) AS tags, MIN(chunk_text) AS preview
                               FROM chunks WHERE memo_name<>? GROUP BY memo_name
                               ORDER BY MAX(importance) DESC,
                                        COALESCE(NULLIF(MAX(event_ts),0), NULLIF(MAX(source_created_ts),0), MAX(created_ts)) DESC LIMIT 300""",
                            (mn,),
                        ).fetchall()
                        fallback = []
                        for c in candidates:
                            cmn = c["memo_name"]
                            if cmn in seen_related:
                                continue
                            ctags = {t.strip().lstrip("#") for t in (c["tags"] or "").replace(",", " ").split() if t.strip()}
                            shared = sorted(base_tags & ctags)
                            type_hit = bool(base_type and base_type == (c["memory_type"] or ""))
                            score = len(shared) * 0.08 + (0.04 if type_hit else 0) + min(int(c["imp"] or 3), 5) * 0.01
                            if score <= 0:
                                continue
                            fallback.append((score, c, shared))
                        fallback.sort(key=lambda x: -x[0])
                        for score, c, shared in fallback[: max(0, 12 - len(related))]:
                            related.append({
                                "memo_name": c["memo_name"],
                                "similarity": round(min(0.79, score), 4),
                                "tags": ",".join(shared) or (c["tags"] or ""),
                                "ts": c["ts"] or "",
                                "importance": int(c["imp"] or 3),
                                "memory_type": c["memory_type"] or "",
                                "preview": (c["preview"] or "")[:180],
                                "fallback": True,
                            })
                finally:
                    conn.close()
                self._json_ok({"memo": mn, "related": related})
            except Exception as e:
                self._json_err(str(e))

        def _api_feedback(self):
            try:
                vec = getattr(plugin, "_vec", None)
                if vec is None:
                    return self._json_err("vec not ready", 503)
                recent_requests = []
                for stat in reversed(list(getattr(plugin, "_last_injection_stats", []) or [])):
                    query = str(stat.get("query") or "").strip()
                    memos = [str(value) for value in (stat.get("memos") or []) if value]
                    if not query or not memos:
                        continue
                    recent_requests.append({
                        "request_id": str(stat.get("request_id") or ""),
                        "ts": float(stat.get("ts") or 0),
                        "ts_iso": stat.get("ts_iso") or "",
                        "query": query,
                        "memos": memos,
                        "outcome": stat.get("outcome") or "",
                        "recall_postprocess": stat.get("recall_postprocess") or {},
                    })
                    if len(recent_requests) >= 30:
                        break
                self._json_ok({
                    "feedback": vec.feedback_event_list(limit=300),
                    "recent_requests": recent_requests,
                    "legacy_feedback": vec.feedback_get_all(),
                    "legacy_active": False,
                    "actions": ["useful", "key", "irrelevant", "incorrect", "stale", "frequent"],
                })
            except Exception as e:
                self._json_err(str(e))

        def _api_keywords(self):
            try:
                conn = self._get_webui_db()
                if conn is None:
                    return self._json_err("db not available", 503)
                try:
                    rows = conn.execute("SELECT memo_name, keyword, weight FROM memo_keywords ORDER BY memo_name, weight DESC").fetchall()
                    result = {}
                    for r in rows:
                        result.setdefault(r["memo_name"], []).append({"keyword": r["keyword"], "weight": r["weight"]})
                finally:
                    conn.close()
                self._json_ok({"keywords": result})
            except Exception as e:
                self._json_err(str(e))

        def _api_debug(self):
            """Diagnostic: dump plugin internal state."""
            try:
                vec = getattr(plugin, "_vec", None)
                memos = getattr(plugin, "_memos", None)
                vec_db_path = getattr(plugin, "vec_db_path", "?")
                db_exists = Path(vec_db_path).exists() if vec_db_path != "?" else False
                db_size = Path(vec_db_path).stat().st_size if db_exists else 0
                chunk_count = 0
                memo_count = 0
                conn = self._get_webui_db()
                if conn is not None:
                    try:
                        chunk_count = conn.execute("SELECT count(*) as c FROM chunks").fetchone()["c"]
                        memo_count = conn.execute("SELECT COUNT(DISTINCT memo_name) AS n FROM chunks").fetchone()["n"]
                    except Exception as ve:
                        chunk_count = f"error: {ve}"
                    finally:
                        conn.close()
                data = {
                    "initialized": getattr(plugin, "_initialized", False),
                    "init_error": getattr(plugin, "_init_error", ""),
                    "vec_is_none": vec is None,
                    "memos_is_none": memos is None,
                    "vec_db_path": vec_db_path,
                    "db_exists": db_exists,
                    "db_size_bytes": db_size,
                    "chunk_count": chunk_count,
                    "memo_count": memo_count,
                    "emb_provider": getattr(plugin, "_emb_model_id", ""),
                    "emb_dim": getattr(plugin, "_emb_dim", None),
                    "log_events_count": len(getattr(plugin, "_log_events", [])),
                    "memos_url": getattr(plugin, "memos_base_url", ""),
                }
                self._json_ok(data)
            except Exception as e:
                self._json_err(str(e))

        def _run_plugin_coro(self, coro, timeout=30):
            main_loop = getattr(getattr(plugin, "_webui", None), "_loop", None)
            if main_loop and main_loop.is_running():
                future = asyncio.run_coroutine_threadsafe(coro, main_loop)
                return future.result(timeout=timeout)
            try:
                closer = getattr(coro, "close", None)
                if callable(closer):
                    closer()
            except Exception:
                pass
            raise RuntimeError("AstrBot 主事件循环不可用，请等待插件初始化或重新加载")

        @staticmethod
        def _plugin_coro_error_status(exc: BaseException) -> int:
            text = str(exc).lower()
            return 503 if (
                "主事件循环不可用" in str(exc)
                or "timeout" in text or "timed out" in text
                or "provider" in text or "circuit" in text
            ) else 500

        def _xinchao_controller(self):
            controller = getattr(plugin, "_xinchao", None)
            if controller is None:
                raise RuntimeError("心潮控制器未加载")
            return controller

        def _api_xinchao_overview(self, qs):
            try:
                controller = self._xinchao_controller()
                key = (qs.get("key", [""])[0] or "").strip()
                overview = self._run_plugin_coro(controller.all_states(), timeout=15)
                selected = controller.scope_key_from_query(key or overview.get("selected", ""))
                overview["selected"] = selected
                overview["state"] = self._run_plugin_coro(controller.status(selected), timeout=15)
                overview["version"] = getattr(plugin, "_PLUGIN_VERSION", "?")
                overview["providerOptions"] = controller.provider_options()
                self._json_ok(overview)
            except Exception as e:
                self._json_err(str(e))

        def _api_xinchao_state(self, qs):
            try:
                key = (qs.get("key", [""])[0] or "").strip()
                controller = self._xinchao_controller()
                self._json_ok(self._run_plugin_coro(controller.status(key), timeout=15))
            except Exception as e:
                self._json_err(str(e))

        def _api_xinchao_settings(self):
            try:
                controller = self._xinchao_controller()
                self._json_ok({
                    "settings": controller.settings_payload(),
                    "providerOptions": controller.provider_options(),
                })
            except Exception as e:
                self._json_err(str(e))

        def _api_xinchao_write(self, path: str, body: dict[str, Any]):
            try:
                controller = self._xinchao_controller()
                key = str(body.get("key") or "")
                if path == "/api/xinchao/settings/save":
                    settings = body.get("settings")
                    if not isinstance(settings, dict):
                        return self._json_err("settings 必须是对象", 400)
                    return self._json_ok({"settings": controller.save_settings(settings)})
                if path == "/api/xinchao/settings/test-api":
                    data = self._run_plugin_coro(controller.test_perception_api(), timeout=55)
                    return self._json_ok(data)
                if path == "/api/xinchao/settle":
                    settle_timeout = max(
                        30.0,
                        float(controller.settings.get("post_perception_timeout_seconds") or 90) + 15.0,
                    )
                    data = self._run_plugin_coro(controller.settle_now(key), timeout=settle_timeout)
                elif path == "/api/xinchao/feedback":
                    data = self._run_plugin_coro(
                        controller.feedback(key, str(body.get("drive") or ""), float(body.get("delta") or 0)),
                        timeout=20,
                    )
                elif path == "/api/xinchao/thought":
                    data = self._run_plugin_coro(
                        controller.add_thought(
                            key,
                            str(body.get("drive") or ""),
                            str(body.get("text") or ""),
                            float(body.get("intensity") or 0.6),
                        ),
                        timeout=20,
                    )
                elif path == "/api/xinchao/simulate":
                    event = body.get("event")
                    if event is not None and not isinstance(event, dict):
                        return self._json_err("event 必须是对象", 400)
                    data = self._run_plugin_coro(
                        controller.simulate(key, float(body.get("hours") or 0), event),
                        timeout=20,
                    )
                elif path == "/api/xinchao/reset":
                    if body.get("confirm") != "RESET":
                        return self._json_err("请输入 RESET 确认重置", 400)
                    data = self._run_plugin_coro(controller.reset(key), timeout=20)
                else:
                    return self._json_err("未知操作", 404)
                self._json_ok(data)
            except (TypeError, ValueError) as e:
                self._json_err(str(e), 400)
            except Exception as e:
                self._json_err(str(e))

        def _backup_dir(self) -> Path:
            import os
            raw = getattr(plugin, "context_archive_backup_dir", "./data/astrbot_plugin_memos_memory/context_backups")
            base = Path(os.path.expanduser(raw or "./data/astrbot_plugin_memos_memory/context_backups"))
            if not base.is_absolute():
                base = Path.cwd() / base
            return base

        @staticmethod
        def _message_preview(item, index: int) -> dict[str, Any]:
            if not isinstance(item, dict):
                item = {"role": "unknown", "content": str(item)}
            content = item.get("content", "")
            if not isinstance(content, str):
                try:
                    content = json.dumps(content, ensure_ascii=False)
                except Exception:
                    content = str(content or "")
            return {
                "index": index,
                "role": item.get("role", "unknown"),
                "chars": len(content),
                "content": content,
                "tool_calls": item.get("tool_calls"),
            }

        @staticmethod
        def _conversation_history_list(conversation: Any) -> list[Any]:
            if conversation is None:
                return []
            raw = getattr(conversation, "history", None)
            if raw is None:
                raw = getattr(conversation, "content", None)
            if isinstance(raw, str):
                try:
                    raw = json.loads(raw or "[]")
                except (TypeError, ValueError, json.JSONDecodeError):
                    return []
            if isinstance(raw, dict):
                raw = raw.get("history") or raw.get("messages") or raw.get("content") or []
            return list(raw) if isinstance(raw, (list, tuple)) else []

        @staticmethod
        def _conversation_identity(conversation: Any) -> tuple[str, str]:
            umo = str(
                getattr(conversation, "user_id", "")
                or getattr(conversation, "unified_msg_origin", "")
                or ""
            )
            cid = str(
                getattr(conversation, "cid", "")
                or getattr(conversation, "conversation_id", "")
                or ""
            )
            return umo, cid

        def _backup_list(self) -> list[dict[str, Any]]:
            base = self._backup_dir()
            if not base.exists():
                return []
            out = []
            for p in sorted(base.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True)[:80]:
                meta = {}
                try:
                    raw = json.loads(p.read_text(encoding="utf-8"))
                    meta = raw.get("plan", {}) if isinstance(raw, dict) else {}
                    umo = raw.get("unified_msg_origin", "") if isinstance(raw, dict) else ""
                    cid = raw.get("conversation_id", "") if isinstance(raw, dict) else ""
                except Exception:
                    umo = ""
                    cid = ""
                st = p.stat()
                out.append({
                    "file": p.name,
                    "mtime": st.st_mtime,
                    "size": st.st_size,
                    "unified_msg_origin": umo,
                    "conversation_id": cid,
                    "total": meta.get("total"),
                    "kept": meta.get("kept"),
                    "removed": meta.get("removed"),
                })
            return out

        async def _collect_context_history(self, selected_umo: str = "", selected_cid: str = "") -> dict[str, Any]:
            mgr = getattr(plugin.context, "conversation_manager", None)
            seen_sessions = set(getattr(plugin, "_seen_context_sessions", set()) or [])
            items = []
            selected = None
            if mgr is not None:
                discovered: dict[tuple[str, str], Any] = {}
                try:
                    conversations = await mgr.get_conversations()
                except Exception:
                    conversations = []
                for conversation in conversations or []:
                    umo, cid = self._conversation_identity(conversation)
                    if umo and cid:
                        discovered[(umo, cid)] = conversation
                        seen_sessions.add(umo)

                current_ids: dict[str, str] = {}
                for umo in sorted(seen_sessions):
                    try:
                        cid = await mgr.get_curr_conversation_id(umo)
                        if cid:
                            current_ids[umo] = str(cid)
                            discovered.setdefault((umo, str(cid)), None)
                    except Exception as e:
                        items.append({"unified_msg_origin": umo, "error": str(e), "history_count": 0})

                rows_with_history: list[tuple[dict[str, Any], list[Any]]] = []
                for (umo, cid), summary in list(discovered.items())[:200]:
                    try:
                        conversation = await mgr.get_conversation(umo, cid)
                        if conversation is None:
                            conversation = summary
                        history = self._conversation_history_list(conversation)
                        stat = getattr(plugin, "_context_stats", {}).get(umo, {})
                        row = {
                            "unified_msg_origin": umo,
                            "conversation_id": cid,
                            "title": str(getattr(conversation, "title", "") or "未命名对话"),
                            "history_count": len(history),
                            "created_at": getattr(conversation, "created_at", 0) or 0,
                            "updated_at": getattr(conversation, "updated_at", 0) or 0,
                            "is_current": current_ids.get(umo) == cid,
                            "last_request_stat": stat,
                        }
                        rows_with_history.append((row, history))
                    except Exception as e:
                        rows_with_history.append(({
                            "unified_msg_origin": umo,
                            "conversation_id": cid,
                            "error": str(e),
                            "history_count": 0,
                            "is_current": current_ids.get(umo) == cid,
                        }, []))

                def row_sort_key(pair):
                    row = pair[0]
                    try:
                        updated = float(row.get("updated_at") or row.get("created_at") or 0)
                    except Exception:
                        updated = 0.0
                    return (bool(row.get("is_current")), updated)

                rows_with_history.sort(key=row_sort_key, reverse=True)
                items.extend(row for row, _ in rows_with_history)
                for row, history in rows_with_history:
                    matches = (
                        (selected_cid and row.get("conversation_id") == selected_cid)
                        or (selected_umo and not selected_cid and row.get("unified_msg_origin") == selected_umo and row.get("is_current"))
                    )
                    if matches or (not selected_umo and not selected_cid and selected is None):
                        selected = {
                            **row,
                            "messages": [self._message_preview(x, i) for i, x in enumerate(history)],
                        }
                        if matches:
                            break
            return {
                "sessions": items,
                "selected": selected,
                "backups": self._backup_list(),
                "settings": {
                    "exclude_command_turns": getattr(plugin, "context_exclude_command_turns", True),
                    "request_keep": getattr(plugin, "context_keep_recent_messages", 0),
                    "request_min": getattr(plugin, "context_min_messages_before_trim", 0),
                    "archive_keep": getattr(plugin, "context_archive_keep_recent_messages", 0),
                    "archive_min": getattr(plugin, "context_archive_min_total_messages", 0),
                    "archive_enabled": getattr(plugin, "context_archive_enable", False),
                    "archive_interval_days": getattr(plugin, "context_archive_interval_days", 0),
                },
            }

        def _api_context_history(self, qs):
            try:
                umo = (qs.get("umo", [""])[0] or "").strip()
                cid = (qs.get("cid", [""])[0] or "").strip()
                data = self._run_plugin_coro(self._collect_context_history(umo, cid), timeout=30)
                self._json_ok(data)
            except Exception as e:
                self._json_err(str(e))

        def _api_context_backup(self, qs):
            try:
                name = (qs.get("file", [""])[0] or "").strip()
                if not name:
                    return self._json_err("missing file", 400)
                base = self._backup_dir().resolve()
                path = (base / name).resolve()
                if base not in path.parents and path != base:
                    return self._json_err("invalid file", 400)
                if not path.exists() or path.suffix.lower() != ".json":
                    return self._json_err("backup not found", 404)
                raw = json.loads(path.read_text(encoding="utf-8"))
                history = raw.get("history", []) if isinstance(raw, dict) else []
                if not isinstance(history, list):
                    history = []
                self._json_ok({
                    "file": path.name,
                    "meta": {k: v for k, v in raw.items() if k != "history"} if isinstance(raw, dict) else {},
                    "messages": [self._message_preview(x, i) for i, x in enumerate(history)],
                })
            except Exception as e:
                self._json_err(str(e))

        def _api_logs(self, qs):
            try:
                if qs.get("clear"):
                    plugin._log_events.clear()
                    if hasattr(plugin, "_save_runtime_telemetry"):
                        plugin._telemetry_dirty = True
                        plugin._save_runtime_telemetry(force=True)
                    return self._json_ok({"cleared": True})
                limit = min(int(qs.get("limit", ["200"])[0]), 1000)
                cat = (qs.get("category", [""])[0] or "").strip()
                logs = plugin._get_logs(limit=limit, category=cat)
                logs = [e for e in logs if isinstance(e, dict) and e.get("category") != "kb_cache"]
                known = ["xinchao", "enhancer", "compress", "inject", "cache", "recall", "sync", "system"]
                counts = {k: 0 for k in known}
                for e in plugin._log_events:
                    c = e["category"]
                    if c == "kb_cache":
                        continue
                    counts[c] = counts.get(c, 0) + 1
                self._json_ok({"logs": logs, "total": sum(counts.values()), "counts": counts})
            except Exception as e:
                self._json_err(str(e))

        def _api_console_logs(self):
            self._json_ok([
                event for event in list(getattr(plugin, "_log_events", []))
                if isinstance(event, dict) and event.get("category") != "kb_cache"
            ])

        def _api_console_stats(self):
            try:
                keys = ["current_time", "semantic_state", "profile", "time_insight", "diary", "event_core", "evidence", "memory_structure", "xinchao", "enhancer", "context", "other_extra"]
                overlay_keys = ["access_supplement"]
                rows = []
                for source in list(getattr(plugin, "_last_injection_stats", []) or [])[-80:]:
                    row = dict(source) if isinstance(source, dict) else {}
                    composition = dict(row.get("composition") or {})
                    composition.pop("kb_cache", None)
                    row.pop("kb_cache", None)
                    row["composition"] = composition
                    row["total_est_chars"] = sum(int(composition.get(key) or 0) for key in keys)
                    rows.append(row)
                totals = {k: 0 for k in keys + overlay_keys}
                for row in rows:
                    comp = row.get("composition", {}) if isinstance(row, dict) else {}
                    for k in keys + overlay_keys:
                        try:
                            totals[k] += int(comp.get(k) or 0)
                        except Exception:
                            pass
                total_chars = sum(int(totals.get(key) or 0) for key in keys)
                events = [
                    event for event in (getattr(plugin, "_log_events", []) or [])
                    if isinstance(event, dict) and event.get("category") != "kb_cache"
                ]
                known = ["xinchao", "enhancer", "compress", "inject", "cache", "recall", "sync", "system"]
                counts = {k: 0 for k in known}
                for e in events:
                    c = e.get("category", "system") if isinstance(e, dict) else "system"
                    counts[c] = counts.get(c, 0) + 1
                rp_stats = getattr(plugin, "_rp_stats", {}) if isinstance(getattr(plugin, "_rp_stats", {}), dict) else {}
                provider_cache_stats = getattr(plugin, "_provider_cache_stats", {}) if isinstance(getattr(plugin, "_provider_cache_stats", {}), dict) else {}
                ctx_stats = getattr(plugin, "_context_stats", {}) if isinstance(getattr(plugin, "_context_stats", {}), dict) else {}
                sys_stats = getattr(plugin, "_system_cache_stats", {}) if isinstance(getattr(plugin, "_system_cache_stats", {}), dict) else {}
                prefix_stats = getattr(plugin, "_prefix_cache_stats", {}) if isinstance(getattr(plugin, "_prefix_cache_stats", {}), dict) else {}
                self._json_ok({
                    "rows": rows,
                    "latest": rows[-1] if rows else {},
                    "totals": totals,
                    "total_chars": total_chars,
                    "counts": counts,
                    "features": {
                        "enhancer": bool(getattr(plugin, "rp_enhancer_enable", False)),
                        "provider_cache": bool(getattr(plugin, "cache_prefix_drift_enable", False)),
                        "context": bool(getattr(plugin, "context_governance_enable", False)),
                        "system_cache_guard": bool(getattr(plugin, "cache_friendly_system_guard_enable", False)),
                        "prefix_drift": bool(getattr(plugin, "cache_prefix_drift_enable", False)),
                        "archive": bool(getattr(plugin, "context_archive_enable", False)),
                        "profile": bool(getattr(plugin, "enable_affiliate_profile", False)),
                        "time_insight": bool(getattr(plugin, "enable_time_insight_affiliate", False)),
                        "xinchao": bool(getattr(getattr(plugin, "_xinchao", None), "settings", {}).get("enable", False)),
                        "managed_memos": getattr(plugin, "memos_mode", "external") == "managed",
                        "access_supplement": str(getattr(plugin, "memory_access_route_mode", "shadow")) == "supplement",
                        "llm_runtime": True,
                    },
                    "latest_runtime": {
                        "enhancer": list(rp_stats.values())[-1] if rp_stats else {},
                        "provider_cache": list(provider_cache_stats.values())[-1] if provider_cache_stats else {},
                        "context": list(ctx_stats.values())[-1] if ctx_stats else {},
                        "system_cache_guard": list(sys_stats.values())[-1] if sys_stats else {},
                        "prefix_drift": list(prefix_stats.values())[-1] if prefix_stats else {},
                        "xinchao": (
                            list(getattr(plugin._xinchao, "_last_injection", {}).values())[-1]
                            if getattr(getattr(plugin, "_xinchao", None), "_last_injection", {})
                            else {}
                        ),
                        "managed_memos": plugin._managed_memos.as_dict() if hasattr(plugin, "_managed_memos") else {"mode": "external"},
                        "llm_runtime": (
                            plugin._llm_runtime.snapshot()
                            if getattr(plugin, "_llm_runtime", None) is not None else {}
                        ),
                    },
                    "log_total": len(events),
                    "telemetry": {
                        "plugin_version": getattr(plugin, "_PLUGIN_VERSION", "?"),
                        "instance": hex(id(plugin)),
                        "sample_count": len(rows),
                        "event_count": len(events),
                        "persisted_at": float(getattr(plugin, "_telemetry_last_save", 0) or 0),
                        "dirty": bool(getattr(plugin, "_telemetry_dirty", False)),
                    },
                })
            except Exception as e:
                self._json_err(str(e))

    return Handler


class WebUIServer:
    """WebUI server using stdlib http.server + threading. No aiohttp dependency."""

    def __init__(self, plugin):
        self.plugin = plugin
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def _registry_key(self) -> tuple[str, int]:
        return (str(self.plugin.webui_host), int(self.plugin.webui_port))

    @staticmethod
    def _server_matches(server: Any, host: str, port: int) -> bool:
        try:
            address = server.server_address
            return int(address[1]) == int(port) and str(address[0]) in {str(host), "0.0.0.0", "127.0.0.1", "::"}
        except Exception:
            return False

    def _close_stale_server(self) -> bool:
        """Take over a WebUI port left behind by an older hot-reloaded plugin instance."""
        host, port = self._registry_key
        registry = getattr(builtins, "_astrbot_memos_webui_servers", None)
        if not isinstance(registry, dict):
            registry = {}
            setattr(builtins, "_astrbot_memos_webui_servers", registry)
        stale = registry.get((host, port))
        candidates = []
        if stale is not None and stale is not self:
            server = getattr(stale, "_server", None)
            if server is not None:
                candidates.append(server)
        if not candidates:
            for obj in gc.get_objects():
                try:
                    if isinstance(obj, ThreadingHTTPServer) and obj is not self._server and self._server_matches(obj, host, port):
                        candidates.append(obj)
                except Exception:
                    continue
        closed = False
        for server in candidates[:2]:
            try:
                server.shutdown()
                server.server_close()
                closed = True
            except Exception:
                continue
        registry.pop((host, port), None)
        return closed

    async def start(self) -> bool:
        """Start the server in a daemon thread. Async signature for compatibility."""
        if not self.plugin.webui_enable:
            return False
        try:
            if await asyncio.to_thread(self._close_stale_server):
                logger.info("[memos-memory] WebUI took over stale hot-reload server on %s:%d", self.plugin.webui_host, self.plugin.webui_port)
            here = Path(__file__).resolve().parent
            dashboard_html = (here / "dashboard.html").read_text(encoding="utf-8") if (here / "dashboard.html").exists() else "<h1>dashboard.html missing</h1>"

            # Console HTML (inline)
            console_html = self._build_console_html()
            xinchao_html = (
                (here / "xinchao.html").read_text(encoding="utf-8")
                if (here / "xinchao.html").exists()
                else "<h1>xinchao.html missing</h1>"
            )
            production_html = (
                (here / "production.html").read_text(encoding="utf-8")
                if (here / "production.html").exists()
                else "<h1>production.html missing</h1>"
            )
            access_html = (
                (here / "access.html").read_text(encoding="utf-8")
                if (here / "access.html").exists()
                else "<h1>access.html missing</h1>"
            )
            forgetting_html = (
                (here / "forgetting.html").read_text(encoding="utf-8")
                if (here / "forgetting.html").exists()
                else "<h1>forgetting.html missing</h1>"
            )
            models_html = (
                (here / "models.html").read_text(encoding="utf-8")
                if (here / "models.html").exists()
                else "<h1>models.html missing</h1>"
            )
            compensation_html = (
                (here / "compensation.html").read_text(encoding="utf-8")
                if (here / "compensation.html").exists()
                else "<h1>compensation.html missing</h1>"
            )
            house_html = (
                (here / "house.html").read_text(encoding="utf-8")
                if (here / "house.html").exists()
                else "<h1>house.html missing</h1>"
            )
            house_assets = {}
            for name in (
                "house-courtyard.png", "house-courtyard-dawn.png",
                "house-courtyard-dusk.png", "house-courtyard-night.png",
                "house-character-atlas-a.png",
                "house-character-atlas-b.png", "house-character-atlas-c.png",
                "house-character-atlas-d.png",
                "house-character-fan.png", "house-character-letter.png",
                "house-character-tea.png", "house-character-desk.png",
                "house-character-window.png", "house-character-chest.png",
                "house-character-playful.png", "house-character-reading.png",
                "house-character-lantern.png", "house-character-wistful.png",
                "house-character-flowers.png", "house-character-steps.png",
                "house-character-yawn.png", "house-character-lean.png",
                "house-character-book.png", "house-character-cup.png",
                "house-character-court-smile.png",
                "house-character-court-surprised.png",
                "house-character-court-soft.png",
                "house-character-rig.js",
                "house-live2d.js",
                "workbench.css", "workbench.js", "scheduling.js", "lucide.min.js", "atelier.css",
            ):
                path = here / "assets" / name
                if path.exists():
                    house_assets[name] = path.read_bytes()
            for folder in ("live2d", "vendor"):
                root = here / "assets" / folder
                if not root.exists():
                    continue
                for path in root.rglob("*"):
                    if path.is_file():
                        relative = path.relative_to(here / "assets").as_posix()
                        house_assets[relative] = path.read_bytes()

            handler_cls = _make_handler(
                self.plugin,
                dashboard_html,
                console_html,
                xinchao_html,
                production_html,
                access_html,
                forgetting_html,
                models_html,
                house_html,
                house_assets,
                compensation_html,
            )
            self._server = ThreadingHTTPServer(
                (self.plugin.webui_host, self.plugin.webui_port),
                handler_cls,
            )
            self._loop = asyncio.get_event_loop()
            # Register before the HTTP thread can accept requests.  The normal
            # plugin startup path also assigns this field after start(), but
            # direct starts and hot-reload handoffs need the main-loop bridge
            # to be available from the very first request.
            if getattr(self.plugin, "_webui", None) in (None, self):
                self.plugin._webui = self
            self._thread = threading.Thread(
                target=self._server.serve_forever,
                name="MemosMemoryWebUI",
                daemon=True,
            )
            self._thread.start()
            registry = getattr(builtins, "_astrbot_memos_webui_servers", None)
            if isinstance(registry, dict):
                registry[self._registry_key] = self
            logger.info("[memos-memory] WebUI started: http://%s:%d/",
                       self.plugin.webui_host, self.plugin.webui_port)
            return True
        except Exception as exc:
            logger.warning("[memos-memory] WebUI start failed: %s", exc)
            return False

    async def stop(self) -> None:
        if self._server is not None:
            server = self._server
            self._server = None
            self._thread = None
            await asyncio.to_thread(server.shutdown)
            server.server_close()
        registry = getattr(builtins, "_astrbot_memos_webui_servers", None)
        if isinstance(registry, dict) and registry.get(self._registry_key) is self:
            registry.pop(self._registry_key, None)
        if getattr(self.plugin, "_webui", None) is self:
            self.plugin._webui = None

    def _build_console_html(self) -> str:
        ver = getattr(self.plugin, "_PLUGIN_VERSION", "?")
        return _CONSOLE_HTML_V2.replace("{{VERSION}}", ver).replace("</head>",
            '<link rel="stylesheet" href="/assets/workbench.css?v=610blue1">'
            '<link rel="stylesheet" href="/assets/atelier.css?v=610blue1">'
            '<script src="/assets/lucide.min.js" defer></script>'
            '<script src="/assets/workbench.js?v=610blue1" defer></script></head>')


_CONSOLE_HTML_V2 = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>memos-memory operations console</title>
<style>
:root{color-scheme:dark;--bg:#080d16;--panel:#0f1725;--panel2:#131d2e;--line:#263449;--line2:#1c293b;--text:#dbe7f6;--muted:#8393a9;--blue:#60a5fa;--green:#34d399;--amber:#fbbf24;--red:#f87171;--violet:#a78bfa;--pink:#f472b6;--cyan:#22d3ee}
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,"Microsoft YaHei",monospace;background:radial-gradient(circle at 20% 0,#10213a 0,#080d16 32%,#070b12 100%);color:var(--text);padding:18px;font-size:12px;letter-spacing:0}
.shell{max-width:1520px;margin:0 auto}.head{display:flex;justify-content:space-between;align-items:flex-start;gap:16px;margin-bottom:14px}h1{font-size:18px;margin:0 0 6px;color:#fff;font-weight:760}.sub{font-size:12px;color:var(--muted);line-height:1.5}.top-actions{display:flex;gap:8px;flex-wrap:wrap;justify-content:flex-end}.top-actions a,.filter-bar button{padding:7px 12px;border:1px solid var(--line);border-radius:7px;background:var(--panel2);color:var(--text);cursor:pointer;font-size:12px;text-decoration:none}.top-actions a:hover,.filter-bar button:hover{border-color:#3b82f6;background:#17243a}
.stats{display:grid;grid-template-columns:repeat(8,minmax(112px,1fr));gap:8px;margin-bottom:12px}.metric{background:rgba(15,23,37,.78);border:1px solid var(--line);border-radius:9px;padding:10px 12px;color:var(--muted)}.metric span{display:block;font-size:21px;line-height:1.1;margin-top:4px;color:#fff;font-weight:760;font-variant-numeric:tabular-nums}
.grid{display:grid;grid-template-columns:1.05fr 1fr;gap:12px;margin-bottom:12px}.panel{background:rgba(15,23,37,.86);border:1px solid var(--line);border-radius:10px;padding:12px;box-shadow:0 24px 80px rgba(0,0,0,.24)}.panel-title{display:flex;justify-content:space-between;align-items:center;color:#fff;font-size:13px;font-weight:760;margin-bottom:10px}.muted{color:var(--muted)}
.mix{display:grid;gap:8px}.mix-row{display:grid;grid-template-columns:92px 1fr 86px;gap:9px;align-items:center}.mix-name{color:#c7d2e3}.mix-val{color:#fff;text-align:right;font-variant-numeric:tabular-nums}.bar{height:9px;background:#0b1220;border:1px solid #1b293d;border-radius:999px;overflow:hidden}.fill{height:100%;width:0;background:var(--blue)}.fill.current_time{background:#f97316}.fill.semantic_state{background:#22c55e}.fill.profile{background:var(--violet)}.fill.diary{background:var(--amber)}.fill.event_core{background:#fb7185}.fill.evidence{background:#38bdf8}.fill.memory_structure{background:#94a3b8}.fill.time_insight{background:var(--cyan)}.fill.xinchao{background:#e879f9}.fill.enhancer{background:var(--green)}.fill.context{background:var(--blue)}.fill.other_extra{background:var(--pink)}
.mini-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-top:10px}.mini{border:1px solid var(--line2);border-radius:7px;padding:8px;color:var(--muted);background:#0c1421}.mini b{display:block;color:#fff;font-size:15px;margin-top:2px;font-variant-numeric:tabular-nums}.state-grid{display:grid;grid-template-columns:repeat(2,1fr);gap:8px}.state{border:1px solid var(--line2);border-radius:8px;background:#0c1421;padding:9px}.state strong{display:flex;align-items:center;justify-content:space-between;color:#fff;margin-bottom:6px}.dot{width:8px;height:8px;border-radius:50%;display:inline-block;background:#64748b}.dot.on{background:#22c55e;box-shadow:0 0 0 3px rgba(34,197,94,.14)}.dot.off{background:#ef4444;box-shadow:0 0 0 3px rgba(239,68,68,.12)}.state pre{white-space:pre-wrap;color:#aebcd0;font-size:11px;line-height:1.45}
#inject-table{max-width:100%;overflow-x:auto}.inject-table{width:100%;min-width:716px;border-collapse:collapse;font-variant-numeric:tabular-nums}.inject-table th,.inject-table td{border-bottom:1px solid var(--line2);padding:7px 6px;text-align:right;white-space:nowrap}.inject-table th:first-child,.inject-table td:first-child{text-align:left}.inject-table th{color:var(--muted);font-weight:600}.inject-table td{color:#d9e5f5}
.filter-bar{display:flex;gap:7px;margin-bottom:12px;flex-wrap:wrap;align-items:center;background:rgba(15,23,37,.72);border:1px solid var(--line);border-radius:10px;padding:10px}.filter-bar button.active{background:#1d4ed8;color:#fff;border-color:#3b82f6}label{color:var(--muted)}input[type=checkbox]{accent-color:#3b82f6}
#log-list{background:rgba(15,23,37,.86);border-radius:10px;border:1px solid var(--line);max-height:calc(100vh - 610px);min-height:260px;overflow-y:auto;box-shadow:0 24px 80px rgba(0,0,0,.25)}.log-entry{padding:8px 12px;border-bottom:1px solid var(--line2);display:grid;grid-template-columns:86px 92px 1fr;gap:10px;align-items:flex-start;line-height:1.45}.log-entry:hover{background:#111d2f}.log-entry:last-child{border-bottom:none}.log-cat{display:inline-block;padding:2px 8px;border-radius:5px;font-size:11px;font-weight:760;min-width:72px;text-align:center;text-transform:uppercase;letter-spacing:.3px}.cat-xinchao{background:rgba(232,121,249,.12);color:#f0abfc;border:1px solid rgba(232,121,249,.26)}.cat-enhancer{background:rgba(52,211,153,.12);color:var(--green);border:1px solid rgba(52,211,153,.26)}.cat-compress{background:rgba(96,165,250,.12);color:var(--blue);border:1px solid rgba(96,165,250,.26)}.cat-inject{background:rgba(251,191,36,.12);color:var(--amber);border:1px solid rgba(251,191,36,.26)}.cat-recall{background:rgba(167,139,250,.12);color:var(--violet);border:1px solid rgba(167,139,250,.26)}.cat-sync{background:rgba(244,114,182,.12);color:var(--pink);border:1px solid rgba(244,114,182,.26)}.cat-cache{background:rgba(20,184,166,.12);color:#5eead4;border:1px solid rgba(20,184,166,.26)}.cat-system{background:rgba(148,163,184,.12);color:#cbd5e1;border:1px solid rgba(148,163,184,.24)}.log-time{color:var(--muted);white-space:nowrap;font-variant-numeric:tabular-nums}.log-msg{word-break:break-word;color:#d9e5f5}.log-detail{display:block;color:#93a4bb;font-size:11px;margin-top:4px;white-space:pre-wrap}.empty{padding:34px;text-align:center;color:var(--muted)}.ctx-used{display:inline-block;margin-left:8px;padding:1px 7px;border-radius:5px;font-size:11px;font-weight:760}.ctx-used.yes{background:rgba(52,211,153,.13);color:#34d399;border:1px solid rgba(52,211,153,.3)}.ctx-used.no{background:rgba(248,113,113,.13);color:#f87171;border:1px solid rgba(248,113,113,.3)}.ctx-used.wait{background:rgba(251,191,36,.12);color:#fbbf24;border:1px solid rgba(251,191,36,.3)}
@media(max-width:980px){.head{display:block}.top-actions{justify-content:flex-start;margin-top:10px}.stats{grid-template-columns:repeat(2,1fr)}.grid{grid-template-columns:1fr}.mini-grid,.state-grid{grid-template-columns:1fr}.log-entry{grid-template-columns:1fr;gap:4px}#log-list{max-height:none}}
</style>
</head>
<body>
<div class="shell">
<div class="head">
<div>
<h1>运行控制台 <span class="muted" style="font-size:12px">v{{VERSION}}</span></h1>
<div class="sub">实时事件 · 请求组成 · 子系统状态 · 最近注入</div>
</div>
<div class="top-actions"><a href="/">打开 WebUI</a><a href="/xinchao">心潮工作台</a><a href="/api/console/stats" target="_blank">Stats API</a><a href="/api/logs?limit=300" target="_blank">Logs API</a></div>
</div>

<div class="stats">
<div class="metric">Total Logs <span id="total">0</span></div>
<div class="metric">Inject <span id="cnt-inject">0</span></div>
<div class="metric">Recall <span id="cnt-recall">0</span></div>
<div class="metric">Provider Cache <span id="cnt-cache">0</span></div>
<div class="metric">Xinchao <span id="cnt-xinchao">0</span></div>
<div class="metric">Enhancer <span id="cnt-enhancer">0</span></div>
<div class="metric">Compress <span id="cnt-compress">0</span></div>
<div class="metric">Sync <span id="cnt-sync">0</span></div>
</div>

<div class="grid">
<section class="panel">
<div class="panel-title"><span>Latest Injection Mix</span><span class="muted" id="latest-time">waiting</span></div>
<div class="mix" id="latest-mix"></div>
<div class="mini-grid">
<div class="mini">Total estimate<b id="latest-total">0 字</b></div>
<div class="mini">Memory block<b id="latest-memory">0 字</b></div>
<div class="mini">Memo count<b id="latest-count">0</b></div>
<div class="mini">ACCESS subset<b id="latest-access">0 字</b></div>
</div>
</section>
<section class="panel">
<div class="panel-title"><span>Subsystem State</span><span class="muted">current process</span></div>
<div class="state-grid" id="state-grid"></div>
</section>
</div>

<section class="panel" style="margin-bottom:12px">
<div class="panel-title"><span>Recent Injection Samples</span><span class="muted">last 10</span></div>
<div id="inject-table"></div>
</section>

<div class="filter-bar">
<button class="active" data-cat="">All</button>
<button data-cat="xinchao">xinchao</button>
<button data-cat="enhancer">enhancer</button>
<button data-cat="compress">compress</button>
<button data-cat="inject">inject</button>
<button data-cat="cache">cache</button>
<button data-cat="recall">recall</button>
<button data-cat="sync">sync</button>
<button data-cat="system">system</button>
<button id="refresh-btn">Refresh</button>
<label style="display:flex;align-items:center;gap:4px;font-size:12px;margin-left:auto"><input type="checkbox" id="auto-refresh" checked> Auto</label>
<button id="clear-btn" style="margin-left:8px">Clear</button>
</div>

<div id="log-list"><div class="empty">Loading...</div></div>
</div>

<script>
var cat="", timer=null;
var cats={xinchao:"cat-xinchao",enhancer:"cat-enhancer",compress:"cat-compress",inject:"cat-inject",cache:"cat-cache",recall:"cat-recall",sync:"cat-sync",system:"cat-system"};
var labels={current_time:"当前时间",semantic_state:"滚动状态",profile:"长期身份核",time_insight:"时间洞察",diary:"日记视角",event_core:"事件核心",evidence:"原文证据",memory_structure:"记忆结构",xinchao:"心潮状态",enhancer:"Enhancer",cache:"Provider缓存",context:"Astr上下文",other_extra:"其他注入"};
var mixKeys=["current_time","semantic_state","profile","time_insight","diary","event_core","evidence","memory_structure","xinchao","enhancer","context","other_extra"];
function esc(s){return String(s==null?"":s).replace(/[&<>"']/g,function(c){if(c==="&")return"&amp;";if(c==="<")return"&lt;";if(c===">")return"&gt;";if(c==='"')return"&quot;";return"&#39;";});}
function n(v){return Number(v||0).toLocaleString("zh-CN")}
function pct(v,t){return t>0?Math.round((Number(v||0)*1000)/t)/10:0}
function pretty(v){try{return JSON.stringify(v,null,2)}catch(e){return String(v)}}
async function fetchJson(url){var r=await fetch(url);var d=await r.json();if(!d.ok)throw new Error(d.error||"request failed");return d.data}
async function fetchAll(){await Promise.all([fetchStats(),fetchLogs()])}
function renderMix(latest,totals){var comp=latest.composition||{},total=Number(latest.total_est_chars||0),label=latest.ts_iso||"waiting";if(latest.outcome)label+=" · "+latest.outcome;if(!total&&totals){comp=totals;total=mixKeys.reduce(function(s,k){return s+Number(comp[k]||0)},0);label=total?"累计样本":"waiting"}document.getElementById("latest-time").textContent=label;document.getElementById("latest-total").textContent=n(total)+" 字";document.getElementById("latest-memory").textContent=n(latest.chars||comp.diary||0)+" 字";document.getElementById("latest-count").textContent=n(latest.count||0);document.getElementById("latest-access").textContent=n(comp.access_supplement||0)+" 字";document.getElementById("latest-mix").innerHTML=total?mixKeys.map(function(k){var v=Number(comp[k]||0),p=pct(v,total);return"<div class='mix-row'><div class='mix-name'>"+labels[k]+"</div><div class='bar'><div class='fill "+k+"' style='width:"+p+"%'></div></div><div class='mix-val'>"+n(v)+" / "+p+"%</div></div>"}).join(""):"<div class='empty'>还没有请求样本。任意一次真实 LLM 对话后这里都会出现。</div>"}
function renderStates(data){var f=data.features||{},rt=data.latest_runtime||{};var defs=[["xinchao","Xinchao",f.xinchao,rt.xinchao],["enhancer","RP enhancer",f.enhancer,rt.enhancer],["access_supplement","ACCESS supplement",f.access_supplement,{}],["llm_runtime","LLM Runtime 2.0",f.llm_runtime,rt.llm_runtime],["provider_cache","Provider cache",f.provider_cache,rt.provider_cache],["system_cache_guard","System guard",f.system_cache_guard,rt.system_cache_guard],["prefix_drift","Prefix drift",f.prefix_drift,rt.prefix_drift],["context","Context trim",f.context,rt.context],["archive","Context archive",f.archive,{}],["profile","Profile",f.profile,{}],["time_insight","Time insight",f.time_insight,{}]];document.getElementById("state-grid").innerHTML=defs.map(function(x){return"<div class='state'><strong>"+esc(x[1])+"<i class='dot "+(x[2]?"on":"off")+"'></i></strong><pre>"+esc(Object.keys(x[3]||{}).length?pretty(x[3]):(x[2]?"已启用，等待运行样本":"未启用"))+"</pre></div>"}).join("")}
async function fetchStats(){try{var data=await fetchJson("/api/console/stats");renderMix(data.latest||{},data.totals||{});renderStates(data);var rows=(data.rows||[]).slice(-10).reverse();var tbl=document.getElementById("inject-table");if(!rows.length){tbl.innerHTML="<div class='empty'>还没有请求样本。任意一次真实 LLM 对话后这里都会出现。</div>"}else{tbl.innerHTML="<table class='inject-table'><thead><tr><th>time</th><th>result</th><th>total</th><th>now</th><th>ctx</th><th>story</th><th>access*</th><th>state</th><th>evidence</th><th>profile</th><th>mind</th><th>enh</th><th>other</th></tr></thead><tbody>"+rows.map(function(x){var c=x.composition||{};return"<tr><td>"+esc(x.ts_iso||"")+"</td><td>"+esc(x.outcome||"legacy")+"</td><td>"+n(x.total_est_chars)+"</td><td>"+n(c.current_time)+"</td><td>"+n(c.context)+"</td><td>"+n(c.diary)+"</td><td>"+n(c.access_supplement)+"</td><td>"+n(c.semantic_state)+"</td><td>"+n(c.evidence)+"</td><td>"+n(c.profile)+"</td><td>"+n(c.xinchao)+"</td><td>"+n(c.enhancer)+"</td><td>"+n(c.other_extra)+"</td></tr>"}).join("")+"</tbody></table><div class='muted' style='padding:8px'>* ACCESS 是历史记忆块中的子集，不重复计入 total。</div>"}}catch(e){document.getElementById("latest-mix").innerHTML="<div class='empty'>Stats API failed: "+esc(e.message)+"</div>";document.getElementById("inject-table").innerHTML="<div class='empty'>无法读取 /api/console/stats："+esc(e.message)+"</div>"}}
async function fetchLogs(){try{var data=await fetchJson(cat?"/api/logs?limit=300&category="+encodeURIComponent(cat):"/api/logs?limit=300");var counts=data.counts||{};document.getElementById("total").textContent=n(data.total||0);["xinchao","inject","recall","cache","enhancer","compress","sync"].forEach(function(k){var el=document.getElementById("cnt-"+k);if(el)el.textContent=n(counts[k]||0)});var logs=data.logs||[],el=document.getElementById("log-list");if(!logs.length){el.innerHTML="<div class='empty'>当前分类没有日志。只有实际触发过对应流程才会出现。</div>";return}el.innerHTML=logs.map(function(l){var detail=l.detail&&Object.keys(l.detail).length?"<span class='log-detail'>"+esc(JSON.stringify(l.detail))+"</span>":"",badge="",d=l.detail||{};if(l.category==="recall"&&d.context_ready&&Number(d.context_query_parts||0)>0){var reason=d.context_used_reason?"原因: "+String(d.context_used_reason):"上下文查询片段: "+Number(d.context_query_parts||0);if(d.context_used===true)badge="<span class='ctx-used yes' title='"+esc(reason)+"'>已用上文</span>";else if(d.context_used===false)badge="<span class='ctx-used no' title='"+esc(reason)+"'>未用上文</span>";else badge="<span class='ctx-used wait' title='"+esc(reason)+"'>判定中</span>"}return"<div class='log-entry'><span class='log-time'>"+esc(l.ts_iso||"")+"</span><span class='log-cat "+(cats[l.category]||"cat-system")+"'>"+esc(l.category||"system")+"</span><span class='log-msg'>"+esc(l.message||"")+badge+detail+"</span></div>"}).join("")}catch(e){document.getElementById("log-list").innerHTML="<div class='empty'>Load failed: "+esc(e.message)+"</div>"}}
function setCat(c,btn){cat=c;document.querySelectorAll(".filter-bar button[data-cat]").forEach(function(b){b.classList.toggle("active",b===btn)});fetchAll()}
async function clearLogs(){await fetch("/api/logs?clear=1");fetchAll()}
function toggleAuto(){if(document.getElementById("auto-refresh").checked){if(!timer)timer=setInterval(fetchAll,3000)}else if(timer){clearInterval(timer);timer=null}}
document.querySelectorAll(".filter-bar button[data-cat]").forEach(function(btn){btn.addEventListener("click",function(){setCat(btn.dataset.cat,btn)})});
document.getElementById("refresh-btn").addEventListener("click",fetchAll);
document.getElementById("clear-btn").addEventListener("click",clearLogs);
document.getElementById("auto-refresh").addEventListener("change",toggleAuto);
toggleAuto();fetchAll();
</script>
</body>
</html>"""
