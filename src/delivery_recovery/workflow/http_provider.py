"""Provider-neutral OpenAI-compatible HTTP adapter.

This adapter is offline-testable with ``httpx.MockTransport``. It makes one
HTTP attempt per ``complete`` call; retry policy belongs to ``ProviderRunner``,
so a hidden SDK retry cannot multiply application attempts. Credentials are
kept in the client headers and are never added to workflow state or traces.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
from pydantic import ValidationError

from .providers import (
    ProviderAuthError,
    ProviderRequest,
    ProviderResponse,
    ProviderSchemaError,
    ProviderThrottleError,
    ProviderTimeoutError,
    ProviderTransportError,
    ToolCall,
)

__all__ = ["OpenAICompatibleProvider"]


class OpenAICompatibleProvider:
    name = "openai-compatible"

    def __init__(
        self,
        *,
        model: str,
        base_url: str = "https://api.openai.com/v1",
        api_key: str = "",
        timeout_seconds: float = 8.0,
        transport: httpx.BaseTransport | None = None,
        thinking: dict[str, Any] | None = None,
    ) -> None:
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        self.thinking = thinking
        self._client = httpx.Client(
            timeout=timeout_seconds,
            transport=transport,
        )

    def complete(self, request: ProviderRequest) -> ProviderResponse:
        payload = {
            "model": self.model,
            "messages": [self._serialize_message(message) for message in request.messages],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": tool["name"],
                        "description": tool.get("description", ""),
                        "parameters": tool.get("parameters", {}),
                    },
                }
                for tool in request.tools
            ],
            "tool_choice": "auto",
            "temperature": 0.0,
            "max_tokens": request.max_output_tokens,
        }
        if self.thinking is not None:
            payload["thinking"] = self.thinking

        timeout = self.timeout_seconds
        if request.remaining_seconds is not None:
            timeout = max(0.001, min(timeout, request.remaining_seconds))

        try:
            response = self._client.post(
                f"{self.base_url}/chat/completions",
                json=payload,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                timeout=timeout,
            )
        except httpx.TimeoutException as exc:
            raise ProviderTimeoutError(str(exc)) from exc
        except httpx.TransportError as exc:
            raise ProviderTransportError(str(exc)) from exc

        if response.status_code in {401, 403}:
            raise ProviderAuthError(f"provider returned HTTP {response.status_code}")
        if response.status_code == 429:
            raise ProviderThrottleError("provider returned HTTP 429")
        if response.status_code in {408, 504}:
            raise ProviderTimeoutError(f"provider returned HTTP {response.status_code}")
        if response.status_code in {400, 404, 409, 422}:
            raise ProviderSchemaError(f"provider returned HTTP {response.status_code}")
        if response.status_code >= 500:
            raise ProviderTransportError(f"provider returned HTTP {response.status_code}")
        if response.status_code >= 400:
            raise ProviderSchemaError(f"provider returned HTTP {response.status_code}")

        try:
            data = response.json()
            message = data["choices"][0]["message"]
            content = message.get("content") or ""
            raw_tool_calls = message.get("tool_calls") or []
            if not isinstance(raw_tool_calls, list):
                raise TypeError("tool_calls must be a list")
            tool_calls = [self._parse_tool_call(item) for item in raw_tool_calls]
            usage = data.get("usage") or {}
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise ProviderSchemaError("provider response did not match the expected schema") from exc

        try:
            return ProviderResponse(
                content=content,
                tool_calls=tool_calls,
                input_tokens=int(usage.get("prompt_tokens", 0) or 0),
                output_tokens=int(usage.get("completion_tokens", 0) or 0),
                usage_reported=bool(usage),
            )
        except (TypeError, ValueError, ValidationError) as exc:
            raise ProviderSchemaError("provider usage did not match the expected schema") from exc

    @staticmethod
    def _serialize_message(message) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "role": message.role,
            "content": message.content,
        }
        if message.tool_calls:
            payload["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": json.dumps(call.arguments),
                    },
                }
                for call in message.tool_calls
            ]
        if message.tool_call_id is not None:
            payload["tool_call_id"] = message.tool_call_id
        if message.name is not None:
            payload["name"] = message.name
        return payload

    @staticmethod
    def _parse_tool_call(item: Any) -> ToolCall:
        try:
            function = item["function"]
            call_id = item.get("id", "")
            name = function["name"]
            raw_arguments = function.get("arguments", "{}")
            arguments = (
                json.loads(raw_arguments)
                if isinstance(raw_arguments, str)
                else raw_arguments
            )
            if not isinstance(arguments, dict):
                raise TypeError("tool arguments must be an object")
            if not isinstance(name, str) or not name:
                raise TypeError("tool name must be a non-empty string")
        except (KeyError, TypeError, ValueError) as exc:
            raise ProviderSchemaError("tool call did not match the expected schema") from exc
        return ToolCall(id=call_id, name=name, arguments=arguments)
