"""Local candidate/evidence builder for the 6.0.0-test5 Shadow pipeline."""
from __future__ import annotations

import json
import logging
import struct
import sqlite3
import time
import threading
from typing import Any, Callable

from .thread_store import ThreadStore
from .thread_candidates import ThreadCandidates
from .thread_evidence import ThreadEvidence
from .thread_arbiter import ThreadArbiter

logger = logging.getLogger(__name__)
BUILDER_VERSION = "test5-v2"


class ThreadBuilder:
    def __init__(self, thread_store: ThreadStore, get_conn: Callable[[], Any],
                 lock: threading.RLock, *, max_retries: int = 3):
        self._ts = thread_store
        self._get_conn = get_conn
        self._lock = lock
        self._max_retries = max(0, int(max_retries))
        self._candidates = ThreadCandidates(get_conn)
        self._evidence = ThreadEvidence()
        self._arbiter = ThreadArbiter()

    def _load_full_episode(self, conn: Any, episode_id: str, scope_id: str) -> dict | None:
        row = conn.execute(
            """SELECT episode_id,memo_name,occurred_at,event_ts,time_basis,memory_type,
                      importance,scene_anchor,retrieval_key,entities_json,evidence_quality,
                      source_batch_id,state_change,long_effect,unresolved_json,card_text,embedding
               FROM episodes WHERE episode_id=? AND active=1""",
            (str(episode_id),),
        ).fetchone()
        if not row:
            return None
        ep = dict(row)
        ep["scope_id"] = str(scope_id or "default")
        blob = ep.get("embedding")
        if blob is not None:
            try:
                ep["_embedding"] = list(struct.unpack(f"{len(blob) // 4}f", blob))
            except (TypeError, ValueError, struct.error):
                ep["_embedding"] = None
        else:
            ep["_embedding"] = None
        turn_rows = conn.execute(
            """SELECT tl.batch_id,tl.turn_index,tl.evidence_index,st.role,st.content,st.event_ts
               FROM episode_turn_links tl
               LEFT JOIN source_turns st ON st.batch_id=tl.batch_id AND st.turn_index=tl.turn_index
               WHERE tl.episode_id=? ORDER BY tl.batch_id,tl.turn_index LIMIT 24""",
            (str(episode_id),),
        ).fetchall()
        ep["_turn_refs"] = [(str(item["batch_id"]), int(item["turn_index"])) for item in turn_rows]
        ep["_source_evidence"] = [
            {
                "batch_id": str(item["batch_id"]),
                "turn_index": int(item["turn_index"]),
                "role": str(item["role"] or ""),
                "content": str(item["content"] or "")[:600],
                "event_ts": float(item["event_ts"] or 0),
            }
            for item in turn_rows if item["content"] is not None
        ]
        evidence_rows = conn.execute(
            """SELECT evidence_index,kind,actor,detail,quote_text,turn_indexes_json,
                      confidence,grounded,evidence_tier
               FROM episode_evidence WHERE episode_id=?
               ORDER BY evidence_index LIMIT 20""",
            (str(episode_id),),
        ).fetchall()
        ep["_episode_evidence"] = [dict(item) for item in evidence_rows]
        return ep

    def process_batch(self, batch_size: int = 10) -> dict[str, Any]:
        if self._ts.is_paused():
            return {"processed": 0, "paused": True}
        try:
            items = self._ts.take_pending_batch(limit=batch_size)
        except sqlite3.OperationalError as exc:
            if "locked" in str(exc).lower() or "busy" in str(exc).lower():
                return {"processed": 0, "busy": True}
            raise
        if not items:
            return {"processed": 0}
        totals = {"processed": 0, "edges_written": 0, "accepted": 0,
                  "rejected": 0, "ambiguous": 0, "errors": 0, "episodes": [], "candidate_counts": []}
        for item in items:
            queue_id = int(item["id"])
            episode_id = str(item["episode_id"])
            scope_id = str(item["scope_id"] or "default")
            t0 = time.perf_counter()
            error_msg = ""
            candidates_found = 0
            local = {"accepted": 0, "rejected": 0, "ambiguous": 0}
            try:
                conn = self._get_conn()
                if not self._ts.renew_claim(queue_id, item["claim_token"]):
                    continue
                with self._lock:
                    ep = self._load_full_episode(conn, episode_id, scope_id)
                if not ep:
                    totals["processed"] += 1
                    self._ts.mark_completed(queue_id, max_retries=self._max_retries, claim_token=item["claim_token"])
                    continue
                with self._lock:
                    pairs = self._candidates.candidates_for(episode_id, scope_id)
                totals["candidate_counts"].append(len(pairs))
                candidates_found = len(pairs)
                prepared: list[tuple[float, dict[str, Any], dict[str, Any]]] = []
                for pair in pairs:
                    other_id = (pair["episode_id_b"] if pair["episode_id_a"] == episode_id
                                else pair["episode_id_a"])
                    with self._lock:
                        other_ep = self._load_full_episode(conn, other_id, scope_id)
                    if not other_ep:
                        continue
                    evidence = self._evidence.compute(ep, other_ep)
                    prepared.append((float(evidence.fused), pair, other_ep))
                prepared.sort(key=lambda item: (-item[0], -float(item[1].get("blocking_score", 0))))
                for index, (score, pair, other_ep) in enumerate(prepared):
                    next_score = prepared[index + 1][0] if index + 1 < len(prepared) else 0.0
                    gap = max(0.0, score - next_score) if index == 0 else 0.0
                    edge = self._arbiter.decide(ep, other_ep, candidate_gap=gap, scope_id=scope_id)
                    if edge is None:
                        continue
                    evidence_payload = json.loads(str(edge.get("evidence_json") or "{}"))
                    evidence_payload["blocking_reasons"] = pair.get("blocking_reasons", [])
                    evidence_payload["blocking_score"] = pair.get("blocking_score", 0)
                    evidence_payload["candidate_gap"] = round(gap, 4)
                    edge["evidence_json"] = json.dumps(evidence_payload, ensure_ascii=False, separators=(",", ":"))
                    if edge["status"] == "rejected" and edge.get("_note") == "score_below_threshold":
                        local["rejected"] += 1
                        continue
                    edge.update(_queue_id=queue_id, _claim_token=item["claim_token"])
                    stored = self._ts.upsert_edge(edge)
                    if not stored.get("locked"):
                        totals["edges_written"] += 1
                    status = str(edge["status"])
                    if status == "accepted":
                        local["accepted"] += 1
                    elif status == "rejected":
                        local["rejected"] += 1
                    else:
                        local["ambiguous"] += 1
                        self._ts.ensure_arbitration_job(int(stored["edge_id"]), scope_id)
                totals["processed"] += 1
                totals["episodes"].append({"episode_id": episode_id, "scope_id": scope_id})
            except Exception as exc:
                error_msg = str(exc)[:300]
                totals["errors"] += 1
                logger.warning("[memos-memory][thread][builder] ep=%s failed open: %s",
                               episode_id, error_msg)
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            for key in ("accepted", "rejected", "ambiguous"):
                totals[key] += local[key]
            self._ts.mark_completed(
                queue_id, elapsed_ms=elapsed_ms, candidates=candidates_found,
                accepted=local["accepted"], rejected=local["rejected"],
                ambiguous=local["ambiguous"], error=error_msg,
                max_retries=self._max_retries, claim_token=item["claim_token"],
            )
        logger.info(
            "[memos-memory][thread][builder] processed=%d edges=%d accepted=%d ambiguous=%d errors=%d",
            totals["processed"], totals["edges_written"], totals["accepted"],
            totals["ambiguous"], totals["errors"],
        )
        return totals
