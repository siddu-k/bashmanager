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

