"""Offline comparison metrics for local, LLM, and fused thread decisions."""
from __future__ import annotations

from collections import defaultdict
from typing import Any

REQUIRED_CASE_TYPES = {
    "same_event_retell", "same_theme_distinct", "continuation_response", "uncertain",
}


def evaluate_relation_predictions(cases: list[dict[str, Any]]) -> dict[str, Any]:
    """Score precomputed predictions without calling a provider.

    Each case carries ``expected_relation``/``expected_decision`` and optional
    ``local``, ``llm``, and ``fused`` prediction dictionaries. This keeps the
    evaluation reproducible and allows the same fixture to compare ablations.
    """
    present_types = {str(case.get("case_type") or "") for case in cases}
    missing = sorted(REQUIRED_CASE_TYPES - present_types)
    engines: dict[str, dict[str, Any]] = {}
    for engine in ("local", "llm", "fused"):
        total = correct = accepted = accepted_correct = causal = causal_correct = 0
        uncertain_forced = 0
        by_type: dict[str, list[int]] = defaultdict(lambda: [0, 0])
        for case in cases:
            prediction = case.get(engine)
            if not isinstance(prediction, dict):
                continue
            total += 1
            expected_decision = str(case.get("expected_decision") or "uncertain")
            expected_relation = str(case.get("expected_relation") or "")
            got_decision = str(prediction.get("decision") or "uncertain")
            got_relation = str(prediction.get("relation") or "")
            ok = got_decision == expected_decision and (
                got_decision != "accepted" or got_relation == expected_relation
            )
            correct += int(ok)
            kind = str(case.get("case_type") or "other")
            by_type[kind][0] += int(ok)
            by_type[kind][1] += 1
            if got_decision == "accepted":
                accepted += 1
                accepted_correct += int(expected_decision == "accepted" and got_relation == expected_relation)
                if got_relation in {"causes", "supersedes", "resolves", "breaks"}:
                    causal += 1
                    causal_correct += int(expected_decision == "accepted" and got_relation == expected_relation)
            if expected_decision == "uncertain" and got_decision == "accepted":
                uncertain_forced += 1
        precision = accepted_correct / accepted if accepted else 1.0
        causal_precision = causal_correct / causal if causal else 1.0
        engines[engine] = {
            "cases": total, "accuracy": correct / total if total else 0.0,
            "accepted_precision": precision, "causal_precision": causal_precision,
            "uncertain_forced": uncertain_forced,
            "by_type": {key: {"correct": value[0], "total": value[1]} for key, value in by_type.items()},
            "test3_gate": bool(total and precision >= 0.92 and causal_precision >= 0.95 and uncertain_forced == 0),
        }
    return {"cases_total": len(cases), "missing_case_types": missing, "engines": engines}
