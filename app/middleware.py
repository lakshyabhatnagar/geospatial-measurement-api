import tempfile
import uuid

import anyio
from starlette.responses import JSONResponse

from app.errors import ServiceError, error_body


class RequestGuard:
    """Bound uploads before multipart parsing and admit one upload per process."""

    def __init__(self, app, settings):
        self.app = app
        self.settings = settings
        self.upload_active = False

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        request_id = str(uuid.uuid4())
        scope.setdefault("state", {})["request_id"] = request_id

        async def identified_send(message):
            if message["type"] == "http.response.start":
                message.setdefault("headers", []).append((b"x-request-id", request_id.encode()))
            await send(message)

        if scope["method"] != "POST" or scope["path"].rstrip("/") != "/api/files":
            return await self.app(scope, receive, identified_send)
        if self.upload_active:
            error = ServiceError(
                "SERVICE_BUSY", "An upload is already processing; retry shortly.", 503
            )
            return await JSONResponse(
                error_body(error, request_id), status_code=503, headers={"Retry-After": "5"}
            )(scope, receive, identified_send)
        # No await between check and assignment: admission is atomic on this event loop.
        self.upload_active = True
        try:
            headers = dict(scope["headers"])
            if b"content-length" in headers:
                try:
                    length = int(headers[b"content-length"])
                    if length < 0:
                        raise ValueError
                except ValueError:
                    raise ServiceError("INVALID_REQUEST", "Invalid Content-Length.") from None
                if length > self.settings.max_request_bytes:
                    raise ServiceError("REQUEST_TOO_LARGE", "Request exceeds its byte limit.", 413)
            with tempfile.SpooledTemporaryFile(max_size=1024**2) as body:
                total = 0
                try:
                    with anyio.fail_after(30):
                        while True:
                            message = await receive()
                            if message["type"] == "http.disconnect":
                                return
                            chunk = message.get("body", b"")
                            total += len(chunk)
                            if total > self.settings.max_request_bytes:
                                raise ServiceError(
                                    "REQUEST_TOO_LARGE", "Request exceeds its byte limit.", 413
                                )
                            await anyio.to_thread.run_sync(body.write, chunk)
                            if not message.get("more_body", False):
                                break
                except TimeoutError:
                    raise ServiceError(
                        "UPLOAD_TIMEOUT", "Upload body was not received within 30 seconds.", 408
                    ) from None
                body.seek(0)
                remaining = total

                async def replay():
                    nonlocal remaining
                    if remaining < 0:
                        return await receive()
                    chunk = await anyio.to_thread.run_sync(body.read, 64 * 1024)
                    remaining -= len(chunk)
                    more = remaining > 0
                    if not more:
                        remaining = -1
                    return {"type": "http.request", "body": chunk, "more_body": more}

                # Shield completed uploads until the blocking processing operation finishes.
                with anyio.CancelScope(shield=True):
                    await self.app(scope, replay, identified_send)
        except ServiceError as error:
            await JSONResponse(error_body(error, request_id), status_code=error.status)(
                scope, receive, identified_send
            )
        except OSError:
            error = ServiceError("STORAGE_UNAVAILABLE", "Request buffering is unavailable.", 503)
            await JSONResponse(error_body(error, request_id), status_code=503)(
                scope, receive, identified_send
            )
        finally:
            self.upload_active = False
