"""Five independent local evidence routes for 6.0.0-test5.

Given two episode dicts, compute five independent evidence routes and
return a structured result. No LLM is called here.

Routes:
  A. entity_anchor   — shared entities, rarity, alias match
  B. semantic_event  — cosine similarity of card embeddings
  C. temporal        — date ordering, proximity, explicit conflict
  D. state_change    — unresolved/long_effect/state_change overlap
  E. source_evidence — exact turn overlap, same batch, evidence quality
"""
from __future__ import annotations

import json
import math
import re
import time
from typing import Any

from .store_utils import cosine as _cosine


# --------------------------------------------------------------------------- #
#  helpers                                                                      #
# --------------------------------------------------------------------------- #

def _json_list(value: Any) -> list:
    if isinstance(value, list):
        return value
    try:
        parsed = json.loads(str(value or "[]"))
        return parsed if isinstance(parsed, list) else []
    except Exception:
        return []


def _tokens(text: str) -> set[str]:
    """CJK bigram sliding window + Latin whole-word tokens."""
    t = str(text or "")
    result: set[str] = set()
    for chunk in re.findall(r'[\u4e00-\u9fff]+', t):
        for i in range(max(1, len(chunk) - 1)):
            result.add(chunk[i:i + 2])
    result.update(re.findall(r'[a-zA-Z0-9]{2,}', t))
    return result


def _clamp(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, float(v)))


# --------------------------------------------------------------------------- #
#  EvidenceResult                                                               #
# --------------------------------------------------------------------------- #

class EvidenceResult:
    """Holds all five route scores and provenance."""

    __slots__ = ("a", "b", "c", "d", "e", "hard_reject", "reject_reason",
                 "details")

    def __init__(self):
        self.a: float = 0.0          # entity_anchor
        self.b: float = 0.0          # semantic_event
        self.c: float = 0.0          # temporal
        self.d: float = 0.0          # state_change
        self.e: float = 0.0          # source_evidence
        self.hard_reject: bool = False
        self.reject_reason: str = ""
        self.details: dict[str, Any] = {}

    @property
    def fused(self) -> float:
        """Weighted fusion of five routes (version v1)."""
        if self.hard_reject:
            return 0.0
        # Weights from test1 strategy object (conservative; no LLM)
        w = {"a": 0.25, "b": 0.20, "c": 0.18, "d": 0.20, "e": 0.17}
        return _clamp(
            self.a * w["a"] + self.b * w["b"] + self.c * w["c"] +
            self.d * w["d"] + self.e * w["e"]
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "route_a": round(self.a, 4),
            "route_b": round(self.b, 4),
            "route_c": round(self.c, 4),
            "route_d": round(self.d, 4),
            "route_e": round(self.e, 4),
            "fused": round(self.fused, 4),
            "hard_reject": self.hard_reject,
            "reject_reason": self.reject_reason,
            "details": self.details,
        }


# --------------------------------------------------------------------------- #
#  ThreadEvidence                                                               #
# --------------------------------------------------------------------------- #

