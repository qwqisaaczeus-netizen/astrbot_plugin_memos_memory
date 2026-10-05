#!/usr/bin/env python3
"""Build and benchmark the 6.0-test5 Shadow layer on a copied database.

The script intentionally prints aggregate metrics only. It fingerprints all
source-of-truth tables before and after the run and fails if any source row is
changed. Pass a database copy, never the live AstrBot path.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import statistics
import time
from pathlib import Path
from typing import Any

from astrbot_plugin_memos_memory.claim_ledger import ClaimLedger
from astrbot_plugin_memos_memory.episodic_store import EpisodicStore
from astrbot_plugin_memos_memory.prospective_memory import ProspectiveMemory
from astrbot_plugin_memos_memory.query_planner import QueryPlanner
from astrbot_plugin_memos_memory.thread_builder import ThreadBuilder
from astrbot_plugin_memos_memory.thread_migration import ThreadMigration
from astrbot_plugin_memos_memory.thread_projector import ThreadProjector
from astrbot_plugin_memos_memory.thread_retrieval import ThreadRetrievalLab


SOURCE_TABLES = ("episodes", "source_batches", "source_turns", "episode_turn_links")
QUERIES = (
    "我们现在的关系还和以前一样吗？",
    "最初到后来发生了什么变化？",
    "以前答应的约定还成立吗？",
    "还有什么事情没有完成？",
    "上次在天台发生了什么？",
    "现在有什么边界需要记住？",
    "第一次谈起这件事是什么时候？",
    "后来为什么会变成这样？",
    "之前的计划完成了吗？",
    "现在仍然喜欢那个称呼吗？",
    "我们下一次准备做什么？",
    "旧关系和现在有什么不同？",
)


class _PlannerPlugin:
    query_plan_enable = True
    character_name = ""


def _source_fingerprint(conn: Any) -> dict[str, str]:
    result: dict[str, str] = {}
    for table in SOURCE_TABLES:
        digest = hashlib.sha256()
        columns = [str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})")]
        order = ",".join(f'"{column}"' for column in columns)
        for row in conn.execute(f"SELECT {order} FROM {table} ORDER BY rowid"):
            digest.update(json.dumps(tuple(row), ensure_ascii=False, default=str,
                                     separators=(",", ":")).encode("utf-8"))
            digest.update(b"\n")
        result[table] = digest.hexdigest()
    return result


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * fraction))))
    return round(ordered[index], 3)


async def run(db_path: Path, scope_id: str, repeat: int) -> dict[str, Any]:
    if not db_path.is_file():
        raise FileNotFoundError(db_path)
    store = EpisodicStore(str(db_path), None, "real-copy-shadow")
    await store.init()
    conn = store._connect()
    before = _source_fingerprint(conn)
    migration = ThreadMigration(store._connect, store._lock, store._threads)
    scan = migration.run_scan(read_only=False, scope_id=scope_id, enqueue_existing=True)

    builder = ThreadBuilder(store._threads, store._connect, store._lock)
    built = {"processed": 0, "edges_written": 0, "accepted": 0,
             "rejected": 0, "ambiguous": 0, "errors": 0}
    for _ in range(1000):
        batch = builder.process_batch(batch_size=25)
        for key in built:
            built[key] += int(batch.get(key) or 0)
        if int(batch.get("processed") or 0) == 0:
            break
    projected = ThreadProjector(store._threads).project(scope_id=scope_id)
    claims = ClaimLedger(store._threads).rebuild_scope(scope_id)
    prospective = ProspectiveMemory(store._threads).rebuild_scope(scope_id)
    generated = ThreadRetrievalLab(store._threads).generate_eval_cases(scope_id)

    planner = QueryPlanner(_PlannerPlugin())
    lab = ThreadRetrievalLab(store._threads)
    timings: list[float] = []
    route_totals = {"claims": 0, "prospective": 0, "additional": 0, "relation_only": 0}
    dedup_reasons: dict[str, int] = {}
    loops = max(1, int(repeat))
    for index in range(loops):
        query = QUERIES[index % len(QUERIES)]
        plan = planner.plan_for_search(query)
        started = time.perf_counter()
        result = lab.run(scope_id=scope_id, query=query, plan=plan,
                         base_result={"hits": []}, record=index < len(QUERIES))
        timings.append((time.perf_counter() - started) * 1000.0)
        route_totals["claims"] += int(result["claims_count"])
        route_totals["prospective"] += int(result["prospective_count"])
        route_totals["additional"] += int(result["additional_episode_count"])
        route_totals["relation_only"] += int(result["relation_only_count"])
        for item in result["dedup"]["removed"]:
            reason = str(item.get("reason") or "unknown")
            dedup_reasons[reason] = dedup_reasons.get(reason, 0) + 1

    eval_cases = store.thread_eval_cases(scope_id=scope_id, enabled_only=True, limit=500)
    eval_hits: dict[str, list[int]] = {}
    temporal_order = [0, 0]
    duplicate_count = 0
    injected_object_count = 0
    for case in eval_cases:
        case_type = str(case.get("case_type") or "")
        plan = planner.plan_for_search(str(case.get("query") or ""))
        result = lab.run(scope_id=scope_id, query=str(case.get("query") or ""), plan=plan,
                         base_result={"hits": []}, record=False)
        score = lab.score_eval_case(case, {"hits": []}, result)
        eval_hits.setdefault(case_type, [0, 0])
        eval_hits[case_type][1] += 1
        eval_hits[case_type][0] += int(score["passed"])
        duplicate_count += int(score["duplicate_count"])
        injected_object_count += int(score["injected_object_count"])
        if case_type in {"evolution", "temporal_order"}:
            temporal_order[1] += 1
            temporal_order[0] += int(score["order_ok"] and score["all_episodes_hit"])

    after = _source_fingerprint(conn)
    source_unchanged = before == after
    status = store.thread_status()
    grade_counts = store.thread_source_grade_counts()
    claim_status = {
        str(row["status"]): int(row["n"])
        for row in conn.execute("SELECT status,COUNT(*) n FROM memory_claims GROUP BY status")
    }
    claim_types = {
        str(row["claim_type"]): int(row["n"])
        for row in conn.execute("SELECT claim_type,COUNT(*) n FROM memory_claims GROUP BY claim_type")
    }
    prospective_status = {
        str(row["status"]): int(row["n"])
        for row in conn.execute("SELECT status,COUNT(*) n FROM prospective_memory_items GROUP BY status")
    }
    output = {
        "database": str(db_path.resolve()),
        "source_unchanged": source_unchanged,
        "source_table_counts": {
            table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in SOURCE_TABLES
        },
        "migration": scan,
        "source_grades": grade_counts,
        "builder": built,
        "projection": projected,
        "claims": {**claims, "status": claim_status, "types": claim_types},
        "prospective": {**prospective, "status": prospective_status},
        "evaluation_cases": generated,
        "source_holdout_eval": {
            key: {"hits": value[0], "cases": value[1],
                  "recall": round(value[0] / max(1, value[1]), 4)}
            for key, value in eval_hits.items()
        },
        "strict_shadow_metrics": {
            "temporal_order_accuracy": round(temporal_order[0] / max(1, temporal_order[1]), 4),
            "temporal_cases": temporal_order[1],
            "cross_layer_duplicate_rate": round(
                duplicate_count / max(1, injected_object_count), 6
            ),
            "baseline_note": "offline validation has no live embedding Provider; 5.1 baseline is tested through the WebUI integration path",
        },
        "shadow_queries": {
            "runs": loops,
            "latency_ms": {
                "mean": round(statistics.fmean(timings), 3),
                "p50": _percentile(timings, 0.50),
                "p95": _percentile(timings, 0.95),
                "max": round(max(timings), 3),
            },
            "route_totals": route_totals,
            "dedup_reasons": dedup_reasons,
        },
        "thread_status": status,
    }
    store.close()
    if not source_unchanged:
        raise RuntimeError("source-of-truth tables changed during Shadow validation")
    return output


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True, type=Path, help="copied episodic database")
    parser.add_argument("--scope", default="real-copy-shadow")
    parser.add_argument("--repeat", type=int, default=120)
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
