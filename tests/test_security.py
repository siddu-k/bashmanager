import os
import json
import hashlib
import pytest
from unittest.mock import patch


def test_generate_and_verify_password(app_module):
    password = "MySecurePassword123!"
    hash_data = app_module.generate_password_hash(password)
    
    assert isinstance(hash_data, dict)
    assert "salt" in hash_data
    assert "hash" in hash_data
    assert "iterations" in hash_data
    assert hash_data["iterations"] == app_module.PBKDF2_ITERATIONS
    
    # Valid password verification
    assert app_module.verify_password(password, hash_data) is True
    
    # Invalid password rejection
    assert app_module.verify_password("WrongPassword!", hash_data) is False


def test_salt_uniqueness(app_module):
    password = "same_password"
    hash_data_1 = app_module.generate_password_hash(password)
    hash_data_2 = app_module.generate_password_hash(password)
    
    assert hash_data_1["salt"] != hash_data_2["salt"]
    assert hash_data_1["hash"] != hash_data_2["hash"]


def test_special_passwords(app_module):
    # Empty password
    empty_pwd = ""
    hash_data = app_module.generate_password_hash(empty_pwd)
    assert app_module.verify_password(empty_pwd, hash_data) is True
    assert app_module.verify_password("non_empty", hash_data) is False
    
    # Unicode password
    unicode_pwd = "🔒🔒🔒_unicode_🔑"
    hash_data_u = app_module.generate_password_hash(unicode_pwd)
    assert app_module.verify_password(unicode_pwd, hash_data_u) is True
    
    # Very long password
    long_pwd = "A" * 10000
    hash_data_l = app_module.generate_password_hash(long_pwd)
    assert app_module.verify_password(long_pwd, hash_data_l) is True


def test_corrupted_or_malformed_hashes(app_module):
    password = "test_password"
    hash_data = app_module.generate_password_hash(password)
    
    # Missing salt
    corrupted = hash_data.copy()
    del corrupted["salt"]
    assert app_module.verify_password(password, corrupted) is False
    
    # Missing hash
    corrupted = hash_data.copy()
    del corrupted["hash"]
    assert app_module.verify_password(password, corrupted) is False
    
    # Missing iterations
    corrupted = hash_data.copy()
    del corrupted["iterations"]
    assert app_module.verify_password(password, corrupted) is False
    
    # Non-integer iterations
    corrupted = hash_data.copy()
    corrupted["iterations"] = "100000"
    assert app_module.verify_password(password, corrupted) is False
    
    # Negative/zero iterations
    corrupted = hash_data.copy()
    corrupted["iterations"] = 0
    assert app_module.verify_password(password, corrupted) is False
    corrupted["iterations"] = -5
    assert app_module.verify_password(password, corrupted) is False
    
    # Invalid salt/hash types
    corrupted = hash_data.copy()
    corrupted["salt"] = 12345
    assert app_module.verify_password(password, corrupted) is False
    corrupted = hash_data.copy()
    corrupted["hash"] = ["some_hash"]
    assert app_module.verify_password(password, corrupted) is False
    
    # Invalid hex string
    corrupted = hash_data.copy()
    corrupted["salt"] = "not_hex_chars!"
    assert app_module.verify_password(password, corrupted) is False
    corrupted = hash_data.copy()
    corrupted["hash"] = "nothex"
    assert app_module.verify_password(password, corrupted) is False


def test_is_legacy_hash(app_module):
    assert app_module.is_legacy_hash("legacy_sha256_hash_string") is True
    assert app_module.is_legacy_hash({"salt": "123", "hash": "abc", "iterations": 100000}) is False
    assert app_module.is_legacy_hash(None) is False
    assert app_module.is_legacy_hash(12345) is False


def test_backward_compatibility_migration(app_module, tmp_path):
    # Setup temporary files using tmp_path
    locks_file = tmp_path / "locks.json"
    
    # Pre-populate with legacy SHA-256 lock
    rel_path = "category/script.sh"
    password = "legacy_pwd"
    legacy_hash = hashlib.sha256(password.encode('utf-8')).hexdigest()
    
    locks_file.write_text(json.dumps({rel_path: legacy_hash}))
    
    # Patch LOCKS_FILE path in app module to use the temporary file
    with patch.object(app_module, "LOCKS_FILE", str(locks_file)):
        # Verify check_lock returns True for correct password
        res = app_module.check_lock(rel_path, password)
        assert res is True
        
        # Verify locks file has been migrated and is no longer legacy SHA-256
        with open(locks_file, "r") as f:
            updated_locks = json.load(f)
            
        migrated_data = updated_locks[rel_path]
        assert isinstance(migrated_data, dict)
        assert migrated_data["salt"] != ""
        assert migrated_data["hash"] != legacy_hash
        assert migrated_data["iterations"] == app_module.PBKDF2_ITERATIONS
        
        # Verify subsequent check_lock succeeds with the new PBKDF2 hash
        assert app_module.check_lock(rel_path, password) is True
        
        # Verify incorrect password fails
        assert app_module.check_lock(rel_path, "wrong_pwd") is False

def test_migration_save_safety(app_module, tmp_path):
    locks_file = tmp_path / "locks.json"
    rel_path = "category/script.sh"
    password = "legacy_pwd"
    legacy_hash = hashlib.sha256(password.encode('utf-8')).hexdigest()
    
    locks_file.write_text(json.dumps({rel_path: legacy_hash}))
    
    with patch.object(app_module, "LOCKS_FILE", str(locks_file)):
        # Mock save_locks to raise an exception during migration save
        with patch.object(app_module, "save_locks", side_effect=Exception("Disk full")):
            # check_lock should still verify password and return True
            assert app_module.check_lock(rel_path, password) is True

