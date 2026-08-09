"""Traceability (4.6.0-test2 / 8.3.D).

Splits "can we trace back to the original conversation?" into independent
questions and maps them to the red/yellow/green light the WebUI shows:

- does the raw turn text still exist?           (source_turns row count)
- does the episode still link to its batch?     (episodes_with_source_batch)
- does it have exact per-turn links?             (exact_turn_link_count)
- is the semantic vector index usable?           (source_vector_ready)
- is the lexical (term) index usable?            (source_lexical_ready)
- what is the evidence-quality distribution?     (source_grounded / mixed_user_edited / diary_derived)

This module never writes; it only reads counts from the Repositories and the
store stats. The WebUI renders `Traceability.status` as the colored light.
"""
from __future__ import annotations

from typing import Any, Literal

from .episode_repo import EpisodeRepo
from .source_archive import SourceArchive

LightLevel = Literal["green", "yellow", "red", "gray"]


def compute_traceability(source: SourceArchive, episodes: EpisodeRepo,
                         store_stats: dict[str, Any] | None = None) -> dict[str, Any]:
    src_counts = source.counts()
    ep_counts = episodes.counts()
    gen_info = store_stats or {}
    batches = int(src_counts.get("batches", 0))
    turns = int(src_counts.get("turns", 0))
    turns_with_vectors = int(src_counts.get("turn_vectors", 0)) + int(src_counts.get("chunk_vectors", 0))
    turns_with_terms = int(src_counts.get("turn_terms", 0))
    episodes_total = int(ep_counts.get("episodes", 0))
    episodes_with_source = int(ep_counts.get("batch_linked_episodes", 0))
    recoverable_episodes = int(ep_counts.get("recoverable_episodes", 0))
    exact_links = int(ep_counts.get("exact_turn_links", 0))
    source_grounded = int(ep_counts.get("source_grounded", 0))
    mixed_user_edited = int(ep_counts.get("mixed_user_edited", 0))
    diary_derived = int(ep_counts.get("diary_derived", 0))
    source_vector_ready = bool(src_counts.get("turn_vectors") or src_counts.get("chunk_vectors"))
    source_lexical_ready = bool(src_counts.get("turn_terms", 0) > 0)
    migration_status = str(gen_info.get("migration_status") or "")
    pending_generation = str(gen_info.get("pending_generation") or "")

    if turns == 0 and episodes_total == 0:
        level: LightLevel = "gray"
        label = "无原文档案（尚未积累或库为空）"
    elif turns == 0:
        level = "red"
        label = "原文轮次不存在（真实数据缺失）"
    elif episodes_total > 0 and recoverable_episodes == 0:
        level = "yellow"
        label = "原文库可用，但当前 Episode 没有精确回链"
    elif not source_vector_ready:
        level = "yellow"
        label = "可追溯，语义索引迁移中（词面检索可用）"
    else:
        level = "green"
        label = "原文、精确回链与语义索引可用"

    # Re-scope the "yellow" when a shadow migration is explicitly running.
    if pending_generation and migration_status in ("preparing",):
        # Pending shadow migration trumps a stale green/gray into yellow.
        if level != "red":
            level = "yellow"
            label = "原文可追溯，向量代际迁移中（旧代际继续服务）"
    return {
        "batches": batches,
        "source_turns": turns,
        "turns_with_vectors": turns_with_vectors,
        "turns_with_terms": turns_with_terms,
        "episodes_total": episodes_total,
        "episodes_with_source_batch": episodes_with_source,
        "recoverable_episodes": recoverable_episodes,
        "batch_only_episodes": max(0, episodes_with_source - recoverable_episodes),
        "exact_turn_link_count": exact_links,
        "episodes_source_grounded": source_grounded,
        "episodes_mixed_user_edited": mixed_user_edited,
        "episodes_diary_derived": diary_derived,
        "source_vector_ready": source_vector_ready,
        "source_lexical_ready": source_lexical_ready,
        "active_generation": str(gen_info.get("active_generation") or ""),
        "pending_generation": pending_generation,
        "migration_status": migration_status,
        "quality_dist": {
            "source_grounded": source_grounded,
            "mixed_user_edited": mixed_user_edited,
            "diary_derived": diary_derived,
        },
        "status": {"level": level, "label": label},
    }
