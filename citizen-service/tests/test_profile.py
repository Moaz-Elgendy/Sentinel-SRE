def test_get_profile(client, auth_headers):
    resp = client.get("/api/profile", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["full_name"] == "Amina Test"


def test_update_profile(client, auth_headers):
    resp = client.put("/api/profile", headers=auth_headers, json={"full_name": "Amina Updated", "phone": "+201222222222"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["full_name"] == "Amina Updated"
    assert body["phone"] == "+201222222222"


def test_profile_requires_auth(client):
    resp = client.get("/api/profile")
    assert resp.status_code in (401, 403)
