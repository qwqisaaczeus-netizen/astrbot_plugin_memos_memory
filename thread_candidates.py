"""Bounded candidate blocking over rebuildable, scoped SQLite postings.

Cold databases need backfill_batch(); foreground lookups only refresh their
anchor and a bounded dirty queue. Common terms cannot hide rare-term routes.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from typing import Any, Callable

MAX_CANDIDATES = 50
MAX_DATE_WINDOW_DAYS = 30
POSTING_LIMIT = 81
_STOP_TERMS = frozenset({"我", "你", "他", "她", "它", "的", "了", "在", "是", "有", "和", "与", "或", "很", "都", "不", "就", "也", "还", "这", "那", "说", "想", "让", "好", "哦", "嗯", "啊", "嘛", "呢", "吧", "过", "着", "聊天", "难过", "开心", "喜欢", "感觉", "知道", "事情", "时候", "今天", "昨天", "后来"})
_STOP_ENTITIES = frozenset({"我", "你", "他", "她", "它", "我们", "你们", "他们", "对方"})


def _terms(text: str) -> list[str]:
    tokens = re.findall(r"[\u4e00-\u9fff]{2,6}|[a-zA-Z0-9]{2,32}", str(text or "")[:16000])
    return list(dict.fromkeys(t.lower() for t in tokens if t not in _STOP_TERMS))[:24]


def _json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    try:
        parsed = json.loads(str(value or "[]"))
        return parsed if isinstance(parsed, list) else []
    except (TypeError, ValueError):
        return []


class ThreadCandidates:
    def __init__(self, get_conn: Callable[[], Any]):
        self._get_conn = get_conn
        self.last_metrics: dict[str, Any] = {}

    @staticmethod
    def init_schema(conn: Any) -> None:
        conn.execute("""CREATE TABLE IF NOT EXISTS thread_candidate_episodes(
            scope_id TEXT NOT NULL, episode_id TEXT NOT NULL, event_ts REAL NOT NULL,
            source_batch_id TEXT NOT NULL, PRIMARY KEY(scope_id,episode_id))""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_candidate_date ON thread_candidate_episodes(scope_id,event_ts,episode_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_candidate_batch ON thread_candidate_episodes(scope_id,source_batch_id,episode_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_candidate_episode ON thread_candidate_episodes(episode_id)")
        conn.execute("""CREATE TABLE IF NOT EXISTS thread_candidate_postings(
            scope_id TEXT NOT NULL, kind TEXT NOT NULL, term TEXT NOT NULL,
            episode_id TEXT NOT NULL, PRIMARY KEY(scope_id,kind,term,episode_id))""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_candidate_posting_episode ON thread_candidate_postings(episode_id)")
        conn.execute("CREATE TABLE IF NOT EXISTS thread_candidate_dirty(episode_id TEXT PRIMARY KEY)")
        # Source values are never changed; triggers invalidate derived data only.
        # ThreadStore can be used standalone in tests and migrations before the
        # host creates its source episode table, so skip triggers until it exists.
        source_tables = {str(row[0]) for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        for table, key in (("episodes", "episode_id"), ("thread_episode_scopes", "episode_id")):
            if table not in source_tables:
                continue
            for action, ref in (("INSERT", "NEW"), ("UPDATE", "NEW"), ("DELETE", "OLD")):
                conn.execute(f"""CREATE TRIGGER IF NOT EXISTS thread_candidate_{table}_{action.lower()}
                    AFTER {action} ON {table} BEGIN
                    INSERT INTO thread_candidate_dirty(episode_id) VALUES({ref}.{key}) ON CONFLICT(episode_id) DO NOTHING; END""")

    def refresh_episode(
        self,
        episode_id: str,
        *,
        commit: bool = True,
        legacy_entity_projection: bool = False,
    ) -> bool:
        conn = self._get_conn()
        ep = self._load_episode(conn, episode_id)
        scopes = [""] + [str(r[0]) for r in conn.execute(
            "SELECT scope_id FROM thread_episode_scopes WHERE episode_id=?", (episode_id,)) if r[0]]
        content = json.dumps([ep, scopes], ensure_ascii=False, sort_keys=True, default=str)
        digest = hashlib.sha256(content.encode()).hexdigest()
        old = conn.execute("SELECT content_hash FROM thread_episode_term_state WHERE episode_id=?", (episode_id,)).fetchone()
        changed = not old or old[0] != digest
        entities = sorted(
            {str(x).strip().lower()[:200] for x in _json_list(ep.get("entities_json"))[:24]}
            - _STOP_ENTITIES
            - {""}
        ) if ep else []
        if changed:
            conn.execute("DELETE FROM thread_candidate_postings WHERE episode_id=?", (episode_id,))
            conn.execute("DELETE FROM thread_candidate_episodes WHERE episode_id=?", (episode_id,))
            conn.execute("DELETE FROM thread_episode_terms WHERE episode_id=?", (episode_id,))
            if ep:
                terms = _terms(f"{ep.get('scene_anchor', '')} {ep.get('retrieval_key', '')}")
                conn.executemany("INSERT INTO thread_candidate_episodes VALUES(?,?,?,?)", [
                    (s, episode_id, float(ep.get("event_ts") or 0), str(ep.get("source_batch_id") or "")) for s in scopes])
                conn.executemany("INSERT OR IGNORE INTO thread_candidate_postings VALUES(?,?,?,?)", [
                    (s, kind, t, episode_id) for s in scopes for kind, values in (("term", terms), ("entity", entities)) for t in values])
                conn.executemany("INSERT OR IGNORE INTO thread_episode_terms VALUES(?,?)", [(episode_id, t) for t in terms])
            entity_hash = hashlib.sha256(
                json.dumps(_json_list(ep.get("entities_json")) if ep else [], ensure_ascii=False).encode()
            ).hexdigest()
            source_updated = float(ep.get("updated_ts") or 0) if ep else time.time()
            conn.execute("""INSERT INTO thread_episode_term_state(
                    episode_id,content_hash,entity_hash,updated_ts)
                VALUES(?,?,?,?) ON CONFLICT(episode_id) DO UPDATE SET
                    content_hash=excluded.content_hash,
                    entity_hash=excluded.entity_hash,
                    updated_ts=excluded.updated_ts""",
                (episode_id, digest, entity_hash, source_updated),
            )
        if legacy_entity_projection:
            # test9 offline tools still expose this projection. It is not read by
            # the test10 online path, so keep its write amplification out of
            # foreground and bulk candidate refreshes.
            conn.execute("DELETE FROM thread_episode_entities WHERE episode_id=?", (episode_id,))
            if entities:
                conn.executemany(
                    "INSERT OR IGNORE INTO thread_episode_entities VALUES(?,?)",
                    [(episode_id, entity) for entity in entities],
                )
        conn.execute("DELETE FROM thread_candidate_dirty WHERE episode_id=?", (episode_id,))
        if commit:
            conn.commit()
        return changed

    def _refresh_term_index(self, conn: Any, limit: int = 500) -> dict[str, int]:
        """Compatibility drain for test9 callers, backed by test10 postings."""
        rows = conn.execute(
            """SELECT episode_id FROM thread_candidate_dirty
               ORDER BY episode_id LIMIT ?""",
            (max(1, min(2000, int(limit))),),
        ).fetchall()
        changed = removed = 0
        try:
            for row in rows:
                episode_id = str(row[0])
                exists = conn.execute(
                    "SELECT 1 FROM episodes WHERE episode_id=? AND active=1",
                    (episode_id,),
                ).fetchone()
                did_change = self.refresh_episode(
                    episode_id,
                    commit=False,
                    legacy_entity_projection=True,
                )
                changed += int(did_change)
                removed += int(did_change and not exists)
                conn.execute(
                    "DELETE FROM thread_episode_index_queue WHERE episode_id=?",
                    (episode_id,),
                )
            bootstrap = conn.execute(
                """SELECT value FROM thread_migration_state
                   WHERE key='candidate_projection_v2_bootstrap'"""
            ).fetchone()
            if (
                not conn.execute("SELECT 1 FROM thread_episode_index_queue LIMIT 1").fetchone()
                and bootstrap
                and str(bootstrap[0]) != "complete"
            ):
                conn.execute(
                    """UPDATE thread_migration_state SET value='complete',updated_ts=?
                       WHERE key='candidate_projection_v2_bootstrap'""",
                    (time.time(),),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return {"queued": len(rows), "changed": changed, "removed": removed}

    def refresh_dirty_batch(self, limit: int = 64, *, commit: bool = True) -> dict[str, int]:
        conn = self._get_conn()
        rows = conn.execute("SELECT episode_id FROM thread_candidate_dirty ORDER BY episode_id LIMIT ?", (max(1, min(500, int(limit))),)).fetchall()
        changed = sum(self.refresh_episode(str(r[0]), commit=False) for r in rows)
        if commit:
            conn.commit()
        return {"processed": len(rows), "changed": changed}

    def backfill_batch(self, *, after_episode_id: str = "", limit: int = 200) -> dict[str, Any]:
        """Explicit keyset backfill. Persist returned cursor in the caller."""
        conn = self._get_conn()
        rows = conn.execute("SELECT episode_id FROM episodes WHERE episode_id>? ORDER BY episode_id LIMIT ?", (after_episode_id, max(1, min(500, int(limit))),)).fetchall()
        try:
            for row in rows:
                self.refresh_episode(
                    str(row[0]),
                    commit=False,
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return {"processed": len(rows), "after_episode_id": str(rows[-1][0]) if rows else after_episode_id, "completed": not rows}

    def candidates_for(self, episode_id: str, scope_id: str) -> list[dict[str, Any]]:
        conn = self._get_conn()
        # Callers sharing a connection must hold its lock across this method.
        try:
            self.refresh_episode(episode_id, commit=False)
            self.refresh_dirty_batch(64, commit=False)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        ep = self._load_episode(conn, episode_id, scope_id)
        self.last_metrics = {"postings_read": 0, "route_queries": 0, "common_routes_skipped": 0}
        if not ep:
            return []
        scope = str(scope_id or "")
        if scope and not conn.execute("SELECT 1 FROM thread_episode_scopes WHERE episode_id=? AND scope_id=?", (episode_id, scope)).fetchone():
            return []
        seen: dict[str, dict[str, Any]] = {}

        def add(other: str, reason: str, score: float) -> None:
            if other == episode_id:
                return
            item = seen.setdefault(other, {"reasons": [], "score": 0.0})
            if reason not in item["reasons"]:
                item["reasons"].append(reason)
            item["score"] = max(item["score"], score)

        values = (("entity", sorted({str(v).strip().lower()[:200] for v in _json_list(ep.get("entities_json"))[:24]} - _STOP_ENTITIES - {""}), .72),
                  ("term", _terms(f"{ep.get('scene_anchor', '')} {ep.get('retrieval_key', '')}"), .58))
        for kind, terms, score in values:
            for term in terms:
                rows = conn.execute("""SELECT episode_id FROM thread_candidate_postings
                    WHERE scope_id=? AND kind=? AND term=? ORDER BY episode_id LIMIT ?""", (scope, kind, term, POSTING_LIMIT)).fetchall()
                self.last_metrics["route_queries"] += 1
                self.last_metrics["postings_read"] += len(rows)
                if len(rows) >= POSTING_LIMIT:
                    self.last_metrics["common_routes_skipped"] += 1
                    # A common posting must not become a recall blind spot. Keep a
                    # small deterministic neighborhood around the anchor ID; this
                    # remains index-bounded while preserving a weak route for old,
                    # far-apart retellings that the date window cannot recover.
                    before = conn.execute(
                        """SELECT episode_id FROM thread_candidate_postings
                           WHERE scope_id=? AND kind=? AND term=? AND episode_id<?
                           ORDER BY episode_id DESC LIMIT 8""",
                        (scope, kind, term, episode_id),
                    ).fetchall()
                    after = conn.execute(
                        """SELECT episode_id FROM thread_candidate_postings
                           WHERE scope_id=? AND kind=? AND term=? AND episode_id>?
                           ORDER BY episode_id ASC LIMIT 8""",
                        (scope, kind, term, episode_id),
                    ).fetchall()
                    self.last_metrics["route_queries"] += 2
                    self.last_metrics["postings_read"] += len(before) + len(after)
                    for row in (*before, *after):
                        add(str(row[0]), f"common_{kind}:{term}", score * .72)
                    continue
                for row in rows:
                    add(str(row[0]), f"{kind}:{term}", score)
        batch = str(ep.get("source_batch_id") or "")
        if batch:
            rows = conn.execute("""SELECT episode_id FROM thread_candidate_episodes WHERE scope_id=?
                AND source_batch_id=? AND episode_id<>? ORDER BY episode_id LIMIT 100""", (scope, batch, episode_id)).fetchall()
            self.last_metrics["postings_read"] += len(rows)
            for row in rows:
                add(str(row[0]), "same_source_batch", .90)
        ts = float(ep.get("event_ts") or 0)
        if ts > 0:
            window = MAX_DATE_WINDOW_DAYS * 86400
            for op, order, bound in (("<=", "DESC", ts - window), (">", "ASC", ts + window)):
                range_op = ">=" if op == "<=" else "<="
                rows = conn.execute(f"""SELECT episode_id,event_ts FROM thread_candidate_episodes
                    WHERE scope_id=? AND event_ts{op}? AND event_ts{range_op}? AND event_ts>0
                    ORDER BY event_ts {order},episode_id {order} LIMIT ?""", (scope, ts, bound, MAX_CANDIDATES + 1)).fetchall()
                self.last_metrics["postings_read"] += len(rows)
                for row in rows:
                    add(str(row[0]), "date_proximity", .34 - min(.14, abs(float(row[1]) - ts) / 86400 / 250))
        ranked = []
        for other, item in seen.items():
            reasons = item["reasons"]
            priority = item["score"] + min(.20, .05 * max(0, len({r.split(':')[0] for r in reasons}) - 1))
            a, b = sorted((episode_id, other))
            ranked.append({"episode_id_a": a, "episode_id_b": b, "blocking_reasons": reasons, "blocking_score": round(min(1., priority), 4)})
        ranked.sort(key=lambda p: (-p["blocking_score"], -len(p["blocking_reasons"]), p["episode_id_a"], p["episode_id_b"]))
        self.last_metrics.update({"unique_candidates": len(seen), "returned": min(MAX_CANDIDATES, len(ranked))})
        return ranked[:MAX_CANDIDATES]

    @staticmethod
    def _load_episode(conn: Any, episode_id: str, scope_id: str = "") -> dict[str, Any] | None:
        row = conn.execute("""SELECT episode_id,memo_name,occurred_at,event_ts,time_basis,memory_type,
            scene_anchor,retrieval_key,entities_json,state_change,long_effect,source_batch_id,
            evidence_quality,unresolved_json,card_text,updated_ts
            FROM episodes WHERE episode_id=? AND active=1""", (str(episode_id),)).fetchone()
        return dict(row) if row else None
