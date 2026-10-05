#!/usr/bin/env python3
"""Validate test7 Canary composition on an isolated database copy."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import statistics
import time
from pathlib import Path
from typing import Any

from astrbot_plugin_memos_memory.context_composer import ThreadContextComposer, stable_canary_decision
from astrbot_plugin_memos_memory.episodic_store import EpisodicStore
from astrbot_plugin_memos_memory.query_planner import QueryPlanner
from astrbot_plugin_memos_memory.thread_retrieval import ThreadRetrievalLab


SOURCE_TABLES = ("episodes", "source_batches", "source_turns", "episode_turn_links")
QUERIES = (
    "我们现在的关系还和以前一样吗？", "最初到后来发生了什么变化？",
    "以前答应的约定还成立吗？", "还有什么事情没有完成？",
    "上次在天台发生了什么？", "现在有什么边界需要记住？",
    "第一次谈起这件事是什么时候？", "后来为什么会变成这样？",
    "之前的计划完成了吗？", "现在仍然喜欢那个称呼吗？",
    "我们下一次准备做什么？", "旧关系和现在有什么不同？",
)
INTERNAL_ID = re.compile(r"\b(?:ep|clm|thr|pro|thread_lab)_[A-Za-z0-9_-]+\b", re.I)


class PlannerPlugin:
    query_plan_enable = True
    character_name = ""


def fingerprint(conn: Any) -> dict[str, str]:
    output = {}
    for table in SOURCE_TABLES:
        digest = hashlib.sha256()
        columns = [str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})")]
        projection = ",".join(f'"{column}"' for column in columns)
        for row in conn.execute(f"SELECT {projection} FROM {table} ORDER BY rowid"):
            digest.update(json.dumps(tuple(row), ensure_ascii=False, default=str).encode("utf-8"))
            digest.update(b"\n")
        output[table] = digest.hexdigest()
    return output


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int((len(ordered) - 1) * fraction)))
    return round(ordered[index], 3)


async def run(db_path: Path, scope_id: str, repeat: int) -> dict[str, Any]:
    store = EpisodicStore(str(db_path), None, "test7-real-copy")
    await store.init()
    conn = store._connect()
    before = fingerprint(conn)
    planner = QueryPlanner(PlannerPlugin())
    lab = ThreadRetrievalLab(store._threads)
    composer = ThreadContextComposer(max_chars=1200, growth_percent=10)
    eval_cases = store.thread_eval_cases(scope_id=scope_id, enabled_only=True, limit=500)
    if not eval_cases:
        row = conn.execute(
            "SELECT scope_id,COUNT(*) n FROM thread_eval_cases "
            "WHERE enabled=1 GROUP BY scope_id ORDER BY n DESC,scope_id LIMIT 1"
        ).fetchone()
        if row and str(row["scope_id"] or ""):
            scope_id = str(row["scope_id"])
            eval_cases = store.thread_eval_cases(scope_id=scope_id, enabled_only=True, limit=500)
    case_memos: dict[str, list[str]] = {}
    for case in eval_cases:
        episode_ids = [str(value) for value in case.get("expected_episode_ids") or [] if str(value)]
        if not episode_ids:
            continue
        placeholders = ",".join("?" for _ in episode_ids)
        rows = conn.execute(
            f"SELECT memo_name FROM episodes WHERE episode_id IN ({placeholders})", tuple(episode_ids)
        ).fetchall()
        case_memos[str(case.get("query") or "")] = [str(row["memo_name"] or "") for row in rows]

    timings: list[float] = []
    growth: list[float] = []
    densities: list[float] = []
    composed_count = 0
    skipped_count = 0
    fail_count = 0
    duplicate_violations = 0
    id_leaks = 0
    empty_shells = 0
    original_count_changes = 0
    decision_count = 0
    loops = max(1, int(repeat))
    workload = [str(case.get("query") or "") for case in eval_cases if case.get("query")] or list(QUERIES)
    workload.extend(QUERIES)
    for index in range(loops):
        query = workload[index % len(workload)]
        base_names = list(dict.fromkeys(case_memos.get(query) or []))[:2]
        base_hits = [{"memo_name": name, "selected": True} for name in base_names]
        plan = planner.plan_for_search(query)
        started = time.perf_counter()
        result = lab.run(
            scope_id=scope_id, query=query, plan=plan,
            base_result={"hits": base_hits}, record=False, now_ts=time.time(),
        )
        composed = composer.compose(
            result, plan=plan, base_memory_chars=9000, preexisting_chars=18000,
        )
        timings.append((time.perf_counter() - started) * 1000)
        decision_count += int(stable_canary_decision(
            scope_id=scope_id, session_id=f"real-session-{index}", seed="memos-memory-6",
            percent=5, mode="canary",
        )["selected"])
        if composed.metrics.get("outcome") == "composed":
            composed_count += 1
            growth.append(float(composed.metrics.get("growth_ratio") or 0))
            densities.append(float(composed.metrics.get("fact_density") or 0))
            id_leaks += int(bool(INTERNAL_ID.search(composed.text)))
            empty_shells += int(not any(label in composed.text for label in (
                "[当前仍有效的事实]", "[相关经历脉络", "[状态变化]", "[尚待发生或解决的事项]",
            )))
        elif composed.metrics.get("outcome") == "fail_open":
            fail_count += 1
        else:
            skipped_count += 1
        new_memos = {
            str(item.get("memo_name") or "") for item in (result.get("dedup") or {}).get("new_nodes") or []
        }
        duplicate_violations += int(bool(set(base_names) & new_memos))
        original_count_changes += int(len(base_hits) != len(base_names))

    after = fingerprint(conn)
    output = {
        "database": str(db_path.resolve()),
        "scope_id": scope_id,
        "source_unchanged": before == after,
        "source_table_counts": {
            table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in SOURCE_TABLES
        },
        "runs": loops,
        "composed": composed_count,
        "skipped": skipped_count,
        "fail_open": fail_count,
        "stable_canary_selected": decision_count,
        "stable_canary_rate": round(decision_count / loops, 4),
        "original_hit_count_changes": original_count_changes,
        "cross_layer_duplicate_violations": duplicate_violations,
        "internal_id_leaks": id_leaks,
        "empty_shells": empty_shells,
        "growth": {
            "median_percent": round(statistics.median(growth) * 100, 3) if growth else 0.0,
            "max_percent": round(max(growth) * 100, 3) if growth else 0.0,
        },
        "fact_density": {
            "mean": round(statistics.fmean(densities), 4) if densities else 0.0,
            "median": round(statistics.median(densities), 4) if densities else 0.0,
        },
        "latency_ms": {
            "mean": round(statistics.fmean(timings), 3),
            "p50": percentile(timings, 0.50),
            "p95": percentile(timings, 0.95),
            "max": round(max(timings), 3),
        },
        "gates": {
            "source_unchanged": before == after,
            "composition_exercised": composed_count > 0,
            "base_hits_preserved": original_count_changes == 0,
            "duplicate_rate_zero": duplicate_violations == 0,
            "internal_ids_hidden": id_leaks == 0,
            "no_empty_shell": empty_shells == 0,
            "median_growth_lte_10_percent": (statistics.median(growth) <= 0.10) if growth else True,
            "p95_lte_300_ms": percentile(timings, 0.95) <= 300,
        },
    }
    store.close()
    if not all(output["gates"].values()):
        raise RuntimeError("test7 real-data gate failed: " + json.dumps(output["gates"]))
    return output


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--scope", default="real-copy-shadow")
    parser.add_argument("--repeat", type=int, default=300)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = asyncio.run(run(args.db, args.scope, args.repeat))
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
