import os
import json
import uuid
from datetime import datetime, timezone

from .validators import validate_safe_path

VERSIONS_DIR = os.path.join(
    os.environ.get("DEV_SHELL_DATA_DIR", os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), 
    "logs", "versions"
)

def _get_script_versions_dir(rel_path):
    safe_rel = rel_path.replace("..", "").replace("\\", "/")
    return str(validate_safe_path(VERSIONS_DIR, safe_rel))

def save_version(rel_path, content, author="admin", comment="Auto-saved"):
    versions_dir = _get_script_versions_dir(rel_path)
    os.makedirs(versions_dir, exist_ok=True)
    
    history_file = os.path.join(versions_dir, "history.json")
    
    history = []
    if os.path.exists(history_file):
        try:
            with open(history_file, 'r', encoding='utf-8') as f:
                history = json.load(f)
        except Exception:
            history = []
            
    version_id = str(uuid.uuid4().hex[:8])
    version_num = len(history) + 1
    
    version_filename = f"v{version_num}_{version_id}.sh"
    version_path = os.path.join(versions_dir, version_filename)
    
    with open(version_path, 'w', encoding='utf-8', newline='\n') as f:
        f.write(content)
        
    entry = {
        "version": version_num,
        "id": version_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "author": author,
        "comment": comment,
        "filename": version_filename
    }
    
    history.append(entry)
    
    with open(history_file, 'w', encoding='utf-8') as f:
        json.dump(history, f, indent=2)
        
    return entry

def get_versions(rel_path):
    versions_dir = _get_script_versions_dir(rel_path)
    history_file = os.path.join(versions_dir, "history.json")
    
    if os.path.exists(history_file):
        try:
            with open(history_file, 'r', encoding='utf-8') as f:
                # Return versions sorted by newest first
                history = json.load(f)
                return sorted(history, key=lambda x: x.get("version", 0), reverse=True)
        except Exception:
            return []
    return []

def get_version_content(rel_path, version_num):
    versions_dir = _get_script_versions_dir(rel_path)
    history = get_versions(rel_path)
    
    for entry in history:
        if str(entry.get("version")) == str(version_num):
            version_path = os.path.join(versions_dir, entry.get("filename"))
            if os.path.exists(version_path):
                with open(version_path, 'r', encoding='utf-8') as f:
                    return f.read()
                    
    return None
