"""Read-only inspection. Never imports Astr, providers or plugin startup code."""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import sqlite3
import time


def readonly(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA query_only=ON")
    return db


def snapshot(source: Path, destination: Path) -> None:
    if source.resolve() == destination.resolve() or destination.exists():
        raise ValueError("snapshot must be a new isolated file")
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Reserve the destination exclusively, then use SQLite online backup so WAL
    # records belong to the same consistent snapshot as the main database.
    with destination.open("xb"):
        pass
    started = time.monotonic()
    def progress(_status, _remaining, _total):
        if time.monotonic() - started > 60:
            raise TimeoutError("online snapshot exceeded 60 seconds")
    try:
        src = readonly(source)
        try:
            dst = sqlite3.connect(destination)
            try:
                src.backup(dst, pages=256, progress=progress, sleep=0.05)
            finally:
                dst.close()
        finally:
            src.close()
    except BaseException:
        destination.unlink(missing_ok=True)
        raise


def inspect_database(path: Path) -> dict:
    db = readonly(path)
    try:
        db.execute("BEGIN")
        schema = {
            row["name"]: {col["name"] for col in db.execute(
                'PRAGMA table_info("' + row["name"].replace('"', '""') + '")'
            )}
            for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name IN ('episodes','source_batches','source_turns')"
            )
        }
        required = {
            "episodes": {"episode_id", "memo_name"},
            "source_batches": {"batch_id", "message_count"},
            "source_turns": {"batch_id", "turn_index", "content", "content_hash"},
        }
        missing = {name: sorted(columns - schema.get(name, set()))
                   for name, columns in required.items() if columns - schema.get(name, set())}
        report = {"contract_version": "6.1-test0-v1", "mode": "read_only",
                  "missing_schema": missing, "episodes": [], "batches": [],
                  "summary": {}, "limits": [
                      "Archive completeness is relative to its stored manifest, not all historical chat.",
                      "No Memos comparison, model evaluation or migration is performed.",
                      "Content and credentials are excluded from this report.",
                  ]}
        if "episodes" not in schema or missing.get("episodes"):
            report["status"] = "unsupported_schema"
            return report
        batches = {}
        if not missing.get("source_batches") and not missing.get("source_turns"):
            for raw in db.execute("SELECT * FROM source_batches ORDER BY batch_id"):
                row = dict(raw)
                turns = {}
                hash_errors = 0
                duplicates = 0
                for turn in db.execute(
                    "SELECT turn_index,content,content_hash FROM source_turns WHERE batch_id=? ORDER BY turn_index",
                    (row["batch_id"],),
                ):
                    index = turn["turn_index"]
                    if index in turns:
                        duplicates += 1
                    valid = (isinstance(turn["content"], str) and
                             hashlib.sha256(turn["content"].encode()).hexdigest() == turn["content_hash"])
                    hash_errors += not valid
                    turns[index] = valid
                expected = row["message_count"]
                contiguous = (type(expected) is int and expected > 0 and len(turns) == expected
                              and all(type(i) is int and 0 <= i < expected for i in turns))
                complete = contiguous and not hash_errors and not duplicates
                item = {"batch_id": row["batch_id"], "expected_messages": expected,
                        "actual_messages": len(turns), "hash_errors": hash_errors,
                        "complete": bool(complete), "status": row.get("status", "unknown"),
                        "first_event_ts": row.get("first_event_ts", 0),
                        "last_event_ts": row.get("last_event_ts", 0)}
                report["batches"].append(item)
                batches[row["batch_id"]] = (item, turns)
        counts = Counter()
        for raw in db.execute("SELECT * FROM episodes ORDER BY episode_id"):
            row = dict(raw)
            if row.get("active", 1) != 1:
                counts["inactive"] += 1
                continue
            batch_id = row.get("source_batch_id") or ""
            source = batches.get(batch_id)
            reasons = []
            classification = "diary_only"
            action = "derive_metadata_preserve_provenance"
            if batch_id and not source:
                classification, action = "broken_link", "repair_mapping_before_upgrade"
                reasons.append("referenced_batch_unavailable")
            elif source:
                batch, turns = source
                start, end = row.get("scene_start_turn"), row.get("scene_end_turn")
                range_valid = (type(start) is int and type(end) is int
                               and 0 <= start <= end < batch["actual_messages"]
                               and all(turns.get(i, False) for i in range(start, end + 1)))
                if batch["complete"] and range_valid:
                    classification, action = "source_complete", "eligible_for_versioned_rewrite_preview"
                else:
                    classification, action = "source_partial", "upgrade_verified_subset_only"
                    if not batch["complete"]:
                        reasons.append("batch_manifest_or_hash_incomplete")
                    if not range_valid:
                        reasons.append("episode_range_not_verified")
            counts[classification] += 1
            report["episodes"].append({
                "episode_id": row["episode_id"], "memo_name": row["memo_name"],
                "source_batch_id": batch_id, "classification": classification,
                "action": action, "reasons": reasons,
                "stored_evidence_quality": row.get("evidence_quality", "unknown"),
                "evidence_semantics_verified": False,
            })
        report["summary"] = dict(sorted(counts.items()))
        report["status"] = "schema_limited" if missing else "inspected"
        return report
    finally:
        db.close()


def compensation_inventory(path: Path) -> dict:
    db = readonly(path)
    try:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "failed_llm_requests" not in tables:
            return {"status": "unsupported_schema"}
        rows = db.execute("SELECT id,task,label,source_batch_id,status FROM failed_llm_requests ORDER BY id").fetchall()
        groups = Counter(r["source_batch_id"] for r in rows
                         if r["source_batch_id"] and r["status"] not in {"restored", "dismissed"})
        return {"status": "inspected", "counts": dict(Counter(r["status"] for r in rows)),
                "duplicate_pending_batches": {k: v for k, v in groups.items() if v > 1},
                "requests": [dict(r) for r in rows]}
    finally:
        db.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--compensation", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--snapshot-dir", type=Path)
    args = parser.parse_args()
    source = args.database
    if args.output.exists() or args.output.resolve() in {
        source.resolve(), args.compensation.resolve() if args.compensation else source.resolve()
    }:
        parser.error("output must be a new file, never a source file")
    if args.snapshot_dir:
        target = args.snapshot_dir / "episodic_memory.db"
        snapshot(source, target)
        source = target
    result = inspect_database(source)
    if args.compensation:
        comp = args.compensation
        if args.snapshot_dir:
            target = args.snapshot_dir / "llm_compensation.db"
            snapshot(comp, target)
            comp = target
        result["compensation"] = compensation_inventory(comp)
        result["limits"].append("The two database snapshots are independent, not a cross-database transaction.")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as out:
        json.dump(result, out, ensure_ascii=False, indent=2)
    print(json.dumps({"status": result["status"], "summary": result["summary"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
