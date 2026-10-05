"""Pure contracts for source ownership, attempts and publish eligibility."""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass


def job_key(session_id: str, source_ids: list[str], revision: str) -> str:
    if not session_id or not source_ids or not revision:
        raise ValueError("session, source identities and revision are required")
    if any(not isinstance(item, str) or not item for item in source_ids):
        raise ValueError("invalid source identity")
    if len(set(source_ids)) != len(source_ids):
        raise ValueError("duplicate source identities")
    payload = [session_id, source_ids, revision]
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False).encode()).hexdigest()


@dataclass(frozen=True)
class AttemptBudget:
    timeout: float
    max_attempts: int = 3

    def __post_init__(self):
        if not math.isfinite(self.timeout) or self.timeout <= 0:
            raise ValueError("timeout must be positive and finite")
        if type(self.max_attempts) is not int or not 1 <= self.max_attempts <= 3:
            raise ValueError("attempt budget must be 1..3")

    def remaining(self, started: float, now: float) -> float:
        if not all(math.isfinite(value) for value in (started, now)) or now < started:
            raise ValueError("invalid monotonic clock")
        return max(0.0, self.timeout - (now - started))


def effective_timeout(explicit: float | None, default: float) -> float:
    value = default if explicit is None else explicit
    AttemptBudget(value)
    return value


def publish_eligible(*, extraction: str, grounded: bool, draft_valid: bool,
                     source_revision: str, expected_revision: str) -> bool:
    return bool(
        extraction in {"llm_primary", "llm_compact_recovery", "llm_sharded_recovery"}
        and grounded and draft_valid and source_revision
        and source_revision == expected_revision
    )


def recovery_action(stage: str, *, artifact_valid: bool, publish_status: str) -> str:
    if publish_status == "outcome_unknown":
        return "reconcile_before_create"
    if publish_status == "committed":
        return "reuse_committed"
    if stage not in {"extract", "plan", "write", "review", "index"}:
        raise ValueError("unknown recovery stage")
    return "reuse_artifact" if artifact_valid else "resume_" + stage
