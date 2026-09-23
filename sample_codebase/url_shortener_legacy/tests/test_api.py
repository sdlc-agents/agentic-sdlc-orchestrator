from __future__ import annotations

TARGET = "https://example.com/some/deep/path?q=1"


def test_create_returns_201_with_short_url(client):
    response = client.post("/api/v1/urls", json={"long_url": TARGET})
    assert response.status_code == 201
    body = response.json()
    assert body["long_url"] == TARGET
    assert body["short_url"].endswith(body["code"])
    assert body["active"] is True


def test_redirect_is_302_to_target(client):
    code = client.post("/api/v1/urls", json={"long_url": TARGET}).json()["code"]
    response = client.get("/" + code, follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == TARGET


def test_unknown_code_is_404(client):
    assert client.get("/nosuchcode", follow_redirects=False).status_code == 404


def test_non_http_scheme_is_rejected(client):
    response = client.post("/api/v1/urls", json={"long_url": "javascript:alert(1)"})
    assert response.status_code == 422


def test_custom_alias_is_honoured_then_conflicts(client):
    first = client.post(
        "/api/v1/urls", json={"long_url": TARGET, "custom_code": "schwab1"}
    )
    assert first.status_code == 201
    assert first.json()["code"] == "schwab1"

    second = client.post(
        "/api/v1/urls", json={"long_url": TARGET, "custom_code": "schwab1"}
    )
    assert second.status_code == 409


def test_reserved_alias_is_rejected(client):
    response = client.post(
        "/api/v1/urls", json={"long_url": TARGET, "custom_code": "healthz"}
    )
    assert response.status_code == 422


def test_expired_link_stops_resolving(client):
    code = client.post(
        "/api/v1/urls", json={"long_url": TARGET, "ttl_seconds": 60}
    ).json()["code"]
    client.app.state.urls.purge_expired(now=9_999_999_999)
    client.app.state.cache.invalidate(code)
    assert client.get("/" + code, follow_redirects=False).status_code == 404


def test_delete_removes_link_from_cache_and_resolution(client):
    code = client.post("/api/v1/urls", json={"long_url": TARGET}).json()["code"]
    assert client.get("/" + code, follow_redirects=False).status_code == 302
    assert client.delete("/api/v1/urls/" + code).status_code == 204
    assert client.get("/" + code, follow_redirects=False).status_code == 404


def test_metadata_endpoint(client):
    code = client.post("/api/v1/urls", json={"long_url": TARGET}).json()["code"]
    body = client.get("/api/v1/urls/" + code).json()
    assert body["code"] == code
    assert client.get("/api/v1/urls/missing").status_code == 404


def test_health_endpoints_are_not_shadowed_by_the_catch_all(client):
    assert client.get("/healthz").json()["status"] == "ok"
    assert client.get("/readyz").json()["status"] == "ready"
