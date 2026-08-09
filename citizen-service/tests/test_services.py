from app.core.database import get_db
from app.models.service import GovernmentService
from app.main import app


def _seed_one_service(client):
    # Insert directly through the overridden session to avoid needing a
    # dedicated admin-only "create service" endpoint (not in Phase 1 scope).
    db_gen = app.dependency_overrides[get_db]()
    db = next(db_gen)
    service = GovernmentService(
        name="Passport Renewal",
        description="Renew an expired passport.",
        required_documents=["Old passport", "Photo"],
        estimated_processing_days=14,
    )
    db.add(service)
    db.commit()
    db.refresh(service)
    db.close()
    return service


def test_list_services_empty_by_default(client):
    resp = client.get("/api/services")
    assert resp.status_code == 200
    assert resp.json() == []


def test_list_and_get_service(client):
    service = _seed_one_service(client)

    resp = client.get("/api/services")
    assert resp.status_code == 200
    assert len(resp.json()) == 1

    resp = client.get(f"/api/services/{service.id}")
    assert resp.status_code == 200
    assert resp.json()["name"] == "Passport Renewal"


def test_get_missing_service_returns_404(client):
    resp = client.get("/api/services/00000000-0000-0000-0000-000000000000")
    assert resp.status_code == 404
