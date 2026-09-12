def test_register_creates_citizen(client):
    resp = client.post(
        "/api/auth/register",
        json={
            "full_name": "Omar Test",
            "national_id": "30001010112233",
            "email": "omar@example.com",
            "phone": "+201111111111",
            "password": "SuperSecret123",
        },
    )
    
    assert resp.status_code == 201
    body = resp.json()
    assert body["email"] == "omar@example.com"
    assert "password" not in body
    assert "password_hash" not in body


def test_register_duplicate_email_returns_409(client, registered_citizen):
    payload, _ = registered_citizen
    resp = client.post(
        "/api/auth/register",
        json={
            "full_name": "Someone Else",
            "national_id": "40001010112233",
            "email": payload["email"],
            "password": "AnotherPass123",
        },
    )
    assert resp.status_code == 409


def test_login_success_returns_token(client, registered_citizen):
    payload, _ = registered_citizen
    resp = client.post("/api/auth/login", json={"email": payload["email"], "password": payload["password"]})
    assert resp.status_code == 200
    assert "access_token" in resp.json()


def test_login_wrong_password_returns_401(client, registered_citizen):
    payload, _ = registered_citizen
    resp = client.post("/api/auth/login", json={"email": payload["email"], "password": "WrongPassword"})
    assert resp.status_code == 401


def test_me_requires_auth(client):
    resp = client.get("/api/auth/me")
    assert resp.status_code in (401, 403)


def test_me_returns_current_citizen(client, auth_headers, registered_citizen):
    payload, _ = registered_citizen
    resp = client.get("/api/auth/me", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["email"] == payload["email"]
