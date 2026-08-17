import asyncio
import logging
import random
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from prometheus_fastapi_instrumentator import Instrumentator

from app.core.config import settings
from app.chaos.state import controller
from app.core.logging_config import configure_logging
from app.middleware.access_log import AccessLogMiddleware
from app.middleware.request_id import RequestIDMiddleware
from app.routers import auth, chaos, health, profile, requests, services

configure_logging(service_name="citizen-service")
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("citizen_service_started")
    yield


app = FastAPI(
    title="Digital Citizen Services Portal — Citizen Service",
    description="Primary backend for citizen registration, profiles, government services, and requests.",
    version="0.1.0",
    lifespan=lifespan,
)

# CORS: allow_origins is read from CORS_ALLOWED_ORIGINS (comma-separated)
# so it's adjustable per deployment without a code change — see
# app/core/config.py. Tighten this for a real deployment.
app.add_middleware(AccessLogMiddleware)


@app.middleware("http")
async def chaos_middleware(request, call_next):
    state = controller.get()
    path = request.url.path
    excluded = {"/healthz", "/readyz", "/metrics", "/docs", "/redoc", "/openapi.json"}
    is_control = path.startswith("/api/chaos/")

    if settings.chaos_mode and path not in excluded and not is_control:
        if state.latency_ms > 0:
            controller.record("latency")
            await asyncio.sleep(state.latency_ms / 1000)

        if state.db_failure:
            controller.record("database")
            return JSONResponse(
                status_code=503,
                content={"detail": "simulated database connection failure"},
            )

        if state.error_rate > 0 and random.random() < state.error_rate:
            controller.record("http_5xx")
            return JSONResponse(
                status_code=503,
                content={"detail": "simulated chaos failure"},
            )

    return await call_next(request)
app.add_middleware(RequestIDMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_allowed_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health.router)
app.include_router(chaos.router)
app.include_router(auth.router)
app.include_router(profile.router)
app.include_router(services.router)
app.include_router(requests.router)

# Exposes GET /metrics in Prometheus text format.
Instrumentator().instrument(app).expose(app, endpoint="/metrics")
