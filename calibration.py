"""Offline, evidence-graded calibration. Never activates a proposed policy."""
from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from pathlib import Path
from typing import Any

# Keep the established ``calibration.py`` API while allowing test10's focused
# offline helpers to live under ``calibration.search`` and
# ``calibration.readonly_snapshot``. Setting __path__ makes this module a
# package-compatible namespace without breaking existing imports.
__path__ = [str(Path(__file__).with_suffix(""))]

POLICY_ID = "test9-frozen-test8-v2"
ROUTE_WEIGHTS = {"a": .25, "b": .20, "c": .18, "d": .20, "e": .17}
BASELINE = {
    "accept": 0.82,
    "gap": 0.15,
    "reject": 0.42,
    "thresholds_by_relation": {
        "retells": 0.82,
        "parallel": 0.82,
        "unrelated_similar": 0.82,
        "continues": 0.82,
    },
}
POLICY_MANIFEST = {
    "policy_id": POLICY_ID,
    "parent": "6.0.0-test8ultra4",
    "feature_versions": {
        "thread_builder": "test5-v2",
        "claim_extractor": "test6-claim-v3",
        "prospective": "test6-prospective-v2",
        "candidate_projection": "test9-v2",
    },
    "thread_link": {**BASELINE, "weights": ROUTE_WEIGHTS},
    "prospective": {"factual_route_floor": 0.35, "relevance_floor": 0.25,
                    "emotion_floor": 0.55, "default_cooldown_seconds": 86400},
    "retrieval": {"candidate_limit": 50, "date_window_days": 30,
                  "subgraph_nodes": 5, "evolution_subgraph_nodes": 7},
    "injection": {"claim_limit": 4, "edge_limit": 6, "deduplicate_baseline": True},
    "consistency": {"local_timeout_seconds": 0.25, "observational_only": True,
                    "visible_reference_required": True},
}
PRESETS = {
    "conservative": {"accept": 0.90, "gap": 0.20, "reject": 0.42},
    "balanced": dict(BASELINE),
    "quality": {"accept": 0.82, "gap": 0.12, "reject": 0.42},
    "economy": {"accept": 0.88, "gap": 0.18, "reject": 0.48},
}


def split_cases(cases: list[dict]) -> dict[str, list[dict]]:
    """Keep connected event/source groups together, including transitive overlap."""
    parent: dict[str, str] = {}

    def root(key: str) -> str:
        parent.setdefault(key, key)
        while parent[key] != key:
            parent[key] = parent[parent[key]]
            key = parent[key]
        return key

    keys = []
    for index, case in enumerate(cases):
        groups = sorted({str(v) for v in case.get("groups", []) if v})
        if not groups:
            raise ValueError(f"case {index} has no provenance group")
        anchor = root(groups[0])
        for key in groups[1:]:
            other = root(key)
            if other != anchor:
                lo, hi = sorted((anchor, other))
                parent[hi] = lo
                anchor = lo
        keys.append(groups[0])
    out = {"train": [], "validation": [], "test": []}
    assignments = {}
    for key, case in zip(keys, cases):
        declared = case.get("partition")
        if declared is not None:
            if declared not in out:
                raise ValueError("invalid partition")
            group = root(key)
            if group in assignments and assignments[group] != declared:
                raise ValueError("provenance leakage across partitions")
            assignments[group] = declared
    for key, case in zip(keys, cases):
        bucket = int(hashlib.sha256(root(key).encode()).hexdigest()[:8], 16) % 10
        partition = assignments.get(root(key), "test" if bucket == 0 else "validation" if bucket < 3 else "train")
        out[partition].append(case)
    return out


