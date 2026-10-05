"""Small offline threshold search that never mutates the active policy."""
from __future__ import annotations

from typing import Any, Callable


def grid_search(
    train: list[dict[str, Any]],
    validation: list[dict[str, Any]],
    scorer: Callable,
    thresholds=(0.4, 0.5, 0.6, 0.7),
) -> dict[str, Any]:
    if not train:
        return {
            "best": None,
            "next_best": [],
            "candidates": 0,
            "selection": "train",
            "warning": "empty_train",
        }
    results = []
    for threshold in thresholds:
        metrics = scorer(train, float(threshold))
        results.append({"threshold": float(threshold), "metrics": metrics})
    results.sort(
        key=lambda item: (
            float(item["metrics"].get("f1", 0)),
            float(item["metrics"].get("precision", 0)),
        ),
        reverse=True,
    )
    best = dict(results[0])
    best["validation_metrics"] = (
        scorer(validation, best["threshold"]) if validation else None
    )
    return {
        "best": best,
        "next_best": results[1:3],
        "candidates": len(results),
        "selection": "train",
        "train_count": len(train),
        "validation_count": len(validation),
    }
