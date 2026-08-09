import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from prometheus_fastapi_instrumentator import Instrumentator

from app.core.config import settings
from app.core.logging_config import configure_logging
from app.routers import health, notifications

configure_logging(service_name="notification-service")
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("notification_service_started")
    yield


app = FastAPI(
    title="Digital Citizen Services Portal — Notification Service",
    description="Receives citizen-facing events and simulates email/SMS delivery.",
    version="0.1.0",
    lifespan=lifespan,
)

# CORS: this service is only ever called server-to-server (citizen-service)
# today, not from the browser, but the frontend origin is allowed in case
# a future admin UI queries it directly.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_allowed_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health.router)
app.include_router(notifications.router)

# Exposes GET /metrics in Prometheus text format.
Instrumentator().instrument(app).expose(app, endpoint="/metrics")
