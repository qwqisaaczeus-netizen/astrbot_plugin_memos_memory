"""Bounded candidate-stage benchmark on synthetic copies of real distributions."""
import argparse
import json
import sqlite3
import statistics
import threading
import time
from pathlib import Path

from astrbot_plugin_memos_memory.thread_candidates import ThreadCandidates
from astrbot_plugin_memos_memory.thread_store import ThreadStore


def run(source, output, sizes=(1000, 5000, 10000, 50000)):
    if output.exists():
        raise ValueError("output must be new")
    output.mkdir(parents=True)
    source_conn = sqlite3.connect(f"{source.resolve().as_uri()}?mode=ro", uri=True)
    source_conn.row_factory = sqlite3.Row
    originals = [dict(r) for r in source_conn.execute("SELECT * FROM episodes WHERE active=1")]
    source_conn.close()
    if not originals:
        raise ValueError("no source episodes")
    results = []
    for size in sizes:
        path = output / f"scale-{size}.db"
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        # Match EpisodicStore's production durability settings. Benchmarking
        # SQLite's default DELETE/FULL mode produced large, irrelevant Windows
        # filesystem variance at 50k rows.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        # Only columns used by the actual candidate route; no fabricated vectors.
        columns = ["episode_id", "memo_name", "source_batch_id", "active", "event_ts",
                   "updated_ts", "occurred_at", "time_basis", "memory_type", "scene_anchor",
                   "retrieval_key", "entities_json", "state_change", "long_effect",
                   "evidence_quality", "unresolved_json", "card_text"]
        numeric = {"active", "event_ts", "updated_ts"}
        conn.execute("CREATE TABLE episodes (" + ",".join(
            c + (" REAL" if c in numeric else " TEXT") + (" PRIMARY KEY" if c == "episode_id" else "")
            for c in columns) + ")")
        conn.execute("CREATE INDEX idx_episode_active ON episodes(active, event_ts DESC)")
        conn.execute("CREATE INDEX idx_episode_source_batch ON episodes(source_batch_id, active)")
        rows = []
        for index in range(size):
            row = dict(originals[index % len(originals)])
            cycle = index // len(originals)
            row.update(episode_id=f"synthetic-{index}", memo_name=f"synthetic/{index}", active=1)
            row["source_batch_id"] = f"{cycle}:{row.get('source_batch_id')}"
            row["event_ts"] = float(row.get("event_ts") or 0) + cycle * 86400
            rows.append(tuple(row.get(c) for c in columns))
        fixture_started = time.perf_counter()
        conn.executemany("INSERT INTO episodes VALUES(" + ",".join("?" for _ in columns) + ")", rows)
        conn.commit()
        fixture_ms = (time.perf_counter() - fixture_started) * 1000
        # Simulate a real 5.x -> 6.0 upgrade: Episodes already exist before the
        # derived projection schema and queue are installed.
        started = time.perf_counter()
        store = ThreadStore(lambda: conn, threading.RLock())
        store.init_schema(conn)
        candidates = ThreadCandidates(lambda: conn)
        candidates._refresh_term_index(conn)
        build_ms = (time.perf_counter() - started) * 1000
        refresh_timings = []
        for _ in range(50):
            started = time.perf_counter()
            refresh = candidates._refresh_term_index(conn)
            refresh_timings.append((time.perf_counter() - started) * 1000)
            assert refresh["queued"] == 0
        timings, counts = [], []
        for i in range(24):
            started = time.perf_counter()
            found = candidates.candidates_for(f"synthetic-{i * (size // 24)}", "")
            timings.append((time.perf_counter() - started) * 1000)
            counts.append(len(found))
            assert len(found) <= 50
        conn.close()
        result = {"episodes": size, "fixture_insert_ms": fixture_ms,
                  "build_and_index_ms": build_ms,
                  "candidate_p50_ms": statistics.median(timings),
                  "candidate_p95_ms": sorted(timings)[22], "queries": len(timings),
                  "candidate_count_p50": statistics.median(counts),
                  "candidate_count_p95": sorted(counts)[22],
                  "candidate_max": max(counts),
                  "empty_refresh_p95_ms": sorted(refresh_timings)[47],
                  "db_bytes": path.stat().st_size,
                  "llm_calls": 0, "synthetic": True}
        results.append(result)
        print(json.dumps(result), flush=True)
    (output / "report.json").write_text(json.dumps({"results": results,
        "limits": "candidate stage only; not full rebuild, retrieval, LLM or WebUI latency"}, indent=2), encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--sizes", default="1000,5000,10000,50000")
    args = parser.parse_args()
    sizes = tuple(int(value.strip()) for value in args.sizes.split(",") if value.strip())
    if not sizes or any(value <= 0 or value > 100000 for value in sizes):
        raise SystemExit("sizes must contain integers from 1 to 100000")
    run(args.source, args.output, sizes)
