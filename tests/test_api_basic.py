def test_list_scripts_returns_json(client):

    response = client.get("/api/scripts")

    assert response.status_code == 200
    data = response.get_json()
    assert isinstance(data, dict)

    for category, scripts in data.items():
        assert isinstance(category, str)
        assert isinstance(scripts, list)
        for script in scripts:
            assert isinstance(script, dict)
            assert "file" in script
            assert "relative_path" in script
            assert "favorite" in script
            assert "locked" in script


def test_workspace_get_returns_success(client):

    response = client.get("/api/workspace")

    assert response.status_code == 200
    data = response.get_json()
    assert data["success"] is True
    assert "workspace" in data
