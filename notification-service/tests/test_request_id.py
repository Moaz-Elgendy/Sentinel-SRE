import uuid

def test_request_id_injected_if_missing(client):
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert "X-Request-ID" in resp.headers
    req_id = resp.headers["X-Request-ID"]
    assert uuid.UUID(req_id)


def test_request_id_echoed_if_present(client):
    custom_id = "my-custom-request-id-123"
    resp = client.get("/healthz", headers={"X-Request-ID": custom_id})
    assert resp.status_code == 200
    assert resp.headers["X-Request-ID"] == custom_id
