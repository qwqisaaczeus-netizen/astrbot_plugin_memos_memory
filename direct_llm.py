from __future__ import annotations

import asyncio
import base64
import ctypes
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlparse


class DirectHTTPError(RuntimeError):
    def __init__(self, status: int, retry_after: str | None = None):
        self.status_code = status
        self.response = SimpleNamespace(headers={"retry-after": retry_after})
        super().__init__(f"direct API HTTP status {status}")


class DirectRequestError(ValueError):
    def __init__(self, status: int):
        self.status_code = status
        super().__init__(f"direct API rejected request: {status}")


class DirectLLMEmptyFinalError(RuntimeError):
    """The upstream completed a request without a usable final answer."""

    def __init__(self, finish_reason: str, completion_tokens: Any, reasoning_tokens: Any):
        self.finish_reason = finish_reason
        self.completion_tokens = completion_tokens
        self.reasoning_tokens = reasoning_tokens
        super().__init__(
            "direct API returned no final text "
            f"finish_reason={finish_reason or 'unknown'} "
            f"completion_tokens={completion_tokens} reasoning_tokens={reasoning_tokens}"
        )


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_ulong), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _dpapi(value: bytes, *, decrypt: bool = False) -> bytes:
    if os.name != "nt":
        raise OSError("DPAPI is only available on Windows")
    source_buffer = ctypes.create_string_buffer(value, len(value))
    source = _DataBlob(len(value), ctypes.cast(source_buffer, ctypes.POINTER(ctypes.c_byte)))
    target = _DataBlob()
    flags = 0x1
    function = (
        ctypes.windll.crypt32.CryptUnprotectData
        if decrypt
        else ctypes.windll.crypt32.CryptProtectData
    )
    description = None if decrypt else "memos-memory-xinchao"
    ok = function(
        ctypes.byref(source), description, None, None, None, flags, ctypes.byref(target)
    )
    if not ok:
        raise ctypes.WinError()
    try:
        return ctypes.string_at(target.pbData, target.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(target.pbData)


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        os.chmod(temp, 0o600)
    except OSError:
        pass
    os.replace(temp, path)


class SecretValueStore:
    """Small local secret store. Windows uses user-scoped DPAPI."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def save(self, value: str) -> None:
        clean = str(value or "").strip()
        if not clean:
            try:
                self.path.unlink(missing_ok=True)
            except OSError:
                pass
            return
        raw = clean.encode("utf-8")
        mode = "plain"
        encoded = base64.b64encode(raw).decode("ascii")
        try:
            encoded = base64.b64encode(_dpapi(raw)).decode("ascii")
            mode = "dpapi"
        except Exception:
            pass
        _atomic_json(self.path, {"version": 1, "mode": mode, "value": encoded})

    def load(self) -> str:
        try:
            item = json.loads(self.path.read_text(encoding="utf-8"))
            raw = base64.b64decode(str(item.get("value") or ""))
            if item.get("mode") == "dpapi":
                raw = _dpapi(raw, decrypt=True)
            return raw.decode("utf-8")
        except Exception:
            return ""


def validate_api_base_url(value: Any) -> str:
    clean = str(value or "").strip().rstrip("/")[:1000]
    if not clean:
        return ""
    parsed = urlparse(clean)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError("API Base URL 必须是未内嵌账号密码的 http(s) 地址")
    return clean


class OpenAICompatibleTextClient:
    """Minimal async chat client that deliberately bypasses Astr provider circuits."""

    def __init__(self, name: str) -> None:
        self.name = str(name or "direct-api")
        self.base_url = ""
        self.model = ""
        self.api_key = ""
        self.max_retries = 1
        self.temperature = 0.1
        self.default_timeout = 60.0
        self._http: Any = None

    def configure(
        self,
        *,
        base_url: Any,
        model: Any,
        api_key: Any,
        max_retries: Any = 1,
        temperature: Any = 0.1,
        timeout: Any = 60.0,
    ) -> None:
        self.base_url = validate_api_base_url(base_url)
        self.model = str(model or "").strip()[:200]
        self.api_key = str(api_key or "").strip()
        try:
            self.max_retries = max(0, min(2, int(max_retries)))
        except (TypeError, ValueError):
            self.max_retries = 1
        try:
            self.temperature = max(0.0, min(2.0, float(temperature)))
        except (TypeError, ValueError):
            self.temperature = 0.1
        try:
            self.default_timeout = max(3.0, min(600.0, float(timeout)))
        except (TypeError, ValueError):
            self.default_timeout = 60.0

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.model and self.api_key)

    def meta(self) -> Any:
        return SimpleNamespace(id=f"direct:{self.model or self.name}", model=self.model)

    def _chat_url(self) -> str:
        if self.base_url.endswith("/chat/completions"):
            return self.base_url
        return self.base_url + "/chat/completions"

    async def _session(self) -> Any:
        if self._http is None or getattr(self._http, "closed", False):
            import aiohttp

            self._http = aiohttp.ClientSession()
        return self._http

    async def text_chat(
        self,
        *,
        prompt: str,
        contexts: list[Any] | None = None,
        system_prompt: str = "",
        timeout: float | None = None,
        max_tokens: int = 8192,
        request_max_retries: int | None = None,
    ) -> Any:
        if not self.configured:
            raise RuntimeError("外置 LLM API 尚未完整配置")
        deadline = time.monotonic() + max(
            0.1, float(timeout if timeout is not None else self.default_timeout)
        )
        messages = []
        if str(system_prompt or ""):
            messages.append({"role": "system", "content": str(system_prompt)})
        for item in contexts or []:
            if isinstance(item, dict) and item.get("role") in {"system", "user", "assistant"}:
                messages.append({
                    "role": str(item["role"]),
                    "content": str(item.get("content") or ""),
                })
        messages.append({"role": "user", "content": str(prompt or "")})
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": max(64, min(32768, int(max_tokens))),
        }
        last_error: BaseException | None = None
        retries = self.max_retries
        if request_max_retries is not None:
            try:
                # AstrBot's argument counts total attempts, including the first.
                # Keep the direct client on the same contract.
                retries = min(retries, max(0, int(request_max_retries) - 1))
            except (TypeError, ValueError):
                pass
        for attempt in range(retries + 1):
            remaining = deadline - time.monotonic()
            if remaining <= 0.05:
                raise asyncio.TimeoutError() from last_error
            try:
                import aiohttp

                session = await self._session()
                async with session.post(
                    self._chat_url(),
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=remaining),
                    allow_redirects=False,
                ) as response:
                    raw = await response.text()
                    if response.status in {401, 403}:
                        raise DirectRequestError(response.status)
                    if response.status == 429 or response.status >= 500:
                        raise DirectHTTPError(response.status, response.headers.get("Retry-After"))
                    if response.status >= 400:
                        raise DirectRequestError(response.status)
                    data = json.loads(raw[:2_000_000])
                    content = (((data.get("choices") or [{}])[0].get("message") or {}).get("content"))
                    if isinstance(content, list):
                        content = "".join(
                            str(item.get("text") or "") if isinstance(item, dict) else str(item)
                            for item in content
                        )
                    text = str(content or "").strip()
                    if not text:
                        choice = (data.get("choices") or [{}])[0]
                        usage = data.get("usage") or {}
                        details = usage.get("completion_tokens_details") or {}
                        raise DirectLLMEmptyFinalError(
                            str(choice.get("finish_reason") or ""),
                            usage.get("completion_tokens"),
                            details.get("reasoning_tokens"),
                        )
                    usage = data.get("usage") or {}
                    safe_usage = {k: v for k in ("prompt_tokens", "completion_tokens", "total_tokens")
                                  if isinstance(v := usage.get(k), int)}
                    return SimpleNamespace(completion_text=text, raw_completion=SimpleNamespace(
                        usage=SimpleNamespace(**safe_usage)))
            except asyncio.CancelledError:
                raise
            except (asyncio.TimeoutError, TimeoutError) as exc:
                last_error = exc
            except Exception as exc:
                last_error = exc
                if isinstance(exc, (ValueError, DirectLLMEmptyFinalError)):
                    raise
            if attempt < retries and deadline - time.monotonic() > 0.25:
                await asyncio.sleep(min(0.6 * (2**attempt), max(0.05, deadline - time.monotonic() - 0.05)))
        if isinstance(last_error, (asyncio.TimeoutError, TimeoutError)):
            raise asyncio.TimeoutError() from last_error
        if last_error is not None:
            raise last_error
        raise RuntimeError("独立 API 调用失败")

    async def test(self, timeout: float = 15.0) -> dict[str, Any]:
        started = time.monotonic()
        response = await self.text_chat(
            prompt='只输出 {"ok":true}',
            system_prompt="你是连接测试器，只输出合法 JSON。",
            timeout=timeout,
            max_tokens=64,
        )
        return {
            "ok": bool(str(getattr(response, "completion_text", "")).strip()),
            "provider": f"direct:{self.model}",
            "latency_ms": int((time.monotonic() - started) * 1000),
        }

    async def close(self) -> None:
        if self._http is not None:
            try:
                await self._http.close()
            except Exception:
                pass
        self._http = None
