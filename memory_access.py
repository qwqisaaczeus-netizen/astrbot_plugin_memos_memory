"""Memory accessibility, interference and reconsolidation layer for 5.0.

The store is deliberately derived from episodes. Memos diaries, archived source
turns and semantic state remain the source of truth, so every table here can be
rebuilt without rewriting user-authored memory.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import time
import uuid
from collections import Counter, defaultdict
from datetime import datetime
from typing import Any, Callable

from .store_utils import extract_terms, json_list


ACCESS_STATES = ("vivid", "latent", "deep")
CUE_GRADES = ("A", "B", "C", "D")
EDGE_TYPES = (
    "same_event_restated",
    "same_theme_distinct_event",
    "contradictory_stage",
    "causal_chain",
    "shared_surface",
    "probable_duplicate",
)
ALGORITHM_VERSION = "5.1.0"
# Proxy time source contributes at this confidence fraction to decay calculation.
_SOURCE_PROXY_CONFIDENCE = 0.45
_BLOCK_POSTING_WINDOW = 24
_BLOCK_KEY_LIMIT = 36

_DATE_RE = re.compile(r"(?<!\d)(20\d{2})[-/.年](\d{1,2})(?:[-/.月](\d{1,2}))?")
_QUOTE_RE = re.compile(r"[“\"「『](.{2,36}?)[”\"」』]")
_DURABLE_TYPES = {
    "identity", "persona_trait", "promise", "promise_or_rule", "boundary",
    "relationship", "relationship_shift", "rule",
}
_CONTRADICTION_PAIRS = (
    ({"离开", "分开", "拒绝", "结束", "放弃"}, {"回来", "和好", "接受", "继续", "答应"}),
    ({"不信任", "怀疑", "疏远"}, {"信任", "依赖", "靠近"}),
    ({"害怕", "排斥", "厌恶"}, {"安心", "喜欢", "接纳"}),
)


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, float(value)))


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _loads(value: Any, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        parsed = json.loads(str(value))
        return parsed
    except (TypeError, ValueError, json.JSONDecodeError):
        return fallback


def _event_date(episode: dict[str, Any]) -> str:
    occurred = str(episode.get("occurred_at") or "").strip()
    match = _DATE_RE.search(occurred)
    if match:
        year, month, day = match.groups()
        return f"{int(year):04d}-{int(month):02d}" + (f"-{int(day):02d}" if day else "")
    try:
        ts = float(episode.get("event_ts") or 0)
    except (TypeError, ValueError):
        ts = 0.0
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d") if ts > 0 else ""


def _age_reference(episode: dict[str, Any]) -> tuple[float, str]:
    """Choose a decay clock without inventing an event date.

    `source_updated_ts` is useful for ageing migrated diaries, but it is only a
    maintenance proxy. It must never enter the factual date cue signature.
    """
    for key, kind in (("event_ts", "event"), ("source_updated_ts", "source_updated_proxy")):
        try:
            value = float(episode.get(key) or 0)
        except (TypeError, ValueError):
            value = 0.0
        if value > 0:
            return value, kind
    return 0.0, "unknown"


def _extract_dates(text: str) -> list[str]:
    dates: list[str] = []
    for match in _DATE_RE.finditer(str(text or "")):
        year, month, day = match.groups()
        normalized = f"{int(year):04d}-{int(month):02d}"
        if day:
            normalized += f"-{int(day):02d}"
        dates.append(normalized)
    return list(dict.fromkeys(dates))


def _episode_text(episode: dict[str, Any]) -> str:
    return "\n".join(
        str(episode.get(key) or "").strip()
        for key in (
            "scene_anchor", "retrieval_key", "state_change", "long_effect",
            "trigger_hint", "card_text",
        )
        if str(episode.get(key) or "").strip()
    )


def _cue_signature(episode: dict[str, Any]) -> dict[str, Any]:
    text = _episode_text(episode)
    entities = sorted({str(item).strip() for item in json_list(episode.get("entities")) if str(item).strip()})
    if not entities:
        entities = sorted({str(item).strip() for item in json_list(episode.get("entities_json")) if str(item).strip()})
    dates = sorted(_extract_dates(text))
    primary_date = _event_date(episode)
    if primary_date:
        dates.insert(0, primary_date)
    quotes = [m.group(1).strip() for m in _QUOTE_RE.finditer(text) if m.group(1).strip()]
    retrieval_terms = sorted(extract_terms(" ".join([
        str(episode.get("retrieval_key") or ""),
        str(episode.get("scene_anchor") or ""),
        str(episode.get("trigger_hint") or ""),
    ])))
    content_terms = sorted(extract_terms(text))
    return {
        "dates": list(dict.fromkeys(dates))[:12],
        "entities": entities[:32],
        "quotes": list(dict.fromkeys(quotes))[:12],
        "retrieval_terms": retrieval_terms[:80],
        "content_terms": content_terms[:160],
        "memory_type": str(episode.get("memory_type") or "event"),
    }


def _jaccard(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / max(1, len(left | right))


def _contains_contradiction(left: set[str], right: set[str]) -> bool:
    for negative, positive in _CONTRADICTION_PAIRS:
        if (left & negative and right & positive) or (left & positive and right & negative):
            return True
    return False


class MemoryAccessRepository:
    """Persistent derived index for accessibility-aware memory retrieval."""

    def __init__(self, get_conn: Callable[[], Any], lock: Any):
        self._get_conn = get_conn
        self._lock = lock

    def init_schema(self, conn: Any) -> None:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS memory_access_state (
                memo_name TEXT PRIMARY KEY,
                content_hash TEXT NOT NULL DEFAULT '',
                access_state TEXT NOT NULL DEFAULT 'vivid',
                accessibility REAL NOT NULL DEFAULT 0.5,
                persistence REAL NOT NULL DEFAULT 0.5,
                vividness REAL NOT NULL DEFAULT 0.5,
                distinctiveness REAL NOT NULL DEFAULT 0.5,
                grounding_bonus REAL NOT NULL DEFAULT 0,
                interference_load REAL NOT NULL DEFAULT 0,
                inhibition REAL NOT NULL DEFAULT 0,
                retrieval_count INTEGER NOT NULL DEFAULT 0,
                successful_use_count INTEGER NOT NULL DEFAULT 0,
                reconsolidation_count INTEGER NOT NULL DEFAULT 0,
                last_retrieved_ts REAL NOT NULL DEFAULT 0,
                last_used_ts REAL NOT NULL DEFAULT 0,
                last_reconsolidated_ts REAL NOT NULL DEFAULT 0,
                event_ts REAL NOT NULL DEFAULT 0,
                age_reference_ts REAL NOT NULL DEFAULT 0,
                age_reference_kind TEXT NOT NULL DEFAULT 'unknown',
                age_reference_confidence REAL NOT NULL DEFAULT 1.0,
                decay_eligible INTEGER NOT NULL DEFAULT 1,
                state_reason_json TEXT NOT NULL DEFAULT '{}',
                state_confidence REAL NOT NULL DEFAULT 0.5,
                state_since_ts REAL NOT NULL DEFAULT 0,
                algorithm_version TEXT NOT NULL DEFAULT '',
                created_ts REAL NOT NULL DEFAULT 0,
                updated_ts REAL NOT NULL DEFAULT 0,
                FOREIGN KEY(memo_name) REFERENCES episodes(memo_name) ON DELETE CASCADE
            )"""
        )
        columns = {
            str(row["name"]) for row in conn.execute("PRAGMA table_info(memory_access_state)").fetchall()
        }
        for col, defn in (
            ("event_ts", "REAL NOT NULL DEFAULT 0"),
            ("age_reference_ts", "REAL NOT NULL DEFAULT 0"),
            ("age_reference_kind", "TEXT NOT NULL DEFAULT 'unknown'"),
            ("age_reference_confidence", "REAL NOT NULL DEFAULT 1.0"),
            ("decay_eligible", "INTEGER NOT NULL DEFAULT 1"),
            ("state_reason_json", "TEXT NOT NULL DEFAULT '{}'"),
            ("state_confidence", "REAL NOT NULL DEFAULT 0.5"),
            ("state_since_ts", "REAL NOT NULL DEFAULT 0"),
            ("algorithm_version", "TEXT NOT NULL DEFAULT ''"),
        ):
            if col not in columns:
                conn.execute(f"ALTER TABLE memory_access_state ADD COLUMN {col} {defn}")
        conn.execute(
            """CREATE TABLE IF NOT EXISTS memory_cue_signatures (
                memo_name TEXT PRIMARY KEY,
                signature_json TEXT NOT NULL DEFAULT '{}',
                rarity_score REAL NOT NULL DEFAULT 0.5,
                cue_count INTEGER NOT NULL DEFAULT 0,
                updated_ts REAL NOT NULL DEFAULT 0,
                FOREIGN KEY(memo_name) REFERENCES episodes(memo_name) ON DELETE CASCADE
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS memory_interference_edges (
                source_memo TEXT NOT NULL,
                target_memo TEXT NOT NULL,
                edge_type TEXT NOT NULL,
                strength REAL NOT NULL DEFAULT 0,
                competition REAL NOT NULL DEFAULT 0,
                route_scores_json TEXT NOT NULL DEFAULT '{}',
                distinctions_json TEXT NOT NULL DEFAULT '{}',
                edge_confidence REAL NOT NULL DEFAULT 0.5,
                classifier_version TEXT NOT NULL DEFAULT '',
                evidence_routes_json TEXT NOT NULL DEFAULT '{}',
                conflict_flags_json TEXT NOT NULL DEFAULT '{}',
                updated_ts REAL NOT NULL DEFAULT 0,
                PRIMARY KEY(source_memo,target_memo,edge_type),
                FOREIGN KEY(source_memo) REFERENCES episodes(memo_name) ON DELETE CASCADE,
                FOREIGN KEY(target_memo) REFERENCES episodes(memo_name) ON DELETE CASCADE
            )"""
        )
        edge_cols = {
            str(row["name"]) for row in conn.execute("PRAGMA table_info(memory_interference_edges)").fetchall()
        }
        for col, defn in (
            ("edge_confidence", "REAL NOT NULL DEFAULT 0.5"),
            ("classifier_version", "TEXT NOT NULL DEFAULT ''"),
            ("evidence_routes_json", "TEXT NOT NULL DEFAULT '{}'"),
            ("conflict_flags_json", "TEXT NOT NULL DEFAULT '{}'"),
        ):
            if col not in edge_cols:
                conn.execute(f"ALTER TABLE memory_interference_edges ADD COLUMN {col} {defn}")
        conn.execute(
            """CREATE TABLE IF NOT EXISTS memory_cue_terms (
                memo_name TEXT NOT NULL,
                cue_kind TEXT NOT NULL,
                cue_value TEXT NOT NULL,
                rarity REAL NOT NULL DEFAULT 0.5,
                updated_ts REAL NOT NULL DEFAULT 0,
                PRIMARY KEY(memo_name,cue_kind,cue_value),
                FOREIGN KEY(memo_name) REFERENCES episodes(memo_name) ON DELETE CASCADE
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS memory_interference_groups (
                group_id TEXT PRIMARY KEY,
                group_type TEXT NOT NULL DEFAULT 'interference',
                member_count INTEGER NOT NULL DEFAULT 0,
                max_strength REAL NOT NULL DEFAULT 0,
                updated_ts REAL NOT NULL DEFAULT 0
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS memory_interference_members (
                group_id TEXT NOT NULL,
                memo_name TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'member',
                PRIMARY KEY(group_id,memo_name),
                FOREIGN KEY(group_id) REFERENCES memory_interference_groups(group_id) ON DELETE CASCADE,
                FOREIGN KEY(memo_name) REFERENCES episodes(memo_name) ON DELETE CASCADE
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS memory_access_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                request_id TEXT NOT NULL DEFAULT '',
                memo_name TEXT NOT NULL DEFAULT '',
                event_kind TEXT NOT NULL,
                access_state TEXT NOT NULL DEFAULT '',
                base_score REAL NOT NULL DEFAULT 0,
                access_score REAL NOT NULL DEFAULT 0,
                cue_support REAL NOT NULL DEFAULT 0,
                route_support REAL NOT NULL DEFAULT 0,
                response_support REAL NOT NULL DEFAULT 0,
                selected INTEGER NOT NULL DEFAULT 0,
                shadow INTEGER NOT NULL DEFAULT 1,
                detail_json TEXT NOT NULL DEFAULT '{}',
                created_ts REAL NOT NULL
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS memory_access_maintenance (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                reason TEXT NOT NULL DEFAULT '',
                episodes INTEGER NOT NULL DEFAULT 0,
                states_changed INTEGER NOT NULL DEFAULT 0,
                edges INTEGER NOT NULL DEFAULT 0,
                detail_json TEXT NOT NULL DEFAULT '{}',
                created_ts REAL NOT NULL
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS memory_access_index_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL DEFAULT ''
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS memory_access_eval_cases (
                case_id TEXT PRIMARY KEY,
                memo_name TEXT NOT NULL DEFAULT '',
                query TEXT NOT NULL DEFAULT '',
                expected_grade TEXT NOT NULL DEFAULT 'D',
                case_type TEXT NOT NULL DEFAULT 'general',
                enabled INTEGER NOT NULL DEFAULT 1,
                notes TEXT NOT NULL DEFAULT '',
                created_ts REAL NOT NULL DEFAULT 0,
                updated_ts REAL NOT NULL DEFAULT 0
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS memory_access_eval_runs (
                run_id TEXT PRIMARY KEY,
                algorithm_version TEXT NOT NULL DEFAULT '',
                config_json TEXT NOT NULL DEFAULT '{}',
                gate_results_json TEXT NOT NULL DEFAULT '{}',
                cases_total INTEGER NOT NULL DEFAULT 0,
                cases_passed INTEGER NOT NULL DEFAULT 0,
                started_ts REAL NOT NULL DEFAULT 0,
                finished_ts REAL NOT NULL DEFAULT 0
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS memory_access_eval_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL DEFAULT '',
                case_id TEXT NOT NULL DEFAULT '',
                found INTEGER NOT NULL DEFAULT 0,
                rank INTEGER NOT NULL DEFAULT -1,
                grade_match INTEGER NOT NULL DEFAULT 0,
                rescue_used INTEGER NOT NULL DEFAULT 0,
                notes TEXT NOT NULL DEFAULT '',
                detail_json TEXT NOT NULL DEFAULT '{}',
                created_ts REAL NOT NULL DEFAULT 0
            )"""
        )
        # ── schema 10 additions ──────────────────────────────────────────────
        # eval_results: compare access-layer rank against baseline recall rank
        eval_result_cols = {
            str(row["name"]) for row in conn.execute("PRAGMA table_info(memory_access_eval_results)").fetchall()
        }
        for col, defn in (
            ("baseline_rank", "INTEGER NOT NULL DEFAULT -1"),
            ("access_rank", "INTEGER NOT NULL DEFAULT -1"),
            ("rank_delta", "INTEGER NOT NULL DEFAULT 0"),
            ("used_real_recall", "INTEGER NOT NULL DEFAULT 0"),
        ):
            if col not in eval_result_cols:
                conn.execute(f"ALTER TABLE memory_access_eval_results ADD COLUMN {col} {defn}")
        # eval_cases: human-confirmed expected target
        eval_case_cols = {
            str(row["name"]) for row in conn.execute("PRAGMA table_info(memory_access_eval_cases)").fetchall()
        }
        for col, defn in (
            ("expected_memo", "TEXT NOT NULL DEFAULT ''"),
            ("confirmed_by", "TEXT NOT NULL DEFAULT ''"),
            ("supervision_level", "TEXT NOT NULL DEFAULT 'heuristic'"),
            ("supervision_confidence", "REAL NOT NULL DEFAULT 0"),
            ("supervision_json", "TEXT NOT NULL DEFAULT '{}'"),
            ("negative_memos_json", "TEXT NOT NULL DEFAULT '[]'"),
            ("auto_verified", "INTEGER NOT NULL DEFAULT 0"),
        ):
            if col not in eval_case_cols:
                conn.execute(f"ALTER TABLE memory_access_eval_cases ADD COLUMN {col} {defn}")
        # events: track whether this evaluation led to a takeover append
        event_cols = {
            str(row["name"]) for row in conn.execute("PRAGMA table_info(memory_access_events)").fetchall()
        }
        for col, defn in (
            ("takeover_applied", "INTEGER NOT NULL DEFAULT 0"),
            ("takeover_reason", "TEXT NOT NULL DEFAULT ''"),
        ):
            if col not in event_cols:
                conn.execute(f"ALTER TABLE memory_access_events ADD COLUMN {col} {defn}")
        # takeover log: one row per appended memory
        conn.execute(
            """CREATE TABLE IF NOT EXISTS memory_access_takeover_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                request_id TEXT NOT NULL DEFAULT '',
                memo_name TEXT NOT NULL DEFAULT '',
                cue_grade TEXT NOT NULL DEFAULT '',
                append_rank INTEGER NOT NULL DEFAULT -1,
                reason TEXT NOT NULL DEFAULT '',
                response_used INTEGER NOT NULL DEFAULT -1,
                breaker_trip INTEGER NOT NULL DEFAULT 0,
                evidence_terms_json TEXT NOT NULL DEFAULT '[]',
                use_detail_json TEXT NOT NULL DEFAULT '{}',
                policy_version TEXT NOT NULL DEFAULT '',
                route_mode TEXT NOT NULL DEFAULT '',
                slot TEXT NOT NULL DEFAULT '',
                scores_json TEXT NOT NULL DEFAULT '{}',
                evidence_quality TEXT NOT NULL DEFAULT '',
                source_recoverable INTEGER NOT NULL DEFAULT 0,
                created_ts REAL NOT NULL DEFAULT 0,
                evaluated_ts REAL NOT NULL DEFAULT 0
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS memory_access_observations (
                request_id TEXT PRIMARY KEY,
                query_text TEXT NOT NULL DEFAULT '',
                query_hash TEXT NOT NULL DEFAULT '',
                shadow INTEGER NOT NULL DEFAULT 1,
                query_grade TEXT NOT NULL DEFAULT 'D',
                candidate_count INTEGER NOT NULL DEFAULT 0,
                selected_count INTEGER NOT NULL DEFAULT 0,
                baseline_json TEXT NOT NULL DEFAULT '[]',
                recommended_json TEXT NOT NULL DEFAULT '[]',
                added_json TEXT NOT NULL DEFAULT '[]',
                removed_json TEXT NOT NULL DEFAULT '[]',
                would_change INTEGER NOT NULL DEFAULT 0,
                rescue_pool_count INTEGER NOT NULL DEFAULT 0,
                deep_rescue_count INTEGER NOT NULL DEFAULT 0,
                source_rescue_count INTEGER NOT NULL DEFAULT 0,
                exact_source_count INTEGER NOT NULL DEFAULT 0,
                response_memories INTEGER NOT NULL DEFAULT -1,
                response_used INTEGER NOT NULL DEFAULT -1,
                response_use_rate REAL NOT NULL DEFAULT -1,
                feedback_verdict TEXT NOT NULL DEFAULT '',
                feedback_note TEXT NOT NULL DEFAULT '',
                detail_json TEXT NOT NULL DEFAULT '{}',
                created_ts REAL NOT NULL DEFAULT 0,
                responded_ts REAL NOT NULL DEFAULT 0,
                updated_ts REAL NOT NULL DEFAULT 0
            )"""
        )
        takeover_cols = {
            str(row["name"]) for row in conn.execute(
                "PRAGMA table_info(memory_access_takeover_log)"
            ).fetchall()
        }
        for col, defn in (
            ("evidence_terms_json", "TEXT NOT NULL DEFAULT '[]'"),
            ("use_detail_json", "TEXT NOT NULL DEFAULT '{}'"),
            ("policy_version", "TEXT NOT NULL DEFAULT ''"),
            ("route_mode", "TEXT NOT NULL DEFAULT ''"),
            ("slot", "TEXT NOT NULL DEFAULT ''"),
            ("scores_json", "TEXT NOT NULL DEFAULT '{}'"),
            ("evidence_quality", "TEXT NOT NULL DEFAULT ''"),
            ("source_recoverable", "INTEGER NOT NULL DEFAULT 0"),
        ):
            if col not in takeover_cols:
                conn.execute(f"ALTER TABLE memory_access_takeover_log ADD COLUMN {col} {defn}")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_takeover_log_req ON memory_access_takeover_log(request_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_takeover_log_memo ON memory_access_takeover_log(memo_name,created_ts DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_access_state ON memory_access_state(access_state,accessibility DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_access_events_request ON memory_access_events(request_id,id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_access_events_memo ON memory_access_events(memo_name,created_ts DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_access_observations_created ON memory_access_observations(created_ts DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_access_observations_changed ON memory_access_observations(would_change,created_ts DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_edges_source ON memory_interference_edges(source_memo,strength DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_edges_target ON memory_interference_edges(target_memo,strength DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_cue_terms_lookup ON memory_cue_terms(cue_kind,cue_value,rarity DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_eval_results_run ON memory_access_eval_results(run_id,case_id)")

    @staticmethod
    def _content_hash(episode: dict[str, Any]) -> str:
        payload = "\u241f".join([
            "access-v3", str(episode.get("memo_name") or ""), _event_date(episode),
            str(episode.get("importance") or ""), _episode_text(episode),
        ])
        return hashlib.sha256(payload.encode("utf-8", "ignore")).hexdigest()

    @staticmethod
    def _interference_fingerprint(episodes: list[dict[str, Any]], max_neighbors: int) -> str:
        """Describe graph inputs without depending on volatile database timestamps."""
        digest = hashlib.sha256()
        digest.update(
            f"{ALGORITHM_VERSION}|bounded_rare_cue_v2|{int(max_neighbors)}\n".encode("utf-8")
        )
        for episode in sorted(episodes, key=lambda item: str(item.get("memo_name") or "")):
            signature = episode.get("signature") or _cue_signature(episode)
            payload = {
                "memo_name": str(episode.get("memo_name") or ""),
                "source_batch_id": str(episode.get("source_batch_id") or ""),
                "state_change": str(episode.get("state_change") or ""),
                "long_effect": str(episode.get("long_effect") or ""),
                "signature": signature,
            }
            digest.update(_json(payload).encode("utf-8", "ignore"))
            digest.update(b"\n")
        return digest.hexdigest()

    def _interference_records(self) -> list[dict[str, Any]]:
        rows = self._get_conn().execute(
            """SELECT e.memo_name,e.state_change,e.long_effect,e.source_batch_id,c.signature_json
               FROM episodes e JOIN memory_cue_signatures c ON c.memo_name=e.memo_name
               WHERE e.active=1"""
        ).fetchall()
        records = [dict(row) for row in rows]
        for item in records:
            item["signature"] = _loads(item.pop("signature_json", "{}"), {})
        return records

    def interference_rebuild_needed(self, episodes: list[dict[str, Any]], *,
                                    max_neighbors: int = 12) -> bool:
        rows = self._get_conn().execute(
            "SELECT key,value FROM memory_access_index_meta WHERE key IN "
            "('access_index_version','interference_fingerprint','interference_corpus_size',"
            "'interference_max_neighbors','interference_strategy')"
        ).fetchall()
        meta = {str(row["key"]): str(row["value"]) for row in rows}
        expected = self._interference_fingerprint(episodes, max_neighbors)
        return not (
            meta.get("access_index_version") == ALGORITHM_VERSION
            and meta.get("interference_strategy") == "bounded_rare_cue_v2"
            and int(float(meta.get("interference_corpus_size") or -1)) == len(episodes)
            and int(float(meta.get("interference_max_neighbors") or -1)) == int(max_neighbors)
            and meta.get("interference_fingerprint") == expected
        )

    @staticmethod
    def _base_dimensions(episode: dict[str, Any], signature: dict[str, Any], now: float) -> dict[str, float]:
        try:
            importance = max(1, min(5, int(episode.get("importance") or 3)))
        except (TypeError, ValueError):
            importance = 3
        memory_type = str(episode.get("memory_type") or "event").lower()
        # Legacy imports commonly label almost every diary importance 4/5 and
        # populate long_effect. Keep those memories valid without turning that
        # migration convention into permanent immunity from natural ageing.
        persistence = 0.30 + (importance - 1) * 0.075
        if memory_type in _DURABLE_TYPES:
            persistence += 0.12
        evidence_quality = str(episode.get("evidence_quality") or "diary_derived")
        if str(episode.get("long_effect") or "").strip() and (
            memory_type in _DURABLE_TYPES or evidence_quality != "diary_derived"
        ):
            persistence += 0.025
        if json_list(episode.get("unresolved")) or json_list(episode.get("unresolved_json")):
            persistence += 0.06
        age_reference_ts, age_reference_kind = _age_reference(episode)
        age_days = max(0.0, (now - age_reference_ts) / 86400.0) if age_reference_ts > 0 else 0.0
        if age_reference_kind == "event":
            vividness = 0.18 + 0.60 * math.exp(-age_days / 75.0)
        elif age_reference_kind == "source_updated_proxy":
            # An edit or sync timestamp proves only that the diary source was
            # touched recently. It must not make an old event look freshly lived.
            vividness = min(0.46, 0.18 + 0.60 * math.exp(-age_days / 75.0))
        else:
            # Unknown time is uncertainty, not an invented 365-day age.
            vividness = 0.22
        if signature.get("quotes"):
            vividness += 0.08
        if signature.get("entities"):
            vividness += min(0.08, len(signature["entities"]) * 0.015)
        if age_reference_kind == "source_updated_proxy":
            vividness = min(vividness, 0.52)
        elif age_reference_kind == "unknown":
            vividness = min(vividness, 0.34)
        grounding_bonus = {
            "source_grounded": 0.10,
            "mixed_user_edited": 0.06,
            "diary_derived": 0.0,
        }.get(evidence_quality, 0.0)
        # A legacy diary without raw source remains a valid memory. Grounding is
        # an evidence bonus, never a penalty against old data.
        distinctiveness = 0.38
        distinctiveness += min(0.18, len(signature.get("entities") or []) * 0.025)
        distinctiveness += min(0.12, len(signature.get("dates") or []) * 0.04)
        distinctiveness += min(0.10, len(signature.get("quotes") or []) * 0.025)
        return {
            "persistence": _clamp(persistence),
            "vividness": _clamp(vividness),
            "distinctiveness": _clamp(distinctiveness),
            "grounding_bonus": grounding_bonus,
        }

    @staticmethod
    def _state_for(score: float, previous: str = "", vivid_threshold: float = 0.68,
                   deep_threshold: float = 0.32) -> str:
        # Hysteresis stops memories oscillating at a threshold on each nightly run.
        margin = 0.055
        if previous == "vivid" and score >= vivid_threshold - margin:
            return "vivid"
        if previous == "deep" and score < deep_threshold + margin:
            return "deep"
        if score >= vivid_threshold:
            return "vivid"
        if score < deep_threshold:
            return "deep"
        return "latent"

    def _decay_eligibility(
        self,
        episode: dict[str, Any],
        signature: dict[str, Any],
        state_data: dict[str, Any],
    ) -> tuple[bool, str, list[str], float]:
        """Check whether a memory qualifies for natural time-based decay.

        Returns (eligible, status, reasons, confidence).
        eligible=False means an absolute protection signal prevents this memory
        from being pushed deeper by time alone. ``deferred`` is not permanent
        protection: evidence is currently too weak to justify forgetting, and a
        later grounded source, factual event time or confirmed use can reclassify it.
        """
        memory_type = str(episode.get("memory_type") or "event").lower()

        # ── Absolute protection ────────────────────────────────────────────────
        if memory_type in _DURABLE_TYPES:
            return False, "protected", [f"durable_type:{memory_type}"], 1.0

        if json_list(episode.get("unresolved")) or json_list(episode.get("unresolved_json")):
            return False, "protected", ["unresolved_items"], 1.0

        # Repeated confirmed use ≈ user positive feedback
        recon_count = int(state_data.get("reconsolidation_count") or 0)
        last_used = float(state_data.get("last_used_ts") or 0)
        if recon_count >= 2 and last_used > time.time() - 86400 * 180:
            return False, "protected", ["user_confirmed_use"], 1.0

        # ── Evidence-insufficient hold ────────────────────────────────────────
        # Legacy Memos often have a source edit timestamp but no factual event
        # time or raw-turn linkage. No feedback is not negative feedback. When
        # such a diary carries an emotional anchor or high importance, time alone
        # is not enough evidence to deepen it.
        _, age_reference_kind = _age_reference(episode)
        evidence_quality = str(episode.get("evidence_quality") or "diary_derived")
        memo_name = str(episode.get("memo_name") or "").strip()
        has_source_link = False
        if memo_name:
            row = self._get_conn().execute(
                """SELECT 1 FROM episode_turn_links l
                   JOIN episodes e ON e.episode_id=l.episode_id
                   WHERE e.memo_name=? LIMIT 1""",
                (memo_name,),
            ).fetchone()
            has_source_link = bool(row)
        try:
            importance = max(1, min(5, int(episode.get("importance") or 3)))
        except (TypeError, ValueError):
            importance = 3
        uncertain_clock = age_reference_kind in {"unknown", "source_updated_proxy"}
        meaningful_legacy = memory_type == "emotional_anchor" or importance >= 4
        if (
            uncertain_clock
            and evidence_quality == "diary_derived"
            and not has_source_link
            and meaningful_legacy
            and recon_count <= 0
        ):
            reasons = ["insufficient_forgetting_evidence", f"clock:{age_reference_kind}"]
            if memory_type == "emotional_anchor":
                reasons.append("emotional_anchor_requires_evidence")
            if importance >= 4:
                reasons.append(f"legacy_importance:{importance}")
            return False, "deferred", reasons, 0.30

        # ── Allow decay, record influencing signals ────────────────────────────
        reasons: list[str] = []
        confidence = 0.70  # default moderate confidence

        has_quotes = bool(signature.get("quotes"))
        has_dates = bool(signature.get("dates"))
        if has_quotes:
            reasons.append("has_quote_cues")
            confidence = min(confidence, 0.55)
        if has_dates:
            reasons.append("has_date_cues")
            confidence = min(confidence, 0.60)
        if evidence_quality == "source_grounded":
            reasons.append("source_grounded")
            confidence = min(confidence, 0.50)

        if not reasons:
            # Ordinary event with no protective signals
            reasons.append("plain_event_eligible")

        return True, "eligible", reasons, confidence

    def sync_episode(self, episode: dict[str, Any], *, now: float | None = None,
                     vivid_threshold: float = 0.68, deep_threshold: float = 0.32,
                     source_proxy_weight: float | None = None) -> dict[str, Any]:
        memo_name = str(episode.get("memo_name") or "").strip()
        if not memo_name:
            return {"updated": False, "reason": "missing_memo_name"}
        now = float(now or time.time())
        try:
            event_ts = float(episode.get("event_ts") or 0)
        except (TypeError, ValueError):
            event_ts = 0.0
        age_reference_ts, age_reference_kind = _age_reference(episode)
        _proxy_w = float(source_proxy_weight) if source_proxy_weight is not None else _SOURCE_PROXY_CONFIDENCE
        age_reference_confidence = {
            "event": 1.0,
            "source_updated_proxy": _clamp(_proxy_w, 0.0, 1.0),
        }.get(age_reference_kind, 0.0)
        signature = _cue_signature(episode)
        dimensions = self._base_dimensions(episode, signature, now)
        content_hash = self._content_hash(episode)
        with self._lock:
            conn = self._get_conn()
            old = conn.execute(
                "SELECT * FROM memory_access_state WHERE memo_name=?", (memo_name,)
            ).fetchone()
            old_data = dict(old) if old else {}
            same_algorithm = str(old_data.get("algorithm_version") or "") == ALGORITHM_VERSION
            if old_data.get("content_hash") == content_hash and same_algorithm:
                persistence = float(old_data.get("persistence") or dimensions["persistence"])
                distinctiveness = float(old_data.get("distinctiveness") or dimensions["distinctiveness"])
                created_ts = float(old_data.get("created_ts") or now)
            else:
                persistence = dimensions["persistence"]
                distinctiveness = dimensions["distinctiveness"]
                created_ts = float(old_data.get("created_ts") or now)
            # Rebuilds derive the same base vividness from the same episode and
            # clock. Verified-use recency is reapplied by maintain(); carrying
            # the previous derived value here made repeated rebuilds decay twice.
            vividness = dimensions["vividness"]
            interference = float(old_data.get("interference_load") or 0)
            inhibition = float(old_data.get("inhibition") or 0)
            recon = min(0.14, int(old_data.get("reconsolidation_count") or 0) * 0.012)
            accessibility = _clamp(
                0.38 * persistence + 0.38 * vividness + 0.17 * distinctiveness
                + dimensions["grounding_bonus"] + recon - interference - inhibition
            )
            if old_data:
                # Existing state transitions belong to maintain(), where the
                # protection gates and confirmation counters are enforced.
                # A rebuild must not silently bypass that policy.
                access_state = str(old_data.get("access_state") or "latent")
            else:
                access_state = self._state_for(
                    accessibility, "", vivid_threshold, deep_threshold,
                )
            eligible, eligibility_status, elig_reasons, elig_confidence = self._decay_eligibility(
                episode, signature, old_data,
            )
            prev_reason_data = _loads(old_data.get("state_reason_json"), {})
            state_reason = {
                "protection": elig_reasons,
                "eligible": eligible,
                "eligibility_status": eligibility_status,
                "algorithm": ALGORITHM_VERSION,
            }
            # A pending transition is meaningful only under the algorithm that
            # proposed it. Keep real use/reconsolidation counters across an
            # upgrade, but never let a test4 threshold vote count toward test5.
            if same_algorithm and "pending_state" in prev_reason_data:
                state_reason["pending_state"] = prev_reason_data["pending_state"]
                state_reason["pending_state_since_ts"] = prev_reason_data.get("pending_state_since_ts", now)
                state_reason["pending_confirmations"] = int(
                    prev_reason_data.get("pending_confirmations") or 0
                )
            state_since_ts = float(old_data.get("state_since_ts") or now)
            if access_state != str(old_data.get("access_state") or ""):
                state_since_ts = now
            conn.execute(
                """INSERT INTO memory_access_state(
                    memo_name,content_hash,access_state,accessibility,persistence,vividness,
                    distinctiveness,grounding_bonus,interference_load,inhibition,
                    retrieval_count,successful_use_count,reconsolidation_count,
                    last_retrieved_ts,last_used_ts,last_reconsolidated_ts,event_ts,
                    age_reference_ts,age_reference_kind,age_reference_confidence,
                    decay_eligible,state_reason_json,state_confidence,state_since_ts,
                    algorithm_version,created_ts,updated_ts
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(memo_name) DO UPDATE SET
                    content_hash=excluded.content_hash,access_state=excluded.access_state,
                    accessibility=excluded.accessibility,persistence=excluded.persistence,
                    vividness=excluded.vividness,distinctiveness=excluded.distinctiveness,
                    grounding_bonus=excluded.grounding_bonus,event_ts=excluded.event_ts,
                    age_reference_ts=excluded.age_reference_ts,
                    age_reference_kind=excluded.age_reference_kind,
                    age_reference_confidence=excluded.age_reference_confidence,
                    decay_eligible=excluded.decay_eligible,
                    state_reason_json=excluded.state_reason_json,
                    state_confidence=excluded.state_confidence,
                    state_since_ts=excluded.state_since_ts,
                    algorithm_version=excluded.algorithm_version,
                    updated_ts=excluded.updated_ts""",
                (
                    memo_name, content_hash, access_state, accessibility, persistence,
                    vividness, distinctiveness, dimensions["grounding_bonus"], interference,
                    inhibition, int(old_data.get("retrieval_count") or 0),
                    int(old_data.get("successful_use_count") or 0),
                    int(old_data.get("reconsolidation_count") or 0),
                    float(old_data.get("last_retrieved_ts") or 0),
                    float(old_data.get("last_used_ts") or 0),
                    float(old_data.get("last_reconsolidated_ts") or 0), event_ts,
                    age_reference_ts, age_reference_kind, age_reference_confidence,
                    int(eligible), _json(state_reason), elig_confidence,
                    state_since_ts, ALGORITHM_VERSION, created_ts, now,
                ),
            )
            cue_count = sum(len(signature.get(key) or []) for key in ("dates", "entities", "quotes", "retrieval_terms"))
            conn.execute(
                """INSERT INTO memory_cue_signatures(memo_name,signature_json,rarity_score,cue_count,updated_ts)
                   VALUES(?,?,?,?,?) ON CONFLICT(memo_name) DO UPDATE SET
                   signature_json=excluded.signature_json,cue_count=excluded.cue_count,updated_ts=excluded.updated_ts""",
                (memo_name, _json(signature), 0.5, cue_count, now),
            )
            conn.execute("DELETE FROM memory_cue_terms WHERE memo_name=?", (memo_name,))
            for cue_kind in ("dates", "entities", "quotes", "retrieval_terms"):
                singular = {"dates": "date", "entities": "entity", "quotes": "quote",
                            "retrieval_terms": "term"}[cue_kind]
                for value in dict.fromkeys(str(item).strip().lower() for item in signature.get(cue_kind) or []):
                    if value:
                        conn.execute(
                            "INSERT OR IGNORE INTO memory_cue_terms(memo_name,cue_kind,cue_value,updated_ts) VALUES(?,?,?,?)",
                            (memo_name, singular, value, now),
                        )
            conn.commit()
        return {"updated": True, "memo_name": memo_name, "access_state": access_state,
                "accessibility": accessibility, "decay_eligible": eligible}

    def _refresh_rarity(self) -> int:
        conn = self._get_conn()
        rows = conn.execute("SELECT memo_name,signature_json FROM memory_cue_signatures").fetchall()
        signatures = {str(row["memo_name"]): _loads(row["signature_json"], {}) for row in rows}
        freq: Counter[str] = Counter()
        for signature in signatures.values():
            terms = set(signature.get("retrieval_terms") or []) | set(signature.get("entities") or []) | set(signature.get("dates") or [])
            freq.update(terms)
        total = max(1, len(signatures))
        for memo_name, signature in signatures.items():
            terms = set(signature.get("retrieval_terms") or []) | set(signature.get("entities") or []) | set(signature.get("dates") or [])
            rarity = sum(math.log1p(total / max(1, freq[t])) for t in terms) / max(1, len(terms))
            rarity = _clamp(rarity / max(1.0, math.log1p(total)), 0.18, 1.0)
            conn.execute("UPDATE memory_cue_signatures SET rarity_score=? WHERE memo_name=?", (rarity, memo_name))
            conn.execute(
                "UPDATE memory_access_state SET distinctiveness=MAX(distinctiveness,?) WHERE memo_name=?",
                (0.30 + rarity * 0.55, memo_name),
            )
        cue_freq = {
            (str(row["cue_kind"]), str(row["cue_value"])): int(row["n"])
            for row in conn.execute(
                "SELECT cue_kind,cue_value,COUNT(*) AS n FROM memory_cue_terms GROUP BY cue_kind,cue_value"
            ).fetchall()
        }
        for (kind, value), count in cue_freq.items():
            rarity = _clamp(math.log1p(total / max(1, count)) / max(1.0, math.log1p(total)), 0.12, 1.0)
            conn.execute(
                "UPDATE memory_cue_terms SET rarity=? WHERE cue_kind=? AND cue_value=?",
                (rarity, kind, value),
            )
        return len(signatures)

    @staticmethod
    def _edge(left: dict[str, Any], right: dict[str, Any],
              ignored_terms: set[str] | None = None) -> dict[str, Any] | None:
        ignored_terms = ignored_terms or set()
        ls = left["signature"]
        rs = right["signature"]
        l_retrieval = set(ls.get("retrieval_terms") or []) - ignored_terms
        r_retrieval = set(rs.get("retrieval_terms") or []) - ignored_terms
        l_content = set(ls.get("content_terms") or []) - ignored_terms
        r_content = set(rs.get("content_terms") or []) - ignored_terms
        l_entities = {str(item).lower() for item in ls.get("entities") or []} - ignored_terms
        r_entities = {str(item).lower() for item in rs.get("entities") or []} - ignored_terms
        l_dates = set(ls.get("dates") or [])
        r_dates = set(rs.get("dates") or [])
        retrieval = _jaccard(l_retrieval, r_retrieval)
        content = _jaccard(l_content, r_content)
        entity = _jaccard(l_entities, r_entities)
        date = 1.0 if l_dates & r_dates else 0.0
        surface = max(retrieval, content)
        left_state = extract_terms(str(left.get("state_change") or ""))
        right_state = extract_terms(str(right.get("state_change") or ""))
        contradiction = 1.0 if _contains_contradiction(left_state, right_state) else 0.0
        both_dated = bool(l_dates and r_dates)
        shared_specific = bool(l_entities & r_entities) or retrieval >= 0.16
        # Same source batch is the strongest evidence two diaries share a real event,
        # even when they were written on different dates (e.g. midnight split).
        same_source = bool(
            left.get("source_batch_id") and right.get("source_batch_id")
            and str(left["source_batch_id"]) == str(right["source_batch_id"])
        )
        same_event = _clamp(0.34 * retrieval + 0.28 * content + 0.22 * entity + 0.16 * date)
        # same_event_restated: shared date OR shared source batch, with content overlap
        if (same_source or (date and same_event >= 0.34)) and (entity > 0 or retrieval >= 0.28):
            edge_type = "same_event_restated"
            base_strength = same_event if not same_source else max(same_event, 0.55)
            strength = _clamp(base_strength)
            competition = _clamp((strength - 0.25) * 0.76)
            edge_confidence = 0.90 if same_source else _clamp(0.55 + same_event * 0.45)
            conflict_flags = {"same_source": same_source, "shared_date": bool(date)}
        elif contradiction and both_dated and shared_specific:
            edge_type = "contradictory_stage"
            strength = _clamp(0.48 + 0.24 * entity + 0.20 * surface)
            competition = 0.04
            edge_confidence = _clamp(0.50 + entity * 0.35)
            conflict_flags = {"contradiction": True, "both_dated": True}
        elif not date and not same_source and surface >= 0.27 and (entity > 0 or retrieval >= 0.34):
            edge_type = "same_theme_distinct_event"
            strength = _clamp(0.42 * retrieval + 0.34 * content + 0.24 * entity)
            competition = _clamp(strength * 0.18, 0.0, 0.12)
            edge_confidence = _clamp(0.40 + surface * 0.40)
            conflict_flags = {"different_dates": both_dated}
        elif entity > 0 and (
            _jaccard(extract_terms(str(left.get("long_effect") or "")), right_state) >= 0.16
            or _jaccard(extract_terms(str(right.get("long_effect") or "")), left_state) >= 0.16
        ):
            edge_type = "causal_chain"
            strength = _clamp(0.40 + 0.25 * entity + 0.20 * surface)
            competition = 0.0
            edge_confidence = _clamp(0.45 + entity * 0.30)
            conflict_flags = {}
        elif surface >= 0.38 and same_event >= 0.28:
            # Looks similar but lacks enough evidence for same_event_restated
            edge_type = "probable_duplicate"
            strength = _clamp(surface * 0.70 + same_event * 0.30)
            competition = 0.0  # diagnostic only, no competition penalty
            edge_confidence = _clamp(0.30 + same_event * 0.35)
            conflict_flags = {"needs_review": True}
        elif surface >= 0.22:
            edge_type = "shared_surface"
            strength = _clamp(surface)
            competition = _clamp((surface - 0.22) * 0.12, 0.0, 0.05)
            edge_confidence = _clamp(0.25 + surface * 0.35)
            conflict_flags = {}
        else:
            return None
        return {
            "edge_type": edge_type,
            "strength": strength,
            "competition": competition,
            "edge_confidence": edge_confidence,
            "classifier_version": ALGORITHM_VERSION,
            "route_scores": {"retrieval": retrieval, "content": content, "entity": entity,
                             "date": date, "contradiction": contradiction, "same_source": float(same_source)},
            "distinctions": {"left_dates": sorted(l_dates)[:4], "right_dates": sorted(r_dates)[:4],
                             "shared_entities": sorted(l_entities & r_entities)[:8]},
            "conflict_flags": conflict_flags,
        }

    def rebuild_interference(self, episodes: list[dict[str, Any]] | None = None,
                             *, max_neighbors: int = 12, now: float | None = None) -> dict[str, Any]:
        started = time.perf_counter()
        now = float(now or time.time())
        if episodes is None:
            records = self._interference_records()
        else:
            records = [dict(item, signature=_cue_signature(item)) for item in episodes if item.get("memo_name")]
        # Candidate blocking is deliberately bounded per memory. The previous
        # posting-window implementation could still create a very large union
        # when many medium-frequency terms overlapped. Here each memory spends a
        # fixed comparison budget, with factual dates/entities and multi-cue
        # support ranked ahead of broad lexical similarity.
        by_key: dict[tuple[str, str], set[int]] = defaultdict(set)
        by_source: dict[str, set[int]] = defaultdict(set)
        for index, item in enumerate(records):
            signature = item["signature"]
            for value in signature.get("dates") or []:
                by_key[("date", str(value))].add(index)
            for value in signature.get("entities") or []:
                by_key[("entity", str(value).lower())].add(index)
            for value in signature.get("retrieval_terms") or []:
                by_key[("term", str(value).lower())].add(index)
            src = str(item.get("source_batch_id") or "").strip()
            if src:
                by_source[src].add(index)
        common_limit = max(3, int(math.ceil(len(records) * 0.18)))
        ignored_terms = {
            value for (kind, value), indexes in by_key.items()
            if kind == "term" and len(indexes) > common_limit
        }
        candidate_budget = max(24, min(64, int(max_neighbors) * 3))
        support_pool_limit = candidate_budget * 4
        support: list[dict[int, float]] = [defaultdict(float) for _ in records]
        route_hits: list[dict[int, int]] = [defaultdict(int) for _ in records]
        kind_weight = {"date": 5.0, "entity": 3.0, "term": 1.0}
        blocking_keys = sorted(
            by_key.items(),
            key=lambda item: (
                0 if item[0][0] == "date" else 1 if item[0][0] == "entity" else 2,
                len(item[1]), item[0][1],
            ),
        )
        per_record_keys = [0] * len(records)
        for (kind, value), indexes in blocking_keys:
            if kind == "term" and value in ignored_terms:
                continue
            ordered = sorted(indexes)
            if len(ordered) < 2:
                continue
            posting_window = min(
                len(ordered) - 1,
                max(6, min(_BLOCK_POSTING_WINDOW, int(max_neighbors) * 2)),
            )
            for pos, left in enumerate(ordered):
                if per_record_keys[left] >= _BLOCK_KEY_LIMIT and kind == "term":
                    continue
                per_record_keys[left] += 1
                # A ring window avoids index-order edge effects while keeping
                # work bounded even for a date/entity shared by many years.
                for step in range(1, posting_window + 1):
                    right = ordered[(pos + step) % len(ordered)]
                    if left == right:
                        continue
                    support[left][right] += kind_weight[kind]
                    route_hits[left][right] += 1
                if len(support[left]) > support_pool_limit:
                    keep = sorted(
                        support[left],
                        key=lambda right: (
                            route_hits[left][right], support[left][right], -right,
                        ),
                        reverse=True,
                    )[:candidate_budget * 2]
                    keep_set = set(keep)
                    support[left] = defaultdict(
                        float, {right: support[left][right] for right in keep}
                    )
                    route_hits[left] = defaultdict(
                        int, {right: route_hits[left][right] for right in keep_set}
                    )
        # Same raw source is first-hand evidence and bypasses lexical budgets.
        mandatory_pairs: set[tuple[int, int]] = set()
        for indexes in by_source.values():
            ordered = sorted(indexes)
            for pos, left in enumerate(ordered[:20]):
                for right in ordered[pos + 1:pos + 1 + 20]:
                    mandatory_pairs.add(tuple(sorted((left, right))))
        # Relationship-stage contradictions often use opposite words rather
        # than shared words. Reserve a tiny bounded lane among already blocked
        # neighbours so a later "accepted/came back" memory is not hidden by
        # dozens of lexically similar neutral memories.
        conflict_priority_pairs: set[tuple[int, int]] = set()
        conflict_buckets: dict[tuple[int, int, str], list[int]] = defaultdict(list)
        for index, record in enumerate(records):
            content_terms = set(record["signature"].get("content_terms") or [])
            entities = {
                str(item).lower() for item in record["signature"].get("entities") or []
            } or {"__global__"}
            for category, (negative, positive) in enumerate(_CONTRADICTION_PAIRS):
                if content_terms & negative:
                    for entity in entities:
                        conflict_buckets[(category, -1, entity)].append(index)
                if content_terms & positive:
                    for entity in entities:
                        conflict_buckets[(category, 1, entity)].append(index)
        for (category, side, entity), left_indexes in sorted(conflict_buckets.items()):
            if side != -1:
                continue
            right_indexes = conflict_buckets.get((category, 1, entity), [])
            if not right_indexes:
                continue
            right_ordered = sorted(set(right_indexes))
            for pos, left in enumerate(sorted(set(left_indexes))):
                start = pos % len(right_ordered)
                for step in range(min(8, len(right_ordered))):
                    right = right_ordered[(start + step) % len(right_ordered)]
                    if left != right:
                        conflict_priority_pairs.add(tuple(sorted((left, right))))
        for left, scores in enumerate(support):
            left_terms = set(records[left]["signature"].get("content_terms") or [])
            conflict_ranked: list[tuple[int, float, int]] = []
            for right, score in scores.items():
                right_terms = set(records[right]["signature"].get("content_terms") or [])
                if _contains_contradiction(left_terms, right_terms):
                    conflict_ranked.append((route_hits[left][right], score, right))
            conflict_ranked.sort(reverse=True)
            conflict_priority_pairs.update(
                tuple(sorted((left, right))) for _, _, right in conflict_ranked[:8]
            )
        pair_candidates = set(mandatory_pairs)
        pair_candidates.update(conflict_priority_pairs)
        for left, scores in enumerate(support):
            ranked = sorted(
                scores,
                key=lambda right: (
                    route_hits[left][right], scores[right], -right,
                ),
                reverse=True,
            )[:candidate_budget]
            pair_candidates.update(tuple(sorted((left, right))) for right in ranked if left != right)
        edges_by_source: dict[str, list[tuple[str, dict[str, Any]]]] = defaultdict(list)
        for left_index, right_index in pair_candidates:
            left, right = records[left_index], records[right_index]
            edge = self._edge(left, right, ignored_terms)
            if edge:
                left_name, right_name = str(left["memo_name"]), str(right["memo_name"])
                edges_by_source[left_name].append((right_name, edge))
                edges_by_source[right_name].append((left_name, edge))
        kept_map: dict[tuple[str, str], dict[str, Any]] = {}
        for source, targets in edges_by_source.items():
            targets.sort(key=lambda item: (float(item[1]["strength"]), item[0]), reverse=True)
            for target, edge in targets[:max(2, min(4, int(max_neighbors)))]:
                pair = tuple(sorted((source, target)))
                previous = kept_map.get(pair)
                if previous is None or float(edge["strength"]) > float(previous["strength"]):
                    kept_map[pair] = edge
        kept = [(pair[0], pair[1], edge) for pair, edge in kept_map.items()]
        with self._lock:
            conn = self._get_conn()
            conn.execute("DELETE FROM memory_interference_edges")
            conn.execute("DELETE FROM memory_interference_members")
            conn.execute("DELETE FROM memory_interference_groups")
            for source, target, edge in kept:
                conn.execute(
                    """INSERT OR REPLACE INTO memory_interference_edges(
                       source_memo,target_memo,edge_type,strength,competition,
                       route_scores_json,distinctions_json,
                       edge_confidence,classifier_version,evidence_routes_json,conflict_flags_json,
                       updated_ts) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (source, target, edge["edge_type"], edge["strength"], edge["competition"],
                     _json(edge["route_scores"]), _json(edge["distinctions"]),
                     edge.get("edge_confidence", 0.5), edge.get("classifier_version", ALGORITHM_VERSION),
                     _json(edge["route_scores"]), _json(edge.get("conflict_flags", {})), now),
                )
            components = self._refresh_interference_derivatives(conn, now=now)
            # Update index meta
            build_ms = round((time.perf_counter() - started) * 1000.0, 3)
            meta = {
                "access_index_version": ALGORITHM_VERSION,
                "last_full_rebuild_ts": str(now),
                "interference_candidate_pairs": str(len(pair_candidates)),
                "interference_mandatory_pairs": str(len(mandatory_pairs)),
                "interference_conflict_priority_pairs": str(len(conflict_priority_pairs)),
                "interference_candidate_budget": str(candidate_budget),
                "interference_build_ms": str(build_ms),
                "interference_corpus_size": str(len(records)),
                "interference_max_neighbors": str(int(max_neighbors)),
                "interference_fingerprint": self._interference_fingerprint(records, max_neighbors),
                "interference_strategy": "bounded_rare_cue_v2",
            }
            conn.executemany(
                "INSERT OR REPLACE INTO memory_access_index_meta(key,value) VALUES(?,?)",
                list(meta.items()),
            )
            conn.commit()
        return {"episodes": len(records), "pairs": len(pair_candidates), "edges": len(kept),
                "groups": len(components), "ignored_common_terms": len(ignored_terms),
                "candidate_budget": candidate_budget, "mandatory_pairs": len(mandatory_pairs),
                "conflict_priority_pairs": len(conflict_priority_pairs),
                "build_ms": round((time.perf_counter() - started) * 1000.0, 3),
                "strategy": "bounded_rare_cue_v2"}

    def _refresh_interference_derivatives(self, conn: Any, *, now: float) -> list[set[str]]:
        """Rebuild group membership and loads from the current edge table."""
        conn.execute("DELETE FROM memory_interference_members")
        conn.execute("DELETE FROM memory_interference_groups")
        rows = [dict(row) for row in conn.execute(
            "SELECT source_memo,target_memo,edge_type,strength,competition,edge_confidence "
            "FROM memory_interference_edges"
        ).fetchall()]
        adjacency: dict[str, set[str]] = defaultdict(set)
        for edge in rows:
            if (edge["edge_type"] == "same_event_restated"
                    and float(edge["strength"]) >= 0.48
                    and float(edge.get("edge_confidence") or 0) >= 0.65):
                adjacency[str(edge["source_memo"])].add(str(edge["target_memo"]))
                adjacency[str(edge["target_memo"])].add(str(edge["source_memo"]))
        components: list[set[str]] = []
        seen: set[str] = set()
        for start in adjacency:
            if start in seen:
                continue
            stack, component = [start], set()
            while stack:
                item = stack.pop()
                if item in component:
                    continue
                component.add(item)
                stack.extend(adjacency.get(item, set()) - component)
            seen.update(component)
            if len(component) > 1:
                components.append(component)
        for component in components:
            group_id = "ig-" + hashlib.sha1("\u241f".join(sorted(component)).encode("utf-8")).hexdigest()[:16]
            strengths = [float(edge["strength"]) for edge in rows
                         if edge["source_memo"] in component and edge["target_memo"] in component]
            conn.execute(
                "INSERT INTO memory_interference_groups(group_id,member_count,max_strength,updated_ts) VALUES(?,?,?,?)",
                (group_id, len(component), max(strengths or [0.0]), now),
            )
            conn.executemany(
                "INSERT INTO memory_interference_members(group_id,memo_name) VALUES(?,?)",
                [(group_id, memo_name) for memo_name in sorted(component)],
            )
        conn.execute("UPDATE memory_access_state SET interference_load=0")
        loads: dict[str, float] = defaultdict(float)
        for edge in rows:
            loads[str(edge["source_memo"])] += float(edge["competition"])
            loads[str(edge["target_memo"])] += float(edge["competition"])
        conn.executemany(
            "UPDATE memory_access_state SET interference_load=? WHERE memo_name=?",
            [(_clamp(load, 0.0, 0.24), memo_name) for memo_name, load in loads.items()],
        )
        return components

    def mark_interference_dirty(self, memo_names: list[str]) -> int:
        """Queue memos whose neighbourhood must be recomputed.

        An empty list **clears** the queue (marks all dirty memos as consumed).
        """
        names = [str(item) for item in memo_names if str(item)]
        if not names:
            with self._lock:
                conn = self._get_conn()
                conn.execute(
                    "INSERT OR REPLACE INTO memory_access_index_meta(key,value) VALUES('interference_dirty_memos','[]')"
                )
                conn.commit()
            return 0
        with self._lock:
            conn = self._get_conn()
            row = conn.execute(
                "SELECT value FROM memory_access_index_meta WHERE key='interference_dirty_memos'"
            ).fetchone()
            existing = _loads(row["value"] if row else "", [])
            if not isinstance(existing, list):
                existing = []
            merged = list(dict.fromkeys([str(item) for item in existing] + names))[:5000]
            conn.execute(
                "INSERT OR REPLACE INTO memory_access_index_meta(key,value) VALUES('interference_dirty_memos',?)",
                (_json(merged),),
            )
            conn.commit()
        return len(names)

    def pop_interference_dirty(self, limit: int = 50) -> list[str]:
        """Atomically take one dirty batch while retaining the remainder."""
        batch_limit = max(1, min(500, int(limit)))
        with self._lock:
            conn = self._get_conn()
            row = conn.execute(
                "SELECT value FROM memory_access_index_meta WHERE key='interference_dirty_memos'"
            ).fetchone()
            existing = _loads(row["value"] if row else "", [])
            if not isinstance(existing, list):
                existing = []
            queue = list(dict.fromkeys(str(item) for item in existing if str(item)))
            batch = queue[:batch_limit]
            remainder = queue[batch_limit:]
            conn.execute(
                "INSERT OR REPLACE INTO memory_access_index_meta(key,value) VALUES('interference_dirty_memos',?)",
                (_json(remainder),),
            )
            conn.commit()
        return batch

    def sync_interference_incremental(self, memo_name: str, *, max_neighbors: int = 12,
                                      now: float | None = None,
                                      refresh_derivatives: bool = True) -> dict[str, Any]:
        """Rebuild only one memo's edges instead of the whole graph.

        Neighbours are drawn from the same source batch, shared factual dates and
        shared rare cues, so cost scales with the neighbourhood rather than the
        corpus.
        """
        memo_name = str(memo_name or "").strip()
        if not memo_name:
            return {"updated": False, "reason": "missing_memo_name"}
        now = float(now or time.time())
        conn = self._get_conn()
        target_row = conn.execute(
            """SELECT e.memo_name,e.state_change,e.long_effect,e.source_batch_id,c.signature_json
               FROM episodes e JOIN memory_cue_signatures c ON c.memo_name=e.memo_name
               WHERE e.memo_name=? AND e.active=1""",
            (memo_name,),
        ).fetchone()
        if not target_row:
            return {"updated": False, "reason": "not_found"}
        target = dict(target_row)
        target["signature"] = _loads(target.pop("signature_json", "{}"), {})
        signature = target["signature"]
        # Candidate block: same source batch, shared date, or shared rare cue.
        neighbour_names: set[str] = set()
        source_batch = str(target.get("source_batch_id") or "").strip()
        if source_batch:
            for row in conn.execute(
                "SELECT memo_name FROM episodes WHERE source_batch_id=? AND active=1 AND memo_name<>?",
                (source_batch, memo_name),
            ).fetchall():
                neighbour_names.add(str(row["memo_name"]))
        cue_groups = {
            "date": [str(item) for item in signature.get("dates") or []][:12],
            "entity": [str(item).lower() for item in signature.get("entities") or []][:24],
            "term": [str(item).lower() for item in signature.get("retrieval_terms") or []][:_BLOCK_KEY_LIMIT],
        }
        clauses: list[str] = []
        params: list[str] = []
        rarity_floor = {"date": 0.0, "entity": 0.22, "term": 0.30}
        for kind, values in cue_groups.items():
            if not values:
                continue
            clauses.append(
                f"(cue_kind=? AND cue_value IN ({','.join('?' for _ in values)}) AND rarity>=?)"
            )
            params.extend([kind, *values, str(rarity_floor[kind])])
        if clauses:
            for row in conn.execute(
                f"""SELECT memo_name,MAX(rarity) AS best_rarity
                     FROM memory_cue_terms
                     WHERE ({' OR '.join(clauses)}) AND memo_name<>?
                     GROUP BY memo_name ORDER BY best_rarity DESC LIMIT 400""",
                (*params, memo_name),
            ).fetchall():
                neighbour_names.add(str(row["memo_name"]))
        if not neighbour_names:
            with self._lock:
                conn.execute(
                    "DELETE FROM memory_interference_edges WHERE source_memo=? OR target_memo=?",
                    (memo_name, memo_name),
                )
                if refresh_derivatives:
                    self._refresh_interference_derivatives(conn, now=now)
                conn.execute(
                    "INSERT OR REPLACE INTO memory_access_index_meta(key,value) VALUES('last_incremental_ts',?)",
                    (str(now),),
                )
                conn.commit()
            return {"updated": True, "memo_name": memo_name, "neighbours": 0, "edges": 0}
        placeholders = ",".join("?" for _ in neighbour_names)
        neighbour_rows = conn.execute(
            f"""SELECT e.memo_name,e.state_change,e.long_effect,e.source_batch_id,c.signature_json
                FROM episodes e JOIN memory_cue_signatures c ON c.memo_name=e.memo_name
                WHERE e.memo_name IN ({placeholders}) AND e.active=1""",
            tuple(neighbour_names),
        ).fetchall()
        edges: list[tuple[str, str, dict[str, Any]]] = []
        for raw in neighbour_rows:
            neighbour = dict(raw)
            neighbour["signature"] = _loads(neighbour.pop("signature_json", "{}"), {})
            edge = self._edge(target, neighbour, set())
            if edge:
                pair = tuple(sorted((memo_name, str(neighbour["memo_name"]))))
                edges.append((pair[0], pair[1], edge))
        edges.sort(key=lambda item: float(item[2]["strength"]), reverse=True)
        edges = edges[:max(2, min(40, int(max_neighbors)))]
        with self._lock:
            conn.execute(
                "DELETE FROM memory_interference_edges WHERE source_memo=? OR target_memo=?",
                (memo_name, memo_name),
            )
            for source, target_name, edge in edges:
                conn.execute(
                    """INSERT OR REPLACE INTO memory_interference_edges(
                       source_memo,target_memo,edge_type,strength,competition,
                       route_scores_json,distinctions_json,
                       edge_confidence,classifier_version,evidence_routes_json,conflict_flags_json,
                       updated_ts) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (source, target_name, edge["edge_type"], edge["strength"], edge["competition"],
                     _json(edge["route_scores"]), _json(edge["distinctions"]),
                     edge.get("edge_confidence", 0.5), edge.get("classifier_version", ALGORITHM_VERSION),
                     _json(edge["route_scores"]), _json(edge.get("conflict_flags", {})), now),
                )
            if refresh_derivatives:
                self._refresh_interference_derivatives(conn, now=now)
            conn.execute(
                "INSERT OR REPLACE INTO memory_access_index_meta(key,value) VALUES('last_incremental_ts',?)",
                (str(now),),
            )
            conn.commit()
        return {"updated": True, "memo_name": memo_name,
                "neighbours": len(neighbour_names), "edges": len(edges)}

    def sync_interference_batch(self, memo_names: list[str], *, max_neighbors: int = 12,
                                now: float | None = None) -> dict[str, Any]:
        """Update a dirty batch once, then reconcile shared derivatives once.

        A large sync often marks several memories from the same date/source.
        Processing each name independently is correct but repeatedly scans and
        rebuilds the same neighbourhood. This method deduplicates the batch and
        defers group/load reconciliation until every target edge is current.
        """
        names = list(dict.fromkeys(str(item).strip() for item in memo_names if str(item).strip()))
        if not names:
            return {"updated": 0, "targets": 0, "neighbours": 0, "edges": 0, "failed": []}
        now = float(now or time.time())
        updated = neighbours = edges = 0
        failed: list[str] = []
        # Newly inserted cue terms start without corpus rarity. Refresh once for
        # the whole burst so rare-term neighbour discovery is immediately live.
        with self._lock:
            self._refresh_rarity()
            self._get_conn().commit()
        # Individual edge writes remain fail-open. The expensive global
        # derivative pass is done once below instead of once per target.
        for memo_name in names:
            try:
                result = self.sync_interference_incremental(
                    memo_name, max_neighbors=max_neighbors, now=now,
                    refresh_derivatives=False,
                )
                updated += int(bool(result.get("updated")))
                neighbours += int(result.get("neighbours") or 0)
                edges += int(result.get("edges") or 0)
            except Exception:
                failed.append(memo_name)
        with self._lock:
            conn = self._get_conn()
            components = self._refresh_interference_derivatives(conn, now=now)
            records = self._interference_records()
            meta = {
                "access_index_version": ALGORITHM_VERSION,
                "last_incremental_ts": str(now),
                "interference_corpus_size": str(len(records)),
                "interference_max_neighbors": str(int(max_neighbors)),
                "interference_fingerprint": self._interference_fingerprint(records, max_neighbors),
                "interference_strategy": "bounded_rare_cue_v2",
            }
            conn.executemany(
                "INSERT OR REPLACE INTO memory_access_index_meta(key,value) VALUES(?,?)",
                list(meta.items()),
            )
            conn.commit()
        return {
            "updated": updated, "targets": len(names), "neighbours": neighbours,
            "edges": edges, "groups": len(components), "failed": failed,
        }

    def index_meta(self) -> dict[str, Any]:
        rows = self._get_conn().execute("SELECT key,value FROM memory_access_index_meta").fetchall()
        meta = {str(row["key"]): str(row["value"]) for row in rows}
        dirty = _loads(meta.get("interference_dirty_memos"), [])
        corpus = int(float(meta.get("interference_corpus_size") or 0))
        pairs = int(float(meta.get("interference_candidate_pairs") or 0))
        pair_ratio = pairs / max(1, corpus)
        if corpus <= 0:
            scale_health = "unbuilt"
        elif pair_ratio <= 48:
            scale_health = "healthy"
        elif pair_ratio <= 64:
            scale_health = "watch"
        else:
            scale_health = "over_budget"
        return {
            "access_index_version": meta.get("access_index_version", ALGORITHM_VERSION),
            "cue_rarity_dirty": meta.get("cue_rarity_dirty", "0") == "1",
            "interference_dirty_memos": dirty if isinstance(dirty, list) else [],
            "last_full_rebuild_ts": float(meta.get("last_full_rebuild_ts") or 0),
            "last_incremental_rebuild_ts": float(meta.get("last_incremental_ts") or 0),
            "interference_candidate_pairs": pairs,
            "interference_mandatory_pairs": int(float(meta.get("interference_mandatory_pairs") or 0)),
            "interference_conflict_priority_pairs": int(
                float(meta.get("interference_conflict_priority_pairs") or 0)
            ),
            "interference_candidate_budget": int(float(meta.get("interference_candidate_budget") or 0)),
            "interference_build_ms": float(meta.get("interference_build_ms") or 0),
            "interference_corpus_size": corpus,
            "interference_pairs_per_memory": round(pair_ratio, 3),
            "interference_strategy": meta.get("interference_strategy", "legacy"),
            "interference_scale_health": scale_health,
            "last_error": meta.get("last_error", ""),
        }

    def rebuild(self, episodes: list[dict[str, Any]], *, config: dict[str, Any] | None = None,
                reason: str = "backfill", reuse_interference: bool = False) -> dict[str, Any]:
        config = config or {}
        now = time.time()
        updated = 0
        for episode in episodes:
            result = self.sync_episode(
                episode, now=now,
                vivid_threshold=float(config.get("vivid_threshold", 0.68)),
                deep_threshold=float(config.get("deep_threshold", 0.32)),
                source_proxy_weight=float(config.get("source_proxy_weight", _SOURCE_PROXY_CONFIDENCE)),
            )
            updated += int(bool(result.get("updated")))
        with self._lock:
            self._refresh_rarity()
            max_neighbors = int(config.get("max_neighbors", 12))
            graph_episodes = episodes if bool(config.get("interference_enable", True)) else []
            if reuse_interference and not self.interference_rebuild_needed(
                graph_episodes, max_neighbors=max_neighbors,
            ):
                conn = self._get_conn()
                edge_count = int(conn.execute(
                    "SELECT COUNT(*) AS n FROM memory_interference_edges"
                ).fetchone()["n"])
                group_count = int(conn.execute(
                    "SELECT COUNT(*) AS n FROM memory_interference_groups"
                ).fetchone()["n"])
                meta = self.index_meta()
                edges = {
                    "episodes": len(graph_episodes), "pairs": int(meta.get("interference_candidate_pairs") or 0),
                    "edges": edge_count, "groups": group_count,
                    "candidate_budget": int(meta.get("interference_candidate_budget") or 0),
                    "mandatory_pairs": int(meta.get("interference_mandatory_pairs") or 0),
                    "build_ms": 0.0, "strategy": str(meta.get("interference_strategy") or ""),
                    "graph_reused": True,
                }
            else:
                edges = self.rebuild_interference(
                    graph_episodes, max_neighbors=max_neighbors, now=now,
                )
                edges["graph_reused"] = False
            maintenance = self.maintain(
                config=config, now=now, reason=reason, record=False,
                advance_transitions=False,
            )
            conn = self._get_conn()
            conn.execute(
                "INSERT INTO memory_access_maintenance(reason,episodes,states_changed,edges,detail_json,created_ts) VALUES(?,?,?,?,?,?)",
                (reason, len(episodes), int(maintenance.get("states_changed") or 0), int(edges["edges"]),
                 _json({"updated": updated, "groups": edges["groups"]}), now),
            )
            conn.commit()
        return {"updated": updated, **edges, **maintenance}

    def maintain(self, *, config: dict[str, Any] | None = None, now: float | None = None,
                 reason: str = "scheduled", record: bool = True,
                 advance_transitions: bool = True) -> dict[str, Any]:
        """Settle accessibility and state transitions.

        5.0 contract:
        - Time decay is weighted by `age_reference_confidence`, so a Memos update
          proxy cannot age a memory as strongly as a factual event time.
        - `unknown` clocks no longer assume a hard 365-day age.
        - A memory that fails the decay eligibility gate never crosses into a
          deeper state from time alone.
        - Crossing vivid->latent or latent->deep requires the same target state
          on two consecutive maintenance runs.
        """
        config = config or {}
        now = float(now or time.time())
        half_life = max(7.0, float(config.get("decay_days", 45.0)))
        vivid_threshold = float(config.get("vivid_threshold", 0.68))
        deep_threshold = float(config.get("deep_threshold", 0.32))
        confirmation_runs = max(1, int(config.get("state_confirmation_runs", 2)))
        depth_rank = {"vivid": 0, "latent": 1, "deep": 2}
        changed = 0
        pending = 0
        blocked = 0
        with self._lock:
            conn = self._get_conn()
            rows = conn.execute("SELECT * FROM memory_access_state").fetchall()
            for raw in rows:
                row = dict(raw)
                memo_name = str(row["memo_name"])
                current_state = str(row["access_state"])
                age_confidence = float(row.get("age_reference_confidence") or 0.0)
                # state_confidence modulates how many confirmation runs are needed:
                # high confidence (>=0.8) → 1 run; low (<0.4) → 3; default → config value
                state_conf = float(row.get("state_confidence") or 0.5)
                if state_conf >= 0.8:
                    row_confirmation_runs = 1
                elif state_conf < 0.4:
                    row_confirmation_runs = max(1, confirmation_runs + 1)
                else:
                    row_confirmation_runs = confirmation_runs
                use_ts = max(
                    float(row.get("last_used_ts") or 0),
                    float(row.get("last_reconsolidated_ts") or 0),
                )
                age_ts = float(row.get("age_reference_ts") or 0)
                reference_ts = max(use_ts, age_ts)
                has_verified_use_clock = use_ts > 0 and use_ts >= age_ts
                reference_kind = str(row.get("age_reference_kind") or "unknown")
                if has_verified_use_clock:
                    # A confirmed response use is a real reconsolidation clock.
                    age_confidence = 1.0
                persistence = _clamp(float(row["persistence"]))
                memory_half_life = half_life * (0.55 + persistence * 1.65)
                if not bool(config.get("decay_enable", True)):
                    recency = max(0.85, float(row.get("vividness") or 0.85))
                elif reference_ts <= 0 or age_confidence <= 0.0:
                    # Unknown clock: never invent an age, and never refresh either.
                    # Hold the value already derived from the episode so migration
                    # time cannot make an old diary look recent.
                    recency = _clamp(float(row.get("vividness") or 0.0))
                else:
                    age_days = max(0.0, (now - reference_ts) / 86400.0)
                    effective_age = age_days * age_confidence
                    recency = math.exp(-effective_age / memory_half_life)
                if reference_kind == "source_updated_proxy" and not has_verified_use_clock:
                    recency = min(recency, 0.52)
                recon = min(0.14, int(row.get("reconsolidation_count") or 0) * 0.012)
                accessibility = _clamp(
                    0.42 * persistence + 0.32 * recency
                    + 0.18 * float(row["distinctiveness"])
                    + float(row["grounding_bonus"]) + recon
                    - float(row["interference_load"]) - float(row["inhibition"])
                )
                reason_data = _loads(row.get("state_reason_json"), {})
                decay_eligible = bool(int(row["decay_eligible"])) if row.get("decay_eligible") is not None else True
                # A protected memory must not be eroded by time at all. Recompute
                # its accessibility with the recency term held at its stored value
                # so ageing cannot drag a promise or an unresolved thread downward.
                if not decay_eligible:
                    protected_recency = max(recency, _clamp(float(row.get("vividness") or 0.0)))
                    protected_accessibility = _clamp(
                        0.42 * persistence + 0.32 * protected_recency
                        + 0.18 * float(row["distinctiveness"])
                        + float(row["grounding_bonus"]) + recon
                        - float(row["interference_load"]) - float(row["inhibition"])
                    )
                    if protected_accessibility > accessibility:
                        reason_data["decay_withheld"] = round(
                            protected_accessibility - accessibility, 4,
                        )
                        blocked += 1
                    else:
                        reason_data.pop("decay_withheld", None)
                    accessibility = max(accessibility, protected_accessibility)
                target_state = self._state_for(
                    accessibility, current_state, vivid_threshold, deep_threshold,
                )
                going_deeper = depth_rank.get(target_state, 1) > depth_rank.get(current_state, 1)
                final_state = current_state
                state_since_ts = float(row.get("state_since_ts") or now)
                if not advance_transitions:
                    # Rebuild refreshes dimensions, cues and gates only. State
                    # changes require a scheduled/manual maintenance vote.
                    pass
                elif target_state == current_state:
                    reason_data.pop("pending_state", None)
                    reason_data.pop("pending_state_since_ts", None)
                    reason_data.pop("pending_confirmations", None)
                elif going_deeper and not decay_eligible:
                    # Second guard: even if other dimensions pushed it down, a
                    # protected memory never crosses into a deeper state.
                    reason_data["blocked_transition"] = target_state
                    eligibility_status = str(reason_data.get("eligibility_status") or "protected")
                    reason_data["blocked_reason"] = (
                        "decay_gate_evidence_deferred"
                        if eligibility_status == "deferred"
                        else "decay_gate_protected"
                    )
                    blocked += 1
                else:
                    confirmations = int(reason_data.get("pending_confirmations") or 0)
                    if str(reason_data.get("pending_state") or "") == target_state:
                        confirmations += 1
                    else:
                        confirmations = 1
                        reason_data["pending_state_since_ts"] = now
                    reason_data["pending_state"] = target_state
                    reason_data["pending_confirmations"] = confirmations
                    if confirmations >= row_confirmation_runs:
                        final_state = target_state
                        state_since_ts = now
                        changed += 1
                        reason_data.pop("pending_state", None)
                        reason_data.pop("pending_state_since_ts", None)
                        reason_data.pop("pending_confirmations", None)
                        reason_data.pop("blocked_transition", None)
                        reason_data.pop("blocked_reason", None)
                    else:
                        pending += 1
                conn.execute(
                    """UPDATE memory_access_state SET access_state=?,accessibility=?,vividness=?,
                       state_reason_json=?,state_since_ts=?,algorithm_version=?,updated_ts=?
                       WHERE memo_name=?""",
                    (final_state, accessibility, _clamp(recency), _json(reason_data),
                     state_since_ts, ALGORITHM_VERSION, now, memo_name),
                )
            if record:
                conn.execute(
                    "INSERT INTO memory_access_maintenance(reason,episodes,states_changed,edges,detail_json,created_ts) VALUES(?,?,?,?,?,?)",
                    (reason, len(rows), changed, self._edge_count(conn),
                     _json({"half_life_days": half_life, "pending_transitions": pending,
                            "blocked_by_gate": blocked, "algorithm": ALGORITHM_VERSION}), now),
                )
            conn.commit()
        return {"episodes": len(rows), "states_changed": changed,
                "pending_transitions": pending, "blocked_by_gate": blocked}

    @staticmethod
    def _edge_count(conn: Any) -> int:
        return int(conn.execute("SELECT COUNT(*) AS n FROM memory_interference_edges").fetchone()["n"])

    def _candidate_state(self, memo_name: str) -> tuple[dict[str, Any], dict[str, Any]]:
        conn = self._get_conn()
        state_row = conn.execute("SELECT * FROM memory_access_state WHERE memo_name=?", (memo_name,)).fetchone()
        cue_row = conn.execute("SELECT * FROM memory_cue_signatures WHERE memo_name=?", (memo_name,)).fetchone()
        state = dict(state_row) if state_row else {
            "memo_name": memo_name, "access_state": "vivid", "accessibility": 0.60,
            "persistence": 0.50, "interference_load": 0.0,
        }
        cue = _loads(cue_row["signature_json"], {}) if cue_row else {}
        if cue_row:
            cue["rarity_score"] = float(cue_row["rarity_score"])
        return state, cue

    @staticmethod
    def _base_score(hit: dict[str, Any]) -> float:
        for key in ("injection_score", "final_score", "rerank_score", "score", "relevance_score", "similarity"):
            try:
                value = float(hit.get(key))
            except (TypeError, ValueError):
                continue
            if math.isfinite(value):
                return _clamp(value)
        return 0.5

    @staticmethod
    def _route_support(hit: dict[str, Any]) -> float:
        routes = hit.get("route_evidence") or hit.get("_route_evidence") or {}
        values: list[float] = []
        if isinstance(routes, dict):
            for value in routes.values():
                try:
                    values.append(_clamp(float(value)))
                except (TypeError, ValueError):
                    if value:
                        values.append(0.55)
        elif isinstance(routes, (list, tuple, set)):
            values.extend([0.55] * len(routes))
        for key in ("_source_turn_hits", "_passage_hits", "bm25_score", "vector_score"):
            value = hit.get(key)
            if isinstance(value, (list, tuple, set, dict)) and value:
                values.append(0.72)
            elif isinstance(value, (int, float)) and value > 0:
                values.append(_clamp(float(value)))
        return max(values or [0.0])

    @staticmethod
    def _source_route_support(hit: dict[str, Any]) -> tuple[float, dict[str, Any]]:
        """Measure first-hand source support without treating batch guesses as exact.

        A source turn can reach an episode through an exact ``episode_turn_links``
        row or through a weaker same-batch fallback.  test7 collapsed both into a
        generic route bonus, which hid the most useful distinction in the 4.x
        archive.  Keep the score bounded so this route still cannot replace the
        baseline retrieval score on its own.
        """
        best = 0.0
        detail = {
            "exact_link": False, "batch_link": False, "lexical": 0.0,
            "relevance": 0.0, "turn_count": 0,
        }
        turns = hit.get("_source_turn_hits") or []
        if isinstance(turns, dict):
            turns = [turns]
        for raw in turns if isinstance(turns, (list, tuple)) else []:
            if not isinstance(raw, dict):
                continue
            lexical = _clamp(float(raw.get("lexical") or 0.0))
            relevance = _clamp(float(raw.get("relevance") or 0.0))
            exact = bool(raw.get("exact_link"))
            support = (0.76 + lexical * 0.16 + relevance * 0.08) if exact else (
                0.36 + lexical * 0.18 + relevance * 0.10
            )
            detail["turn_count"] += 1
            if support > best:
                best = support
                detail.update({
                    "exact_link": exact, "batch_link": not exact,
                    "lexical": round(lexical, 4), "relevance": round(relevance, 4),
                })
        routes = hit.get("_route_evidence") or hit.get("route_evidence") or []
        if isinstance(routes, dict):
            routes = [routes]
        for raw in routes if isinstance(routes, (list, tuple)) else []:
            if not isinstance(raw, dict) or str(raw.get("route") or "") != "source_turn":
                continue
            lexical = _clamp(float(raw.get("lexical") or 0.0))
            relevance = _clamp(float(raw.get("relevance") or 0.0))
            exact = bool(raw.get("exact_link"))
            batch_link = _clamp(float(raw.get("batch_link") or 0.0))
            support = (0.76 + lexical * 0.16 + relevance * 0.08) if exact else (
                0.30 + batch_link * 0.18 + lexical * 0.12 + relevance * 0.08
            )
            if support > best:
                best = support
                detail.update({
                    "exact_link": exact, "batch_link": not exact,
                    "lexical": round(lexical, 4), "relevance": round(relevance, 4),
                })
        return _clamp(best), detail

    @staticmethod
    def _cue_support(query: str, cue: dict[str, Any]) -> tuple[float, dict[str, Any]]:
        query_terms = extract_terms(query)
        cue_terms = set(cue.get("retrieval_terms") or []) | set(cue.get("content_terms") or [])
        entities = {str(item).lower() for item in cue.get("entities") or []}
        dates = {str(item) for item in cue.get("dates") or []}
        query_dates = set(_extract_dates(query))
        query_lower = query.lower()
        term_overlap = _jaccard(query_terms, cue_terms)
        entity_hits = sorted(item for item in entities if item and item in query_lower)
        date_hits = sorted(
            item for item in dates if item and any(
                item == query_date
                or (len(query_date) == 7 and item.startswith(query_date + "-"))
                or (len(item) == 7 and query_date.startswith(item + "-"))
                for query_date in query_dates
            )
        )
        quote_hits = [item for item in cue.get("quotes") or [] if len(str(item)) >= 3 and str(item).lower() in query_lower]
        exact = bool(date_hits or quote_hits or (entity_hits and term_overlap >= 0.08))
        support = _clamp(term_overlap * 1.7 + min(0.32, len(entity_hits) * 0.16)
                         + min(0.38, len(date_hits) * 0.28) + min(0.42, len(quote_hits) * 0.32))
        return support, {"exact": exact, "entity_hits": entity_hits[:8], "date_hits": date_hits[:4],
                         "quote_hits": quote_hits[:3], "term_overlap": term_overlap}

    @staticmethod
    def _grade_for_hits(has_quote: bool, has_date: bool, rare_entity_count: int,
                        rare_term_count: int) -> str:
        """Assign a cue grade. Weak cues never sum into a strong grade.

        A: verbatim quote (optionally with a date)
        B: date + a rare entity/object/action/result
        C: rare entity + at least two independent rare event terms
        D: date alone, common name, or a single broad term
        """
        if has_quote:
            return "A"
        # Objects, actions and outcomes are indexed as `term` cues rather than
        # `entity`, so a date plus any rare specific cue qualifies as grade B.
        if has_date and (rare_entity_count >= 1 or rare_term_count >= 1):
            return "B"
        if rare_entity_count >= 1 and rare_term_count >= 2:
            return "C"
        if rare_term_count >= 3:
            return "C"
        return "D"

    def classify_query_cues(self, query: str, *, entity_rarity_min: float = 0.30,
                            term_rarity_min: float = 0.48) -> dict[str, Any]:
        """Grade the cues in a query and report the per-memo hits behind them.

        A date on its own only opens a `temporal_scope`; it never claims an exact
        rescue, because "what happened that day" and "where did that ticket go"
        are different intents over the same date.
        """
        query_lower = str(query or "").lower()
        query_terms = extract_terms(query_lower)
        query_dates = set(_extract_dates(query_lower))
        lookup_values = list(dict.fromkeys(list(query_dates) + sorted(query_terms)))[:240]
        per_memo: dict[str, dict[str, Any]] = {}
        if lookup_values:
            placeholders = ",".join("?" for _ in lookup_values)
            rows = self._get_conn().execute(
                f"""SELECT t.memo_name,t.cue_kind,t.cue_value,t.rarity,a.access_state
                     FROM memory_cue_terms t
                     JOIN memory_access_state a ON a.memo_name=t.memo_name
                    WHERE t.cue_value IN ({placeholders})""",
                lookup_values,
            ).fetchall()
        else:
            rows = []
        # Quotes are matched by substring, so they need their own scan.
        quote_rows = self._get_conn().execute(
            "SELECT memo_name,cue_value,rarity FROM memory_cue_terms WHERE cue_kind='quote'"
        ).fetchall()
        for raw in rows:
            memo_name = str(raw["memo_name"])
            kind, value = str(raw["cue_kind"]), str(raw["cue_value"])
            rarity = float(raw["rarity"] or 0.0)
            entry = per_memo.setdefault(memo_name, {
                "dates": [], "quotes": [], "entities": [], "terms": [],
                "access_state": str(raw["access_state"]),
            })
            if kind == "date" and any(
                value == date or value.startswith(date + "-") or date.startswith(value + "-")
                for date in query_dates
            ):
                entry["dates"].append(value)
            elif kind == "entity" and len(value) >= 2 and value in query_lower and rarity >= entity_rarity_min:
                entry["entities"].append(value)
            elif kind == "term" and value in query_terms and rarity >= term_rarity_min:
                entry["terms"].append(value)
        for raw in quote_rows:
            value = str(raw["cue_value"])
            if len(value) >= 4 and value in query_lower:
                memo_name = str(raw["memo_name"])
                entry = per_memo.setdefault(memo_name, {
                    "dates": [], "quotes": [], "entities": [], "terms": [], "access_state": "",
                })
                entry["quotes"].append(value)
        for memo_name, entry in per_memo.items():
            entry["grade"] = self._grade_for_hits(
                bool(entry["quotes"]), bool(entry["dates"]),
                len(entry["entities"]), len(entry["terms"]),
            )
            entry["reasons"] = (
                ["quote:" + q[:24] for q in entry["quotes"][:2]]
                + ["date:" + d for d in entry["dates"][:2]]
                + ["entity:" + e for e in entry["entities"][:3]]
                + ["term:" + t for t in entry["terms"][:3]]
            )
        best_grade = "D"
        for entry in per_memo.values():
            if CUE_GRADES.index(entry["grade"]) < CUE_GRADES.index(best_grade):
                best_grade = entry["grade"]
        date_only = bool(query_dates) and best_grade == "D"
        return {
            "query_grade": best_grade,
            "date_only": date_only,
            "temporal_scope": sorted(query_dates)[:4],
            "per_memo": per_memo,
        }

    def _precise_rescue_candidates(self, query: str, excluded: set[str],
                                   limit: int = 12, *,
                                   entity_rarity_min: float = 0.30,
                                   same_day_gap_threshold: float = 0.15) -> list[dict[str, Any]]:
        """Find cue-addressable memories outside the normal retrieval pool.

        Only grade A and B qualify on their own. Grade C needs a clear score gap
        over the runner-up. Grade D (a bare date, a common name, one broad word)
        only widens the temporal scope and can never claim an exact rescue.
        """
        classification = self.classify_query_cues(
            query, entity_rarity_min=entity_rarity_min,
        )
        per_memo = classification["per_memo"]
        scored: list[tuple[float, str, dict[str, Any]]] = []
        for memo_name, entry in per_memo.items():
            if memo_name in excluded:
                continue
            grade = entry["grade"]
            if grade == "D":
                continue
            score = 0.0
            if entry["quotes"]:
                score += 0.78 + 0.10 * len(entry["quotes"])
            if entry["dates"]:
                score += 0.32
            score += 0.24 * len(entry["entities"])
            score += 0.10 * len(entry["terms"])
            if entry.get("access_state") == "deep":
                score += 0.04
            scored.append((score, memo_name, entry))
        scored.sort(key=lambda item: (-item[0], item[1]))
        # Grade C must clearly beat the runner-up before it is treated as a rescue.
        qualified: list[tuple[float, str, dict[str, Any]]] = []
        for index, (score, memo_name, entry) in enumerate(scored):
            if entry["grade"] in ("A", "B"):
                qualified.append((score, memo_name, entry))
                continue
            runner_up = scored[index + 1][0] if index + 1 < len(scored) else 0.0
            if score - runner_up >= same_day_gap_threshold:
                qualified.append((score, memo_name, entry))
        return [
            {
                "memo_name": memo_name,
                "score": _clamp(0.40 + min(0.30, score * 0.20)),
                "_access_rescue_source": "precise_cue_index",
                "_access_rescue_reasons": entry["reasons"][:6],
                "_access_rescue_grade": entry["grade"],
            }
            for score, memo_name, entry in qualified[:max(1, min(50, int(limit)))]
        ]

    def _source_link_rescue_candidates(self, query: str, excluded: set[str],
                                       limit: int = 12) -> list[dict[str, Any]]:
        """Resolve a distinctive source phrase through exact turn links.

        This is deliberately lexical and read-only. It complements the live
        vector source route when the wording survived in the archive but not in
        the diary/card. A broad term or a date cannot trigger it: at least two
        corpus-rare source terms, or a quoted phrase present in the turn, are
        required before an episode can enter the rescue pool.
        """
        query_text = " ".join(str(query or "").split()).strip()
        query_terms = sorted(extract_terms(query_text))[:48]
        if not query_terms:
            return []
        conn = self._get_conn()
        total_turns = int(conn.execute(
            "SELECT COUNT(*) AS n FROM source_turns"
        ).fetchone()["n"] or 0)
        if total_turns <= 0:
            return []
        placeholders = ",".join("?" for _ in query_terms)
        frequencies = {
            str(row["term"]): int(row["n"] or 0)
            for row in conn.execute(
                f"""SELECT term,COUNT(DISTINCT turn_id) AS n
                    FROM source_turn_terms WHERE term IN ({placeholders}) GROUP BY term""",
                query_terms,
            ).fetchall()
        }
        rare_limit = max(2, min(12, int(math.ceil(total_turns * 0.035))))
        rare_terms = {term for term, count in frequencies.items() if 0 < count <= rare_limit}
        quoted_parts = [
            " ".join(part.split()).strip()
            for part in re.findall(r'["\u201c\u201d\u2018\u2019]([^"\u201c\u201d\u2018\u2019]{4,80})["\u201c\u201d\u2018\u2019]', query_text)
        ]
        rows: list[Any] = []
        if len(rare_terms) >= 2:
            rare_list = sorted(rare_terms)
            rare_placeholders = ",".join("?" for _ in rare_list)
            rows.extend(conn.execute(
                f"""SELECT e.memo_name,s.batch_id,s.turn_index,s.role,s.content,s.event_ts,
                           COUNT(DISTINCT t.term) AS rare_hits
                    FROM source_turn_terms t
                    JOIN source_turns s ON s.id=t.turn_id
                    JOIN episode_turn_links l
                      ON l.batch_id=s.batch_id AND l.turn_index=s.turn_index
                    JOIN episodes e ON e.episode_id=l.episode_id
                    WHERE e.active=1 AND t.term IN ({rare_placeholders})
                    GROUP BY e.memo_name,s.id
                    HAVING rare_hits>=2
                    ORDER BY rare_hits DESC,s.id DESC LIMIT ?""",
                (*rare_list, max(8, min(100, int(limit) * 4))),
            ).fetchall())
        # A verbatim phrase is stronger than a bag of rare terms. Query it
        # directly so natural but corpus-common wording is not excluded from
        # source-verified recall. Exact episode_turn_links ownership remains
        # mandatory.
        for phrase in quoted_parts[:3]:
            rows.extend(conn.execute(
                """SELECT e.memo_name,s.batch_id,s.turn_index,s.role,s.content,s.event_ts,
                          0 AS rare_hits
                   FROM source_turns s
                   JOIN episode_turn_links l
                     ON l.batch_id=s.batch_id AND l.turn_index=s.turn_index
                   JOIN episodes e ON e.episode_id=l.episode_id
                   WHERE e.active=1 AND INSTR(LOWER(s.content),LOWER(?))>0
                   ORDER BY s.id DESC LIMIT ?""",
                (phrase, max(4, min(40, int(limit) * 2))),
            ).fetchall())
        best_by_memo: dict[str, tuple[float, dict[str, Any]]] = {}
        for raw in rows:
            memo_name = str(raw["memo_name"] or "")
            if not memo_name or memo_name in excluded:
                continue
            content = " ".join(str(raw["content"] or "").split())
            content_lower = content.lower()
            matched = sorted(term for term in rare_terms if term in extract_terms(content_lower))
            rare_coverage = len(matched) / max(2, len(rare_terms))
            phrase = next((part for part in quoted_parts if part.lower() in content_lower), "")
            if not phrase and len(matched) < 2:
                continue
            grade = "A" if phrase else "B"
            score = _clamp(0.58 + min(0.22, len(matched) * 0.045)
                           + min(0.12, rare_coverage * 0.12) + (0.08 if phrase else 0.0))
            hit = {
                "memo_name": memo_name,
                "score": score,
                "relevance": score,
                "_access_rescue_source": "source_turn_index",
                "_access_rescue_reasons": (
                    (["source_quote:" + phrase[:36]] if phrase else [])
                    + ["source_term:" + term for term in matched[:5]]
                ),
                "_access_rescue_grade": grade,
                "_source_turn_hits": [{
                    "batch_id": str(raw["batch_id"] or ""),
                    "turn_index": int(raw["turn_index"] or 0),
                    "role": str(raw["role"] or ""),
                    "content": content,
                    "event_ts": float(raw["event_ts"] or 0),
                    "relevance": score,
                    "lexical": round(min(1.0, rare_coverage + (0.25 if phrase else 0.0)), 4),
                    "exact_link": True,
                }],
                "_route_evidence": [{
                    "route": "source_turn", "rank": 0, "relevance": score,
                    "lexical": round(min(1.0, rare_coverage + (0.25 if phrase else 0.0)), 4),
                    "exact_link": True, "batch_link": 1.0,
                }],
            }
            current = best_by_memo.get(memo_name)
            if current is None or score > current[0]:
                best_by_memo[memo_name] = (score, hit)
        return [
            item for _score, item in sorted(
                best_by_memo.values(), key=lambda pair: pair[0], reverse=True
            )[:max(1, min(50, int(limit)))]
        ]

    def _apply_contrastive_ranking(self, items: list[dict[str, Any]], *,
                                   date_only_query: bool,
                                   enabled: bool = True) -> dict[str, Any]:
        """Separate evidenced targets from known confusable neighbours.

        The adjustment is intentionally pairwise and bounded. It never runs for
        date browsing, same-event restatements, or two candidates with equally
        plausible cue evidence. This avoids turning the interference graph into
        a destructive deduplicator.
        """
        diagnostics = {"enabled": bool(enabled), "pairs": 0, "adjusted": 0,
                       "max_adjustment": 0.0, "decisions": []}
        if not enabled or date_only_query or len(items) < 2:
            return diagnostics
        by_name = {str(item.get("memo_name") or ""): item for item in items}
        names = [name for name in by_name if name]
        if len(names) < 2:
            return diagnostics
        placeholders = ",".join("?" for _ in names)
        rows = self._get_conn().execute(
            f"""SELECT source_memo,target_memo,edge_type,strength,edge_confidence
                FROM memory_interference_edges
                WHERE source_memo IN ({placeholders}) AND target_memo IN ({placeholders})
                  AND edge_type IN ('same_theme_distinct_event','contradictory_stage',
                                    'probable_duplicate','shared_surface')
                  AND edge_confidence>=0.55""",
            (*names, *names),
        ).fetchall()
        adjustments: dict[str, float] = defaultdict(float)

        def signal(item: dict[str, Any]) -> float:
            grade = str(item.get("cue_grade") or "D")
            grade_signal = {"A": 0.58, "B": 0.43, "C": 0.20, "D": 0.0}.get(grade, 0.0)
            source = item.get("source_route") or {}
            source_signal = (
                0.30
                + float(source.get("lexical") or 0.0) * 0.32
                + float(source.get("relevance") or 0.0) * 0.22
            ) if source.get("exact_link") else 0.0
            return max(
                grade_signal + float(item.get("cue_support") or 0.0) * 0.32,
                source_signal + float(item.get("source_route_support") or 0.0) * 0.28,
            )

        for raw in rows:
            left = by_name.get(str(raw["source_memo"] or ""))
            right = by_name.get(str(raw["target_memo"] or ""))
            if left is None or right is None:
                continue
            diagnostics["pairs"] += 1
            left_signal, right_signal = signal(left), signal(right)
            if abs(left_signal - right_signal) < 0.16 or max(left_signal, right_signal) < 0.48:
                continue
            winner, loser = (left, right) if left_signal > right_signal else (right, left)
            gap = abs(left_signal - right_signal)
            confidence = _clamp(float(raw["edge_confidence"] or 0.0))
            strength = _clamp(float(raw["strength"] or 0.0))
            spread = min(0.12, 0.035 + gap * 0.07 + confidence * 0.025 + strength * 0.015)
            winner_name = str(winner["memo_name"])
            loser_name = str(loser["memo_name"])
            adjustments[winner_name] = min(0.08, adjustments[winner_name] + spread * 0.58)
            adjustments[loser_name] = max(-0.06, adjustments[loser_name] - spread * 0.42)
            diagnostics["decisions"].append({
                "winner": winner_name, "loser": loser_name,
                "edge_type": str(raw["edge_type"]), "signal_gap": round(gap, 4),
                "spread": round(spread, 4),
            })
        for memo_name, adjustment in adjustments.items():
            item = by_name[memo_name]
            item["contrastive_adjustment"] = round(adjustment, 6)
            item["access_score"] = _clamp(float(item.get("access_score") or 0.0) + adjustment)
        diagnostics["adjusted"] = len(adjustments)
        diagnostics["max_adjustment"] = round(max((abs(x) for x in adjustments.values()), default=0.0), 4)
        diagnostics["decisions"] = diagnostics["decisions"][:12]
        return diagnostics

    def disambiguate_same_day(self, query: str, memo_names: list[str], *,
                              gap_threshold: float = 0.15) -> dict[str, Any]:
        """Pick the intended memory when several share one date.

        Ordering: verbatim quote, then rare entity/object/action/result
        combinations, then retrieval-key overlap. When the top two are too close
        the result is reported as ambiguous rather than asserting one answer.
        """
        names = [str(item) for item in memo_names if str(item)]
        if len(names) <= 1:
            return {
                "temporal_scope_count": len(names),
                "strong_cue_count": 0,
                "disambiguation_score": 1.0 if names else 0.0,
                "score_gap_to_second": 1.0 if names else 0.0,
                "rescue_grade": None,
                "rescue_reasons": [],
                "ambiguous_same_day": False,
                "ranked": [{"memo_name": name, "score": 1.0, "reasons": []} for name in names],
            }
        query_lower = str(query or "").lower()
        query_terms = extract_terms(query_lower)
        scored: list[tuple[float, str, list[str], str]] = []
        for memo_name in names:
            _state, cue = self._candidate_state(memo_name)
            score = 0.0
            reasons: list[str] = []
            quotes = [str(item).lower() for item in cue.get("quotes") or []]
            matched_quote = next((q for q in quotes if len(q) >= 4 and q in query_lower), "")
            if matched_quote:
                score += 1.0
                reasons.append("quote:" + matched_quote[:24])
            entities = {str(item).lower() for item in cue.get("entities") or []}
            entity_hits = sorted(item for item in entities if item and item in query_lower)
            if entity_hits:
                score += 0.30 + 0.18 * (len(entity_hits) - 1)
                reasons.extend("entity:" + item for item in entity_hits[:3])
            retrieval_terms = set(cue.get("retrieval_terms") or [])
            overlap = _jaccard(query_terms, retrieval_terms)
            if overlap > 0:
                score += overlap * 0.60
                reasons.append(f"retrieval_overlap:{overlap:.2f}")
            grade = self._grade_for_hits(
                bool(matched_quote), True, len(entity_hits),
                len(query_terms & retrieval_terms),
            )
            scored.append((score, memo_name, reasons, grade))
        scored.sort(key=lambda item: (-item[0], item[1]))
        first = scored[0]
        second_score = scored[1][0] if len(scored) > 1 else 0.0
        gap = first[0] - second_score
        ambiguous = gap < float(gap_threshold)
        return {
            "temporal_scope_count": len(names),
            "strong_cue_count": sum(1 for item in scored if item[3] in ("A", "B")),
            "disambiguation_score": round(first[0], 4),
            "score_gap_to_second": round(gap, 4),
            "rescue_grade": None if ambiguous else first[3],
            "rescue_reasons": first[2][:6],
            "ambiguous_same_day": ambiguous,
            "ranked": [
                {"memo_name": name, "score": round(score, 4), "reasons": reasons, "grade": grade}
                for score, name, reasons, grade in scored[:6]
            ],
        }

    def evaluate(self, *, query: str, candidates: list[dict[str, Any]], selected_names: list[str],
                 request_id: str = "", config: dict[str, Any] | None = None,
                 psychological_bias: float = 0.0, record: bool = True) -> dict[str, Any]:
        config = config or {}
        request_id = request_id or uuid.uuid4().hex
        selected_set = {str(item) for item in selected_names if item}
        shadow = bool(config.get("shadow_mode", True))
        enabled = bool(config.get("enable", True))
        exact_relief = _clamp(float(config.get("exact_cue_relief", 0.85)))
        deep_rescue_enable = bool(config.get("deep_rescue_enable", True))
        interference_enable = bool(config.get("interference_enable", True))
        bias_limit = _clamp(float(config.get("psychological_bias_strength", 0.08)), 0.0, 0.20)
        bias = max(-bias_limit, min(bias_limit, float(psychological_bias or 0.0)))
        working_candidates = list(candidates)
        original_candidate_names = {
            str(item.get("memo_name") or item.get("name") or "").strip()
            for item in working_candidates
        }
        entity_rarity_min = _clamp(float(config.get("cue_grade_b_rarity_min", 0.30)), 0.05, 0.95)
        gap_threshold = _clamp(float(config.get("same_day_gap_threshold", 0.15)), 0.0, 1.0)
        classification = self.classify_query_cues(query, entity_rarity_min=entity_rarity_min)
        query_grade = str(classification.get("query_grade") or "D")
        temporal_scope = list(classification.get("temporal_scope") or [])
        date_only_query = bool(classification.get("date_only"))
        # A date shared by several memories is a browse scope, not a unique target.
        same_day_names = [
            name for name, entry in (classification.get("per_memo") or {}).items()
            if entry.get("dates")
        ]
        same_day = self.disambiguate_same_day(
            query, same_day_names, gap_threshold=gap_threshold,
        ) if len(same_day_names) > 1 else {
            "temporal_scope_count": len(same_day_names), "strong_cue_count": 0,
            "disambiguation_score": 0.0, "score_gap_to_second": 0.0,
            "rescue_grade": None, "rescue_reasons": [], "ambiguous_same_day": False,
            "ranked": [],
        }
        rescue_pool: list[dict[str, Any]] = []
        source_enriched_count = 0
        if enabled and deep_rescue_enable and bool(config.get("independent_cue_rescue", True)):
            cue_rescues = self._precise_rescue_candidates(
                query, original_candidate_names,
                limit=int(config.get("rescue_candidate_limit", 12)),
                entity_rarity_min=entity_rarity_min,
                same_day_gap_threshold=gap_threshold,
            )
            source_rescues = self._source_link_rescue_candidates(
                query, set(),
                limit=int(config.get("rescue_candidate_limit", 12)),
            ) if bool(config.get("source_link_rescue", True)) else []
            source_by_memo = {
                str(item.get("memo_name") or ""): item for item in source_rescues
                if str(item.get("memo_name") or "")
            }
            for index, existing in enumerate(working_candidates):
                memo_name = str(existing.get("memo_name") or existing.get("name") or "").strip()
                source_hit = source_by_memo.pop(memo_name, None)
                if source_hit is None:
                    continue
                merged = dict(existing)
                for key in ("_source_turn_hits", "_route_evidence"):
                    values = list(merged.get(key) or [])
                    for value in source_hit.get(key) or []:
                        if value not in values:
                            values.append(value)
                    merged[key] = values
                merged["_access_source_enriched"] = True
                merged["_access_rescue_grade"] = str(
                    source_hit.get("_access_rescue_grade") or merged.get("_access_rescue_grade") or "D"
                )
                merged["_access_rescue_reasons"] = list(dict.fromkeys(
                    list(merged.get("_access_rescue_reasons") or [])
                    + list(source_hit.get("_access_rescue_reasons") or [])
                ))[:8]
                working_candidates[index] = merged
                source_enriched_count += 1
            source_rescues = list(source_by_memo.values())
            rescue_by_memo: dict[str, dict[str, Any]] = {}
            for rescue in cue_rescues + source_rescues:
                memo_name = str(rescue.get("memo_name") or "")
                current = rescue_by_memo.get(memo_name)
                if current is None or self._base_score(rescue) > self._base_score(current):
                    rescue_by_memo[memo_name] = rescue
            rescue_pool = list(rescue_by_memo.values())
            working_candidates.extend(rescue_pool)
        per_memo_grades = {
            name: str(entry.get("grade") or "D")
            for name, entry in (classification.get("per_memo") or {}).items()
        }
        items: list[dict[str, Any]] = []
        for rank, hit in enumerate(working_candidates):
            memo_name = str(hit.get("memo_name") or hit.get("name") or "").strip()
            if not memo_name:
                continue
            state, cue = self._candidate_state(memo_name)
            base = self._base_score(hit)
            cue_support, cue_detail = self._cue_support(query, cue)
            route_support = self._route_support(hit)
            source_route_support, source_route_detail = self._source_route_support(hit)
            route_support = max(route_support, source_route_support)
            access = _clamp(float(state.get("accessibility") or 0.5))
            interference = (
                _clamp(float(state.get("interference_load") or 0), 0.0, 0.35)
                if interference_enable else 0.0
            )
            cue_grade = str(
                hit.get("_access_rescue_grade")
                or per_memo_grades.get(memo_name, "D")
            )
            # test2: only grade A/B (or a clearly separated C) is an exact cue.
            # A bare date stays a temporal scope so "that day" cannot rescue every
            # diary written that day.
            exact_cue = cue_grade in ("A", "B")
            if (
                source_route_detail.get("exact_link")
                and source_route_support >= 0.76
                and (
                    float(source_route_detail.get("lexical") or 0.0) >= 0.20
                    or str(hit.get("_access_rescue_source") or "") == "source_turn_index"
                )
            ):
                # Exact archived turn ownership is first-hand evidence, not a
                # same-batch inference. The lexical rescue itself already
                # requires two rare terms, so broad queries do not qualify.
                exact_cue = True
            if not exact_cue and cue_grade == "C" and not same_day.get("ambiguous_same_day"):
                exact_cue = bool(cue_detail["quote_hits"]) or len(cue_detail["entity_hits"]) >= 1
            ambiguous_same_day = bool(
                same_day.get("ambiguous_same_day") and memo_name in same_day_names
            )
            if ambiguous_same_day:
                exact_cue = exact_cue and cue_grade == "A"
            cost = (1.0 - access) * 0.16 + interference * 0.30
            if exact_cue:
                cost *= 1.0 - exact_relief
            access_score = _clamp(
                base + cue_support * 0.16 + route_support * 0.06
                + source_route_support * 0.06 + bias - cost
            )
            access_state = str(state.get("access_state") or "vivid")
            if exact_cue:
                presentation = "full_or_evidence"
                rescued = access_state == "deep" and deep_rescue_enable
            elif access_state == "vivid":
                presentation = "normal"
                rescued = False
            elif access_state == "latent":
                presentation = "passage_or_card"
                rescued = False
            else:
                presentation = "cue_only"
                rescued = False
            item = {
                "memo_name": memo_name, "rank": rank + 1, "selected": memo_name in selected_set,
                "access_state": access_state, "base_score": base, "access_score": access_score,
                "accessibility": access, "cue_support": cue_support, "route_support": route_support,
                "source_route_support": source_route_support,
                "source_route": source_route_detail,
                "interference_load": interference, "exact_cue": bool(exact_cue),
                "cue_grade": cue_grade, "temporal_scope_only": bool(
                    cue_grade == "D" and cue_detail["date_hits"]
                ),
                "ambiguous_same_day": ambiguous_same_day,
                "rescued": rescued, "presentation": presentation, "cue_detail": cue_detail,
                "candidate_source": str(hit.get("_access_rescue_source") or "retrieval_pool"),
                "rescue_reasons": list(hit.get("_access_rescue_reasons") or []),
                "source_turn_hits": [
                    {
                        "batch_id": str(value.get("batch_id") or ""),
                        "turn_index": int(value.get("turn_index") or 0),
                        "role": str(value.get("role") or ""),
                        "content": str(value.get("content") or "")[:720],
                        "relevance": float(value.get("relevance") or 0.0),
                        "lexical": float(value.get("lexical") or 0.0),
                        "exact_link": bool(value.get("exact_link")),
                    }
                    for value in (hit.get("_source_turn_hits") or [])[:2]
                    if isinstance(value, dict)
                ],
            }
            items.append(item)
        contrastive = self._apply_contrastive_ranking(
            items,
            date_only_query=date_only_query,
            enabled=bool(config.get("contrastive_ranking", True)),
        )
        ranked = sorted(items, key=lambda item: (item["access_score"], item["base_score"], -item["rank"]), reverse=True)
        original = [item["memo_name"] for item in items if item["selected"]]
        target_count = max(1, len(original)) if original else min(3, len(ranked))
        recommended = [item["memo_name"] for item in ranked[:target_count]]
        changed = [name for name in recommended if name not in selected_set]
        result = {
            "enabled": enabled, "shadow": shadow, "request_id": request_id,
            "candidate_count": len(items), "selected_count": len(original),
            "retrieval_candidate_count": len(working_candidates) - len(rescue_pool),
            "rescue_pool_count": len(rescue_pool),
            "source_enriched_count": source_enriched_count,
            "state_counts": dict(Counter(item["access_state"] for item in items)),
            "exact_cue_count": sum(int(item["exact_cue"]) for item in items),
            "deep_rescue_count": sum(int(item["rescued"]) for item in items),
            "query_grade": query_grade,
            "date_only_query": date_only_query,
            "temporal_scope": temporal_scope,
            "grade_counts": dict(Counter(item["cue_grade"] for item in items)),
            "same_day": same_day,
            "source_rescue_count": sum(
                int(item.get("candidate_source") == "source_turn_index") for item in ranked
            ),
            "exact_source_count": sum(
                int(bool((item.get("source_route") or {}).get("exact_link"))) for item in ranked
            ),
            "contrastive": contrastive,
            "query": str(query)[:max(200, min(8000, int(config.get("observation_query_max_chars", 2000))))],
            "original": original, "recommended": recommended, "changed": changed,
            "would_change": recommended != original[:target_count], "items": ranked,
        }
        if record and enabled:
            self._record_evaluation(
                result,
                observation_keep=max(200, min(50000, int(config.get("observation_keep", 5000)))),
            )
        return result

    @staticmethod
    def _observation_item(item: dict[str, Any]) -> dict[str, Any]:
        """Keep score evidence without duplicating archived conversation text."""
        source_route = item.get("source_route") or {}
        return {
            "memo_name": str(item.get("memo_name") or ""),
            "rank": int(item.get("rank") or 0),
            "selected": bool(item.get("selected")),
            "recommended": False,
            "access_state": str(item.get("access_state") or ""),
            "base_score": round(float(item.get("base_score") or 0), 6),
            "access_score": round(float(item.get("access_score") or 0), 6),
            "accessibility": round(float(item.get("accessibility") or 0), 6),
            "cue_support": round(float(item.get("cue_support") or 0), 6),
            "route_support": round(float(item.get("route_support") or 0), 6),
            "source_route_support": round(float(item.get("source_route_support") or 0), 6),
            "interference_load": round(float(item.get("interference_load") or 0), 6),
            "exact_cue": bool(item.get("exact_cue")),
            "cue_grade": str(item.get("cue_grade") or "D"),
            "rescued": bool(item.get("rescued")),
            "presentation": str(item.get("presentation") or ""),
            "candidate_source": str(item.get("candidate_source") or "retrieval_pool"),
            "rescue_reasons": list(item.get("rescue_reasons") or [])[:8],
            "source_exact_link": bool(source_route.get("exact_link")),
        }

    def _record_evaluation(self, evaluation: dict[str, Any], *, observation_keep: int = 5000) -> None:
        now = time.time()
        with self._lock:
            conn = self._get_conn()
            for item in evaluation.get("items") or []:
                conn.execute(
                    """INSERT INTO memory_access_events(
                       request_id,memo_name,event_kind,access_state,base_score,access_score,
                       cue_support,route_support,selected,shadow,detail_json,created_ts
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (evaluation["request_id"], item["memo_name"], "shadow_evaluation",
                     item["access_state"], item["base_score"], item["access_score"],
                     item["cue_support"], item["route_support"], int(item["selected"]),
                     int(bool(evaluation.get("shadow"))), _json({
                         "exact_cue": item["exact_cue"], "rescued": item["rescued"],
                         "presentation": item["presentation"], "cue_detail": item["cue_detail"],
                         "candidate_source": item.get("candidate_source", "retrieval_pool"),
                         "rescue_reasons": item.get("rescue_reasons") or [],
                         "cue_grade": item.get("cue_grade", "D"),
                         "temporal_scope_only": bool(item.get("temporal_scope_only")),
                         "ambiguous_same_day": bool(item.get("ambiguous_same_day")),
                     }), now),
                )
                if item["selected"]:
                    conn.execute(
                        "UPDATE memory_access_state SET retrieval_count=retrieval_count+1,last_retrieved_ts=? WHERE memo_name=?",
                        (now, item["memo_name"]),
                    )
            baseline = [str(item) for item in evaluation.get("original") or [] if str(item)]
            recommended = [str(item) for item in evaluation.get("recommended") or [] if str(item)]
            baseline_set = set(baseline)
            recommended_set = set(recommended)
            compact_items = [self._observation_item(item) for item in evaluation.get("items") or []]
            for item in compact_items:
                item["recommended"] = item["memo_name"] in recommended_set
            query = str(evaluation.get("query") or "")
            detail = {
                "state_counts": evaluation.get("state_counts") or {},
                "grade_counts": evaluation.get("grade_counts") or {},
                "same_day": evaluation.get("same_day") or {},
                "contrastive": evaluation.get("contrastive") or {},
                "temporal_scope": evaluation.get("temporal_scope") or [],
                "date_only_query": bool(evaluation.get("date_only_query")),
                "retrieval_candidate_count": int(evaluation.get("retrieval_candidate_count") or 0),
                "source_enriched_count": int(evaluation.get("source_enriched_count") or 0),
                "items": compact_items[:80],
            }
            conn.execute(
                """INSERT INTO memory_access_observations(
                   request_id,query_text,query_hash,shadow,query_grade,candidate_count,
                   selected_count,baseline_json,recommended_json,added_json,removed_json,
                   would_change,rescue_pool_count,deep_rescue_count,source_rescue_count,
                   exact_source_count,detail_json,created_ts,updated_ts
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(request_id) DO UPDATE SET
                     query_text=excluded.query_text,query_hash=excluded.query_hash,
                     shadow=excluded.shadow,query_grade=excluded.query_grade,
                     candidate_count=excluded.candidate_count,
                     selected_count=excluded.selected_count,
                     baseline_json=excluded.baseline_json,
                     recommended_json=excluded.recommended_json,
                     added_json=excluded.added_json,removed_json=excluded.removed_json,
                     would_change=excluded.would_change,
                     rescue_pool_count=excluded.rescue_pool_count,
                     deep_rescue_count=excluded.deep_rescue_count,
                     source_rescue_count=excluded.source_rescue_count,
                     exact_source_count=excluded.exact_source_count,
                     detail_json=excluded.detail_json,updated_ts=excluded.updated_ts""",
                (
                    str(evaluation.get("request_id") or ""), query,
                    hashlib.sha256(query.encode("utf-8", "ignore")).hexdigest(),
                    int(bool(evaluation.get("shadow"))), str(evaluation.get("query_grade") or "D"),
                    int(evaluation.get("candidate_count") or 0), len(baseline),
                    _json(baseline), _json(recommended),
                    _json([name for name in recommended if name not in baseline_set]),
                    _json([name for name in baseline if name not in recommended_set]),
                    int(bool(evaluation.get("would_change"))),
                    int(evaluation.get("rescue_pool_count") or 0),
                    int(evaluation.get("deep_rescue_count") or 0),
                    int(evaluation.get("source_rescue_count") or 0),
                    int(evaluation.get("exact_source_count") or 0),
                    _json(detail), now, now,
                ),
            )
            keep = max(200, min(50000, int(observation_keep)))
            conn.execute(
                """DELETE FROM memory_access_observations
                   WHERE feedback_verdict=''
                     AND request_id NOT IN (
                       SELECT request_id FROM memory_access_observations
                       ORDER BY created_ts DESC LIMIT ?
                     )""",
                (keep,),
            )
            conn.commit()

    def record_response_use(self, *, request_id: str, response_text: str,
                            memo_names: list[str], shadow: bool = True,
                            reconsolidate: bool = True, event_keep: int = 4000) -> dict[str, Any]:
        response_terms = extract_terms(response_text)
        now = time.time()
        used = 0
        use_rows: list[dict[str, Any]] = []
        with self._lock:
            conn = self._get_conn()
            for memo_name in dict.fromkeys(str(item) for item in memo_names if item):
                row = conn.execute("SELECT signature_json FROM memory_cue_signatures WHERE memo_name=?", (memo_name,)).fetchone()
                signature = _loads(row["signature_json"], {}) if row else {}
                memory_terms = set(signature.get("retrieval_terms") or []) | set(signature.get("entities") or []) | set(signature.get("content_terms") or [])
                support = _jaccard(response_terms, memory_terms)
                is_used = support >= 0.035 or bool(set(signature.get("entities") or []) & response_terms)
                used += int(is_used)
                use_rows.append({
                    "memo_name": memo_name,
                    "support": round(float(support), 6),
                    "used": bool(is_used),
                })
                conn.execute(
                    """INSERT INTO memory_access_events(request_id,memo_name,event_kind,response_support,
                       selected,shadow,detail_json,created_ts) VALUES(?,?,?,?,?,?,?,?)""",
                    (request_id, memo_name, "response_use", support, 1, int(shadow),
                     _json({"used": is_used, "reconsolidation_applied": bool(is_used and reconsolidate and not shadow)}), now),
                )
                if is_used and reconsolidate and not shadow:
                    conn.execute(
                        """UPDATE memory_access_state SET successful_use_count=successful_use_count+1,
                           reconsolidation_count=reconsolidation_count+1,last_used_ts=?,
                           last_reconsolidated_ts=?,accessibility=MIN(1.0,accessibility+0.035),
                           vividness=MIN(1.0,vividness+0.05),updated_ts=? WHERE memo_name=?""",
                        (now, now, now, memo_name),
                    )
            memory_count = len(set(str(item) for item in memo_names if item))
            use_rate = used / max(1, memory_count) if memory_count else 0.0
            observation = conn.execute(
                "SELECT detail_json FROM memory_access_observations WHERE request_id=?",
                (str(request_id),),
            ).fetchone()
            if observation is not None:
                observation_detail = _loads(observation["detail_json"], {})
                observation_detail["response_use"] = use_rows
                conn.execute(
                    """UPDATE memory_access_observations
                       SET response_memories=?,response_used=?,response_use_rate=?,
                           detail_json=?,responded_ts=?,updated_ts=?
                       WHERE request_id=?""",
                    (memory_count, used, use_rate, _json(observation_detail), now, now, str(request_id)),
                )
            keep = max(200, min(50000, int(event_keep)))
            conn.execute(
                "DELETE FROM memory_access_events WHERE id NOT IN (SELECT id FROM memory_access_events ORDER BY id DESC LIMIT ?)",
                (keep,),
            )
            conn.commit()
        return {
            "memories": len(set(memo_names)), "used": used,
            "use_rate": round(used / max(1, len(set(memo_names))), 4) if memo_names else 0.0,
            "shadow": shadow,
        }

    # ── evaluation suite (test2) ──────────────────────────────────────────────

    def generate_eval_cases(self, *, replace: bool = False) -> dict[str, Any]:
        """Build source-grounded and structure-grounded evaluation cases."""
        now = time.time()
        conn = self._get_conn()
        rows = conn.execute(
            """SELECT e.memo_name,e.occurred_at,e.event_ts,e.memory_type,e.importance,
                      e.scene_anchor,e.retrieval_key,e.unresolved_json,e.source_batch_id,
                      e.evidence_quality,a.age_reference_kind,a.state_reason_json,c.signature_json,
                      EXISTS(SELECT 1 FROM episode_turn_links l
                             WHERE l.episode_id=e.episode_id) AS has_source_link
               FROM episodes e LEFT JOIN memory_cue_signatures c ON c.memo_name=e.memo_name
               LEFT JOIN memory_access_state a ON a.memo_name=e.memo_name
               WHERE e.active=1"""
        ).fetchall()
        records = [dict(row) for row in rows]
        row_by_memo = {str(row["memo_name"]): row for row in records}
        signatures = {
            str(row["memo_name"]): _loads(row.get("signature_json"), {}) for row in records
        }
        term_members: dict[str, set[str]] = defaultdict(set)
        quote_members: dict[str, set[str]] = defaultdict(set)
        date_entity_members: dict[tuple[str, str], set[str]] = defaultdict(set)
        by_date: dict[str, list[str]] = defaultdict(list)
        for memo_name, signature in signatures.items():
            terms = set(str(x).strip() for x in (signature.get("retrieval_terms") or []))
            terms.update(str(x).strip() for x in (signature.get("content_terms") or []))
            terms.update(str(x).strip() for x in (signature.get("entities") or []))
            for term in terms:
                if term:
                    term_members[term.lower()].add(memo_name)
            for quote in signature.get("quotes") or []:
                normalized = " ".join(str(quote).lower().split())
                if normalized:
                    quote_members[normalized].add(memo_name)
            dates = [str(x) for x in (signature.get("dates") or []) if len(str(x)) == 10]
            entities = [str(x).strip() for x in (signature.get("entities") or []) if str(x).strip()]
            for date_value in dates:
                by_date[date_value].append(memo_name)
                for entity in entities:
                    date_entity_members[(date_value, entity.lower())].add(memo_name)

        source_rows = conn.execute(
            """SELECT e.memo_name,l.turn_index,l.evidence_index,s.role,s.content
               FROM episode_turn_links l
               JOIN episodes e ON e.episode_id=l.episode_id
               JOIN source_turns s ON s.batch_id=l.batch_id AND s.turn_index=l.turn_index
               WHERE e.active=1 ORDER BY e.memo_name,l.turn_index"""
        ).fetchall()
        source_turns: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for raw in source_rows:
            source_turns[str(raw["memo_name"])].append(dict(raw))
        source_term_members: dict[str, set[str]] = defaultdict(set)
        for memo_name, linked_rows in source_turns.items():
            for linked_row in linked_rows:
                for term in extract_terms(str(linked_row.get("content") or "")):
                    source_term_members[term].add(memo_name)
        evidence_quote_rows = conn.execute(
            """SELECT e.memo_name,ev.quote_text,ev.grounded
               FROM episode_evidence ev JOIN episodes e ON e.episode_id=ev.episode_id
               WHERE e.active=1 AND TRIM(ev.quote_text)<>''"""
        ).fetchall()
        grounded_quotes: dict[str, set[str]] = defaultdict(set)
        for raw in evidence_quote_rows:
            if int(raw["grounded"] or 0):
                grounded_quotes[str(raw["memo_name"])].add(str(raw["quote_text"]).strip())

        negative_map: dict[str, set[str]] = defaultdict(set)
        for raw in conn.execute(
            """SELECT source_memo,target_memo,edge_type,edge_confidence
               FROM memory_interference_edges
               WHERE edge_type IN ('same_theme_distinct_event','contradictory_stage','probable_duplicate')"""
        ).fetchall():
            if float(raw["edge_confidence"] or 0) < 0.55:
                continue
            left, right = str(raw["source_memo"]), str(raw["target_memo"])
            negative_map[left].add(right)
            negative_map[right].add(left)

        cases: list[dict[str, Any]] = []

        def add_case(row: dict[str, Any], case_type: str, query: str, grade: str,
                     level: str, confidence: float, *, negatives: list[str] | None = None,
                     notes: dict[str, Any] | None = None) -> None:
            query = " ".join(str(query or "").split()).strip()
            if not query:
                return
            cases.append({
                "memo_name": str(row.get("memo_name") or ""),
                "case_type": case_type,
                "query": query[:180],
                "expected_grade": grade,
                "supervision_level": level,
                "supervision_confidence": round(_clamp(confidence), 4),
                "auto_verified": int(level in {"source_verified", "structural_verified"}),
                "negative_memos": sorted(set(negatives or []))[:12],
                "supervision": dict(notes or {}),
            })

        for row in records:
            memo_name = str(row["memo_name"])
            signature = signatures.get(memo_name, {})
            dates = [str(item) for item in signature.get("dates") or []]
            entities = [str(item) for item in signature.get("entities") or []]
            quotes = [str(item) for item in signature.get("quotes") or []]
            retrieval_terms = [str(item).strip() for item in signature.get("retrieval_terms") or []]
            memory_type = str(row.get("memory_type") or "event").lower()
            full_dates = [item for item in dates if len(item) == 10]
            linked = source_turns.get(memo_name, [])
            user_turn = next((x for x in linked if str(x.get("role") or "").lower() == "user"), None)
            source_content = str((user_turn or (linked[0] if linked else {})).get("content") or "")
            unique_terms = sorted(
                (term for term in dict.fromkeys(entities + retrieval_terms)
                 if 2 <= len(term) <= 20 and len(term_members.get(term.lower(), set())) == 1),
                key=lambda term: (-len(term), term),
            )
            negatives = list(negative_map.get(memo_name, set()))

            if linked and source_content:
                clauses = [" ".join(part.split()).strip(" ，,。.!！?？;；:：")
                           for part in re.split(r"[\r\n。！？!?；;]+", source_content)]
                clauses = [part for part in clauses if 6 <= len(part) <= 56]
                if not clauses:
                    compact = " ".join(source_content.split()).strip()
                    clauses = [compact[:56]] if len(compact) >= 6 else []
                if clauses:
                    def fragment_score(fragment: str) -> tuple[int, int, int]:
                        terms = extract_terms(fragment)
                        unique_count = sum(1 for term in terms
                                           if len(source_term_members.get(term, set())) == 1)
                        return unique_count, min(len(fragment), 36), -abs(len(fragment) - 24)

                    fragment = max(clauses, key=fragment_score)[:48]
                    add_case(row, "source_turn_holdout",
                             f"你还记得我当时说过“{fragment}”吗？",
                             "A", "source_verified", 0.99,
                             negatives=(
                                 [other for date_value in full_dates
                                  for other in by_date.get(date_value, [])
                                  if other != memo_name]
                                 + negatives
                             ),
                             notes={"basis": "episode_turn_link",
                                    "turn_index": int(linked[0]["turn_index"]),
                                    "source_role": str((user_turn or linked[0]).get("role") or ""),
                                    "fragment_chars": len(fragment)})

            unique_quote = next((q for q in quotes
                                 if len(quote_members.get(" ".join(q.lower().split()), set())) == 1), "")
            if unique_quote:
                linked_text = "\n".join(str(x.get("content") or "") for x in linked)
                quote_in_source = bool(linked and unique_quote in linked_text)
                quote_grounded = unique_quote in grounded_quotes.get(memo_name, set())
                level = "source_verified" if quote_in_source and quote_grounded else "structural_verified"
                add_case(row, "verbatim_quote", f"你说过“{unique_quote[:36]}”，那次是怎么回事？",
                         "A", level, 0.99 if level == "source_verified" else 0.94, negatives=negatives,
                         notes={"basis": "unique_quote", "quote_in_source": quote_in_source,
                                "quote_grounded": quote_grounded})

            unique_pair = next(((date_value, entity) for date_value in full_dates for entity in entities
                                if len(date_entity_members.get((date_value, entity.lower()), set())) == 1), None)
            if unique_pair:
                linked_text = "\n".join(str(x.get("content") or "") for x in linked).lower()
                pair_in_source = bool(linked and unique_pair[1].lower() in linked_text)
                level = "source_verified" if pair_in_source else "structural_verified"
                add_case(row, "date_plus_entity",
                         f"{unique_pair[0]} 那天关于{unique_pair[1]}的事情后来怎么样了？",
                         "B", level, 0.97 if linked else 0.92,
                         negatives=[x for x in by_date.get(unique_pair[0], []) if x != memo_name] + negatives,
                         notes={"basis": "unique_date_entity", "source_linked": bool(linked),
                                "entity_in_source": pair_in_source})

            if len(unique_terms) >= 2:
                add_case(row, "unique_cue_holdout",
                         "我提到“" + "、".join(unique_terms[:3]) + "”的那段经历是什么？",
                         "B", "structural_verified", min(0.95, 0.86 + 0.03 * len(unique_terms[:3])),
                         negatives=negatives,
                         notes={"basis": "corpus_unique_cue_combo", "cue_count": len(unique_terms[:3])})

            if full_dates:
                add_case(row, "date_only_browse", f"{full_dates[0]} 那天发生了什么？", "D",
                         "heuristic", 0.45,
                         notes={"basis": "date_scope_only", "diagnostic_only": True})
            if memory_type in _DURABLE_TYPES or json_list(row.get("unresolved_json")):
                cue = "、".join(unique_terms[:3]) or str(row.get("retrieval_key") or row.get("scene_anchor") or memo_name)
                add_case(row, "protected_memory", cue, "C", "structural_verified",
                         0.90 if unique_terms else 0.70, negatives=negatives,
                         notes={"basis": "durable_type", "unique_cues": len(unique_terms)})
            if (memory_type not in _DURABLE_TYPES and not quotes
                    and int(row.get("importance") or 3) <= 3):
                add_case(row, "plain_daily", str(row.get("scene_anchor") or memo_name), "D",
                         "heuristic", 0.50,
                         notes={"basis": "low_distinctiveness_daily", "diagnostic_only": True})
            reason_data = _loads(row.get("state_reason_json"), {})
            if str(reason_data.get("eligibility_status") or "") == "deferred":
                add_case(row, "evidence_deferred_legacy",
                         str(row.get("retrieval_key") or row.get("scene_anchor") or memo_name),
                         "C", "heuristic", 0.40,
                         notes={"diagnostic_only": True,
                                "clock": str(row.get("age_reference_kind") or "unknown"),
                                "evidence_quality": str(row.get("evidence_quality") or "diary_derived"),
                                "has_source_link": bool(row.get("has_source_link"))})

        for date_value, names in by_date.items():
            if len(set(names)) <= 1:
                continue
            generated_for_date = False
            for memo_name in sorted(set(names))[:6]:
                row = row_by_memo.get(memo_name)
                signature = signatures.get(memo_name, {})
                cues = [str(x) for x in (signature.get("entities") or [])
                        if len(term_members.get(str(x).lower(), set())) == 1]
                if not row or not cues:
                    continue
                add_case(row, "same_day_multi", f"{date_value} 那天关于{cues[0]}的事情是什么？",
                         "B", "structural_verified", 0.93,
                         negatives=[x for x in names if x != memo_name],
                         notes={"basis": "same_day_unique_cue", "same_day_members": sorted(set(names))[:12]})
                generated_for_date = True
            if not generated_for_date:
                row = row_by_memo.get(sorted(set(names))[0])
                if row:
                    add_case(row, "same_day_multi", f"{date_value} 那天发生了什么？",
                             "D", "heuristic", 0.35,
                             negatives=[x for x in names if x != str(row["memo_name"])],
                             notes={"basis": "ambiguous_date_scope", "diagnostic_only": True,
                                    "same_day_members": sorted(set(names))[:12]})

        add_case({"memo_name": ""}, "unrelated_control", "今天天气还不错，你在做什么呢？",
                 "D", "heuristic", 0.35,
                 notes={"diagnostic_only": True, "basis": "generic_unrelated_control"})
        with self._lock:
            if replace:
                conn.execute("DELETE FROM memory_access_eval_cases")
            written = 0
            generated_ids: list[str] = []
            for case in cases:
                payload = "\u241f".join([str(case.get("case_type")), str(case.get("memo_name"))])
                case_id = "ec-" + hashlib.sha1(payload.encode("utf-8", "ignore")).hexdigest()[:16]
                generated_ids.append(case_id)
                conn.execute(
                    """INSERT INTO memory_access_eval_cases(
                       case_id,memo_name,query,expected_grade,case_type,enabled,notes,created_ts,updated_ts,
                       supervision_level,supervision_confidence,supervision_json,negative_memos_json,auto_verified)
                       VALUES(?,?,?,?,?,1,?,?,?,?,?,?,?,?)
                       ON CONFLICT(case_id) DO UPDATE SET
                       query=excluded.query,expected_grade=excluded.expected_grade,notes=excluded.notes,
                       supervision_level=CASE WHEN memory_access_eval_cases.confirmed_by<>'' THEN 'human' ELSE excluded.supervision_level END,
                       supervision_confidence=CASE WHEN memory_access_eval_cases.confirmed_by<>'' THEN 1.0 ELSE excluded.supervision_confidence END,
                       supervision_json=excluded.supervision_json,
                       negative_memos_json=excluded.negative_memos_json,
                       auto_verified=excluded.auto_verified,updated_ts=excluded.updated_ts""",
                    (case_id, str(case.get("memo_name") or ""), str(case.get("query") or ""),
                     str(case.get("expected_grade") or "D"), str(case.get("case_type") or "general"),
                     _json(case.get("supervision") or {}), now, now,
                     str(case.get("supervision_level") or "heuristic"),
                     float(case.get("supervision_confidence") or 0),
                     _json(case.get("supervision") or {}), _json(case.get("negative_memos") or []),
                     int(case.get("auto_verified") or 0)),
                )
                written += 1
            # Remove obsolete machine cases while preserving human corrections
            # and enabled/disabled choices on cases that still exist.
            if generated_ids:
                conn.execute(
                    "CREATE TEMP TABLE IF NOT EXISTS memory_access_generated_eval_ids(case_id TEXT PRIMARY KEY)"
                )
                conn.execute("DELETE FROM memory_access_generated_eval_ids")
                conn.executemany(
                    "INSERT OR IGNORE INTO memory_access_generated_eval_ids(case_id) VALUES(?)",
                    ((case_id,) for case_id in generated_ids),
                )
                conn.execute(
                    """DELETE FROM memory_access_eval_cases
                       WHERE (confirmed_by='' OR confirmed_by IS NULL)
                         AND NOT EXISTS (
                           SELECT 1 FROM memory_access_generated_eval_ids g
                           WHERE g.case_id=memory_access_eval_cases.case_id
                         )"""
                )
                conn.execute("DROP TABLE memory_access_generated_eval_ids")
            conn.commit()
        levels = Counter(str(c.get("supervision_level") or "heuristic") for c in cases)
        return {"generated": written, "case_types": dict(Counter(c["case_type"] for c in cases)),
                "supervision_levels": dict(levels),
                "strict_eligible": int(levels.get("source_verified", 0)),
                "calibration_eligible": int(levels.get("source_verified", 0)
                                            + levels.get("structural_verified", 0))}

    def list_eval_cases(self, *, case_type: str = "", enabled_only: bool = True,
                        limit: int = 500) -> list[dict[str, Any]]:
        clauses, params = ["1=1"], []
        if case_type:
            clauses.append("case_type=?")
            params.append(case_type)
        if enabled_only:
            clauses.append("enabled=1")
        rows = self._get_conn().execute(
            f"""SELECT * FROM memory_access_eval_cases WHERE {' AND '.join(clauses)}
                ORDER BY case_type,case_id LIMIT ?""",
            (*params, max(1, min(50000, int(limit)))),
        ).fetchall()
        return [dict(row) for row in rows]

    def set_eval_case_enabled(self, case_id: str, enabled: bool) -> bool:
        with self._lock:
            conn = self._get_conn()
            cursor = conn.execute(
                "UPDATE memory_access_eval_cases SET enabled=?,updated_ts=? WHERE case_id=?",
                (int(bool(enabled)), time.time(), str(case_id)),
            )
            conn.commit()
        return cursor.rowcount > 0

    def confirm_eval_case(self, case_id: str, *, expected_memo: str = "",
                          confirmed_by: str = "user") -> bool:
        """Human-confirm the expected target memo for a case. Only confirmed
        cases participate in gate calculations when ``recalled_only`` is used."""
        expected_memo = str(expected_memo or "").strip()
        confirmed_by = str(confirmed_by or "").strip()
        if not expected_memo or not confirmed_by:
            return False
        with self._lock:
            conn = self._get_conn()
            cursor = conn.execute(
                """UPDATE memory_access_eval_cases
                   SET expected_memo=?,confirmed_by=?,supervision_level='human',
                       supervision_confidence=1.0,updated_ts=?
                   WHERE case_id=?""",
                (expected_memo, confirmed_by, time.time(), str(case_id)),
            )
            conn.commit()
        return cursor.rowcount > 0

    def run_eval(self, *, config: dict[str, Any] | None = None,
                 case_type: str = "", baseline_candidates: dict[str, list[dict[str, Any]]] | None = None,
                 recall_fn: Callable[..., Any] | None = None,
                 recalled_only: bool = False,
                 scope: str = "calibration",
                 ) -> dict[str, Any]:
        """Run the case set through the Shadow evaluator and record results.

        When ``recall_fn`` is provided it must be a callable accepting
        ``(query, top_k)`` and returning ``(candidates, selected_names)`` —
        typically a wrapper around the plugin's live recall pipeline.  This lets
        the gates compare the access layer's suggestions against the real 4.6
        baseline, rather than an empty pool.
        """
        config = dict(config or {})
        config["shadow_mode"] = True
        started = time.time()
        run_id = "er-" + uuid.uuid4().hex[:16]
        scope = str(scope or "calibration").strip().lower()
        if recalled_only:
            scope = "strict"
        if scope not in {"strict", "calibration", "all"}:
            scope = "calibration"
        # Strict validation accepts human corrections and source-linked targets.
        # Structural cases calibrate ranking but can never unlock takeover alone.
        cases = self.list_eval_cases(case_type=case_type, enabled_only=True, limit=50000)
        if scope == "strict":
            cases = [c for c in cases if self._eval_case_trust(c) == "strict"]
        elif scope == "calibration":
            cases = [c for c in cases if self._eval_case_trust(c) in {"strict", "calibration"}]
        available_cases = len(cases)
        sample_limit = max(20, min(500, int(config.get("auto_eval_case_limit", 80))))
        rotation_row = self._get_conn().execute(
            "SELECT COUNT(*) AS n FROM memory_access_eval_runs"
        ).fetchone()
        rotation = int(rotation_row["n"] or 0) if rotation_row else 0
        if scope == "calibration" and len(cases) > sample_limit:
            cases = self._stratified_eval_sample(cases, sample_limit, rotation)
        baseline_candidates = baseline_candidates or {}
        results: list[dict[str, Any]] = []
        latencies: list[float] = []
        for case in cases:
            case_id = str(case["case_id"])
            expected_memo = str(case.get("expected_memo") or case.get("memo_name") or "")
            expected_grade = str(case.get("expected_grade") or "D")
            supervision_level = "human" if str(case.get("confirmed_by") or "").strip() else str(
                case.get("supervision_level") or "heuristic"
            )
            supervision_confidence = float(case.get("supervision_confidence") or 0)
            negative_memos = [str(x) for x in json_list(case.get("negative_memos_json")) if str(x)]
            used_real_recall = False
            baseline_rank = -1
            recall_error = ""
            # Try live recall first, then fall back to pre-supplied candidates
            if recall_fn is not None:
                try:
                    fn_result = recall_fn(str(case.get("query") or ""),
                                           int(config.get("rescue_candidate_limit", 12)))
                    # A synchronous WebUI wrapper is preferred. Supporting an
                    # awaitable here is safe only when this thread owns no running
                    # event loop; blocking the active AstrBot loop would deadlock.
                    if hasattr(fn_result, "__await__"):
                        import asyncio as _aio
                        try:
                            _aio.get_running_loop()
                        except RuntimeError:
                            fn_result = _aio.run(fn_result)
                        else:
                            close = getattr(fn_result, "close", None)
                            if callable(close):
                                close()
                            raise RuntimeError(
                                "async recall_fn cannot be awaited from a synchronous evaluation on the active loop"
                            )
                    if isinstance(fn_result, tuple) and len(fn_result) == 2:
                        candidates, selected = fn_result
                    elif isinstance(fn_result, dict):
                        candidates = list(fn_result.get("hits") or [])
                        selected = list(fn_result.get("selected") or
                                        [str(h.get("memo_name")) for h in candidates if h.get("selected")])
                    else:
                        candidates, selected = list(fn_result), []
                    candidates = list(candidates)
                    selected = list(selected)
                    used_real_recall = True
                    # compute baseline rank from the 4.6 selected list
                    if expected_memo and expected_memo in selected:
                        baseline_rank = selected.index(expected_memo) + 1
                    elif expected_memo:
                        cand_names = [str(c.get("memo_name") or c.get("name") or "") for c in candidates]
                        if expected_memo in cand_names:
                            baseline_rank = cand_names.index(expected_memo) + 1
                except Exception as exc:
                    recall_error = str(exc)[:300]
                    candidates = baseline_candidates.get(case_id, [])
                    selected = []
            else:
                candidates = baseline_candidates.get(case_id, [])
                selected = []
            begin = time.perf_counter()
            try:
                evaluation = self.evaluate(
                    query=str(case.get("query") or ""), candidates=candidates,
                    selected_names=selected, config=config, record=False,
                )
                failed_open = False
            except Exception as exc:
                evaluation = {"items": [], "error": str(exc)[:200]}
                failed_open = True
            latencies.append((time.perf_counter() - begin) * 1000.0)
            items = evaluation.get("items") or []
            names = [str(item.get("memo_name")) for item in items]
            rank = names.index(expected_memo) + 1 if expected_memo in names else -1
            found = rank > 0
            negative_ranks = {
                name: names.index(name) + 1 for name in negative_memos if name in names
            }
            negative_top3 = sorted(name for name, neg_rank in negative_ranks.items() if neg_rank <= 3)
            best_negative_rank = min(negative_ranks.values(), default=-1)
            beats_all_negatives = bool(
                found and negative_memos
                and all(name not in negative_ranks or rank < negative_ranks[name]
                        for name in negative_memos)
            )
            top_item = items[0] if items else {}
            actual_grade = str(top_item.get("cue_grade") or "D")
            if expected_memo:
                own = next((item for item in items if str(item.get("memo_name")) == expected_memo), {})
                actual_grade = str(own.get("cue_grade") or actual_grade)
            grade_match = actual_grade == expected_grade
            rescue_used = any(
                str(item.get("candidate_source")) in {"precise_cue_index", "source_turn_index"}
                for item in items
            )
            source_link_rescue_used = any(
                str(item.get("candidate_source")) == "source_turn_index" for item in items
            )
            # Compare access-layer rank to the 4.6 baseline rank
            access_rank = rank
            rank_delta = (baseline_rank - access_rank) if (baseline_rank > 0 and access_rank > 0) else 0
            results.append({
                "case_id": case_id, "case_type": str(case.get("case_type") or ""),
                "found": found, "rank": rank, "grade_match": grade_match,
                "rescue_used": rescue_used, "expected_grade": expected_grade,
                "source_link_rescue_used": source_link_rescue_used,
                "actual_grade": actual_grade, "failed_open": failed_open,
                "same_day": evaluation.get("same_day") or {},
                "exact_cue_count": int(evaluation.get("exact_cue_count") or 0),
                "baseline_rank": baseline_rank, "access_rank": access_rank,
                "rank_delta": rank_delta, "used_real_recall": used_real_recall,
                "confirmed": bool(str(case.get("confirmed_by") or "").strip()),
                "expected_memo": expected_memo,
                "supervision_level": supervision_level,
                "supervision_confidence": round(supervision_confidence, 4),
                "negative_memos": negative_memos,
                "negative_ranks": negative_ranks,
                "best_negative_rank": best_negative_rank,
                "beats_all_negatives": beats_all_negatives,
                "hard_negative_top3": negative_top3,
                "recall_error": recall_error,
            })
        finished = time.time()
        strict_results = [item for item in results
                          if item.get("confirmed") or item.get("supervision_level") == "source_verified"]
        strict_latencies = [latency for item, latency in zip(results, latencies)
                            if item.get("confirmed") or item.get("supervision_level") == "source_verified"]
        gates = self._compute_safety_gates(strict_results, strict_latencies)
        gates["evaluated_cases"] = len(results)
        gates["eligible_cases"] = len(strict_results)
        gates["real_recall_cases"] = sum(
            1 for item in strict_results if item.get("used_real_recall")
        )
        gates["evaluation_scope"] = scope
        gates["sample"] = {
            "available_cases": available_cases,
            "selected_cases": len(cases),
            "limit": sample_limit if scope == "calibration" else 0,
            "rotation": rotation,
            "complete": len(cases) >= available_cases,
        }
        gates["supervision_counts"] = dict(Counter(
            str(item.get("supervision_level") or "heuristic") for item in results
        ))
        gates["calibration"] = self._compute_calibration_metrics(results)
        gates["source_canary"] = self._compute_source_canary_gate(strict_results)
        passed = sum(1 for item in results if item["grade_match"])
        with self._lock:
            conn = self._get_conn()
            conn.execute(
                """INSERT INTO memory_access_eval_runs(
                   run_id,algorithm_version,config_json,gate_results_json,
                   cases_total,cases_passed,started_ts,finished_ts) VALUES(?,?,?,?,?,?,?,?)""",
                (run_id, ALGORITHM_VERSION, _json(config), _json(gates),
                 len(results), passed, started, finished),
            )
            for item in results:
                conn.execute(
                    """INSERT INTO memory_access_eval_results(
                       run_id,case_id,found,rank,grade_match,rescue_used,notes,detail_json,created_ts,
                       baseline_rank,access_rank,rank_delta,used_real_recall)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (run_id, item["case_id"], int(item["found"]), int(item["rank"]),
                     int(item["grade_match"]), int(item["rescue_used"]),
                     item["case_type"], _json(item), finished,
                     int(item.get("baseline_rank", -1)), int(item.get("access_rank", -1)),
                     int(item.get("rank_delta", 0)), int(item.get("used_real_recall", False))),
                )
            conn.commit()
        return {
            "run_id": run_id, "algorithm_version": ALGORITHM_VERSION,
            "cases_total": len(results), "cases_passed": passed,
            "gates": gates, "results": results,
            "calibration": gates.get("calibration") or {},
            "p95_ms": gates.get("G7", {}).get("p95_ms", 0.0),
        }

    @staticmethod
    def _eval_case_trust(case: dict[str, Any]) -> str:
        if str(case.get("confirmed_by") or "").strip():
            return "strict"
        level = str(case.get("supervision_level") or "heuristic")
        confidence = float(case.get("supervision_confidence") or 0)
        if level == "source_verified" and confidence >= 0.90:
            return "strict"
        if level == "structural_verified" and confidence >= 0.85:
            return "calibration"
        return "diagnostic"

    @classmethod
    def _stratified_eval_sample(cls, cases: list[dict[str, Any]], limit: int,
                                rotation: int) -> list[dict[str, Any]]:
        """Bound live-provider cost while rotating coverage across the corpus."""
        limit = max(1, int(limit))
        selected: list[dict[str, Any]] = []
        selected_ids: set[str] = set()

        def order(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
            return sorted(items, key=lambda item: hashlib.sha1(
                f"{rotation}:{item.get('case_id')}".encode("utf-8", "ignore")
            ).hexdigest())

        def add(items: list[dict[str, Any]], cap: int | None = None) -> None:
            added = 0
            for item in order(items):
                case_id = str(item.get("case_id") or "")
                if not case_id or case_id in selected_ids or len(selected) >= limit:
                    continue
                selected.append(item)
                selected_ids.add(case_id)
                added += 1
                if cap is not None and added >= cap:
                    break

        strict = [item for item in cases if cls._eval_case_trust(item) == "strict"]
        add(strict, max(8, limit // 3))
        for case_type in sorted({str(item.get("case_type") or "general") for item in cases}):
            add([item for item in cases if str(item.get("case_type") or "general") == case_type], 1)
        hard_negatives = [item for item in cases if json_list(item.get("negative_memos_json"))]
        add(hard_negatives, max(4, limit // 4))
        seen_memos = {str(item.get("memo_name") or "") for item in selected}
        unique_memory_pool = []
        for item in order(cases):
            memo_name = str(item.get("memo_name") or "")
            if memo_name and memo_name not in seen_memos:
                unique_memory_pool.append(item)
                seen_memos.add(memo_name)
        add(unique_memory_pool)
        add(cases)
        return selected[:limit]

    @staticmethod
    def _compute_calibration_metrics(results: list[dict[str, Any]]) -> dict[str, Any]:
        """Report retrieval quality without collapsing trust levels together."""
        targeted = [item for item in results if item.get("expected_memo")]
        weighted_total = sum(float(item.get("supervision_confidence") or 0) for item in targeted)

        def hit_at(limit: int) -> float:
            if not targeted:
                return 0.0
            numerator = sum(
                float(item.get("supervision_confidence") or 0)
                for item in targeted if 0 < int(item.get("rank") or -1) <= limit
            )
            return round(numerator / max(weighted_total, 1e-9), 4)

        mrr_num = sum(
            float(item.get("supervision_confidence") or 0) / int(item["rank"])
            for item in targeted if int(item.get("rank") or -1) > 0
        )
        hard_negative_cases = [item for item in targeted if item.get("negative_memos")]
        intrusions = [item for item in hard_negative_cases if item.get("hard_negative_top3")]
        contrastive_wins = [item for item in hard_negative_cases if item.get("beats_all_negatives")]
        source_link_rescues = [item for item in targeted if item.get("source_link_rescue_used")]
        non_regression_pool = [item for item in targeted
                               if item.get("used_real_recall") and int(item.get("baseline_rank") or -1) > 0]
        non_regressed = [item for item in non_regression_pool
                         if 0 < int(item.get("access_rank") or -1) <= int(item.get("baseline_rank") or -1)]
        by_level: dict[str, dict[str, Any]] = {}
        for level in ("human", "source_verified", "structural_verified", "heuristic"):
            items = [item for item in targeted if item.get("supervision_level") == level]
            by_level[level] = {
                "cases": len(items),
                "hit_at_3": round(sum(1 for item in items if 0 < int(item.get("rank") or -1) <= 3)
                                  / max(1, len(items)), 4),
                "mrr": round(sum(1 / int(item["rank"]) for item in items
                                 if int(item.get("rank") or -1) > 0) / max(1, len(items)), 4),
            }
        # Macro averages stop long or richly annotated memories from dominating
        # the score merely because they generated more query variants.
        by_target: dict[str, list[dict[str, Any]]] = defaultdict(list)
        by_type: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for item in targeted:
            by_target[str(item.get("expected_memo") or "")].append(item)
            by_type[str(item.get("case_type") or "general")].append(item)

        def macro_hit(groups: dict[str, list[dict[str, Any]]], limit: int) -> float:
            values = [sum(1 for item in items if 0 < int(item.get("rank") or -1) <= limit)
                      / max(1, len(items)) for items in groups.values() if items]
            return round(sum(values) / max(1, len(values)), 4)

        def macro_mrr(groups: dict[str, list[dict[str, Any]]]) -> float:
            values = [sum(1 / int(item["rank"]) for item in items
                          if int(item.get("rank") or -1) > 0) / max(1, len(items))
                      for items in groups.values() if items]
            return round(sum(values) / max(1, len(values)), 4)

        return {
            "targeted_cases": len(targeted),
            "weighted_hit_at_1": hit_at(1),
            "weighted_hit_at_3": hit_at(3),
            "weighted_hit_at_5": hit_at(5),
            "weighted_mrr": round(mrr_num / max(weighted_total, 1e-9), 4),
            "macro_memory_hit_at_3": macro_hit(by_target, 3),
            "macro_memory_mrr": macro_mrr(by_target),
            "macro_type_hit_at_3": macro_hit(by_type, 3),
            "covered_memories": len(by_target),
            "covered_case_types": len(by_type),
            "hard_negative_cases": len(hard_negative_cases),
            "hard_negative_top3_intrusions": len(intrusions),
            "hard_negative_clean_rate": round(1 - len(intrusions) / max(1, len(hard_negative_cases)), 4),
            "hard_negative_target_win_rate": round(
                len(contrastive_wins) / max(1, len(hard_negative_cases)), 4
            ),
            "source_link_rescue_cases": len(source_link_rescues),
            "baseline_non_regression": round(len(non_regressed) / max(1, len(non_regression_pool)), 4),
            "baseline_comparable_cases": len(non_regression_pool),
            "by_supervision": by_level,
        }

    @staticmethod
    def _compute_source_canary_gate(results: list[dict[str, Any]]) -> dict[str, Any]:
        """Evaluate the first-hand source cases that may drive 5.0 canaries."""
        source_cases = [
            item for item in results
            if str(item.get("supervision_level") or "") == "source_verified"
            and str(item.get("case_type") or "") == "source_turn_holdout"
        ]
        hard_negative_cases = [item for item in source_cases if item.get("negative_memos")]
        comparable = [item for item in source_cases if int(item.get("baseline_rank") or -1) > 0]
        failures: dict[str, list[str]] = {
            "not_top3": [str(item.get("case_id") or "") for item in source_cases
                         if not 0 < int(item.get("rank") or -1) <= 3],
            "not_source_linked": [str(item.get("case_id") or "") for item in source_cases
                                  if not item.get("source_link_rescue_used")],
            "not_real_recall": [str(item.get("case_id") or "") for item in source_cases
                                if not item.get("used_real_recall")],
            "failed_open": [str(item.get("case_id") or "") for item in source_cases
                            if item.get("failed_open")],
            "hard_negative_loss": [str(item.get("case_id") or "") for item in hard_negative_cases
                                   if not item.get("beats_all_negatives")],
            "baseline_regression": [str(item.get("case_id") or "") for item in comparable
                                    if not 0 < int(item.get("access_rank") or -1)
                                    <= int(item.get("baseline_rank") or -1)],
        }
        non_regressed = [item for item in comparable
                         if 0 < int(item.get("access_rank") or -1)
                         <= int(item.get("baseline_rank") or -1)]
        ready = bool(source_cases) and not any(failures.values())
        return {
            "ready": ready,
            "cases": len(source_cases),
            "top3_rate": round(sum(1 for item in source_cases
                                    if 0 < int(item.get("rank") or -1) <= 3)
                               / max(1, len(source_cases)), 4),
            "source_link_rate": round(sum(1 for item in source_cases
                                           if item.get("source_link_rescue_used"))
                                      / max(1, len(source_cases)), 4),
            "real_recall_rate": round(sum(1 for item in source_cases
                                           if item.get("used_real_recall"))
                                      / max(1, len(source_cases)), 4),
            "hard_negative_cases": len(hard_negative_cases),
            "hard_negative_win_rate": round(sum(1 for item in hard_negative_cases
                                                  if item.get("beats_all_negatives"))
                                             / max(1, len(hard_negative_cases)), 4),
            "baseline_comparable_cases": len(comparable),
            "baseline_non_regression": round(len(non_regressed) / max(1, len(comparable)), 4),
            "failures": {key: value[:10] for key, value in failures.items()},
            "policy": "source_exact_canary",
        }

    @staticmethod
    def _compute_safety_gates(results: list[dict[str, Any]],
                              latencies: list[float]) -> dict[str, Any]:
        """Evaluate the eight hard takeover gates.

        Any failing gate must show which cases failed, instead of collapsing into
        a single vague score.
        """
        def subset(case_type: str) -> list[dict[str, Any]]:
            return [item for item in results if item["case_type"] == case_type]

        def ratio_gate(name: str, items: list[dict[str, Any]],
                       predicate: Callable[[dict[str, Any]], bool],
                       minimum: float, minimum_samples: int = 1) -> dict[str, Any]:
            total = len(items)
            ok = [item for item in items if predicate(item)]
            value = (len(ok) / total) if total else 0.0
            return {
                "gate": name, "numerator": len(ok), "denominator": total,
                "value": round(value, 4), "required": minimum,
                "minimum_samples": minimum_samples,
                "passed": total >= minimum_samples and value >= minimum,
                "status": "insufficient_samples" if total < minimum_samples else "evaluated",
                "failures": [item["case_id"] for item in items if not predicate(item)][:10],
            }

        protected = subset("protected_memory")
        date_entity = subset("date_plus_entity")
        verbatim = subset("verbatim_quote")
        unrelated = subset("unrelated_control")
        same_day = subset("same_day_multi")
        plain = subset("plain_daily")
        ordered = sorted(latencies)
        p95 = ordered[max(0, int(len(ordered) * 0.95) - 1)] if ordered else 0.0
        gates = {
            # G1: protected memory must actually be found (rank>0), not just "didn't crash"
            "G1": ratio_gate("protected_retention", protected,
                lambda i: i["found"] and i["rank"] > 0, 1.0),
            # G2: date+entity must stay in Top-3 and never rank below the real baseline.
            "G2": ratio_gate("date_entity_top3", date_entity,
                lambda i: 0 < i["access_rank"] <= 3 and (
                    i.get("baseline_rank", -1) <= 0 or i["access_rank"] <= i["baseline_rank"]
                ), 1.0),
            "G3": ratio_gate("verbatim_top1", verbatim, lambda i: i["rank"] == 1, 0.95),
            "G4": ratio_gate("unrelated_no_rescue", unrelated, lambda i: not i["rescue_used"], 0.98),
            "G5": ratio_gate("same_day_not_collapsed", same_day, lambda i: i["exact_cue_count"] <= 1, 1.0),
            # G6: when real recall was used, access rank must not be worse than baseline
            "G6": ratio_gate("legacy_diary_eligible",
                [i for i in plain if i.get("used_real_recall")],
                lambda i: i["access_rank"] > 0 and (i.get("baseline_rank", -1) <= 0 or i["access_rank"] <= i["baseline_rank"]),
                1.0),
            "G7": {
                "gate": "latency_p95", "p95_ms": round(p95, 3), "required": 25.0,
                "passed": bool(ordered) and p95 < 25.0, "samples": len(ordered),
                "status": "insufficient_samples" if not ordered else "evaluated",
            },
            "G8": ratio_gate("fail_open", results, lambda i: not i["failed_open"], 1.0),
        }
        gates["passed_count"] = sum(1 for key, value in gates.items()
                                    if key.startswith("G") and value.get("passed"))
        gates["total_count"] = sum(1 for key in gates if key.startswith("G"))
        gates["ready_for_takeover"] = gates["passed_count"] == gates["total_count"]
        return gates

    def latest_eval_run(self) -> dict[str, Any] | None:
        row = self._get_conn().execute(
            "SELECT * FROM memory_access_eval_runs ORDER BY started_ts DESC LIMIT 1"
        ).fetchone()
        if not row:
            return None
        item = dict(row)
        item["gates"] = _loads(item.pop("gate_results_json", "{}"), {})
        item["config"] = _loads(item.pop("config_json", "{}"), {})
        return item

    def eval_run_results(self, run_id: str, *, limit: int = 500) -> list[dict[str, Any]]:
        rows = self._get_conn().execute(
            """SELECT r.*,c.query,c.expected_grade,c.case_type AS case_type_ref
               FROM memory_access_eval_results r
               LEFT JOIN memory_access_eval_cases c ON c.case_id=r.case_id
               WHERE r.run_id=? ORDER BY r.id LIMIT ?""",
            (str(run_id), max(1, min(2000, int(limit)))),
        ).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["detail"] = _loads(item.pop("detail_json", "{}"), {})
            out.append(item)
        return out

    def safety_gates(self) -> dict[str, Any]:
        run = self.latest_eval_run()
        if not run:
            return {"available": False, "reason": "no_eval_run",
                    "ready_for_takeover": False, "shadow_locked": True}
        gates = run.get("gates") or {}
        return {
            "available": True,
            "run_id": run.get("run_id"),
            "algorithm_version": run.get("algorithm_version"),
            "cases_total": run.get("cases_total"),
            "cases_passed": run.get("cases_passed"),
            "gates": gates,
            "ready_for_takeover": bool(gates.get("ready_for_takeover")),
            "shadow_locked": not bool(gates.get("ready_for_takeover")),
        }

    # ── test3 takeover ────────────────────────────────────────────────────

    def _legacy_takeover_prerequisites(self, *, config: dict[str, Any] | None = None,
                               now: float | None = None) -> dict[str, Any]:
        """Check the prerequisites for one conservative canary append."""
        config = config or {}
        now = float(now or time.time())
        run = self.latest_eval_run()
        conditions: list[dict[str, Any]] = []
        c1 = bool(config.get("takeover_enable", False))
        conditions.append({"key": "takeover_enable", "passed": c1,
                           "reason": "未开启 memory_access_takeover_enable"})
        gates = (run or {}).get("gates") or {}
        all_green = bool(gates.get("ready_for_takeover")) if gates else False
        conditions.append({"key": "gates_all_green", "passed": all_green,
                           "reason": f"最近评测 {gates.get('passed_count',0)}/8 道门通过"})
        cases_total = int(gates.get("eligible_cases") or 0)
        min_cases = int(config.get("takeover_min_eval_cases", 30))
        c3 = cases_total >= min_cases
        conditions.append({"key": "enough_cases", "passed": c3,
                           "reason": f"评测案例 {cases_total} < 最低 {min_cases}"})
        real_recall_cases = int(gates.get("real_recall_cases") or 0)
        used_real = cases_total > 0 and real_recall_cases == cases_total
        conditions.append({"key": "used_real_recall", "passed": used_real,
                           "reason": f"真实召回案例 {real_recall_cases}/{cases_total}"})
        c5 = str((run or {}).get("algorithm_version") or "") == ALGORITHM_VERSION
        conditions.append({"key": "version_match", "passed": c5,
                           "reason": f"评测版本 ≠ 当前 {ALGORITHM_VERSION}"})
        max_age_days = int(config.get("eval_max_age_days", 7))
        run_finished = float((run or {}).get("finished_ts") or 0)
        c6 = run_finished > 0 and (now - run_finished) / 86400.0 < max_age_days
        conditions.append({"key": "eval_fresh", "passed": c6,
                           "reason": f"评测超过 {max_age_days} 天"})
        calibration = gates.get("calibration") or {}
        comparable = int(calibration.get("baseline_comparable_cases") or 0)
        non_regression = float(calibration.get("baseline_non_regression") or 0.0)
        c7 = comparable >= min_cases and non_regression >= 1.0
        conditions.append({
            "key": "baseline_non_regression", "passed": c7,
            "reason": f"4.6 可比案例 {comparable}，非退化率 {non_regression:.1%}",
        })
        eligible = all(c["passed"] for c in conditions)
        return {
            "eligible": eligible, "conditions": conditions,
            "policy": "source_exact_canary", "max_appends": 1,
        }

    def takeover_prerequisites(self, *, config: dict[str, Any] | None = None,
                               now: float | None = None) -> dict[str, Any]:
        """Check the independent safety contract for one source-backed canary."""
        config = config or {}
        now = float(now or time.time())
        run = self.latest_eval_run()
        gates = (run or {}).get("gates") or {}
        canary = gates.get("source_canary") or {}
        cases_total = int(canary.get("cases") or 0)
        min_cases = max(1, int(config.get("takeover_source_min_eval_cases", 5)))
        real_recall_rate = float(canary.get("real_recall_rate") or 0.0)
        max_age_days = int(config.get("eval_max_age_days", 7))
        run_finished = float((run or {}).get("finished_ts") or 0)
        breaker = self.breaker_status(config=config)
        conditions = [
            {"key": "takeover_enable", "passed": bool(config.get("takeover_enable", False)),
             "reason": "已开启在线来源证据金丝雀"},
            {"key": "source_canary_green", "passed": bool(canary.get("ready")),
             "reason": "来源链接、Top-3、硬负例与失败开放均通过"},
            {"key": "enough_source_cases", "passed": cases_total >= min_cases,
             "reason": f"严格来源案例 {cases_total}/{min_cases}"},
            {"key": "used_real_recall", "passed": cases_total > 0 and real_recall_rate >= 1.0,
             "reason": f"真实召回覆盖率 {real_recall_rate:.1%}"},
            {"key": "version_match",
             "passed": str((run or {}).get("algorithm_version") or "") == ALGORITHM_VERSION,
             "reason": f"评测必须由当前算法 {ALGORITHM_VERSION} 生成"},
            {"key": "eval_fresh",
             "passed": run_finished > 0 and (now - run_finished) / 86400.0 < max_age_days,
             "reason": f"评测有效期 {max_age_days} 天"},
            {"key": "breaker_clear", "passed": not bool(breaker.get("tripped")),
             "reason": "在线断路器正常" if not breaker.get("tripped") else "在线断路器已触发"},
        ]
        return {
            "eligible": all(item["passed"] for item in conditions),
            "conditions": conditions,
            "policy": "source_exact_canary",
            "max_appends": 1,
            "source_canary": canary,
        }

    def breaker_status(self, *, config: dict[str, Any] | None = None) -> dict[str, Any]:
        config = config or {}
        threshold = int(config.get("takeover_breaker_threshold", 3))
        conn = self._get_conn()
        route_mode = str(config.get("route_mode") or "")
        where = "WHERE route_mode=?" if route_mode == "supplement" else ""
        params: tuple[Any, ...] = (
            (route_mode, max(threshold * 2, 20))
            if where else (max(threshold * 2, 20),)
        )
        rows = conn.execute(
            f"""SELECT response_used, breaker_trip FROM memory_access_takeover_log
                {where} ORDER BY id DESC LIMIT ?""",
            params,
        ).fetchall()
        consecutive_unused = 0
        for row in rows:
            breaker_trip = int(row["breaker_trip"])
            response_used = int(row["response_used"])
            if breaker_trip == 1:
                if response_used == 0:
                    return {
                        "tripped": True, "consecutive_unused": max(consecutive_unused, threshold),
                        "threshold": threshold, "persistent": True,
                    }
                break
            if response_used == 0:
                consecutive_unused += 1
            else:
                break
        tripped = consecutive_unused >= threshold
        return {"tripped": tripped, "consecutive_unused": consecutive_unused,
                "threshold": threshold}

    def trip_breaker(self, *, reason: str = "consecutive_unused",
                     route_mode: str = "supplement") -> None:
        with self._lock:
            conn = self._get_conn()
            active = conn.execute(
                """SELECT 1 FROM memory_access_takeover_log
                   WHERE breaker_trip=1 AND response_used=0 AND route_mode=?
                   ORDER BY id DESC LIMIT 1""",
                (str(route_mode or "supplement"),),
            ).fetchone()
            if active:
                return
            conn.execute(
                """INSERT INTO memory_access_takeover_log(
                   request_id,memo_name,cue_grade,append_rank,reason,
                   response_used,breaker_trip,route_mode,created_ts,evaluated_ts)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                ("breaker", "", "", -1, reason, 0, 1,
                 str(route_mode or "supplement"), time.time(), 0),
            )
            conn.commit()

    def reset_breaker(self) -> bool:
        with self._lock:
            conn = self._get_conn()
            conn.execute(
                "UPDATE memory_access_takeover_log SET response_used=1 WHERE breaker_trip=1"
            )
            conn.commit()
        return True

    def compute_supplement_appends(
        self,
        evaluation: dict[str, Any],
        *,
        original_selected: list[str],
        temporal_constraint: dict[str, Any] | None = None,
        config: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Select independent temporal and ACCESS-rescue supplements.

        The baseline list is immutable. This selector performs exact-ID
        exclusion only; similar memories remain distinct candidates. Missing
        source archives reduce evidence confidence but do not disqualify old
        diary-derived memories.
        """
        config = config or {}
        max_total = max(0, min(2, int(config.get("supplement_max", 2))))
        temporal_max = max(0, min(1, int(config.get("supplement_temporal_max", 1))))
        rescue_max = max(0, min(1, int(config.get("supplement_rescue_max", 1))))
        if max_total <= 0 or not evaluation.get("enabled", True):
            return []

        original_set = {str(value) for value in original_selected if str(value)}
        unique_items: dict[str, dict[str, Any]] = {}
        for raw in evaluation.get("items") or []:
            memo_name = str(raw.get("memo_name") or "").strip()
            if memo_name and memo_name not in unique_items:
                unique_items[memo_name] = dict(raw)

        constraint = dict(temporal_constraint or {})
        ranges = [
            value for value in (constraint.get("ranges") or [])
            if isinstance(value, dict) and value.get("start") and value.get("end")
        ]
        ordinal = str(constraint.get("ordinal") or "")
        temporal_active = bool(ranges or ordinal)
        explicit_range = any(
            str(value.get("reason") or "").startswith("explicit_") for value in ranges
        )

        # Stage 2 has an independent time candidate route. Without this step,
        # T would merely re-rank the baseline pool and could never recover a
        # memory the baseline did not retrieve. Range lookups use the indexed
        # event clock with a one-day guard, then verify the factual event date.
        if temporal_active:
            query_text = str(evaluation.get("query") or "")
            temporal_words = {
                "今天", "昨天", "昨日", "前天", "明天", "上次", "上一次", "之前那次",
                "第一次", "最后一次", "最近一次", "最新一次", "最早", "最开始", "起初",
                "今年", "去年", "前年", "上个月", "这个月", "本月", "春天", "夏天",
                "秋天", "冬天", "时候", "那天", "那次", "记得", "发生",
            }
            query_terms = {
                term for term in extract_terms(query_text)
                if term not in temporal_words and not term.isdigit()
                and not _DATE_RE.fullmatch(term)
            }
            temporal_rows: list[dict[str, Any]] = []
            conn = self._get_conn()
            if ranges:
                starts = [datetime.fromisoformat(str(value["start"])).timestamp() for value in ranges]
                ends = [
                    datetime.fromisoformat(str(value["end"])).timestamp() + 86400.0
                    for value in ranges
                ]
                rows = conn.execute(
                    """SELECT memo_name,event_ts,occurred_at,time_basis,evidence_quality,
                              card_text,scene_anchor,retrieval_key,entities_json
                         FROM episodes
                        WHERE active=1 AND event_ts>=? AND event_ts<?
                        ORDER BY event_ts DESC LIMIT 2048""",
                    (min(starts) - 86400.0, max(ends) + 86400.0),
                ).fetchall()
                for row in rows:
                    item = dict(row)
                    event_date = _event_date(item)
                    if any(
                        str(value["start"]) <= event_date <= str(value["end"])
                        for value in ranges
                    ):
                        temporal_rows.append(item)
            elif ordinal and query_terms:
                # Ordinals without a range must remain topic-bound. Searching
                # all history for bare "first" would select an arbitrary first
                # memory, so require at least one lexical/entity term.
                terms = sorted(query_terms, key=lambda value: (-len(value), value))[:8]
                clauses = []
                params: list[str] = []
                for term in terms:
                    safe = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                    clauses.append(
                        "LOWER(card_text || ' ' || scene_anchor || ' ' || retrieval_key || ' ' || entities_json) "
                        "LIKE ? ESCAPE '\\'"
                    )
                    params.append(f"%{safe.lower()}%")
                ordinal_order = "ASC" if ordinal == "first" else "DESC"
                rows = conn.execute(
                    f"""SELECT memo_name,event_ts,occurred_at,time_basis,evidence_quality,
                               card_text,scene_anchor,retrieval_key,entities_json
                          FROM episodes
                         WHERE active=1 AND ({' OR '.join(clauses)})
                         ORDER BY event_ts {ordinal_order} LIMIT 2048""",
                    tuple(params),
                ).fetchall()
                temporal_rows = [dict(row) for row in rows]

            denominator = max(1, min(6, len(query_terms)))
            for row in temporal_rows:
                memo_name = str(row.get("memo_name") or "").strip()
                if not memo_name or memo_name in unique_items:
                    continue
                searchable = " ".join(str(row.get(key) or "") for key in (
                    "card_text", "scene_anchor", "retrieval_key", "entities_json",
                ))
                matched = query_terms & extract_terms(searchable)
                lexical = _clamp(len(matched) / denominator)
                if ordinal and not ranges and lexical <= 0.0:
                    continue
                unique_items[memo_name] = {
                    "memo_name": memo_name,
                    "rank": 100000 + len(unique_items),
                    "selected": False,
                    "access_state": "vivid",
                    "accessibility": 0.5,
                    "base_score": max(0.22 if ranges else 0.0, 0.30 + lexical * 0.55),
                    "access_score": max(0.22 if ranges else 0.0, 0.30 + lexical * 0.55),
                    "cue_support": lexical,
                    "route_support": 0.0,
                    "source_route_support": 0.0,
                    "source_route": {},
                    "exact_cue": False,
                    "cue_grade": "C" if lexical >= 0.34 else "D",
                    "ambiguous_same_day": False,
                    "rescued": False,
                    "presentation": "passage_or_card",
                    "candidate_source": "temporal_index",
                    "rescue_reasons": ["temporal_index"] + [
                        "term:" + term for term in sorted(matched)[:4]
                    ],
                    "source_turn_hits": [],
                }
        candidate_names = [name for name in unique_items if name not in original_set]
        if not candidate_names:
            return []

        placeholders = ",".join("?" for _ in candidate_names)
        rows = self._get_conn().execute(
            f"""SELECT e.memo_name,e.event_ts,e.occurred_at,e.time_basis,
                       e.evidence_quality,e.source_batch_id,
                       COALESCE(a.age_reference_confidence,0.5) AS age_reference_confidence,
                       EXISTS(
                           SELECT 1 FROM episode_turn_links l
                            WHERE l.episode_id=e.episode_id
                       ) AS source_recoverable
                  FROM episodes e
                  LEFT JOIN memory_access_state a ON a.memo_name=e.memo_name
                 WHERE e.active=1 AND e.memo_name IN ({placeholders})""",
            tuple(candidate_names),
        ).fetchall()
        episode_by_name = {str(row["memo_name"]): dict(row) for row in rows}

        allow_diary = bool(config.get("supplement_allow_diary_derived", True))
        source_bonus_max = _clamp(float(config.get("supplement_source_bonus", 0.08)), 0.0, 0.15)
        temporal_threshold = _clamp(
            float(config.get("supplement_temporal_threshold", 0.56)), 0.35, 0.95
        )
        rescue_threshold = _clamp(
            float(config.get("supplement_rescue_threshold", 0.62)), 0.35, 0.95
        )
        ordinal_pool = []
        for name, item in unique_items.items():
            row = episode_by_name.get(name)
            if name in original_set or row is None:
                continue
            event_ts = float(row.get("event_ts") or 0.0)
            if event_ts <= 0:
                continue
            if max(float(item.get("base_score") or 0.0), float(item.get("cue_support") or 0.0)) < 0.20:
                continue
            ordinal_pool.append((event_ts, name))
        ordinal_pool.sort()
        ordinal_target = ""
        if ordinal_pool:
            if ordinal == "first":
                ordinal_target = ordinal_pool[0][1]
            elif ordinal == "latest":
                ordinal_target = ordinal_pool[-1][1]
            elif ordinal == "previous":
                ordinal_target = ordinal_pool[-2 if len(ordinal_pool) > 1 else -1][1]

        quality_scores = {
            "source_grounded": 1.0,
            "mixed_user_edited": 0.82,
            "diary_derived": 0.58,
        }
        basis_scores = {
            "source_turn": 1.0,
            "conversation_now": 0.98,
            "explicit_diary": 0.90,
            "memo_content": 0.82,
            "source_updated": 0.62,
            "source_updated_proxy": 0.35,
            "memo_created_proxy": 0.35,
            "unknown": 0.35,
        }
        scored: list[dict[str, Any]] = []
        for memo_name, item in unique_items.items():
            if memo_name in original_set:
                continue
            episode = episode_by_name.get(memo_name)
            if episode is None:
                continue
            evidence_quality = str(episode.get("evidence_quality") or "diary_derived")
            if evidence_quality == "diary_derived" and not allow_diary:
                continue
            event_date = _event_date(episode)
            temporal_score = 0.0
            temporal_reasons: list[str] = []
            if event_date:
                for value in ranges:
                    start, end = str(value["start"]), str(value["end"])
                    if start <= event_date[:10] <= end:
                        temporal_score = 1.0
                        temporal_reasons.append(str(value.get("reason") or "range_match"))
                        break
            if ordinal_target and memo_name == ordinal_target:
                temporal_score = max(temporal_score, 1.0)
                temporal_reasons.append("ordinal:" + ordinal)

            semantic = _clamp(float(item.get("base_score") or 0.0))
            cue = max(
                _clamp(float(item.get("cue_support") or 0.0)),
                0.80 if item.get("exact_cue") else 0.0,
            )
            route = max(
                _clamp(float(item.get("route_support") or 0.0)),
                _clamp(float(item.get("source_route_support") or 0.0)),
            )
            accessibility = _clamp(float(item.get("accessibility") or 0.5))
            source_recoverable = bool(episode.get("source_recoverable"))
            source_route = item.get("source_route") or {}
            source_exact = bool(source_route.get("exact_link"))
            quality = quality_scores.get(evidence_quality, 0.62)
            basis = str(episode.get("time_basis") or "unknown")
            age_confidence = _clamp(float(episode.get("age_reference_confidence") or 0.5))
            time_confidence = min(age_confidence, basis_scores.get(basis, 0.50))
            evidence = _clamp((quality + time_confidence) / 2.0)

            if temporal_active:
                if explicit_range:
                    final_score = (
                        semantic * 0.28 + temporal_score * 0.42 + cue * 0.13
                        + evidence * 0.12 + accessibility * 0.05
                    )
                else:
                    final_score = (
                        semantic * 0.34 + temporal_score * 0.36 + cue * 0.15
                        + evidence * 0.10 + accessibility * 0.05
                    )
            else:
                final_score = (
                    semantic * 0.48 + cue * 0.24 + evidence * 0.10
                    + accessibility * 0.10 + route * 0.08
                )
            source_bonus = 0.0
            if source_exact and source_recoverable:
                source_bonus = source_bonus_max
            elif source_recoverable:
                source_bonus = source_bonus_max * 0.45
            final_score = _clamp(final_score + source_bonus)

            candidate_source = str(item.get("candidate_source") or "retrieval_pool")
            access_state = str(item.get("access_state") or "vivid")
            rescue_signal = bool(
                (item.get("exact_cue") and not item.get("ambiguous_same_day"))
                or (source_exact and float(item.get("source_route_support") or 0.0) >= 0.70)
                or (
                    candidate_source in {"precise_cue_index", "source_turn_index"}
                    and cue >= 0.25
                )
                or (access_state in {"latent", "deep"} and cue >= 0.30 and semantic >= 0.45)
            )
            reason_codes = list(dict.fromkeys(
                temporal_reasons
                + list(item.get("rescue_reasons") or [])
                + (["exact_cue"] if item.get("exact_cue") else [])
                + (["source_recoverable"] if source_recoverable else [])
                + (["diary_derived_compat"] if evidence_quality == "diary_derived" else [])
            ))[:10]
            scored.append({
                "memo_name": memo_name,
                "cue_grade": str(item.get("cue_grade") or "D"),
                "access_score": float(item.get("access_score") or 0.0),
                "final_score": round(final_score, 6),
                "semantic_score": round(semantic, 6),
                "temporal_score": round(temporal_score, 6),
                "cue_score": round(cue, 6),
                "evidence_score": round(evidence, 6),
                "accessibility_score": round(accessibility, 6),
                "route_score": round(route, 6),
                "time_confidence": round(time_confidence, 6),
                "event_date": event_date,
                "time_basis": basis,
                "evidence_quality": evidence_quality,
                "source_recoverable": source_recoverable,
                "source_exact": source_exact,
                "source_route": source_route,
                "source_turn_hits": list(item.get("source_turn_hits") or [])[:2],
                "candidate_source": candidate_source,
                "access_state": access_state,
                "ambiguous_same_day": bool(item.get("ambiguous_same_day")),
                "temporal_eligible": bool(
                    temporal_active and temporal_score >= 0.75
                    and final_score >= temporal_threshold
                ),
                "rescue_eligible": bool(
                    not evaluation.get("date_only_query")
                    and rescue_signal and final_score >= rescue_threshold
                ),
                "reason_codes": reason_codes,
            })

        def ordering(item: dict[str, Any]) -> tuple[float, float, float, str]:
            return (
                -float(item.get("final_score") or 0.0),
                -float(item.get("temporal_score") or 0.0),
                -float(item.get("cue_score") or 0.0),
                str(item.get("memo_name") or ""),
            )

        selected: list[dict[str, Any]] = []
        selected_names: set[str] = set()

        def append_slot(slot: str, candidates: list[dict[str, Any]], limit: int) -> None:
            def slot_ordering(item: dict[str, Any]):
                # Preserve the purpose of both independent slots: when a true
                # time-index candidate exists, T should not consume the best
                # exact-cue candidate that A was designed to rescue.
                route_priority = (
                    0 if slot == "T" and item.get("candidate_source") == "temporal_index"
                    else 1
                )
                return (route_priority, *ordering(item))

            for item in sorted(candidates, key=slot_ordering):
                if len(selected) >= max_total or limit <= 0:
                    return
                memo_name = str(item.get("memo_name") or "")
                if not memo_name or memo_name in selected_names:
                    continue
                reason_codes = list(item.get("reason_codes") or [])
                selected.append({
                    **item,
                    "slot": slot,
                    "route": "access_temporal" if slot == "T" else "access_rescue",
                    "reason": "; ".join(reason_codes[:5]) or (
                        "temporal_match" if slot == "T" else "access_rescue"
                    ),
                    "presentation": (
                        "compact_source_evidence"
                        if item.get("source_exact") and item.get("source_recoverable")
                        else "matched_passage_or_episode"
                    ),
                    "append_rank": len(original_selected) + len(selected) + 1,
                    "policy_version": "access_supplement_v1",
                })
                selected_names.add(memo_name)
                limit -= 1

        temporal_candidates = [item for item in scored if item["temporal_eligible"]]
        # A bare date/month is a browse scope. When several memories fit it,
        # choosing one arbitrary supplement would imply false uniqueness.
        if evaluation.get("date_only_query") and len(temporal_candidates) > 1:
            temporal_candidates = []
        append_slot("T", temporal_candidates, temporal_max)
        append_slot("A", [item for item in scored if item["rescue_eligible"]], rescue_max)
        return selected[:max_total]

    def compute_takeover_appends(self, evaluation: dict[str, Any],
                                 *, original_selected: list[str],
                                 config: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """Given a Shadow evaluation result, compute which memories to append.

        Returns a list of dicts. The caller appends these to selected_hits.
        This method never mutates anything.
        """
        config = config or {}
        # 5.0 remains a canary, not a general re-ranker. Keep the historic config
        # readable but hard-cap real influence to one appended memory.
        max_appends = min(1, int(config.get("takeover_max_appends", 1)))
        min_grade = str(config.get("takeover_min_grade", "A"))
        grade_rank = {"A": 0, "B": 1, "C": 2, "D": 3}
        min_rank = grade_rank.get(min_grade, 1)
        original_set = set(str(n) for n in original_selected)
        appends: list[dict[str, Any]] = []
        for item in (evaluation.get("items") or []):
            if len(appends) >= max_appends:
                break
            memo_name = str(item.get("memo_name") or "")
            if not memo_name or memo_name in original_set:
                continue
            grade = str(item.get("cue_grade") or "D")
            if grade_rank.get(grade, 3) > min_rank:
                continue
            if not item.get("exact_cue"):
                continue
            if item.get("ambiguous_same_day"):
                continue
            source = item.get("source_route") or {}
            candidate_source = str(item.get("candidate_source") or "retrieval_pool")
            source_exact = bool(source.get("exact_link"))
            source_lexical = float(source.get("lexical") or 0.0)
            source_support = float(item.get("source_route_support") or 0.0)
            source_verified = bool(
                source_exact
                and source_support >= 0.76
                and (
                    candidate_source == "source_turn_index"
                    or source_lexical >= float(config.get("takeover_source_lexical_min", 0.34))
                )
            )
            if not source_verified:
                continue
            if bool(evaluation.get("date_only_query")):
                continue
            reasons = item.get("rescue_reasons") or []
            reason = "; ".join(str(r) for r in reasons[:3]) or "source_exact"
            appends.append({
                "memo_name": memo_name, "cue_grade": grade,
                "reason": reason, "append_rank": len(original_selected) + len(appends) + 1,
                "access_score": float(item.get("access_score") or 0.0),
                "candidate_source": candidate_source,
                "presentation": "compact_source_evidence",
                "source_exact": True,
                "source_lexical": round(source_lexical, 4),
                "source_route_support": round(source_support, 4),
            })
        return appends

    def log_takeover_append(self, *, request_id: str, memo_name: str,
                            cue_grade: str, append_rank: int, reason: str,
                            evidence_terms: list[str] | None = None,
                            policy_version: str = "source_exact_canary",
                            route_mode: str = "", slot: str = "",
                            scores: dict[str, Any] | None = None,
                            evidence_quality: str = "",
                            source_recoverable: bool = False) -> None:
        with self._lock:
            conn = self._get_conn()
            conn.execute(
                """INSERT INTO memory_access_takeover_log(
                   request_id,memo_name,cue_grade,append_rank,reason,
                   response_used,breaker_trip,created_ts,evaluated_ts,
                   evidence_terms_json,use_detail_json,policy_version,
                   route_mode,slot,scores_json,evidence_quality,source_recoverable)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (str(request_id), str(memo_name), str(cue_grade), int(append_rank),
                 str(reason), -1, 0, time.time(), 0,
                 _json(list(dict.fromkeys(str(x) for x in (evidence_terms or []) if str(x)))[:16]),
                 "{}", str(policy_version or "source_exact_canary"),
                 str(route_mode or ""), str(slot or ""), _json(scores or {}),
                 str(evidence_quality or ""), int(bool(source_recoverable))),
            )
            conn.commit()

    def list_takeover_log(self, *, limit: int = 50,
                          include_breaker: bool = True) -> list[dict[str, Any]]:
        """Return recent takeover decisions for audit and WebUI observability."""
        where = "" if include_breaker else "WHERE breaker_trip=0"
        rows = self._get_conn().execute(
            f"""SELECT id,request_id,memo_name,cue_grade,append_rank,reason,
                       response_used,breaker_trip,created_ts,evaluated_ts,
                       evidence_terms_json,use_detail_json,policy_version
                       ,route_mode,slot,scores_json,evidence_quality,source_recoverable
                FROM memory_access_takeover_log {where}
                ORDER BY id DESC LIMIT ?""",
            (max(1, min(500, int(limit))),),
        ).fetchall()
        output = []
        for row in rows:
            item = dict(row)
            item["evidence_terms"] = json_list(item.pop("evidence_terms_json", "[]"))
            item["use_detail"] = _loads(item.pop("use_detail_json", "{}"), {})
            item["scores"] = _loads(item.pop("scores_json", "{}"), {})
            item["source_recoverable"] = bool(item.get("source_recoverable"))
            output.append(item)
        return output

    def evaluate_takeover_response_use(self, *, request_id: str,
                                        response_text: str,
                                        reconsolidate: bool = True) -> dict[str, Any]:
        """After the model responds, check whether takeover-appended memories
        were actually used. Updates response_used and may trip the breaker."""
        response_terms = set(extract_terms(response_text)) if response_text else set()
        updated = 0
        unused = 0
        reconsolidated = 0
        with self._lock:
            conn = self._get_conn()
            rows = conn.execute(
                """SELECT id,memo_name,evidence_terms_json FROM memory_access_takeover_log
                   WHERE request_id=? AND response_used=-1""",
                (str(request_id),),
            ).fetchall()
            for row in rows:
                sig = conn.execute(
                    "SELECT signature_json FROM memory_cue_signatures WHERE memo_name=?",
                    (str(row["memo_name"]),),
                ).fetchone()
                memory_terms = set()
                entity_terms = set()
                quote_terms = set()
                if sig:
                    s = _loads(sig["signature_json"], {})
                    memory_terms = (
                        set(s.get("retrieval_terms") or [])
                        | set(s.get("content_terms") or [])
                    )
                    entity_terms = set(s.get("entities") or [])
                    quote_terms = set(s.get("quotes") or [])
                logged_terms = set(json_list(row["evidence_terms_json"]))
                memory_terms.update(logged_terms)
                response_lower = str(response_text or "").lower()
                entity_hit = any(str(term).lower() in response_lower for term in entity_terms if len(str(term)) >= 2)
                quote_hit = any(str(term).lower() in response_lower for term in quote_terms if len(str(term)) >= 4)
                overlap = memory_terms & response_terms
                # One broad word is not evidence that the model used the append.
                used = bool(quote_hit or len(overlap) >= 2 or (entity_hit and len(overlap) >= 1))
                conn.execute(
                    """UPDATE memory_access_takeover_log
                       SET response_used=?,evaluated_ts=?,use_detail_json=? WHERE id=?""",
                    (int(used), time.time(), _json({
                        "quote_hit": quote_hit, "entity_hit": entity_hit,
                        "overlap": sorted(str(x) for x in overlap)[:12],
                        "required_overlap": 2,
                    }), int(row["id"])),
                )
                if used and reconsolidate:
                    # A source-exact canary is the only online ACCESS path in
                    # 5.0. Once the response demonstrably uses it, this is
                    # first-hand retrieval evidence and may reconsolidate the
                    # derived access state. Shadow candidates never reach here.
                    now = time.time()
                    conn.execute(
                        """UPDATE memory_access_state
                           SET successful_use_count=successful_use_count+1,
                               reconsolidation_count=reconsolidation_count+1,
                               last_used_ts=?,last_reconsolidated_ts=?,
                               accessibility=MIN(1.0,accessibility+0.035),
                               vividness=MIN(1.0,vividness+0.05),updated_ts=?
                           WHERE memo_name=?""",
                        (now, now, now, str(row["memo_name"])),
                    )
                    conn.execute(
                        """INSERT INTO memory_access_events(
                           request_id,memo_name,event_kind,response_support,
                           selected,shadow,detail_json,created_ts
                           ) VALUES(?,?,?,?,?,?,?,?)""",
                        (str(request_id), str(row["memo_name"]), "canary_reconsolidation",
                         min(1.0, len(overlap) / 4.0 + (0.35 if entity_hit else 0.0)
                             + (0.45 if quote_hit else 0.0)),
                         1, 0, _json({
                             "used": True, "policy": "source_exact_canary",
                             "quote_hit": quote_hit, "entity_hit": entity_hit,
                             "overlap": sorted(str(x) for x in overlap)[:12],
                         }), now),
                    )
                    reconsolidated += 1
                if used:
                    updated += 1
                else:
                    unused += 1
            conn.commit()
        return {"updated": updated, "unused": unused, "reconsolidated": reconsolidated}

    def record_manual_feedback(self, *, memo_name: str, action: str,
                               note: str = "") -> dict[str, Any]:
        """Apply bounded, reversible supervision to the derived access state.

        Feedback never mutates a diary, source turn, Episode or cue signature.
        It only adjusts the local access prior and leaves a complete audit row.
        """
        actions = {
            "still_important": {"access": 0.08, "vivid": 0.06, "inhibit": -0.05},
            "natural_recall": {"access": 0.04, "vivid": 0.03, "inhibit": -0.02},
            "should_not_surface": {"access": -0.05, "vivid": -0.02, "inhibit": 0.06},
            "wrong_association": {"access": -0.10, "vivid": -0.04, "inhibit": 0.12},
        }
        action = str(action or "").strip()
        if action not in actions:
            raise ValueError("unsupported memory feedback action")
        memo_name = str(memo_name or "").strip()
        if not memo_name:
            raise ValueError("memo_name is required")
        note = str(note or "").strip()[:500]
        delta = actions[action]
        now = time.time()
        with self._lock:
            conn = self._get_conn()
            row = conn.execute(
                "SELECT access_state,accessibility,vividness,inhibition FROM memory_access_state WHERE memo_name=?",
                (memo_name,),
            ).fetchone()
            if row is None:
                raise ValueError("memory access state not found")
            before = dict(row)
            accessibility = _clamp(float(row["accessibility"]) + float(delta["access"]))
            vividness = _clamp(float(row["vividness"]) + float(delta["vivid"]))
            inhibition = _clamp(float(row["inhibition"]) + float(delta["inhibit"]))
            conn.execute(
                """UPDATE memory_access_state SET accessibility=?,vividness=?,inhibition=?,updated_ts=?
                   WHERE memo_name=?""",
                (accessibility, vividness, inhibition, now, memo_name),
            )
            conn.execute(
                """INSERT INTO memory_access_events(
                   request_id,memo_name,event_kind,access_state,base_score,access_score,
                   selected,shadow,detail_json,created_ts
                   ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                ("webui-feedback", memo_name, "manual_feedback", str(row["access_state"]),
                 float(row["accessibility"]), accessibility, 0, 0, _json({
                     "action": action, "note": note,
                     "before": before,
                     "after": {
                         "accessibility": accessibility,
                         "vividness": vividness,
                         "inhibition": inhibition,
                     },
                     "non_destructive": True,
                 }), now),
            )
            conn.commit()
        return {
            "memo_name": memo_name, "action": action,
            "before": before,
            "after": {
                "accessibility": round(accessibility, 4),
                "vividness": round(vividness, 4),
                "inhibition": round(inhibition, 4),
            },
            "memory_content_changed": False,
        }

    @staticmethod
    def _decode_observation_row(row: Any, *, include_detail: bool = False) -> dict[str, Any]:
        item = dict(row)
        for source, target, fallback in (
            ("baseline_json", "baseline", []),
            ("recommended_json", "recommended", []),
            ("added_json", "added", []),
            ("removed_json", "removed", []),
        ):
            item[target] = _loads(item.pop(source, None), fallback)
        detail = _loads(item.pop("detail_json", None), {})
        if include_detail:
            item["detail"] = detail
        item["would_change"] = bool(item.get("would_change"))
        item["shadow"] = bool(item.get("shadow"))
        item["response_available"] = int(item.get("response_memories") or -1) >= 0
        return item

    def memory_access_observation_summary(self, *, days: int = 30) -> dict[str, Any]:
        requested_days = int(days)
        days = max(1, min(3650, requested_days)) if requested_days > 0 else 0
        start = time.time() - days * 86400.0 if days else 0.0
        conn = self._get_conn()
        row = conn.execute(
            """SELECT COUNT(*) AS requests,
                      SUM(would_change) AS changed,
                      SUM(CASE WHEN rescue_pool_count>0 THEN 1 ELSE 0 END) AS pool_rescue_requests,
                      SUM(CASE WHEN deep_rescue_count>0 THEN 1 ELSE 0 END) AS deep_rescue_requests,
                      SUM(CASE WHEN source_rescue_count>0 THEN 1 ELSE 0 END) AS source_rescue_requests,
                      SUM(CASE WHEN response_memories>=0 THEN 1 ELSE 0 END) AS responses,
                      SUM(CASE WHEN response_used>0 THEN 1 ELSE 0 END) AS response_used_requests,
                      SUM(CASE WHEN feedback_verdict<>'' THEN 1 ELSE 0 END) AS feedback_count,
                      AVG(CASE WHEN response_use_rate>=0 THEN response_use_rate END) AS avg_use_rate
               FROM memory_access_observations WHERE created_ts>=?""",
            (start,),
        ).fetchone()
        feedback = {
            str(value["feedback_verdict"]): int(value["n"])
            for value in conn.execute(
                """SELECT feedback_verdict,COUNT(*) AS n FROM memory_access_observations
                   WHERE created_ts>=? AND feedback_verdict<>'' GROUP BY feedback_verdict""",
                (start,),
            ).fetchall()
        }
        requests = int(row["requests"] or 0)
        changed = int(row["changed"] or 0)
        return {
            "days": days,
            "requests": requests,
            "changed": changed,
            "changed_rate": round(changed / max(1, requests), 4),
            "pool_rescue_requests": int(row["pool_rescue_requests"] or 0),
            "deep_rescue_requests": int(row["deep_rescue_requests"] or 0),
            "source_rescue_requests": int(row["source_rescue_requests"] or 0),
            "responses": int(row["responses"] or 0),
            "response_used_requests": int(row["response_used_requests"] or 0),
            "average_response_use_rate": round(float(row["avg_use_rate"] or 0), 4),
            "feedback_count": int(row["feedback_count"] or 0),
            "feedback": feedback,
        }

    def list_memory_access_observations(
        self, *, limit: int = 100, offset: int = 0, changed_only: bool = False,
        rescued_only: bool = False, query: str = "", include_detail: bool = False,
    ) -> list[dict[str, Any]]:
        clauses = ["1=1"]
        params: list[Any] = []
        if changed_only:
            clauses.append("would_change=1")
        if rescued_only:
            clauses.append("(rescue_pool_count>0 OR deep_rescue_count>0 OR source_rescue_count>0)")
        if query:
            clauses.append("query_text LIKE ? ESCAPE '\\'")
            escaped = str(query)[:200].replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            params.append(f"%{escaped}%")
        rows = self._get_conn().execute(
            f"""SELECT * FROM memory_access_observations
                 WHERE {' AND '.join(clauses)} ORDER BY created_ts DESC LIMIT ? OFFSET ?""",
            (*params, max(1, min(500, int(limit))), max(0, int(offset))),
        ).fetchall()
        return [self._decode_observation_row(row, include_detail=include_detail) for row in rows]

    def memory_access_observation_detail(self, request_id: str) -> dict[str, Any] | None:
        row = self._get_conn().execute(
            "SELECT * FROM memory_access_observations WHERE request_id=?",
            (str(request_id or ""),),
        ).fetchone()
        return self._decode_observation_row(row, include_detail=True) if row else None

    def record_memory_access_observation_feedback(
        self, *, request_id: str, verdict: str, note: str = "",
    ) -> dict[str, Any]:
        verdict = str(verdict or "").strip().lower()
        if verdict not in {"baseline_better", "shadow_better", "equivalent", "uncertain", "clear"}:
            raise ValueError("unsupported observation feedback verdict")
        request_id = str(request_id or "").strip()
        note = str(note or "").strip()[:1000]
        with self._lock:
            conn = self._get_conn()
            exists = conn.execute(
                "SELECT 1 FROM memory_access_observations WHERE request_id=?", (request_id,),
            ).fetchone()
            if not exists:
                raise ValueError("memory access observation not found")
            stored_verdict = "" if verdict == "clear" else verdict
            conn.execute(
                """UPDATE memory_access_observations
                   SET feedback_verdict=?,feedback_note=?,updated_ts=? WHERE request_id=?""",
                (stored_verdict, "" if verdict == "clear" else note, time.time(), request_id),
            )
            conn.commit()
        return {
            "request_id": request_id, "verdict": stored_verdict,
            "note": "" if verdict == "clear" else note,
        }

    def memory_access_export_payload(self, *, observation_limit: int = 5000) -> dict[str, Any]:
        """Return ACCESS-only diagnostics; no diary/source text or service secrets."""
        conn = self._get_conn()
        observations = self.list_memory_access_observations(
            limit=observation_limit, include_detail=True,
        )
        states = [dict(row) for row in conn.execute(
            """SELECT memo_name,access_state,accessibility,persistence,vividness,distinctiveness,
                      interference_load,inhibition,retrieval_count,successful_use_count,
                      reconsolidation_count,age_reference_kind,age_reference_confidence,
                      decay_eligible,state_confidence,algorithm_version,updated_ts
               FROM memory_access_state ORDER BY memo_name"""
        ).fetchall()]
        edges = []
        for row in conn.execute(
            """SELECT source_memo,target_memo,edge_type,strength,competition,
                      edge_confidence,classifier_version,updated_ts
               FROM memory_interference_edges ORDER BY strength DESC"""
        ).fetchall():
            edges.append(dict(row))
        cases = self.list_eval_cases(enabled_only=False, limit=5000)
        latest_run = self.latest_eval_run()
        return {
            "format": "memos-memory-access-analysis-v1",
            "algorithm_version": ALGORITHM_VERSION,
            "created_ts": time.time(),
            "summary_30d": self.memory_access_observation_summary(days=30),
            "summary_all": self.memory_access_observation_summary(days=0),
            "overview": self.overview(),
            "observations": observations,
            "states": states,
            "interference_edges": edges,
            "eval_cases": cases,
            "latest_eval_run": latest_run,
            "latest_eval_results": (
                self.eval_run_results(str(latest_run.get("run_id") or ""), limit=5000)
                if latest_run else []
            ),
        }

    def activity_trends(self, *, days: int = 30) -> list[dict[str, Any]]:
        """Return compact daily ACCESS activity for WebUI observability."""
        days = max(1, min(180, int(days)))
        start = time.time() - days * 86400.0
        conn = self._get_conn()
        rows = conn.execute(
            """SELECT strftime('%Y-%m-%d',created_ts,'unixepoch','localtime') AS day,
                      COUNT(DISTINCT CASE WHEN event_kind='shadow_evaluation' THEN request_id END) AS requests,
                      SUM(CASE WHEN event_kind='shadow_evaluation'
                                    AND json_extract(detail_json,'$.rescued')=1 THEN 1 ELSE 0 END) AS rescues,
                      SUM(CASE WHEN event_kind='response_use'
                                    AND json_extract(detail_json,'$.used')=1 THEN 1 ELSE 0 END) AS response_uses,
                      SUM(CASE WHEN event_kind='canary_reconsolidation' THEN 1 ELSE 0 END) AS reconsolidations,
                      SUM(CASE WHEN event_kind='manual_feedback' THEN 1 ELSE 0 END) AS feedback
               FROM memory_access_events WHERE created_ts>=?
               GROUP BY day ORDER BY day""",
            (start,),
        ).fetchall()
        by_day = {str(row["day"]): dict(row) for row in rows if row["day"]}
        output = []
        now = time.time()
        for offset in range(days - 1, -1, -1):
            day = time.strftime("%Y-%m-%d", time.localtime(now - offset * 86400.0))
            row = by_day.get(day, {})
            output.append({
                "day": day,
                "requests": int(row.get("requests") or 0),
                "rescues": int(row.get("rescues") or 0),
                "response_uses": int(row.get("response_uses") or 0),
                "reconsolidations": int(row.get("reconsolidations") or 0),
                "feedback": int(row.get("feedback") or 0),
            })
        return output

    def overview(self) -> dict[str, Any]:
        conn = self._get_conn()
        states = {row["access_state"]: int(row["n"]) for row in conn.execute(
            "SELECT access_state,COUNT(*) AS n FROM memory_access_state GROUP BY access_state"
        ).fetchall()}
        events = int(conn.execute("SELECT COUNT(*) AS n FROM memory_access_events").fetchone()["n"])
        groups = int(conn.execute("SELECT COUNT(*) AS n FROM memory_interference_groups").fetchone()["n"])
        edges = self._edge_count(conn)
        age_references = {
            str(row["age_reference_kind"]): int(row["n"])
            for row in conn.execute(
                "SELECT age_reference_kind,COUNT(*) AS n FROM memory_access_state GROUP BY age_reference_kind"
            ).fetchall()
        }
        edge_types = {
            str(row["edge_type"]): int(row["n"])
            for row in conn.execute(
                "SELECT edge_type,COUNT(*) AS n FROM memory_interference_edges GROUP BY edge_type"
            ).fetchall()
        }
        latest = conn.execute("SELECT * FROM memory_access_maintenance ORDER BY id DESC LIMIT 1").fetchone()
        evaluation = conn.execute(
            """SELECT COUNT(DISTINCT request_id) AS requests,
                      SUM(CASE WHEN json_extract(detail_json,'$.rescued')=1 THEN 1 ELSE 0 END) AS rescues
               FROM memory_access_events WHERE event_kind='shadow_evaluation'"""
        ).fetchone()
        observation_count = int(conn.execute(
            "SELECT COUNT(*) AS n FROM memory_access_observations"
        ).fetchone()["n"] or 0)
        eligibility_rows = conn.execute(
            """SELECT COALESCE(json_extract(state_reason_json,'$.eligibility_status'),
                              CASE WHEN decay_eligible=1 THEN 'eligible' ELSE 'protected' END) AS status,
                      COUNT(*) AS n
               FROM memory_access_state GROUP BY status"""
        ).fetchall()
        eligibility = {str(row["status"]): int(row["n"]) for row in eligibility_rows}
        evidence_rows = conn.execute(
            "SELECT evidence_quality,COUNT(*) AS n FROM episodes WHERE active=1 GROUP BY evidence_quality"
        ).fetchall()
        source_linked = int(conn.execute(
            "SELECT COUNT(DISTINCT episode_id) AS n FROM episode_turn_links"
        ).fetchone()["n"] or 0)
        eval_case_rows = conn.execute(
            "SELECT case_type,COUNT(*) AS n FROM memory_access_eval_cases GROUP BY case_type"
        ).fetchall()
        eval_supervision_rows = conn.execute(
            """SELECT CASE WHEN confirmed_by<>'' THEN 'human' ELSE supervision_level END AS level,
                      COUNT(*) AS n
               FROM memory_access_eval_cases GROUP BY level"""
        ).fetchall()
        total_episodes = int(conn.execute(
            "SELECT COUNT(*) AS n FROM episodes WHERE active=1"
        ).fetchone()["n"] or 0)
        confidence_rows = conn.execute(
            """SELECT age_reference_kind,AVG(age_reference_confidence) AS avg_conf,COUNT(*) AS n
               FROM memory_access_state GROUP BY age_reference_kind"""
        ).fetchall()
        pending_transitions = int(conn.execute(
            """SELECT COUNT(*) AS n FROM memory_access_state
               WHERE json_extract(state_reason_json,'$.pending_state') IS NOT NULL"""
        ).fetchone()["n"] or 0)
        blocked_transitions = int(conn.execute(
            """SELECT COUNT(*) AS n FROM memory_access_state
               WHERE json_extract(state_reason_json,'$.blocked_transition') IS NOT NULL"""
        ).fetchone()["n"] or 0)
        grade_rows = conn.execute(
            """SELECT json_extract(detail_json,'$.cue_grade') AS grade,COUNT(*) AS n
               FROM memory_access_events WHERE event_kind='shadow_evaluation'
               GROUP BY grade"""
        ).fetchall()
        return {
            "total": sum(states.values()), "states": {key: states.get(key, 0) for key in ACCESS_STATES},
            "edges": edges, "groups": groups, "events": events,
            "edge_types": edge_types,
            "age_references": age_references,
            "age_reference_confidence": {
                str(row["age_reference_kind"]): {
                    "average": round(float(row["avg_conf"] or 0), 4), "count": int(row["n"]),
                }
                for row in confidence_rows
            },
            "decay_eligibility": {
                "eligible": int(eligibility.get("eligible", 0)),
                "protected": int(eligibility.get("protected", 0)),
                "deferred": int(eligibility.get("deferred", 0)),
            },
            "evidence_readiness": {
                "source_linked": source_linked,
                "source_unlinked": max(0, total_episodes - source_linked),
                "source_link_ratio": round(source_linked / max(1, total_episodes), 4),
                "evidence_quality": {
                    str(row["evidence_quality"] or "unknown"): int(row["n"])
                    for row in evidence_rows
                },
                "eval_case_types": {
                    str(row["case_type"] or "unknown"): int(row["n"])
                    for row in eval_case_rows
                },
                "eval_supervision": {
                    str(row["level"] or "heuristic"): int(row["n"])
                    for row in eval_supervision_rows
                },
            },
            "pending_transitions": pending_transitions,
            "blocked_transitions": blocked_transitions,
            "cue_grades": {
                str(row["grade"] or "unknown"): int(row["n"]) for row in grade_rows
            },
            "shadow_requests": max(observation_count, int(evaluation["requests"] or 0)),
            "deep_rescues": int(evaluation["rescues"] or 0),
            "observation_summary_30d": self.memory_access_observation_summary(days=30),
            "activity_trends": {
                "days_7": self.activity_trends(days=7),
                "days_30": self.activity_trends(days=30),
            },
            "latest_maintenance": dict(latest) if latest else None,
            "index_meta": self.index_meta(),
            "safety_gates": self.safety_gates(),
            "algorithm_version": ALGORITHM_VERSION,
        }

    def list_states(self, *, state: str = "", query: str = "", limit: int = 100,
                    offset: int = 0) -> list[dict[str, Any]]:
        clauses, params = ["1=1"], []
        if state in ACCESS_STATES:
            clauses.append("a.access_state=?")
            params.append(state)
        if query:
            clauses.append("(a.memo_name LIKE ? OR e.scene_anchor LIKE ? OR e.retrieval_key LIKE ?)")
            like = f"%{query[:100]}%"
            params.extend([like, like, like])
        rows = self._get_conn().execute(
            f"""SELECT a.*,e.occurred_at,e.event_ts,e.memory_type,e.importance,
                       e.scene_anchor,e.retrieval_key,c.rarity_score,c.cue_count
                FROM memory_access_state a JOIN episodes e ON e.memo_name=a.memo_name
                LEFT JOIN memory_cue_signatures c ON c.memo_name=a.memo_name
                WHERE {' AND '.join(clauses)} ORDER BY a.accessibility DESC,e.event_ts DESC
                LIMIT ? OFFSET ?""",
            (*params, max(1, min(500, int(limit))), max(0, int(offset))),
        ).fetchall()
        return [dict(row) for row in rows]

    def detail(self, memo_name: str) -> dict[str, Any] | None:
        state, cue = self._candidate_state(memo_name)
        if not state or not self._get_conn().execute(
            "SELECT 1 FROM memory_access_state WHERE memo_name=?", (memo_name,)
        ).fetchone():
            return None
        state["cue_signature"] = cue
        state["interference"] = self.list_edges(memo_name=memo_name, limit=40)
        state["events"] = self.list_events(memo_name=memo_name, limit=30)
        reason_data = _loads(state.get("state_reason_json"), {})
        state["state_reason"] = reason_data
        state["decay_eligible"] = bool(int(state["decay_eligible"])) if state.get("decay_eligible") is not None else True
        state["eligibility_status"] = str(reason_data.get("eligibility_status") or (
            "eligible" if state["decay_eligible"] else "protected"
        ))
        state["eligibility_label"] = {
            "eligible": "可自然淡出",
            "protected": "受保护",
            "deferred": "证据不足，暂缓淡出",
        }.get(state["eligibility_status"], state["eligibility_status"])
        state["protection_reasons"] = list(reason_data.get("protection") or [])
        state["pending_state"] = str(reason_data.get("pending_state") or "")
        state["blocked_transition"] = str(reason_data.get("blocked_transition") or "")
        kind = str(state.get("age_reference_kind") or "unknown")
        state["age_reference_label"] = {
            "event": "事实事件时间",
            "source_updated_proxy": "来源更新时间（仅衰减代理，不是发生日期）",
            "unknown": "未知（不使用迁移时间推断新旧）",
        }.get(kind, kind)
        since = float(state.get("state_since_ts") or 0)
        state["state_stable_days"] = round(max(0.0, (time.time() - since) / 86400.0), 2) if since else None
        # Which cue grades could bring this memory back
        dates = [str(item) for item in cue.get("dates") or []]
        entities = [str(item) for item in cue.get("entities") or []]
        quotes = [str(item) for item in cue.get("quotes") or []]
        rescue_grades: dict[str, list[str]] = {"A": [], "B": [], "C": [], "D": []}
        rescue_grades["A"] = ["quote:" + item[:24] for item in quotes[:3]]
        if dates and entities:
            rescue_grades["B"] = [f"date:{dates[0]}+entity:{item}" for item in entities[:3]]
        if entities:
            rescue_grades["C"] = ["entity:" + item for item in entities[:3]]
        if dates:
            rescue_grades["D"] = ["date:" + item for item in dates[:3]]
        state["rescue_cue_grades"] = rescue_grades
        return state

    def list_edges(self, *, memo_name: str = "", edge_type: str = "", limit: int = 100) -> list[dict[str, Any]]:
        clauses, params = ["1=1"], []
        if memo_name:
            clauses.append("(source_memo=? OR target_memo=?)")
            params.extend([memo_name, memo_name])
        if edge_type in EDGE_TYPES:
            clauses.append("edge_type=?")
            params.append(edge_type)
        rows = self._get_conn().execute(
            f"SELECT * FROM memory_interference_edges WHERE {' AND '.join(clauses)} ORDER BY strength DESC LIMIT ?",
            (*params, max(1, min(1000, int(limit)))),
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["route_scores"] = _loads(item.pop("route_scores_json", "{}"), {})
            item["distinctions"] = _loads(item.pop("distinctions_json", "{}"), {})
            item["evidence_routes"] = _loads(item.pop("evidence_routes_json", "{}"), {})
            item["conflict_flags"] = _loads(item.pop("conflict_flags_json", "{}"), {})
            result.append(item)
        return result

    def list_events(self, *, memo_name: str = "", limit: int = 100) -> list[dict[str, Any]]:
        if memo_name:
            rows = self._get_conn().execute(
                "SELECT * FROM memory_access_events WHERE memo_name=? ORDER BY id DESC LIMIT ?",
                (memo_name, max(1, min(1000, int(limit)))),
            ).fetchall()
        else:
            rows = self._get_conn().execute(
                "SELECT * FROM memory_access_events ORDER BY id DESC LIMIT ?",
                (max(1, min(1000, int(limit))),),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["detail"] = _loads(item.pop("detail_json", "{}"), {})
            result.append(item)
        return result

    def delete_memo(self, memo_name: str) -> None:
        with self._lock:
            conn = self._get_conn()
            conn.execute("DELETE FROM memory_access_state WHERE memo_name=?", (memo_name,))
            conn.execute("DELETE FROM memory_cue_signatures WHERE memo_name=?", (memo_name,))
            conn.execute("DELETE FROM memory_cue_terms WHERE memo_name=?", (memo_name,))
            conn.execute("DELETE FROM memory_interference_edges WHERE source_memo=? OR target_memo=?", (memo_name, memo_name))
            conn.execute("DELETE FROM memory_access_events WHERE memo_name=?", (memo_name,))
            conn.execute("DELETE FROM memory_interference_members WHERE memo_name=?", (memo_name,))
            # Evaluation cases referencing a removed memo would silently fail forever.
            conn.execute(
                """DELETE FROM memory_access_eval_results WHERE case_id IN
                   (SELECT case_id FROM memory_access_eval_cases WHERE memo_name=?)""",
                (memo_name,),
            )
            conn.execute("DELETE FROM memory_access_eval_cases WHERE memo_name=?", (memo_name,))
            conn.commit()
