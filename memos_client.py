"""
memos v0.29.1 REST API 客户端。
封装最常用的几个端点:建/查/删 memo + 服务可用性检测。
基于 memos v1 REST API 常用端点。
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Optional

import aiohttp

logger = logging.getLogger(__name__)

# 默认超时(秒)。建/查都是 sub-second 完成,留 5 秒防抖动。
_DEFAULT_TIMEOUT = aiohttp.ClientTimeout(total=5)

# v0.29.1 暴露的 REST 路由来自 proto-service 生成的 grpc-gateway:
#   POST /api/v1/memos                  -> CreateMemo
#   GET  /api/v1/memos                  -> ListMemos (支持 page_size/filter)
#   GET  /api/v1/memos/{name}           -> GetMemo
#   PATCH /api/v1/memos/{name}          -> UpdateMemo (改 content/visibility)
#   DELETE /api/v1/memos/{name}         -> DeleteMemo
# 这里我们只用 CreateMemo / GetMemo / ListMemos 三个,够用了。
_API_PREFIX = "/api/v1"


class MemosError(Exception):
    """memos API 调用异常"""


def _http_error_message(action: str, status: int, body: str) -> str:
    hint = ""
    if status == 401:
        hint = "（memos 0.30 私有模式：token 缺失或无效，请在 memos 设置页生成 Access Token 并填入 memos_token）"
    elif status == 403:
        hint = "（memos 返回 403：token 权限不足）"
    return f"{action} 失败 {status}: {body[:200]}{hint}"


class MemosClient:
    """memos REST 客户端。使用 token 鉴权。

    v4.5.4: 适配 memos 0.30.0 —— 私有模式下匿名 API 仅限 setup/auth/shared
    路由，因此带 token 访问 /api/v1/memos* 才是认证成功的标志；未认证时
    用 /healthz（免认证）区分“服务在线”与“完全不可达”。
    """

    def __init__(self, base_url: str, token: str = "", timeout: float = 5.0):
        # 去掉末尾斜杠,避免 //api/v1 这种双斜杠
        self.base_url = base_url.rstrip("/")
        self.token = token.strip()
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: Optional[aiohttp.ClientSession] = None
        self._connected: bool = False
        self._auth_ok: bool = False

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            headers = {"Content-Type": "application/json"}
            if self.token:
                headers["Authorization"] = f"Bearer {self.token}"
            self._session = aiohttp.ClientSession(
                timeout=self.timeout,
                headers=headers,
            )
        return self._session

    def _url(self, path: str) -> str:
        return f"{self.base_url}{_API_PREFIX}{path}"

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    async def health_check(self) -> bool:
        """探测 memos 服务是否可达。返回 True/False,不抛异常。

        0.30 兼容：先试带 token 的 /api/v1/memos（200 = 认证且可用）；
        401/403 说明服务在跑但未认证；都不通时再试免认证的 /healthz。
        """
        session = await self._ensure_session()
        try:
            async with session.get(self._url("/memos?page_size=1")) as resp:
                if resp.status == 200:
                    self._connected = True
                    self._auth_ok = True
                    return True
                if resp.status in (401, 403):
                    # 服务在跑，但 token 缺失/无效（0.30 私有模式）
                    self._connected = True
                    self._auth_ok = False
                    return True
        except (aiohttp.ClientError, asyncio.TimeoutError):
            pass
        try:
            async with session.get(self.base_url + "/healthz") as resp:
                ok = resp.status == 200
                self._connected = ok
                if ok:
                    self._auth_ok = False
                return ok
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            logger.warning("[memos] 健康检查失败: %s", exc)
            self._connected = False
            self._auth_ok = False
            return False

    async def diagnose(self) -> dict[str, Any]:
        """连接/认证诊断（供 WebUI 显示）。0.30 下区分：
        服务在线但未认证（需在 memos 设置页生成 Access Token）。"""
        out: dict[str, Any] = {
            "base_url": self.base_url,
            "connected": False,
            "authenticated": False,
            "note": "",
        }
        session = await self._ensure_session()
        try:
            async with session.get(self.base_url + "/healthz") as resp:
                out["connected"] = resp.status == 200
        except (aiohttp.ClientError, asyncio.TimeoutError):
            out["note"] = "无法连接 memos 服务"
            return out
        if not out["connected"]:
            out["note"] = "无法连接 memos 服务"
            return out
        try:
            async with session.get(self._url("/memos?page_size=1")) as resp:
                out["authenticated"] = resp.status == 200
        except (aiohttp.ClientError, asyncio.TimeoutError):
            pass
        if not out["authenticated"]:
            out["note"] = "memos 在线但未认证：请创建管理员并生成 Access Token 填入 memos_token（memos 0.30 私有模式要求）"
        return out

    async def create_memo(
        self,
        content: str,
        visibility: str = "PRIVATE",
    ) -> dict[str, Any]:
        """建一条 memo。返回 dict(name, content, ...)。

        Args:
            content: 完整日记文本(memos 自身支持 content 内的 #tag 自动提取)
            visibility: PRIVATE / PROTECTED / PUBLIC。AstrBot 内部的私人记忆用 PRIVATE
        """
        session = await self._ensure_session()
        payload = {"content": content, "visibility": visibility}
        async with session.post(self._url("/memos"), json=payload) as resp:
            if resp.status not in (200, 201):
                body = await resp.text()
                raise MemosError(_http_error_message("create_memo", resp.status, body))
            data = await resp.json()
        return data

    async def get_memo(self, name: str) -> dict[str, Any] | None:
        """取单条 memo。name 形如 'memos/123'。不存在则返回 None。"""
        # name 已经是完整路径,直接拼到 /api/v1 后面
        # 但保险起见,如果是纯数字也自动补前缀
        if not name.startswith("memos/"):
            name = f"memos/{name}"
        session = await self._ensure_session()
        url = self.base_url + _API_PREFIX + "/" + name
        async with session.get(url) as resp:
            if resp.status == 404:
                return None
            if resp.status != 200:
                body = await resp.text()
                raise MemosError(_http_error_message("get_memo", resp.status, body))
            return await resp.json()

    async def list_memos(
        self,
        page_size: int = 50,
        filter_expr: str = "",
        page_token: str = "",
    ) -> dict[str, Any]:
        """列 memo(单页)。返回 {"memos": [...], "next_page_token": "..."}。
        filter_expr 可选 CEL 表达式,如 'tags.contains("孕期")'。
        page_token 用于翻页(上一页返回的 next_page_token)。
        """
        session = await self._ensure_session()
        params: dict[str, Any] = {"page_size": page_size}
        if filter_expr:
            params["filter"] = filter_expr
        if page_token:
            params["page_token"] = page_token
        async with session.get(self._url("/memos"), params=params) as resp:
            if resp.status != 200:
                body = await resp.text()
                self._connected = True
                if resp.status in (401, 403):
                    self._auth_ok = False
                raise MemosError(_http_error_message("list_memos", resp.status, body))
            try:
                data = await resp.json()
            except (aiohttp.ContentTypeError, json.JSONDecodeError, ValueError) as exc:
                raise MemosError(f"list_memos 返回了无效 JSON: {type(exc).__name__}") from exc
        self._connected = True
        self._auth_ok = True
        if not isinstance(data, dict):
            raise MemosError("list_memos 返回结构无效: 根节点必须是对象")
        memos = data.get("memos", [])
        if not isinstance(memos, list) or any(not isinstance(item, dict) for item in memos):
            raise MemosError("list_memos 返回结构无效: memos 必须是对象数组")
        next_token = data.get("nextPageToken", data.get("next_page_token", ""))
        if next_token is None:
            next_token = ""
        if not isinstance(next_token, str):
            raise MemosError("list_memos 返回结构无效: page token 必须是字符串")
        return {"memos": memos, "next_page_token": next_token}

    async def list_all_memos(self, page_size: int = 100) -> list[dict[str, Any]]:
        """翻页拉取全部 memo(自动处理 page_token)。用于全量 reindex / 对账。"""
        out: list[dict[str, Any]] = []
        token = ""
        seen_tokens: set[str] = set()
        while True:
            batch = await self.list_memos(page_size=page_size, page_token=token)
            if not isinstance(batch, dict):
                raise MemosError("list_all_memos 收到无效分页结果")
            memos = batch.get("memos")
            next_token = batch.get("next_page_token", "")
            if not isinstance(memos, list) or any(not isinstance(item, dict) for item in memos):
                raise MemosError("list_all_memos 收到无效 memos 分页")
            if not isinstance(next_token, str):
                raise MemosError("list_all_memos 收到无效 page token")
            out.extend(memos)
            if not next_token:
                break
            if next_token == token or next_token in seen_tokens:
                raise MemosError("list_all_memos 检测到重复 page token，已停止以避免无限循环")
            seen_tokens.add(next_token)
            token = next_token
        return out


    async def delete_memo(self, name: str) -> bool:
        """删一条 memo。返回是否成功。"""
        if not name.startswith("memos/"):
            name = f"memos/{name}"
        session = await self._ensure_session()
        url = self.base_url + _API_PREFIX + "/" + name
        async with session.delete(url) as resp:
            # 200/204 都视为成功
            if resp.status in (200, 204):
                return True
            body = await resp.text()
            raise MemosError(_http_error_message("delete_memo", resp.status, body))

    async def update_memo(self, name: str, content: str) -> dict[str, Any]:
        """改一条 memo 的内容(用于手动编辑的场景)。"""
        if not name.startswith("memos/"):
            name = f"memos/{name}"
        session = await self._ensure_session()
        url = self.base_url + _API_PREFIX + "/" + name
        payload = {"content": content}
        async with session.patch(url, json=payload) as resp:
            if resp.status != 200:
                body = await resp.text()
                raise MemosError(_http_error_message("update_memo", resp.status, body))
            return await resp.json()


    async def ping(self) -> bool:
        """Alias for health_check — main.py uses this name."""
        return await self.health_check()


# 写一个极简自测:连本机 memos,创建一条测试 memo,读取,删除。
# 不到 5 秒,跑完才确认 v0.29.1 接口形状对。
# 注意:这个 __main__ 块在生产 AstrBot 启动时不会跑(只是开发时验证用)。
