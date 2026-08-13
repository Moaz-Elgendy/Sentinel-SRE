import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from prometheus_fastapi_instrumentator import Instrumentator

from app.core.config import settings
from app.core.logging_config import configure_logging
from app.middleware.access_log import AccessLogMiddleware
from app.middleware.request_id import RequestIDMiddleware
from app.routers import auth, health, profile, requests, services

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
app.add_middleware(RequestIDMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_allowed_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health.router)
app.include_router(auth.router)
app.include_router(profile.router)
app.include_router(services.router)
app.include_router(requests.router)

# Exposes GET /metrics in Prometheus text format.
Instrumentator().instrument(app).expose(app, endpoint="/metrics")
