"""Raw-body guards for the recovery endpoint.

FastAPI's default JSON decoder keeps the last value for a repeated object key.
That silently accepts ambiguous input, so this middleware parses the raw body
with a duplicate-rejecting object hook before typed validation. It also bounds
the request body to the planned 64 KiB limit.

The parsed body is replayed to the application so FastAPI still owns normal
typed validation and OpenAPI generation.
"""

from __future__ import annotations

import json
from typing import Any

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

__all__ = ["JSONBodyGuardMiddleware", "MAX_BODY_BYTES"]

MAX_BODY_BYTES = 64 * 1024
TARGET_PATH = "/v1/recovery/evaluations"


class DuplicateJSONKeyError(ValueError):
    """A JSON object contained the same key more than once."""

    def __init__(self, key: str) -> None:
        super().__init__(f"duplicate JSON key {key!r}")
        self.key = key


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DuplicateJSONKeyError(key)
        result[key] = value
    return result


def _error(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": message}},
    )


class JSONBodyGuardMiddleware:
    """Apply duplicate-key and size checks to the JSON recovery endpoint."""

    def __init__(self, app: ASGIApp, *, max_body_bytes: int = MAX_BODY_BYTES) -> None:
        self.app = app
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or scope.get("method") != "POST"
            or scope.get("path") != TARGET_PATH
        ):
            await self.app(scope, receive, send)
            return

        body = bytearray()
        while True:
            message: Message = await receive()
            if message["type"] == "http.disconnect":
                return
            body.extend(message.get("body", b""))
            if len(body) > self.max_body_bytes:
                await _error(
                    413,
                    "REQUEST_TOO_LARGE",
                    f"JSON body exceeds {self.max_body_bytes} bytes",
                )(scope, receive, send)
                return
            if not message.get("more_body", False):
                break

        if body:
            try:
                json.loads(
                    body.decode("utf-8"),
                    object_pairs_hook=_reject_duplicate_keys,
                )
            except DuplicateJSONKeyError as exc:
                await _error(422, "DUPLICATE_JSON_KEY", str(exc))(
                    scope, receive, send
                )
                return
            except (UnicodeDecodeError, json.JSONDecodeError):
                # FastAPI will produce its normal validation response.
                pass

        replayed = False

        async def replay() -> Message:
            nonlocal replayed
            if not replayed:
                replayed = True
                return {
                    "type": "http.request",
                    "body": bytes(body),
                    "more_body": False,
                }
            return await receive()

        await self.app(scope, replay, send)
