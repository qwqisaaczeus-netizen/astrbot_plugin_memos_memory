"""Run from the parent of the plugin package. All writes go to a new output dir."""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import struct
import threading
import time
from collections import Counter
from pathlib import Path

from astrbot_plugin_memos_memory.calibration import calibrate, split_cases
from astrbot_plugin_memos_memory.claim_ledger import ClaimLedger
from astrbot_plugin_memos_memory.consistency_guard import evaluate as evaluate_consistency
from astrbot_plugin_memos_memory.prospective_memory import ProspectiveMemory
from astrbot_plugin_memos_memory.thread_arbiter import ThreadArbiter
from astrbot_plugin_memos_memory.thread_builder import ThreadBuilder
from astrbot_plugin_memos_memory.thread_candidates import ThreadCandidates
from astrbot_plugin_memos_memory.thread_migration import ThreadMigration
from astrbot_plugin_memos_memory.thread_retrieval import ThreadRetrievalLab
from astrbot_plugin_memos_memory.thread_store import ThreadStore


def digest(path):
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def run(source: Path, output: Path):
    source = source.resolve()
    output = output.resolve()
    if output.exists():
        raise ValueError("output must be a new directory")
    if source.parent == output or source.parent in output.parents:
        raise ValueError("output must be outside source directory")
    files = [p for p in (source, Path(str(source) + "-wal"), Path(str(source) + "-shm")) if p.exists()]
    before = {p.name: digest(p) for p in files}
    output.mkdir(parents=True)
    raw = output / "source_copy"
    raw.mkdir()
    # SQLite's online backup API captures DB and WAL as one consistent image.
    # The source is opened query-only; every derived table is created later in
    # evaluation.db, never in the supplied database.
    source_uri = source.as_uri() + "?mode=ro"
    snapshot_path = raw / "source.snapshot.db"
    with sqlite3.connect(source_uri, uri=True) as source_conn:
        source_conn.execute("PRAGMA query_only=ON")
        with sqlite3.connect(snapshot_path) as snapshot_conn:
            source_conn.backup(snapshot_conn)
    after_snapshot = {p.name: digest(p) for p in files}
    if before != after_snapshot:
        raise RuntimeError("source changed during online snapshot; retry when the export is stable")
    with sqlite3.connect(snapshot_path) as copied:
        with sqlite3.connect(output / "evaluation.db") as target:
            copied.backup(target)
    conn = sqlite3.connect(output / "evaluation.db")
    conn.row_factory = sqlite3.Row
    thread_store = ThreadStore(lambda: conn, threading.RLock())
    thread_store.init_schema(conn)
    candidate_index = ThreadCandidates(lambda: conn)
    projection_stats = candidate_index._refresh_term_index(conn)
    rows = [dict(r) for r in conn.execute("SELECT * FROM episodes WHERE active=1 ORDER BY episode_id")]
    episode_contract_hash = hashlib.sha256(json.dumps([
        {key: row.get(key) for key in (
            "episode_id", "memo_name", "occurred_at", "event_ts", "time_basis",
            "memory_type", "scene_anchor", "retrieval_key", "card_text", "evidence_quality",
        )} for row in rows
    ], sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")).hexdigest()
    links = {}
    for r in conn.execute("SELECT episode_id,batch_id,turn_index FROM episode_turn_links"):
        links.setdefault(r["episode_id"], []).append((r["batch_id"], r["turn_index"]))
    for row in rows:
        row["_turn_refs"] = links.get(row["episode_id"], [])
        blob = row.get("embedding")
        row["_embedding"] = list(struct.unpack(f"<{len(blob)//4}f", blob)) if blob and len(blob) % 4 == 0 else None
    arbiter = ThreadArbiter()
    cases, timings = [], []
    hard_cases: Counter = Counter()
    grouped = split_cases([
        {"groups": ["memo:" + str(ep["memo_name"])] +
         (["batch:" + str(ep["source_batch_id"])] if ep.get("source_batch_id") else []),
         "episode_id": ep["episode_id"]} for ep in rows
    ])
    partitions = {item["episode_id"]: key for key, items in grouped.items() for item in items}
    rows_by_id = {str(row["episode_id"]): row for row in rows}
    # Use the production candidate routes and bound, then reproduce builder gap
    # semantics. This is not an all-pairs scan.
    for left in rows:
        prepared = []
        for pair in candidate_index.candidates_for(str(left["episode_id"]), ""):
            other_id = (str(pair["episode_id_b"]) if str(pair["episode_id_a"]) == str(left["episode_id"])
                        else str(pair["episode_id_a"]))
            right = rows_by_id.get(other_id)
            if right is None:
                continue
            if partitions[left["episode_id"]] != partitions[right["episode_id"]]:
                continue
            started = time.perf_counter()
            result = arbiter._evidence.compute(left, right)
            relation, _ = arbiter._infer_relation(left, right, result)
            timings.append((time.perf_counter() - started) * 1000)
            prepared.append((float(result.fused), right, result, relation, pair))
        prepared.sort(key=lambda item: (-item[0], -float(item[4].get("blocking_score", 0))))
        for index, (score, right, result, relation, pair) in enumerate(prepared):
            next_score = prepared[index + 1][0] if index + 1 < len(prepared) else 0.0
            gap = max(0.0, score - next_score) if index == 0 else 0.0
            shared = set(left["_turn_refs"]) & set(right["_turn_refs"])
            same_text = left["card_text"] == right["card_text"]
            safe_negative = bool(
                result.hard_reject or (
                    not shared and result.a < .15 and result.b < .20
                    and result.d < .10 and result.e < .10 and result.c < .20
                )
            )
            expected = "accepted" if shared and same_text else "rejected" if safe_negative else "unknown"
            label_kind = "weak_exact_positive" if expected == "accepted" else (
                "weak_safe_negative" if expected == "rejected" else "weak_unknown"
            )
            if shared:
                hard_cases["same_source_turn_multi_carrier"] += 1
            if result.b >= .70 and result.c <= .20:
                hard_cases["semantic_similar_different_time"] += 1
            if result.a >= .40 and result.c <= .20:
                hard_cases["same_entity_cross_time"] += 1
            if result.d >= .25:
                hard_cases["state_change_overlap"] += 1
            if result.details.get("date_conflict"):
                hard_cases["date_conflict"] += 1
            groups = ["memo:" + str(ep["memo_name"]) for ep in (left, right)]
            groups.extend("batch:" + str(ep["source_batch_id"]) for ep in (left, right) if ep.get("source_batch_id"))
            cases.append({"groups": groups, "partition": partitions[left["episode_id"]],
                          "label_kind": label_kind, "expected": expected,
                          "features": {k: getattr(result, k) for k in "abcde"},
                          "relation": relation, "gap": gap, "hard_reject": result.hard_reject,
                          "date_conflict": result.details.get("date_conflict"),
                          "blocking_reasons": list(pair.get("blocking_reasons") or []),
                          "source_grade": [left["evidence_quality"], right["evidence_quality"]]})

    # Exercise the 5.1 -> 6.0 derived-data handoff on the evaluation copy.
    # No provider or Memos call is involved.
    scope_id = "test9-calibration"
    migration = ThreadMigration(lambda: conn, threading.RLock(), thread_store)
    migration_result = migration.run_scan(
        read_only=False, scope_id=scope_id, enqueue_existing=True, batch_size=100,
    )
    builder = ThreadBuilder(thread_store, lambda: conn, threading.RLock())
    builder_totals: Counter = Counter()
    for _ in range(max(2, (len(rows) // 25) + 3)):
        result = builder.process_batch(batch_size=25)
        for key, value in result.items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                builder_totals[key] += value
        if not int(result.get("processed") or 0):
            break
    claim_result = ClaimLedger(thread_store).rebuild_scope(scope_id)
    prospective_result = ProspectiveMemory(thread_store).rebuild_scope(scope_id)
    migration_second = migration.run_scan(
        read_only=False, scope_id=scope_id, enqueue_existing=False, batch_size=100,
    )
    claim_second = ClaimLedger(thread_store).rebuild_scope(scope_id)
    prospective_engine = ProspectiveMemory(thread_store)
    prospective_items = thread_store.list_prospective(scope_id=scope_id, limit=1000)
    eligible_items = [item for item in prospective_items if str(item.get("status")) in {"pending", "due"}]
    positive_checked = positive_selected = exact_selected = 0
    for item in eligible_items[:40]:
        description = str(item.get("description") or "")
        if not description:
            continue
        positive_checked += 1
        probe = prospective_engine.trigger(scope_id, description, record=False)
        selected = probe.get("selected") or {}
        positive_selected += int(bool(selected))
        exact_selected += int(str(selected.get("item_id") or "") == str(item.get("item_id") or ""))
    negative_probe = prospective_engine.trigger(scope_id, "你好，今天只是普通问候", record=False)
    consistency_positive = evaluate_consistency({
        "query": "那件事是什么时候？",
        "answer": "2025年3月12日我们去天台看星星。",
        "references": [{"id": "probe", "text": "我们去天台看星星",
                        "occurred_at": "2024年3月12日", "status": "historical"}],
        "thread_text": "2024年3月12日 | 我们去天台看星星",
    })
    consistency_hidden = evaluate_consistency({
        "query": "那件事是什么时候？",
        "answer": "2025年3月12日我们去天台看星星。",
        "references": [{"id": "probe", "text": "我们去天台看星星",
                        "occurred_at": "2024年3月12日", "status": "historical"}],
        "thread_text": "",
    })
    retrieval_lab = ThreadRetrievalLab(thread_store)
    first_episode = rows[0] if rows else {}
    dedup_probe = retrieval_lab._deduplicate(
        [{"episode_id": first_episode.get("episode_id"), "memo_name": first_episode.get("memo_name")}],
        {"nodes": [dict(first_episode)] if first_episode else []},
    )
    episode_rows_after = [dict(r) for r in conn.execute("SELECT * FROM episodes WHERE active=1 ORDER BY episode_id")]
    episode_contract_hash_after = hashlib.sha256(json.dumps([
        {key: row.get(key) for key in (
            "episode_id", "memo_name", "occurred_at", "event_ts", "time_basis",
            "memory_type", "scene_anchor", "retrieval_key", "card_text", "evidence_quality",
        )} for row in episode_rows_after
    ], sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")).hexdigest()
    derived_pipeline = {
        "migration_first": migration_result,
        "builder": dict(builder_totals),
        "claims_first": claim_result,
        "prospective_first": prospective_result,
        "migration_second": migration_second,
        "claims_second": claim_second,
        "idempotent_claim_count": int(claim_result.get("written") or 0) == int(claim_second.get("written") or 0),
        "episode_contract_unchanged": episode_contract_hash == episode_contract_hash_after,
        "module_probes": {
            "prospective_positive_checked": positive_checked,
            "prospective_any_selected": positive_selected,
            "prospective_exact_selected": exact_selected,
            "prospective_generic_greeting_selected": bool(negative_probe.get("selected")),
            "consistency_visible_date_conflict": any(
                item.get("error_type") == "date_conflict" for item in consistency_positive.get("findings", [])
            ),
            "consistency_hidden_reference_findings": len(consistency_hidden.get("findings", [])),
            "dedup_same_episode_removed": int(dedup_probe.get("duplicate_count") or 0),
        },
        "provider_calls": 0,
    }
    report = calibrate(cases)
    after = {p.name: digest(p) for p in files}
    def table_count(name: str) -> int:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        return int(conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]) if exists else 0

    coverage = {
        "thread_link_pairs": len(cases),
        "claims": table_count("memory_claims"),
        "claim_transitions": table_count("memory_claim_transitions"),
        "prospective_observations": table_count("prospective_trigger_observations"),
        "recall_observations": table_count("recall_observations"),
        "access_observations": table_count("memory_access_observations"),
        "thread_query_observations": table_count("thread_query_observations"),
        "consistency_observations": table_count("thread_consistency_observations"),
    }
    report.update({"source_hashes": before, "source_unchanged": before == after,
                   "snapshot_hash": digest(snapshot_path),
                   "episodes": len(rows), "comparisons": len(cases),
                   "source_grades": dict(Counter(r["evidence_quality"] for r in rows)),
                   "exact_source_episodes": sum(bool(r["_turn_refs"]) for r in rows),
                   "projection": projection_stats,
                   "hard_case_inventory": dict(hard_cases),
                   "derived_pipeline": derived_pipeline,
                   "module_coverage": coverage,
                   "pair_compute_ms": {"p50": sorted(timings)[len(timings)//2] if timings else 0,
                                       "p95": sorted(timings)[int(len(timings)*.95)] if timings else 0},
                   "limitations": ["weak labels are not independent ground truth",
                                    "connected provenance may leave validation/test empty",
                                    "candidate and pair latency is not end-to-end retrieval latency",
                                    "1k/5k/10k/50k scale gate requires separate benchmark"]})
    conn.close()
    (output / "cases.private.json").write_text(json.dumps(cases, ensure_ascii=False), encoding="utf-8")
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: report[k] for k in ("episodes", "comparisons", "source_unchanged", "split_counts", "gates")}, ensure_ascii=False))
    if before != after:
        raise RuntimeError("source changed during evaluation")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    run(args.source, args.output)