class ThreadEvidence:
    """Compute five-route local evidence for an episode pair."""

    ARBITER_VERSION = "test5-local-v2"

    # ── Route A: entity anchor ──────────────────────────────────────────────
    @staticmethod
    def route_a(ep_a: dict, ep_b: dict) -> float:
        ents_a = {str(e).strip().lower() for e in _json_list(ep_a.get("entities_json")) if str(e).strip()}
        ents_b = {str(e).strip().lower() for e in _json_list(ep_b.get("entities_json")) if str(e).strip()}
        if not ents_a or not ents_b:
            return 0.0
        shared = ents_a & ents_b
        union = ents_a | ents_b
        jaccard = len(shared) / max(1, len(union))
        # Bonus if the shared entity is rare-looking (len > 2 Chinese chars)
        rarity_bonus = sum(0.05 for e in shared if len(e) >= 2) / max(1, len(shared))
        return _clamp(jaccard * 0.80 + rarity_bonus * 0.20)

    # ── Route B: semantic event ─────────────────────────────────────────────
    @staticmethod
    def route_b(ep_a: dict, ep_b: dict) -> float:
        """Cosine similarity of card embeddings; falls back to 0 if absent."""
        emb_a = ep_a.get("_embedding")
        emb_b = ep_b.get("_embedding")
        if emb_a is None or emb_b is None:
            return 0.0
        if not isinstance(emb_a, list) or not isinstance(emb_b, list):
            return 0.0
        if len(emb_a) != len(emb_b) or not emb_a:
            return 0.0
        try:
            return _clamp(float(_cosine(emb_a, emb_b)))
        except Exception:
            return 0.0

    # ── Route C: temporal ───────────────────────────────────────────────────
    @staticmethod
    def route_c(ep_a: dict, ep_b: dict) -> tuple[float, bool]:
        """Returns (score, has_date_conflict)."""
        ts_a = float(ep_a.get("event_ts") or 0)
        ts_b = float(ep_b.get("event_ts") or 0)
        if ts_a <= 0 or ts_b <= 0:
            return 0.3, False  # unknown → neutral
        delta_days = abs(ts_a - ts_b) / 86400.0
        if delta_days < 1:
            return 0.90, False   # same-day: very likely same event
        if delta_days <= 7:
            return 0.65, False
        if delta_days <= 30:
            return 0.40, False
        if delta_days <= 180:
            # Could be a sequel; not a conflict unless explicit date mentioned
            return 0.20, False
        # More than 180 days apart with explicit dates → likely distinct
        return 0.05, True

    # ── Route D: state change ───────────────────────────────────────────────
    @staticmethod
    def route_d(ep_a: dict, ep_b: dict) -> float:
        state_a = set(_tokens(ep_a.get("state_change", "") or ""))
        state_b = set(_tokens(ep_b.get("state_change", "") or ""))
        long_a = set(_tokens(ep_a.get("long_effect", "") or ""))
        long_b = set(_tokens(ep_b.get("long_effect", "") or ""))
        unres_a = bool(_json_list(ep_a.get("unresolved_json")))
        unres_b = bool(_json_list(ep_b.get("unresolved_json")))

        shared_state = len(state_a & state_b)
        shared_long = len(long_a & long_b)
        score = 0.0
        if shared_state:
            score += min(0.50, shared_state * 0.15)
        if shared_long:
            score += min(0.30, shared_long * 0.10)
        if unres_a and unres_b:
            score += 0.20  # both unresolved → possibly same pending issue
        return _clamp(score)

    # ── Route E: source evidence ────────────────────────────────────────────
    @staticmethod
    def route_e(ep_a: dict, ep_b: dict) -> float:
        batch_a = str(ep_a.get("source_batch_id") or "")
        batch_b = str(ep_b.get("source_batch_id") or "")
        turns_a = {
            (str(item[0]), int(item[1])) for item in (ep_a.get("_turn_refs") or [])
            if isinstance(item, (list, tuple)) and len(item) >= 2
        }
        turns_b = {
            (str(item[0]), int(item[1])) for item in (ep_b.get("_turn_refs") or [])
            if isinstance(item, (list, tuple)) and len(item) >= 2
        }
        if turns_a and turns_b and turns_a & turns_b:
            overlap = len(turns_a & turns_b) / max(1, min(len(turns_a), len(turns_b)))
            return _clamp(0.82 + 0.18 * overlap)
        if batch_a and batch_b and batch_a == batch_b:
            # One compression batch can legitimately contain several events.
            # It is strong co-occurrence evidence, never proof of a retelling.
            return 0.62
        qual_a = str(ep_a.get("evidence_quality") or "diary_derived")
        qual_b = str(ep_b.get("evidence_quality") or "diary_derived")
        quality_bonus = (
            0.15 if qual_a == "source_grounded" else
            0.08 if qual_a == "mixed_user_edited" else 0.0
        ) + (
            0.15 if qual_b == "source_grounded" else
            0.08 if qual_b == "mixed_user_edited" else 0.0
        )
        return _clamp(quality_bonus)

    # ── hard constraints ────────────────────────────────────────────────────
    @staticmethod
    def hard_constraints(ep_a: dict, ep_b: dict,
                         date_conflict: bool) -> tuple[bool, str]:
        """Returns (reject, reason)."""
        scope_a = str(ep_a.get("scope_id") or "")
        scope_b = str(ep_b.get("scope_id") or "")
        if scope_a and scope_b and scope_a != scope_b:
            return True, "scope_mismatch"
        # A large date gap blocks only a retells interpretation. It can still be
        # a continuation, stage change, parallel event, or explicit resolution.
        return False, ""

    # ── main entry ──────────────────────────────────────────────────────────
    def compute(self, ep_a: dict, ep_b: dict) -> EvidenceResult:
        res = EvidenceResult()
        res.a = self.route_a(ep_a, ep_b)
        res.b = self.route_b(ep_a, ep_b)
        c_score, date_conflict = self.route_c(ep_a, ep_b)
        res.c = c_score
        res.d = self.route_d(ep_a, ep_b)
        res.e = self.route_e(ep_a, ep_b)
        rejected, reason = self.hard_constraints(ep_a, ep_b, date_conflict)
        res.hard_reject = rejected
        res.reject_reason = reason
        res.details = {
            "date_conflict": date_conflict,
            "shared_turn_refs": sorted(
                list({tuple(x) for x in (ep_a.get("_turn_refs") or [])}
                     & {tuple(x) for x in (ep_b.get("_turn_refs") or [])})
            )[:20],
            "arbiter_version": self.ARBITER_VERSION,
        }
        return res
