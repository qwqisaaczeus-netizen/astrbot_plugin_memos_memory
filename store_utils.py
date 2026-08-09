"""Shared helpers for the episodic data layer (4.6.0-test2).

Kept import-free of astrbot so it can be imported by every Repository module
without creating cycles. Both SourceArchive and EpisodeRepo depend on it.
"""
from __future__ import annotations

import json
import math
import re
import struct
from typing import Any

__all__ = [
    "serialize_f32",
    "deserialize_f32",
    "cosine",
    "json_list",
    "extract_terms",
    "escape_like",
]


def serialize_f32(vec: list[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


def deserialize_f32(blob: bytes | None) -> list[float]:
    if not blob:
        return []
    return list(struct.unpack(f"{len(blob) // 4}f", blob))


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)


def json_list(value: Any) -> list:
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            return []
    return []


def extract_terms(text: str) -> set[str]:
    source = " ".join(str(text or "").lower().split())
    if not source:
        return set()
    out = set(re.findall(r"[a-z0-9_/-]{2,}|[\u4e00-\u9fff]{2,8}", source))
    try:
        import jieba  # type: ignore

        out.update(word.strip().lower() for word in jieba.lcut(source) if len(word.strip()) >= 2)
    except Exception:
        out.update(source[index:index + 2] for index in range(max(0, len(source) - 1)))
    return {item for item in out if item}


def escape_like(term: str) -> str:
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")