def score_cases(cases: list[dict], policy: dict, omit: str = "") -> dict[str, Any]:
    confusion: Counter = Counter()
    decisions: Counter = Counter()
    relation_decisions: dict[str, Counter] = {}
    tp = fp = fn = 0
    weights = dict(policy.get("weights") or ROUTE_WEIGHTS)
    for case in cases:
        values = case["features"]
        if any(not math.isfinite(float(values.get(k, 0))) for k in weights):
            raise ValueError("non-finite evidence")
        score = sum(float(values.get(k, 0)) * w for k, w in weights.items() if k != omit)
        gap = case.get("gap")
        if gap is not None and not math.isfinite(float(gap)):
            raise ValueError("non-finite candidate gap")
        # Match runtime's exact-source shortcut and causal deferral.
        shortcut = (omit != "e" and omit != "b" and values.get("e", 0) >= .95
                    and values.get("b", 0) >= .65 and not case.get("date_conflict"))
        relation = str(case.get("relation") or "parallel")
        relation_accept = float((policy.get("thresholds_by_relation") or {}).get(
            relation, policy["accept"]
        ))
        predicted = bool(not case.get("hard_reject") and score >= policy["reject"] and (shortcut or (
            case.get("relation") not in {"causes", "supersedes", "resolves", "breaks"}
            and score >= relation_accept and gap is not None and float(gap) >= policy["gap"])))
        decision = "accepted" if predicted else "deferred"
        decisions[decision] += 1
        relation_decisions.setdefault(relation, Counter())[decision] += 1
        expected = case.get("expected")
        if expected not in ("accepted", "rejected"):
            continue
        positive = expected == "accepted"
        tp += int(predicted and positive)
        fp += int(predicted and not positive)
        fn += int(not predicted and positive)
        confusion[f"{expected}->{('accepted' if predicted else 'deferred')}"] += 1
    return {"cases": sum(confusion.values()), "evaluated_total": len(cases),
            "decisions": dict(decisions),
            "relation_decisions": {key: dict(value) for key, value in relation_decisions.items()},
            "tp": tp, "fp": fp, "fn": fn,
            "precision": tp / (tp + fp) if tp + fp else None,
            "recall": tp / (tp + fn) if tp + fn else None,
            "confusion": dict(confusion)}


def calibrate(cases: list[dict]) -> dict[str, Any]:
    split = split_cases(cases)
    policies = [{"accept": a, "gap": g, "reject": r,
                 "thresholds_by_relation": {key: a for key in BASELINE["thresholds_by_relation"]}}
                for a in (.78, .82, .86, .90) for g in (.10, .15, .20)
                for r in (.38, .42, .48)]

    def rank(policy: dict) -> tuple:
        m = score_cases(split["train"], policy)
        return (-(m["fp"]), m["tp"], policy["accept"], policy["gap"])

    train_ranked = sorted(policies, key=rank, reverse=True)
    has_training_labels = any(c.get("expected") in ("accepted", "rejected") for c in split["train"])
    has_validation_labels = any(c.get("expected") in ("accepted", "rejected") for c in split["validation"])

    def validation_rank(policy: dict) -> tuple:
        metrics = score_cases(split["validation"], policy)
        return (-(metrics["fp"]), metrics["tp"], policy["accept"], policy["gap"], policy["reject"])

    ranked = sorted(train_ranked, key=validation_rank, reverse=True)
    proposal_supported = has_training_labels and has_validation_labels
    proposal = ranked[0] if proposal_supported else dict(BASELINE)
    validation = score_cases(split["validation"], proposal)
    held_out = score_cases(split["test"], proposal)
    # Weak labels are diagnostic only, irrespective of apparent precision.
    gold_test = [c for c in split["test"] if c.get("label_kind") == "human"]
    gold_metrics = score_cases(gold_test, proposal)
    eligible = (len(gold_test) >= 30 and (gold_metrics["precision"] or 0) >= .92
                and (gold_metrics["recall"] or 0) >= .85)
    fingerprint = hashlib.sha256(json.dumps(cases, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return {"policy_id": POLICY_ID, "dataset_fingerprint": fingerprint,
            "active_policy": dict(BASELINE), "policy_manifest": POLICY_MANIFEST,
            "proposal": proposal, "runner_up": ranked[1] if proposal_supported else dict(BASELINE),
            "proposal_supported": proposal_supported,
            "selection_protocol": "validation_rank_after_train_enumeration",
            "split_counts": {k: len(v) for k, v in split.items()},
            "label_counts": dict(Counter(c.get("label_kind", "weak") for c in cases)),
            "validation": validation, "held_out": held_out, "gold_test": gold_metrics,
            "baseline": {k: score_cases(v, BASELINE) for k, v in split.items()},
            "ablations": {k: score_cases(split["validation"], BASELINE, k) for k in "abcde"},
            "presets": PRESETS, "eligible_for_review": eligible, "activate": False,
            "gates": {"thread_link": "review_required" if eligible else "insufficient_evidence",
                      "claim_accuracy": "not_measured", "supersession_precision": "not_measured",
                      "recall_regression": "not_measured", "final_5x_upstream": "not_available"}}
