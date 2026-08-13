import logging
import time
from typing import Awaitable, Callable
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.middleware.request_id import get_request_id

logger = logging.getLogger(__name__)


class AccessLogMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        if request.url.path in ("/healthz", "/readyz", "/metrics"):
            return await call_next(request)

        start_time = time.time()
        response = None
        try:
            response = await call_next(request)
            return response
        finally:
            duration_ms = (time.time() - start_time) * 1000
            status_code = response.status_code if response else 500
            
            logger.info(
                "http_request",
                extra={
                    "method": request.method,
                    "path": request.url.path,
                    "status_code": status_code,
                    "duration_ms": round(duration_ms, 2),
                }
            )
