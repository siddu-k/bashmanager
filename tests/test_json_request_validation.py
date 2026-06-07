def test_post_endpoint_rejects_malformed_json(client):
    response = client.post(
        "/api/scripts/run",
        data='{"path": "demo.sh"',
        content_type="application/json",
    )

    assert response.status_code == 400
    data = response.get_json()
    assert data["success"] is False
    assert data["error"] == "Invalid JSON payload"


def test_post_endpoint_allows_empty_json_payload(client):
    response = client.post("/api/scripts/run", json={})

    assert response.status_code == 400
    data = response.get_json()
    assert data["error"] == "Path cannot be empty"
