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
EDGE_TYPES = (
    "same_event_restated",
    "same_theme_distinct_event",
    "contradictory_stage",
    "causal_chain",
    "shared_surface",
)

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
                created_ts REAL NOT NULL DEFAULT 0,
                updated_ts REAL NOT NULL DEFAULT 0,
                FOREIGN KEY(memo_name) REFERENCES episodes(memo_name) ON DELETE CASCADE
            )"""
        )
        columns = {
            str(row["name"]) for row in conn.execute("PRAGMA table_info(memory_access_state)").fetchall()
        }
        if "event_ts" not in columns:
            conn.execute("ALTER TABLE memory_access_state ADD COLUMN event_ts REAL NOT NULL DEFAULT 0")
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
                updated_ts REAL NOT NULL DEFAULT 0,
                PRIMARY KEY(source_memo,target_memo,edge_type),
                FOREIGN KEY(source_memo) REFERENCES episodes(memo_name) ON DELETE CASCADE,
                FOREIGN KEY(target_memo) REFERENCES episodes(memo_name) ON DELETE CASCADE
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
        conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_access_state ON memory_access_state(access_state,accessibility DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_access_events_request ON memory_access_events(request_id,id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_access_events_memo ON memory_access_events(memo_name,created_ts DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_edges_source ON memory_interference_edges(source_memo,strength DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_edges_target ON memory_interference_edges(target_memo,strength DESC)")

    @staticmethod
    def _content_hash(episode: dict[str, Any]) -> str:
        payload = "\u241f".join([
            str(episode.get("memo_name") or ""), _event_date(episode),
            str(episode.get("importance") or ""), _episode_text(episode),
        ])
        return hashlib.sha256(payload.encode("utf-8", "ignore")).hexdigest()

    @staticmethod
    def _base_dimensions(episode: dict[str, Any], signature: dict[str, Any], now: float) -> dict[str, float]:
        try:
            importance = max(1, min(5, int(episode.get("importance") or 3)))
        except (TypeError, ValueError):
            importance = 3
        memory_type = str(episode.get("memory_type") or "event").lower()
        persistence = 0.34 + (importance - 1) * 0.105
        if memory_type in _DURABLE_TYPES:
            persistence += 0.10
        if str(episode.get("long_effect") or "").strip():
            persistence += 0.05
        if json_list(episode.get("unresolved")) or json_list(episode.get("unresolved_json")):
            persistence += 0.04
        try:
            event_ts = float(episode.get("event_ts") or 0)
        except (TypeError, ValueError):
            event_ts = 0.0
        age_days = max(0.0, (now - event_ts) / 86400.0) if event_ts > 0 else 365.0
        vividness = 0.18 + 0.60 * math.exp(-age_days / 75.0)
        if signature.get("quotes"):
            vividness += 0.08
        if signature.get("entities"):
            vividness += min(0.08, len(signature["entities"]) * 0.015)
        evidence_quality = str(episode.get("evidence_quality") or "diary_derived")
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

    def sync_episode(self, episode: dict[str, Any], *, now: float | None = None,
                     vivid_threshold: float = 0.68, deep_threshold: float = 0.32) -> dict[str, Any]:
        memo_name = str(episode.get("memo_name") or "").strip()
        if not memo_name:
            return {"updated": False, "reason": "missing_memo_name"}
        now = float(now or time.time())
        try:
            event_ts = float(episode.get("event_ts") or 0)
        except (TypeError, ValueError):
            event_ts = 0.0
        signature = _cue_signature(episode)
        dimensions = self._base_dimensions(episode, signature, now)
        content_hash = self._content_hash(episode)
        with self._lock:
            conn = self._get_conn()
            old = conn.execute(
                "SELECT * FROM memory_access_state WHERE memo_name=?", (memo_name,)
            ).fetchone()
            old_data = dict(old) if old else {}
            if old_data.get("content_hash") == content_hash:
                persistence = float(old_data.get("persistence") or dimensions["persistence"])
                distinctiveness = float(old_data.get("distinctiveness") or dimensions["distinctiveness"])
                created_ts = float(old_data.get("created_ts") or now)
            else:
                persistence = dimensions["persistence"]
                distinctiveness = dimensions["distinctiveness"]
                created_ts = float(old_data.get("created_ts") or now)
            vividness = max(dimensions["vividness"], float(old_data.get("vividness") or 0) * 0.94)
            interference = float(old_data.get("interference_load") or 0)
            inhibition = float(old_data.get("inhibition") or 0)
            recon = min(0.14, int(old_data.get("reconsolidation_count") or 0) * 0.012)
            accessibility = _clamp(
                0.43 * persistence + 0.30 * vividness + 0.17 * distinctiveness
                + dimensions["grounding_bonus"] + recon - interference - inhibition
            )
            access_state = self._state_for(
                accessibility, str(old_data.get("access_state") or ""),
                vivid_threshold, deep_threshold,
            )
            conn.execute(
                """INSERT INTO memory_access_state(
                    memo_name,content_hash,access_state,accessibility,persistence,vividness,
                    distinctiveness,grounding_bonus,interference_load,inhibition,
                    retrieval_count,successful_use_count,reconsolidation_count,
                    last_retrieved_ts,last_used_ts,last_reconsolidated_ts,event_ts,created_ts,updated_ts
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(memo_name) DO UPDATE SET
                    content_hash=excluded.content_hash,access_state=excluded.access_state,
                    accessibility=excluded.accessibility,persistence=excluded.persistence,
                    vividness=excluded.vividness,distinctiveness=excluded.distinctiveness,
                    grounding_bonus=excluded.grounding_bonus,event_ts=excluded.event_ts,
                    updated_ts=excluded.updated_ts""",
                (
                    memo_name, content_hash, access_state, accessibility, persistence,
                    vividness, distinctiveness, dimensions["grounding_bonus"], interference,
                    inhibition, int(old_data.get("retrieval_count") or 0),
                    int(old_data.get("successful_use_count") or 0),
                    int(old_data.get("reconsolidation_count") or 0),
                    float(old_data.get("last_retrieved_ts") or 0),
                    float(old_data.get("last_used_ts") or 0),
                    float(old_data.get("last_reconsolidated_ts") or 0), event_ts, created_ts, now,
                ),
            )
            cue_count = sum(len(signature.get(key) or []) for key in ("dates", "entities", "quotes", "retrieval_terms"))
            conn.execute(
                """INSERT INTO memory_cue_signatures(memo_name,signature_json,rarity_score,cue_count,updated_ts)
                   VALUES(?,?,?,?,?) ON CONFLICT(memo_name) DO UPDATE SET
                   signature_json=excluded.signature_json,cue_count=excluded.cue_count,updated_ts=excluded.updated_ts""",
                (memo_name, _json(signature), 0.5, cue_count, now),
            )
            conn.commit()
        return {"updated": True, "memo_name": memo_name, "access_state": access_state,
                "accessibility": accessibility}

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
        return len(signatures)

    @staticmethod
    def _edge(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any] | None:
        ls = left["signature"]
        rs = right["signature"]
        l_retrieval = set(ls.get("retrieval_terms") or [])
        r_retrieval = set(rs.get("retrieval_terms") or [])
        l_content = set(ls.get("content_terms") or [])
        r_content = set(rs.get("content_terms") or [])
        l_entities = set(ls.get("entities") or [])
        r_entities = set(rs.get("entities") or [])
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
        same_event = _clamp(0.34 * retrieval + 0.28 * content + 0.22 * entity + 0.16 * date)
        if date and same_event >= 0.34 and (entity > 0 or retrieval >= 0.30):
            edge_type = "same_event_restated"
            strength = same_event
            competition = _clamp((same_event - 0.25) * 0.76)
        elif contradiction and (entity > 0 or surface >= 0.18):
            edge_type = "contradictory_stage"
            strength = _clamp(0.48 + 0.24 * entity + 0.20 * surface)
            competition = 0.04  # disagreement is informative; preserve both stages.
        elif not date and surface >= 0.27 and (entity > 0 or retrieval >= 0.34):
            edge_type = "same_theme_distinct_event"
            strength = _clamp(0.42 * retrieval + 0.34 * content + 0.24 * entity)
            competition = _clamp(strength * 0.18, 0.0, 0.12)
        elif entity > 0 and (
            _jaccard(extract_terms(str(left.get("long_effect") or "")), right_state) >= 0.16
            or _jaccard(extract_terms(str(right.get("long_effect") or "")), left_state) >= 0.16
        ):
            edge_type = "causal_chain"
            strength = _clamp(0.40 + 0.25 * entity + 0.20 * surface)
            competition = 0.0
        elif surface >= 0.22:
            edge_type = "shared_surface"
            strength = _clamp(surface)
            competition = _clamp((surface - 0.22) * 0.12, 0.0, 0.05)
        else:
            return None
        return {
            "edge_type": edge_type,
            "strength": strength,
            "competition": competition,
            "route_scores": {"retrieval": retrieval, "content": content, "entity": entity, "date": date,
                             "contradiction": contradiction},
            "distinctions": {"left_dates": sorted(l_dates)[:4], "right_dates": sorted(r_dates)[:4],
                             "shared_entities": sorted(l_entities & r_entities)[:8]},
        }

    def rebuild_interference(self, episodes: list[dict[str, Any]] | None = None,
                             *, max_neighbors: int = 12, now: float | None = None) -> dict[str, Any]:
        now = float(now or time.time())
        if episodes is None:
            rows = self._get_conn().execute(
                """SELECT e.memo_name,e.state_change,e.long_effect,c.signature_json
                   FROM episodes e JOIN memory_cue_signatures c ON c.memo_name=e.memo_name
                   WHERE e.active=1"""
            ).fetchall()
            records = [dict(row) for row in rows]
            for item in records:
                item["signature"] = _loads(item.pop("signature_json", "{}"), {})
        else:
            records = [dict(item, signature=_cue_signature(item)) for item in episodes if item.get("memo_name")]
        by_term: dict[str, set[int]] = defaultdict(set)
        for index, item in enumerate(records):
            signature = item["signature"]
            keys = set(signature.get("retrieval_terms") or []) | set(signature.get("entities") or []) | set(signature.get("dates") or [])
            for term in list(keys)[:160]:
                by_term[term].add(index)
        pair_candidates: set[tuple[int, int]] = set()
        for indexes in by_term.values():
            ordered = sorted(indexes)
            for pos, left in enumerate(ordered):
                for right in ordered[pos + 1:pos + 1 + max(40, max_neighbors * 4)]:
                    pair_candidates.add((left, right))
        edges_by_source: dict[str, list[tuple[str, dict[str, Any]]]] = defaultdict(list)
        for left_index, right_index in pair_candidates:
            left, right = records[left_index], records[right_index]
            edge = self._edge(left, right)
            if edge:
                left_name, right_name = str(left["memo_name"]), str(right["memo_name"])
                edges_by_source[left_name].append((right_name, edge))
                edges_by_source[right_name].append((left_name, edge))
        kept: list[tuple[str, str, dict[str, Any]]] = []
        for source, targets in edges_by_source.items():
            targets.sort(key=lambda item: (float(item[1]["strength"]), item[0]), reverse=True)
            for target, edge in targets[:max(2, min(40, int(max_neighbors)))]:
                kept.append((source, target, edge))
        with self._lock:
            conn = self._get_conn()
            conn.execute("DELETE FROM memory_interference_edges")
            conn.execute("DELETE FROM memory_interference_members")
            conn.execute("DELETE FROM memory_interference_groups")
            for source, target, edge in kept:
                conn.execute(
                    """INSERT OR REPLACE INTO memory_interference_edges(
                       source_memo,target_memo,edge_type,strength,competition,
                       route_scores_json,distinctions_json,updated_ts) VALUES(?,?,?,?,?,?,?,?)""",
                    (source, target, edge["edge_type"], edge["strength"], edge["competition"],
                     _json(edge["route_scores"]), _json(edge["distinctions"]), now),
                )
            components: list[set[str]] = []
            adjacency: dict[str, set[str]] = defaultdict(set)
            for source, target, edge in kept:
                if edge["edge_type"] in {"same_event_restated", "contradictory_stage"} and edge["strength"] >= 0.48:
                    adjacency[source].add(target)
                    adjacency[target].add(source)
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
                strengths = [edge["strength"] for source, target, edge in kept if source in component and target in component]
                conn.execute(
                    "INSERT INTO memory_interference_groups(group_id,member_count,max_strength,updated_ts) VALUES(?,?,?,?)",
                    (group_id, len(component), max(strengths or [0.0]), now),
                )
                for memo_name in sorted(component):
                    conn.execute("INSERT INTO memory_interference_members(group_id,memo_name) VALUES(?,?)", (group_id, memo_name))
            conn.execute("UPDATE memory_access_state SET interference_load=0")
            loads = conn.execute(
                """SELECT source_memo,COALESCE(SUM(competition),0) AS load
                   FROM memory_interference_edges GROUP BY source_memo"""
            ).fetchall()
            for row in loads:
                conn.execute(
                    "UPDATE memory_access_state SET interference_load=? WHERE memo_name=?",
                    (_clamp(float(row["load"]), 0.0, 0.24), str(row["source_memo"])),
                )
            conn.commit()
        return {"episodes": len(records), "pairs": len(pair_candidates), "edges": len(kept),
                "groups": len(components)}

    def rebuild(self, episodes: list[dict[str, Any]], *, config: dict[str, Any] | None = None,
                reason: str = "backfill") -> dict[str, Any]:
        config = config or {}
        now = time.time()
        updated = 0
        for episode in episodes:
            result = self.sync_episode(
                episode, now=now,
                vivid_threshold=float(config.get("vivid_threshold", 0.68)),
                deep_threshold=float(config.get("deep_threshold", 0.32)),
            )
            updated += int(bool(result.get("updated")))
        with self._lock:
            self._refresh_rarity()
            edges = self.rebuild_interference(
                episodes if bool(config.get("interference_enable", True)) else [],
                max_neighbors=int(config.get("max_neighbors", 12)), now=now,
            )
            maintenance = self.maintain(config=config, now=now, reason=reason, record=False)
            conn = self._get_conn()
            conn.execute(
                "INSERT INTO memory_access_maintenance(reason,episodes,states_changed,edges,detail_json,created_ts) VALUES(?,?,?,?,?,?)",
                (reason, len(episodes), int(maintenance.get("states_changed") or 0), int(edges["edges"]),
                 _json({"updated": updated, "groups": edges["groups"]}), now),
            )
            conn.commit()
        return {"updated": updated, **edges, **maintenance}

    def maintain(self, *, config: dict[str, Any] | None = None, now: float | None = None,
                 reason: str = "scheduled", record: bool = True) -> dict[str, Any]:
        config = config or {}
        now = float(now or time.time())
        half_life = max(7.0, float(config.get("decay_days", 45.0)))
        vivid_threshold = float(config.get("vivid_threshold", 0.68))
        deep_threshold = float(config.get("deep_threshold", 0.32))
        changed = 0
        with self._lock:
            conn = self._get_conn()
            rows = conn.execute("SELECT * FROM memory_access_state").fetchall()
            for raw in rows:
                row = dict(raw)
                reference_ts = max(
                    float(row.get("last_used_ts") or 0),
                    float(row.get("last_reconsolidated_ts") or 0),
                    float(row.get("event_ts") or 0),
                    float(row.get("created_ts") or 0) if not float(row.get("event_ts") or 0) else 0,
                )
                age_days = max(0.0, (now - reference_ts) / 86400.0) if reference_ts else 365.0
                persistence = _clamp(float(row["persistence"]))
                memory_half_life = half_life * (0.55 + persistence * 1.65)
                recency = (
                    math.exp(-age_days / memory_half_life)
                    if bool(config.get("decay_enable", True))
                    else max(0.85, float(row.get("vividness") or 0.85))
                )
                recon = min(0.14, int(row.get("reconsolidation_count") or 0) * 0.012)
                accessibility = _clamp(
                    0.48 * persistence + 0.24 * recency
                    + 0.18 * float(row["distinctiveness"])
                    + float(row["grounding_bonus"]) + recon
                    - float(row["interference_load"]) - float(row["inhibition"])
                )
                state = self._state_for(accessibility, str(row["access_state"]), vivid_threshold, deep_threshold)
                if state != row["access_state"]:
                    changed += 1
                conn.execute(
                    "UPDATE memory_access_state SET access_state=?,accessibility=?,vividness=?,updated_ts=? WHERE memo_name=?",
                    (state, accessibility, _clamp(recency), now, row["memo_name"]),
                )
            if record:
                conn.execute(
                    "INSERT INTO memory_access_maintenance(reason,episodes,states_changed,edges,detail_json,created_ts) VALUES(?,?,?,?,?,?)",
                    (reason, len(rows), changed, self._edge_count(conn), _json({"half_life_days": half_life}), now),
                )
            conn.commit()
        return {"episodes": len(rows), "states_changed": changed}

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
        items: list[dict[str, Any]] = []
        for rank, hit in enumerate(candidates):
            memo_name = str(hit.get("memo_name") or hit.get("name") or "").strip()
            if not memo_name:
                continue
            state, cue = self._candidate_state(memo_name)
            base = self._base_score(hit)
            cue_support, cue_detail = self._cue_support(query, cue)
            route_support = self._route_support(hit)
            access = _clamp(float(state.get("accessibility") or 0.5))
            interference = (
                _clamp(float(state.get("interference_load") or 0), 0.0, 0.35)
                if interference_enable else 0.0
            )
            cost = (1.0 - access) * 0.16 + interference * 0.30
            if cue_detail["exact"]:
                cost *= 1.0 - exact_relief
            access_score = _clamp(base + cue_support * 0.16 + route_support * 0.08 + bias - cost)
            access_state = str(state.get("access_state") or "vivid")
            if cue_detail["exact"]:
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
                "interference_load": interference, "exact_cue": bool(cue_detail["exact"]),
                "rescued": rescued, "presentation": presentation, "cue_detail": cue_detail,
            }
            items.append(item)
        ranked = sorted(items, key=lambda item: (item["access_score"], item["base_score"], -item["rank"]), reverse=True)
        original = [item["memo_name"] for item in items if item["selected"]]
        target_count = max(1, len(original)) if original else min(3, len(ranked))
        recommended = [item["memo_name"] for item in ranked[:target_count]]
        changed = [name for name in recommended if name not in selected_set]
        result = {
            "enabled": enabled, "shadow": shadow, "request_id": request_id,
            "candidate_count": len(items), "selected_count": len(original),
            "state_counts": dict(Counter(item["access_state"] for item in items)),
            "exact_cue_count": sum(int(item["exact_cue"]) for item in items),
            "deep_rescue_count": sum(int(item["rescued"]) for item in items),
            "original": original, "recommended": recommended, "changed": changed,
            "would_change": recommended != original[:target_count], "items": ranked,
        }
        if record and enabled:
            self._record_evaluation(result)
        return result

    def _record_evaluation(self, evaluation: dict[str, Any]) -> None:
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
                     }), now),
                )
                if item["selected"]:
                    conn.execute(
                        "UPDATE memory_access_state SET retrieval_count=retrieval_count+1,last_retrieved_ts=? WHERE memo_name=?",
                        (now, item["memo_name"]),
                    )
            conn.commit()

    def record_response_use(self, *, request_id: str, response_text: str,
                            memo_names: list[str], shadow: bool = True,
                            reconsolidate: bool = True, event_keep: int = 4000) -> dict[str, Any]:
        response_terms = extract_terms(response_text)
        now = time.time()
        used = 0
        with self._lock:
            conn = self._get_conn()
            for memo_name in dict.fromkeys(str(item) for item in memo_names if item):
                row = conn.execute("SELECT signature_json FROM memory_cue_signatures WHERE memo_name=?", (memo_name,)).fetchone()
                signature = _loads(row["signature_json"], {}) if row else {}
                memory_terms = set(signature.get("retrieval_terms") or []) | set(signature.get("entities") or []) | set(signature.get("content_terms") or [])
                support = _jaccard(response_terms, memory_terms)
                is_used = support >= 0.035 or bool(set(signature.get("entities") or []) & response_terms)
                used += int(is_used)
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
            keep = max(200, min(50000, int(event_keep)))
            conn.execute(
                "DELETE FROM memory_access_events WHERE id NOT IN (SELECT id FROM memory_access_events ORDER BY id DESC LIMIT ?)",
                (keep,),
            )
            conn.commit()
        return {"memories": len(set(memo_names)), "used": used, "shadow": shadow}

    def overview(self) -> dict[str, Any]:
        conn = self._get_conn()
        states = {row["access_state"]: int(row["n"]) for row in conn.execute(
            "SELECT access_state,COUNT(*) AS n FROM memory_access_state GROUP BY access_state"
        ).fetchall()}
        events = int(conn.execute("SELECT COUNT(*) AS n FROM memory_access_events").fetchone()["n"])
        groups = int(conn.execute("SELECT COUNT(*) AS n FROM memory_interference_groups").fetchone()["n"])
        edges = self._edge_count(conn)
        latest = conn.execute("SELECT * FROM memory_access_maintenance ORDER BY id DESC LIMIT 1").fetchone()
        evaluation = conn.execute(
            """SELECT COUNT(DISTINCT request_id) AS requests,
                      SUM(CASE WHEN json_extract(detail_json,'$.rescued')=1 THEN 1 ELSE 0 END) AS rescues
               FROM memory_access_events WHERE event_kind='shadow_evaluation'"""
        ).fetchone()
        return {
            "total": sum(states.values()), "states": {key: states.get(key, 0) for key in ACCESS_STATES},
            "edges": edges, "groups": groups, "events": events,
            "shadow_requests": int(evaluation["requests"] or 0),
            "deep_rescues": int(evaluation["rescues"] or 0),
            "latest_maintenance": dict(latest) if latest else None,
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
            conn.execute("DELETE FROM memory_interference_edges WHERE source_memo=? OR target_memo=?", (memo_name, memo_name))
            conn.execute("DELETE FROM memory_access_events WHERE memo_name=?", (memo_name,))
            conn.commit()
