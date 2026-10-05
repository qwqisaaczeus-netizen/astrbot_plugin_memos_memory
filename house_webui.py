from __future__ import annotations

from typing import Any


class HouseWebUIHandlers:
    """Route adapter kept separate from the already-large main WebUI handler."""

    @staticmethod
    def _service(handler: Any) -> Any:
        service = getattr(handler._house_plugin, "_house", None)
        if service is None:
            raise RuntimeError("心笺小院服务未加载")
        return service

    @classmethod
    def handle_get(cls, handler: Any, path: str, qs: dict[str, list[str]]) -> bool:
        if not path.startswith("/api/house/"):
            return False
        try:
            service = cls._service(handler)
            scope = str((qs.get("scope") or [""])[0] or "")
            if path in {"/api/house/overview", "/api/house/scene"}:
                data = handler._run_plugin_coro(service.overview(scope), timeout=15)
            elif path == "/api/house/session":
                data = {
                    "active": service.store.get_active_session(service._scope(scope)),
                    "items": service.store.list_sessions(service._scope(scope), 60),
                }
            elif path in {"/api/house/artifacts", "/api/house/history"}:
                artifact_type = str((qs.get("type") or [""])[0] or "")
                limit = int((qs.get("limit") or ["100"])[0] or 100)
                data = {
                    "items": handler._run_plugin_coro(
                        service.artifacts(scope, artifact_type, limit), timeout=15,
                    )
                }
            elif path == "/api/house/dreams":
                data = {
                    "items": handler._run_plugin_coro(service.dreams(scope), timeout=15),
                }
            elif path == "/api/house/artifact":
                artifact_id = str((qs.get("id") or [""])[0] or "")
                item = service.store.get_artifact(artifact_id)
                if not item:
                    return handler._json_err("小屋内容不存在", 404) or True
                data = {
                    "artifact": item,
                    "sources": service.store.list_source_snapshots(item["session_id"], 80),
                }
            elif path == "/api/house/settings":
                data = service.settings_payload()
            elif path == "/api/house/sources":
                session_id = str((qs.get("session_id") or [""])[0] or "")
                data = {"items": service.store.list_source_snapshots(session_id, 80)}
            elif path == "/api/house/diagnostics":
                data = {
                    "settings": service.settings_payload(),
                    "stats": service.store.stats(service._scope(scope)),
                    "database": str(service.store.path),
                }
            else:
                return False
            handler._json_ok(data)
        except (ValueError, KeyError) as exc:
            handler._json_err(str(exc), 400)
        except Exception as exc:
            handler._json_err(str(exc), handler._plugin_coro_error_status(exc))
        return True

    @classmethod
    def handle_post(cls, handler: Any, path: str, body: dict[str, Any]) -> bool:
        if not path.startswith("/api/house/"):
            return False
        try:
            service = cls._service(handler)
            if path == "/api/house/settings/save":
                settings = body.get("settings")
                if not isinstance(settings, dict):
                    raise ValueError("settings must be an object")
                data = service.save_settings(settings)
            elif path == "/api/house/settings/test":
                data = handler._run_plugin_coro(service.test_connection(), timeout=190)
            elif path in {
                "/api/house/artifact/read", "/api/house/artifact/feedback",
                "/api/house/artifact/archive", "/api/house/artifact/delete",
            }:
                action = path.rsplit("/", 1)[-1]
                data = handler._run_plugin_coro(service.artifact_action(action, body), timeout=20)
            elif path == "/api/house/artifact/send":
                if str(body.get("confirm") or "") != "SEND":
                    raise ValueError("寄出前需要明确确认")
                data = handler._run_plugin_coro(service.send_artifact(body), timeout=45)
            elif path == "/api/house/artifact/rewrite":
                artifact = service.store.get_artifact(str(body.get("artifact_id") or ""))
                if not artifact:
                    raise KeyError("artifact not found")
                data = handler._run_plugin_coro(
                    service.generate(
                        artifact.get("scope_key", ""), preview=True,
                        requested_type=artifact.get("artifact_type", ""),
                    ),
                    timeout=190,
                )
            elif path in {"/api/house/generate/preview", "/api/house/generate/now"}:
                data = handler._run_plugin_coro(
                    service.generate(
                        str(body.get("scope") or ""),
                        preview=path.endswith("/preview"),
                        requested_type=str(body.get("artifact_type") or ""),
                        enforce_limits=not path.endswith("/preview"),
                    ),
                    timeout=190,
                )
            elif path == "/api/house/session/settle":
                data = handler._run_plugin_coro(
                    service.settle_now(str(body.get("scope") or "")), timeout=190,
                )
            elif path == "/api/house/character/interact":
                data = handler._run_plugin_coro(service.character_interact(body), timeout=20)
            else:
                return False
            handler._json_ok(data)
        except (ValueError, KeyError) as exc:
            handler._json_err(str(exc), 400)
        except Exception as exc:
            handler._json_err(str(exc), handler._plugin_coro_error_status(exc))
        return True