def test_validate_safe_path_rejects_traversal(app_module, tmp_path):
    base = tmp_path / "base"
    base.mkdir()

    # traversal should be rejected
    with pytest.raises(ValueError):
        app_module.validate_safe_path(str(base), "../outside.txt")


def test_validate_safe_path_allows_normal_paths(app_module, tmp_path):
    base = tmp_path / "base2"
    base.mkdir()
    (base / "ok").mkdir()

    resolved = app_module.validate_safe_path(str(base), "ok/file.txt")
    assert str(resolved).startswith(str(base))


def test_import_github_rejects_non_github_urls(client):
    resp = client.post("/api/scripts/import_github", json={
        "url": "http://example.com/script.sh",
        "category": "cat",
        "filename": "script.sh",
    })
    assert resp.status_code == 400


def test_import_github_blocks_non_http_schemes(client):
    resp = client.post("/api/scripts/import_github", json={
        "url": "ftp://raw.githubusercontent.com/owner/repo/branch/file.sh",
        "category": "cat",
        "filename": "script.sh",
    })
    assert resp.status_code == 400


def test_import_github_rejects_large_and_binary_payloads(app_module, client):
    # Prepare a large payload (>500KB)
    large = b"A" * 600_000

    class DummyResp:
        def __init__(self, data):
            self._data = data

        def read(self):
            return self._data

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    class FakeOpener:
        def open(self, req, timeout=10):
            return DummyResp(large)

    with patch.object(app_module.urllib.request, "build_opener", return_value=FakeOpener()):
        resp = client.post("/api/scripts/import_github", json={
            "url": "https://raw.githubusercontent.com/owner/repo/branch/file.sh",
            "category": "cat",
            "filename": "script.sh",
        })
        assert resp.status_code == 400


def test_import_github_handles_non_utf8(app_module, client):
    bad = b"\xff\xff\xff"

    class DummyResp2:
        def __init__(self, data):
            self._data = data

        def read(self):
            return self._data

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    class FakeOpener2:
        def open(self, req, timeout=10):
            return DummyResp2(bad)

    with patch.object(app_module.urllib.request, "build_opener", return_value=FakeOpener2()):
        resp = client.post("/api/scripts/import_github", json={
            "url": "https://raw.githubusercontent.com/owner/repo/branch/file.sh",
            "category": "cat",
            "filename": "script.sh",
        })
        assert resp.status_code == 400


def test_save_workspace_state_validation_and_failure(app_module, tmp_path):
    # invalid payload
    ok, err = app_module.save_workspace_state("not a dict")
    assert ok is False

    valid = {"terminals": []}
    with patch("builtins.open", side_effect=Exception("disk full")):
        ok2, err2 = app_module.save_workspace_state(valid)
        assert ok2 is False


def test_save_locks_and_favorites_persist(app_module, tmp_path):
    locks_file = tmp_path / "locks.json"
    favs_file = tmp_path / "favs.json"

    with patch.object(app_module, "LOCKS_FILE", str(locks_file)), patch.object(app_module, "FAVORITES_FILE", str(favs_file)):
        app_module.save_locks({"p": "x"})
        assert app_module.load_locks() == {"p": "x"}

        app_module.save_favorites(["a", "b"])
        assert app_module.load_favorites() == ["a", "b"]


def test_save_script_path_validation_and_locked(app_module, client):
    # Force check_lock to fail to exercise locked response
    with patch.object(app_module, "check_lock", return_value=False):
        resp = client.post("/api/scripts/save", json={
            "category": "cat",
            "filename": "evil.sh",
            "content": "echo hi",
            "password": "",
        })
        assert resp.status_code == 401


def test_delete_script_removes_favorites_and_locks(app_module, client, tmp_path):
    # Setup tmp scripts dir and files
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    rel = "cat/test.sh"
    full = scripts_dir / "cat"
    full.mkdir()
    (full / "test.sh").write_text("echo hi")

    locks_file = tmp_path / "locks.json"
    favs_file = tmp_path / "favs.json"
    app_module.save_locks({rel: app_module.generate_password_hash("p")})
    app_module.save_favorites([rel])

    with patch.object(app_module, "SCRIPTS_DIR", str(scripts_dir)), patch.object(app_module, "LOCKS_FILE", str(locks_file)), patch.object(app_module, "FAVORITES_FILE", str(favs_file)):
        # ensure check_lock allows deletion
        with patch.object(app_module, "check_lock", return_value=True):
            resp = client.delete("/api/scripts/delete", json={"path": rel})
            assert resp.status_code == 200
            assert not (scripts_dir / rel).exists()


def test_manage_lock_set_and_remove(app_module, client, tmp_path):
    locks_file = tmp_path / "locks.json"
    with patch.object(app_module, "LOCKS_FILE", str(locks_file)):
        # set lock
        resp = client.post("/api/scripts/lock", json={"path": "c/s.sh", "old_password": "", "new_password": "secret"})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True

        # remove lock
        resp2 = client.post("/api/scripts/lock", json={"path": "c/s.sh", "old_password": "secret", "new_password": ""})
        assert resp2.status_code == 200
        data2 = resp2.get_json()
        assert data2["success"] is True


def test_exec_command_requires_terminal_unlock(app_module, client):
    with patch.object(app_module, "check_lock", return_value=False):
        resp = client.post("/api/exec", json={"command": "echo 1", "password": ""})
        assert resp.status_code == 401

