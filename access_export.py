"""Portable, ACCESS-only analysis exports for 5.x."""
from __future__ import annotations

import hashlib
import json
import re
import time
import zipfile
from pathlib import Path
from typing import Any


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, indent=2, default=str).encode("utf-8")


def _jsonl_bytes(items: list[dict[str, Any]]) -> bytes:
    return b"".join(
        (json.dumps(item, ensure_ascii=False, separators=(",", ":"), default=str) + "\n").encode("utf-8")
        for item in items
    )


_SECRET_PATTERNS = (
    re.compile(r"(?i)\bmemos_pat_[A-Za-z0-9._-]{8,}"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{12,}"),
    re.compile(r"(?i)\b(sk|rk|pk)-[A-Za-z0-9_-]{16,}"),
    re.compile(r"(?i)(api[_ -]?key|access[_ -]?token|memos[_ -]?token)(\s*[:=]\s*)[^\s,;]{8,}"),
)


def _redact_secrets(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _redact_secrets(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_secrets(item) for item in value]
    if isinstance(value, tuple):
        return [_redact_secrets(item) for item in value]
    if not isinstance(value, str):
        return value
    output = value
    for pattern in _SECRET_PATTERNS:
        if pattern.groups >= 2:
            output = pattern.sub(lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]", output)
        else:
            output = pattern.sub("[REDACTED]", output)
    return output


class AccessAnalysisExporter:
    """Write bounded diagnostics without copying diaries or archived source turns."""

    def __init__(self, export_dir: str, *, keep: int = 10):
        self.export_dir = Path(export_dir).expanduser().resolve()
        self.keep = max(1, min(100, int(keep)))

    def _resolve_archive(self, file_name: str) -> Path:
        raw = Path(str(file_name or "")).name
        path = (self.export_dir / raw).resolve()
        if path.parent != self.export_dir or not path.is_file() or path.suffix.lower() != ".zip":
            raise FileNotFoundError("ACCESS analysis export not found")
        return path

    def list_archives(self) -> list[dict[str, Any]]:
        if not self.export_dir.exists():
            return []
        output = []
        for path in sorted(self.export_dir.glob("access_analysis_*.zip"), reverse=True):
            stat = path.stat()
            output.append({
                "file": path.name,
                "size": stat.st_size,
                "created_ts": stat.st_mtime,
            })
        return output

    def archive_path(self, file_name: str) -> Path:
        return self._resolve_archive(file_name)

    def create(self, payload: dict[str, Any], *, plugin_version: str) -> dict[str, Any]:
        self.export_dir.mkdir(parents=True, exist_ok=True)
        payload = _redact_secrets(payload)
        stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime())
        path = self.export_dir / f"access_analysis_{stamp}.zip"
        if path.exists():
            path = self.export_dir / f"access_analysis_{stamp}_{time.time_ns() % 1000000:06d}.zip"

        observations = list(payload.get("observations") or [])
        states = list(payload.get("states") or [])
        edges = list(payload.get("interference_edges") or [])
        cases = list(payload.get("eval_cases") or [])
        results = list(payload.get("latest_eval_results") or [])
        manifest = {
            "format": str(payload.get("format") or "memos-memory-access-analysis-v1"),
            "plugin_version": str(plugin_version),
            "algorithm_version": str(payload.get("algorithm_version") or ""),
            "created_ts": float(payload.get("created_ts") or time.time()),
            "contains_query_text": True,
            "contains_diary_text": False,
            "contains_full_source_turn_text": False,
            "contains_eval_query_excerpts": True,
            "contains_service_tokens": False,
            "counts": {
                "observations": len(observations),
                "states": len(states),
                "interference_edges": len(edges),
                "eval_cases": len(cases),
                "latest_eval_results": len(results),
            },
        }
        readme = (
            "Memos Memory ACCESS analysis bundle\n\n"
            "This archive contains local retrieval queries, baseline/Shadow rankings, "
            "response-use signals, ACCESS states, interference edges and evaluation results.\n"
            "It does not contain Memos tokens, provider credentials, diary bodies or complete raw source turns.\n"
            "Evaluation queries may contain short excerpts derived from source evidence.\n"
            "Queries may still contain private conversation details. Share this file intentionally.\n"
        ).encode("utf-8")
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            archive.writestr("manifest.json", _json_bytes(manifest))
            archive.writestr("summary.json", _json_bytes({
                "summary_30d": payload.get("summary_30d") or {},
                "summary_all": payload.get("summary_all") or {},
                "overview": payload.get("overview") or {},
                "latest_eval_run": payload.get("latest_eval_run"),
            }))
            archive.writestr("observations.jsonl", _jsonl_bytes(observations))
            archive.writestr("access_states.jsonl", _jsonl_bytes(states))
            archive.writestr("interference_edges.jsonl", _jsonl_bytes(edges))
            archive.writestr("eval_cases.jsonl", _jsonl_bytes(cases))
            archive.writestr("latest_eval_results.jsonl", _jsonl_bytes(results))
            archive.writestr("README.txt", readme)

        archives = self.list_archives()
        for old in archives[self.keep:]:
            try:
                self._resolve_archive(str(old.get("file") or "")).unlink()
            except (FileNotFoundError, OSError):
                continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        return {
            "created": True,
            "file": path.name,
            "size": path.stat().st_size,
            "sha256": digest,
            "counts": manifest["counts"],
            "privacy": {
                "query_text": True,
                "diary_text": False,
                "full_source_turn_text": False,
                "eval_query_excerpts": True,
                "service_tokens": False,
            },
        }
