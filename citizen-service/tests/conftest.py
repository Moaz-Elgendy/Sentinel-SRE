import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base, get_db
from app.core.notifications import get_notification_client
from app.main import app

# Tests run against an in-memory SQLite DB so the suite has no external
# dependencies (no live Postgres required). Production always uses Postgres
# via DATABASE_* env vars — see app/core/config.py.
engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


@pytest.fixture(autouse=True)
def _fresh_schema():
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)


def override_get_db():
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()


app.dependency_overrides[get_db] = override_get_db


class _FakeNotificationClient:
    """No-op stand-in for NotificationClient so the suite never makes a
    real HTTP call to notification-service (which may not be running)."""

    def __init__(self):
        self.sent = []

    def send(self, **kwargs):
        self.sent.append(kwargs)


fake_notifier = _FakeNotificationClient()
app.dependency_overrides[get_notification_client] = lambda: fake_notifier


@pytest.fixture
def client():
    fake_notifier.sent.clear()
    return TestClient(app)


@pytest.fixture
def registered_citizen(client):
    payload = {
        "full_name": "Amina Test",
        "national_id": "29901010112233",
        "email": "amina@example.com",
        "phone": "+201000000000",
        "password": "SuperSecret123",
    }
    resp = client.post("/api/auth/register", json=payload)
    assert resp.status_code == 201
    return payload, resp.json()


@pytest.fixture
def auth_headers(client, registered_citizen):
    payload, _ = registered_citizen
    resp = client.post("/api/auth/login", json={"email": payload["email"], "password": payload["password"]})
    assert resp.status_code == 200
    token = resp.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}
