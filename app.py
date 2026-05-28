import os
import json
import time
import subprocess  # nosec B404
import tempfile
import threading
import queue
import uuid
import psutil
import hashlib
import hmac
import secrets
import binascii
import urllib.request
import urllib.parse
import re
import shutil
import logging
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from flask import Flask, request, jsonify, send_from_directory, Response

# Setup logger for DevShell backend logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("devshell")

from utils.validators import validate_safe_path, validate_git_branch, validate_repo_name

PBKDF2_ITERATIONS = 100_000

app = Flask(__name__, static_folder="ui", static_url_path="")

@app.errorhandler(ValueError)
def handle_validation_error(e):
    return jsonify({"error": str(e)}), 400

BASE_DIR = os.environ.get(
    "DEV_SHELL_DATA_DIR", os.path.dirname(os.path.abspath(__file__))
)
SCRIPTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts")
FAVORITES_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "favorites.json"
)
LOCKS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "locks.json")
LOG_ROOT = os.path.join(BASE_DIR, "logs")
EXECUTION_LOG_DIR = os.path.join(LOG_ROOT, "executions")
SESSION_LOG_DIR = os.path.join(LOG_ROOT, "sessions")
HISTORY_FILE = os.path.join(LOG_ROOT, "history.jsonl")
FAILED_HISTORY_FILE = os.path.join(LOG_ROOT, "failed.jsonl")
COMMAND_HISTORY_FILE = os.path.join(LOG_ROOT, "command_history.json")
WORKSPACE_DIR = os.path.join(LOG_ROOT, "workspaces")
WORKSPACE_STATE_FILE = os.path.join(WORKSPACE_DIR, "workspace_state.json")
WORKSPACE_PROFILE_DIR = os.path.join(WORKSPACE_DIR, "profiles")
os.makedirs(WORKSPACE_DIR, exist_ok=True)
os.makedirs(WORKSPACE_PROFILE_DIR, exist_ok=True)

# Reliability intelligence infrastructure (filesystem-only, append-friendly)
RELIABILITY_DIR = os.path.join(LOG_ROOT, 'reliability')
RELIABILITY_SUMMARY_VERSION = 1
RELIABILITY_SUMMARY_FILE = os.path.join(RELIABILITY_DIR, 'summary.json')
RELIABILITY_SUMMARY_TMP = os.path.join(RELIABILITY_DIR, 'summary.json.tmp')
RELIABILITY_SUMMARY_BACKUP = os.path.join(RELIABILITY_DIR, 'summary.json.backup')
RELIABILITY_EVENTS_FILE = os.path.join(RELIABILITY_DIR, 'events.jsonl')
RELIABILITY_TREND_WINDOW = 5
RELIABILITY_FLAKY_WINDOW = 10
RELIABILITY_SLOW_STDDEV = 2
MAX_RELIABILITY_EVENTS = 5000
RELIABILITY_REGRESSION_RECENT = 5
RELIABILITY_REGRESSION_BASELINE = 10
RELIABILITY_REGRESSION_THRESHOLD = 1.5
RELIABILITY_SYNC_EVENT_LOOKBACK = 100
RELIABILITY_AGGREGATION_TAIL = 2500
RELIABILITY_DIAGNOSTICS_TTL_SEC = 45
RELIABILITY_SUMMARY_SAVE_INTERVAL_SEC = 2.0
MAX_SESSION_SCAN_FOR_DIAGNOSTICS = 200
RELIABILITY_DIAGNOSTIC_SOURCES = {
    'history': 'logs/history.jsonl',
    'sessions': 'logs/sessions',
    'workspace': 'logs/workspaces/workspace_state.json',
    'reliability': 'logs/reliability/summary.json',
    'failed_history': 'logs/failed.jsonl',
}
os.makedirs(RELIABILITY_DIR, exist_ok=True)

_reliability_cache_lock = threading.Lock()
_reliability_cache = {
    'records': None,
    'records_signature': None,
    'diagnostics': None,
    'diagnostics_signature': None,
}
_last_summary_save_monotonic = 0.0

# Failure classification types
FAILURE_TYPES = {
    'permission_error': 'Permission denied or insufficient privileges',
    'dependency_error': 'Missing dependency or import failed',
    'timeout': 'Execution timeout exceeded',
    'shell_error': 'Shell error or syntax issue',
    'missing_file': 'Required file not found',
    'interrupted': 'Execution interrupted by user',
    'unknown_failure': 'Unknown or unclassified failure',
}

SESSIONS_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "sessions.json"
)
MAX_HISTORY_ENTRIES = 1000
MAX_FAILED_HISTORY_ENTRIES = 500
MAX_EXECUTION_LOG_FILES = 250
LOG_RETENTION_DAYS = 30
MAX_HISTORY_EXCERPT_CHARS = 2000

# Thread-safe registry for running script processes (keyed by run_id)
active_processes = {}
active_processes_lock = threading.Lock()


def validate_workspace_snapshot(data):
    if not isinstance(data, dict):
        return False, "Workspace snapshot must be an object"

    terminals = data.get("terminals")
    if terminals is not None and not isinstance(terminals, list):
        return False, "Invalid terminals structure"

    active_terminal = data.get("activeTerminalId")
    if active_terminal is not None and not isinstance(active_terminal, int):
        return False, "Invalid active terminal"

    version = data.get("version")
    if version is not None and not isinstance(version, int):
        return False, "Invalid snapshot version"

    active_script = data.get("activeScript")
    if active_script is not None and not isinstance(active_script, str):
        return False, "Invalid active script reference"

    return True, None


def load_workspace_state():
    if not os.path.exists(WORKSPACE_STATE_FILE):
        return None
    try:
        with open(WORKSPACE_STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        corrupted_path = WORKSPACE_STATE_FILE + ".corrupted"
        try:
            shutil.move(WORKSPACE_STATE_FILE, corrupted_path)
        except Exception:  # nosec B110
            pass
        return {"corrupted": True, "error": str(e)}


def save_workspace_state(data):
    valid, error = validate_workspace_snapshot(data)
    if not valid:
        return False, error

    payload = {
        "version": 2,
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "workspace": data,
    }

    try:
        with open(WORKSPACE_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        return True, None
    except Exception as e:
        return False, str(e)


def get_workspace_profile_path(name):
    safe_name = re.sub(r"[^a-zA-Z0-9_-]", "_", name)
    return os.path.join(WORKSPACE_PROFILE_DIR, f"{safe_name}.json")


def list_workspace_profiles():
    profiles = []
    for file in os.listdir(WORKSPACE_PROFILE_DIR):
        if not file.endswith(".json"):
            continue
        profiles.append(file[:-5])
    return sorted(profiles)


def _ensure_log_dirs():
    os.makedirs(EXECUTION_LOG_DIR, exist_ok=True)
    os.makedirs(SESSION_LOG_DIR, exist_ok=True)
    os.makedirs(RELIABILITY_DIR, exist_ok=True)


def _utc_now():
    return datetime.now(timezone.utc)


def _iso_now():
    return _utc_now().isoformat(timespec="seconds")


def _slugify(value, fallback="execution"):
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", str(value or "")).strip("-._")
    return safe[:48] or fallback


def _append_jsonl(file_path, record):
    os.makedirs(os.path.dirname(file_path), exist_ok=True)
    with open(file_path, "a", encoding="utf-8", newline="\n") as f:
        json.dump(record, f, ensure_ascii=False)
        f.write("\n")


def _read_jsonl(file_path, max_entries=None):
    records = []
    if not os.path.exists(file_path):
        return records
    try:
        with open(file_path, 'r', encoding='utf-8', errors='replace') as f:
            if max_entries:
                lines = f.readlines()[-max_entries:]
            else:
                lines = f
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    parsed = json.loads(line)
                    if isinstance(parsed, dict):
                        records.append(parsed)
                except (json.JSONDecodeError, TypeError, ValueError):
                    continue
    except OSError:
        return []
    return records


def _reliability_source_signature():
    """Cheap cache key from mtimes of reliability input files."""
    paths = (HISTORY_FILE, FAILED_HISTORY_FILE, RELIABILITY_SUMMARY_FILE, WORKSPACE_STATE_FILE)
    signature = []
    for path in paths:
        try:
            signature.append((path, os.path.getmtime(path)))
        except OSError:
            signature.append((path, None))
    if os.path.isdir(SESSION_LOG_DIR):
        try:
            session_count = len([
                name for name in os.listdir(SESSION_LOG_DIR)
                if name.endswith('.json') and '.corrupted' not in name
            ])
            session_mtime = os.path.getmtime(SESSION_LOG_DIR)
        except OSError:
            session_count = 0
            session_mtime = None
        signature.append((SESSION_LOG_DIR, session_mtime, session_count))
    return tuple(signature)


def _invalidate_reliability_cache(keys=None):
    with _reliability_cache_lock:
        if keys:
            for key in keys:
                _reliability_cache[key] = None
        else:
            _reliability_cache['records'] = None
            _reliability_cache['records_signature'] = None
            _reliability_cache['diagnostics'] = None
            _reliability_cache['diagnostics_signature'] = None


def _maybe_save_reliability_summary(summary, force=False):
    """Throttle summary.json writes during rapid execution bursts."""
    global _last_summary_save_monotonic
    now = time.perf_counter()
    if not force and (now - _last_summary_save_monotonic) < RELIABILITY_SUMMARY_SAVE_INTERVAL_SEC:
        return True
    if _save_reliability_summary(summary):
        _last_summary_save_monotonic = now
        _invalidate_reliability_cache(keys=['diagnostics'])
        return True
    return False


def _sanitize_execution_record(entry):
    """Validate and normalize execution metadata from history/session sources."""
    if not isinstance(entry, dict):
        return None
    execution_id = entry.get('id')
    if not execution_id or not isinstance(execution_id, (str, int)):
        return None
    execution_id = str(execution_id).strip()[:64]
    if not execution_id:
        return None

    success = bool(entry.get('success', entry.get('status') == 'success'))
    exit_code = _normalize_exit_code(entry.get('exit_code'))
    duration_seconds = _normalize_duration(entry.get('duration_seconds'))
    display_name = str(entry.get('display_name') or entry.get('display') or '_unknown')[:256]
    kind = str(entry.get('kind') or 'script')[:32]
    if kind not in ('script', 'command'):
        kind = 'script'

    sanitized = {
        'id': execution_id,
        'kind': kind,
        'display_name': display_name,
        'command': str(entry.get('command', ''))[:2000],
        'started_at': str(entry.get('started_at', ''))[:64],
        'finished_at': str(entry.get('finished_at', ''))[:64],
        'status': 'success' if success else 'failed',
        'success': success,
        'exit_code': exit_code,
        'duration_seconds': duration_seconds if duration_seconds > 0 else None,
        'log_file': str(entry.get('log_file', ''))[:256],
        'session_file': str(entry.get('session_file', ''))[:128],
        'output_excerpt': str(entry.get('output_excerpt', ''))[:MAX_HISTORY_EXCERPT_CHARS],
        'error': str(entry.get('error', ''))[:MAX_HISTORY_EXCERPT_CHARS],
        'source': str(entry.get('source', 'history'))[:32],
    }
    if entry.get('failure_type'):
        failure_type = entry.get('failure_type')
        sanitized['failure_type'] = failure_type if failure_type in FAILURE_TYPES else 'unknown_failure'
    elif not success:
        sanitized['failure_type'] = _classify_failure(
            exit_code,
            error_message=sanitized.get('error', ''),
            output=sanitized.get('output_excerpt', ''),
        )
    return sanitized


def _index_records_by_script(records):
    indexed = {}
    for record in records:
        name = record.get('display_name')
        if not name:
            continue
        indexed.setdefault(name, []).append(record)
    return indexed


def _trim_jsonl(file_path, max_entries):
    if not os.path.exists(file_path):
        return
    with open(file_path, "r", encoding="utf-8", errors="replace") as f:
        lines = f.readlines()
    if len(lines) <= max_entries:
        return
    with open(file_path, "w", encoding="utf-8", newline="\n") as f:
        f.writelines(lines[-max_entries:])


def _cleanup_old_execution_logs():
    if not os.path.exists(EXECUTION_LOG_DIR):
        return
    now = time.time()
    cutoff = now - (LOG_RETENTION_DAYS * 24 * 60 * 60)
    logs = []
    for name in os.listdir(EXECUTION_LOG_DIR):
        path = os.path.join(EXECUTION_LOG_DIR, name)
        if not os.path.isfile(path):
            continue
        try:
            logs.append((os.path.getmtime(path), path))
        except OSError:
            continue

    for _, path in logs:
        try:
            if os.path.getmtime(path) < cutoff:
                os.remove(path)
        except OSError:
            pass

    logs = sorted(logs, key=lambda item: item[0], reverse=True)
    for _, path in logs[MAX_EXECUTION_LOG_FILES:]:
        try:
            os.remove(path)
        except OSError:
            pass


def _format_duration(seconds):
    if seconds < 60:
        return f"{seconds:.2f}s"
    minutes = int(seconds // 60)
    remaining = seconds % 60
    return f"{minutes}m {remaining:.1f}s"


def _start_execution_record(kind, display_name, command_text, shell_cmd="", cwd=""):
    _ensure_log_dirs()
    started_at = _utc_now()
    monotonic_start = time.perf_counter()
    execution_id = uuid.uuid4().hex[:8]
    timestamp_token = started_at.strftime("%Y%m%dT%H%M%SZ")
    log_name = f"{timestamp_token}_{kind}_{_slugify(display_name)}_{execution_id}.log"
    log_path = os.path.join(EXECUTION_LOG_DIR, log_name)
    log_handle = open(log_path, "w", encoding="utf-8", newline="\n")

    record = {
        "id": execution_id,
        "kind": kind,
        "display_name": display_name,
        "command": command_text,
        "shell": shell_cmd,
        "cwd": cwd,
        "started_at": started_at.isoformat(),
        "status": "running",
        "exit_code": None,
        "duration_seconds": None,
        "log_file": log_name,
        "log_path": log_path,
        "output_excerpt": "",
        "success": False,
        "session_file": f"{execution_id}.json",
    }

    log_handle.write(f'[{record["started_at"]}] execution started\n')
    log_handle.write(f"kind: {kind}\n")
    log_handle.write(f"id: {execution_id}\n")
    log_handle.write(f"display: {display_name}\n")
    log_handle.write(f"command: {command_text}\n")
    if shell_cmd:
        log_handle.write(f"shell: {shell_cmd}\n")
    if cwd:
        log_handle.write(f"cwd: {cwd}\n")
    log_handle.write("\n")
    log_handle.flush()

    session_data = {
        "metadata": {
            "id": execution_id,
            "kind": kind,
            "display_name": display_name,
            "command": command_text,
            "shell": shell_cmd,
            "cwd": cwd,
            "started_at": started_at.isoformat(),
        },
        "events": [],
    }

    return {
        "record": record,
        "handle": log_handle,
        "excerpt_lines": [],
        "excerpt_size": 0,
        "session_data": session_data,
        "monotonic_start": monotonic_start,
    }


def _append_execution_line(execution, stream_type, content):
    if execution is None:
        return
    line = content.rstrip("\n")
    if not line and stream_type != "system":
        return
    timestamp = _iso_now()
    elapsed = round(time.perf_counter() - execution["monotonic_start"], 4)
    execution["session_data"]["events"].append(
        {"timestamp": elapsed, "stream": stream_type, "content": line}
    )
    execution["handle"].write(f"[{timestamp}] {stream_type}: {line}\n")
    execution["handle"].flush()
    excerpt_line = f"{stream_type}: {line}"
    execution["excerpt_lines"].append(excerpt_line)
    execution["excerpt_size"] += len(excerpt_line) + 1
    while (
        execution["excerpt_lines"]
        and execution["excerpt_size"] > MAX_HISTORY_EXCERPT_CHARS
    ):
        removed = execution["excerpt_lines"].pop(0)
        execution["excerpt_size"] -= len(removed) + 1


def _finalize_execution(
    execution,
    success,
    exit_code,
    duration_seconds,
    resource_usage=None,
    error_message="",
):
    if execution is None:
        return None

    record = execution["record"]
    record["status"] = "success" if success else "failed"
    record["success"] = bool(success)
    record["exit_code"] = int(exit_code) if exit_code is not None else None
    record["duration_seconds"] = (
        round(duration_seconds, 3) if duration_seconds is not None else None
    )
    record["duration"] = _format_duration(duration_seconds or 0)
    record["finished_at"] = _iso_now()
    record["output_excerpt"] = "\n".join(execution["excerpt_lines"])[
        -MAX_HISTORY_EXCERPT_CHARS:
    ]
    if resource_usage:
        record["resources"] = resource_usage
    if error_message:
        record["error"] = error_message

    execution["handle"].write("\n")
    execution["handle"].write(f'[{record["finished_at"]}] status: {record["status"]}\n')
    if record["exit_code"] is not None:
        execution["handle"].write(f'exit_code: {record["exit_code"]}\n')
    if record["duration_seconds"] is not None:
        execution["handle"].write(f'duration_seconds: {record["duration_seconds"]}\n')
    if error_message:
        execution["handle"].write(f"error: {error_message}\n")
    if resource_usage:
        execution["handle"].write(
            f"resources: {json.dumps(resource_usage, ensure_ascii=False)}\n"
        )
    session_path = os.path.join(SESSION_LOG_DIR, record["session_file"])
    execution["session_data"]["metadata"].update(
        {
            "finished_at": record["finished_at"],
            "duration_seconds": record["duration_seconds"],
            "exit_code": record["exit_code"],
            "status": record["status"],
            "success": record["success"],
        }
    )
    if resource_usage:
        execution["session_data"]["metadata"]["resources"] = resource_usage
    with open(session_path, "w", encoding="utf-8") as sf:
        json.dump(execution["session_data"], sf, indent=2, ensure_ascii=False)
    execution["handle"].close()

    history_record = {
        "id": record["id"],
        "kind": record["kind"],
        "session_file": record["session_file"],
        "display_name": record["display_name"],
        "command": record["command"],
        "shell": record["shell"],
        "cwd": record["cwd"],
        "started_at": record["started_at"],
        "finished_at": record["finished_at"],
        "status": record["status"],
        "success": record["success"],
        "exit_code": record["exit_code"],
        "duration_seconds": record["duration_seconds"],
        "duration": record["duration"],
        "log_file": record["log_file"],
        "output_excerpt": record["output_excerpt"],
    }
    if error_message:
        history_record["error"] = error_message
    if resource_usage:
        history_record["resources"] = resource_usage
    
    # Add failure classification for failed executions
    if not success:
        failure_type = _classify_failure(
            record['exit_code'],
            error_message=error_message,
            output=record['output_excerpt']
        )
        history_record['failure_type'] = failure_type

    _append_jsonl(HISTORY_FILE, history_record)
    if not success:
        _append_jsonl(FAILED_HISTORY_FILE, history_record)

    _trim_jsonl(HISTORY_FILE, MAX_HISTORY_ENTRIES)
    _trim_jsonl(FAILED_HISTORY_FILE, MAX_FAILED_HISTORY_ENTRIES)
    _cleanup_old_execution_logs()
    _invalidate_reliability_cache()
    _update_reliability_after_execution(history_record)
    _sync_reliability_from_session_file(record['session_file'])

    return history_record


def load_command_history():
    if not os.path.exists(COMMAND_HISTORY_FILE):
        return []

    try:
        with open(COMMAND_HISTORY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)

    except Exception:
        return []


def save_command_history(command):
    if not command.strip():
        return

    history = load_command_history()

    # Remove duplicates
    history = [c for c in history if c != command]

    history.insert(0, command)

    # Keep latest 200
    history = history[:200]

    with open(COMMAND_HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, indent=2)


def _load_history_entries(query="", status="all", kind="all", limit=200):
    entries = _read_jsonl(HISTORY_FILE)
    query = (query or "").strip().lower()
    status = (status or "all").strip().lower()
    kind = (kind or "all").strip().lower()

    def matches(entry):
        if status != "all" and entry.get("status", "").lower() != status:
            return False
        if kind != "all" and entry.get("kind", "").lower() != kind:
            return False
        if not query:
            return True
        haystack = " ".join(
            [
                str(entry.get("command", "")),
                str(entry.get("display_name", "")),
                str(entry.get("output_excerpt", "")),
                str(entry.get("status", "")),
                str(entry.get("kind", "")),
                str(entry.get("exit_code", "")),
            ]
        ).lower()
        return query in haystack

    filtered = [entry for entry in reversed(entries) if matches(entry)]
    return filtered[:limit]


def _history_summary():
    entries = _read_jsonl(HISTORY_FILE)
    total = len(entries)
    failed = sum(1 for entry in entries if entry.get("status") == "failed")
    scripts = sum(1 for entry in entries if entry.get("kind") == "script")
    commands = sum(1 for entry in entries if entry.get("kind") == "command")
    return {
        "total": total,
        "failed": failed,
        "successful": total - failed,
        "scripts": scripts,
        "commands": commands,
    }


# ─── Reliability Intelligence Infrastructure ───────────────────────

def _corrupted_fallback_path(file_path):
    return file_path + '.corrupted'


def _isolate_corrupted_file(file_path):
    if not os.path.exists(file_path):
        return
    corrupted = _corrupted_fallback_path(file_path)
    suffix = 1
    while os.path.exists(corrupted):
        corrupted = f'{file_path}.corrupted.{suffix}'
        suffix += 1
    try:
        shutil.move(file_path, corrupted)
    except OSError:
        pass


def _safe_load_json(file_path, default=None, required_keys=None):
    """Load JSON with corruption isolation via .corrupted fallback files."""
    default = default if default is not None else {}
    required_keys = required_keys or []
    if not os.path.exists(file_path):
        return json.loads(json.dumps(default))

    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError('expected object')
        if required_keys and not all(key in data for key in required_keys):
            raise ValueError('missing required keys')
        return data
    except (json.JSONDecodeError, OSError, ValueError, TypeError):
        _isolate_corrupted_file(file_path)
        return json.loads(json.dumps(default))


def _migrate_reliability_summary(data):
    """Upgrade on-disk summary payloads to the current schema version."""
    if not isinstance(data, dict):
        data = {}

    version = data.get('version')
    if version is None:
        # Pre-version summaries: preserve scripts/global, stamp v1
        data = {
            'version': RELIABILITY_SUMMARY_VERSION,
            'scripts': data.get('scripts') if isinstance(data.get('scripts'), dict) else {},
            'global': data.get('global') if isinstance(data.get('global'), dict) else {},
            'updated_at': data.get('updated_at'),
        }
    elif version < RELIABILITY_SUMMARY_VERSION:
        data['version'] = RELIABILITY_SUMMARY_VERSION
    elif version > RELIABILITY_SUMMARY_VERSION:
        # Forward-compatible: normalize what we understand today
        data['version'] = RELIABILITY_SUMMARY_VERSION

    return data


def _cap_failure_breakdown(breakdown):
    """Keep failure_breakdown bounded to known failure types only."""
    if not isinstance(breakdown, dict):
        return {}

    capped = {}
    overflow = 0
    for key, value in breakdown.items():
        count = max(0, int(value or 0))
        if count <= 0:
            continue
        if key in FAILURE_TYPES:
            capped[key] = capped.get(key, 0) + count
        else:
            overflow += count
    if overflow:
        capped['unknown_failure'] = capped.get('unknown_failure', 0) + overflow
    return capped


def _load_reliability_summary():
    """Load reliability summary from storage with backup and corruption recovery."""
    default = {'version': RELIABILITY_SUMMARY_VERSION, 'scripts': {}, 'global': {}}
    corrupted = False
    data = _migrate_reliability_summary(_safe_load_json(
        RELIABILITY_SUMMARY_FILE,
        default=default,
        required_keys=['scripts'],
    ))
    if not data.get('scripts') and os.path.exists(RELIABILITY_SUMMARY_FILE + '.corrupted'):
        corrupted = True
    if data.get('scripts'):
        normalized = _normalize_reliability_summary(data)
        if corrupted:
            normalized['corrupted'] = True
        return normalized

    if os.path.exists(RELIABILITY_SUMMARY_BACKUP):
        backup = _migrate_reliability_summary(_safe_load_json(
            RELIABILITY_SUMMARY_BACKUP,
            default=default,
            required_keys=['scripts'],
        ))
        if backup.get('scripts'):
            normalized = _normalize_reliability_summary(backup)
            normalized['corrupted'] = True
            return normalized

    return _normalize_reliability_summary(default)


def _save_reliability_summary(summary):
    """Persist summary via tmp file + os.replace for crash-safe atomic writes."""
    try:
        payload = _normalize_reliability_summary(summary)
        if os.path.exists(RELIABILITY_SUMMARY_FILE):
            try:
                shutil.copy2(RELIABILITY_SUMMARY_FILE, RELIABILITY_SUMMARY_BACKUP)
            except OSError:
                pass
        payload['updated_at'] = _iso_now()
        os.makedirs(RELIABILITY_DIR, exist_ok=True)
        with open(RELIABILITY_SUMMARY_TMP, 'w', encoding='utf-8') as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(RELIABILITY_SUMMARY_TMP, RELIABILITY_SUMMARY_FILE)
        return True
    except OSError:
        try:
            if os.path.exists(RELIABILITY_SUMMARY_TMP):
                os.remove(RELIABILITY_SUMMARY_TMP)
        except OSError:
            pass
        return False


def _normalize_duration(seconds):
    """Normalize duration to a non-negative float."""
    if seconds is None:
        return 0.0
    try:
        value = float(seconds)
    except (ValueError, TypeError):
        return 0.0
    return max(0.0, value)


def _normalize_exit_code(exit_code):
    if exit_code is None:
        return None
    try:
        return int(exit_code)
    except (ValueError, TypeError):
        return None


def _normalize_reliability_summary(summary):
    """Ensure summary schema is stable for reads and API responses."""
    if not isinstance(summary, dict):
        summary = {}
    scripts = summary.get('scripts')
    if not isinstance(scripts, dict):
        scripts = {}

    normalized_scripts = {}
    for script_name, stats in scripts.items():
        if not isinstance(stats, dict):
            continue
        total_runs = max(0, int(stats.get('total_runs', 0) or 0))
        failures = max(0, int(stats.get('failures', 0) or 0))
        if failures > total_runs:
            failures = total_runs
        reliability_score = round(
            ((total_runs - failures) / total_runs * 100) if total_runs else 0,
            1,
        )
        normalized_scripts[str(script_name)] = {
            'script_name': str(script_name),
            'total_runs': total_runs,
            'failures': failures,
            'flaky_executions': max(0, int(stats.get('flaky_executions', 0) or 0)),
            'slow_executions': max(0, int(stats.get('slow_executions', 0) or 0)),
            'average_duration': round(_normalize_duration(stats.get('average_duration')), 3),
            'reliability_score': round(float(stats.get('reliability_score', reliability_score) or 0), 1),
            'success_rate': round(float(stats.get('success_rate', reliability_score) or 0), 1),
            'trend': stats.get('trend', 'stable') if stats.get('trend') in ('improving', 'degrading', 'stable') else 'stable',
            'trend_summary': stats.get('trend_summary') if isinstance(stats.get('trend_summary'), dict) else {},
            'failure_breakdown': _cap_failure_breakdown(stats.get('failure_breakdown')),
            'duration_regression': stats.get('duration_regression') if isinstance(stats.get('duration_regression'), dict) else {},
            'flaky': stats.get('flaky') if isinstance(stats.get('flaky'), dict) else {},
            'recurring_failures': stats.get('recurring_failures') if isinstance(stats.get('recurring_failures'), list) else [],
            'last_run': str(stats.get('last_run', '') or ''),
        }

    global_stats = summary.get('global')
    if not isinstance(global_stats, dict):
        global_stats = {}

    normalized = {
        'version': RELIABILITY_SUMMARY_VERSION,
        'scripts': normalized_scripts,
        'global': {
            'total_runs': max(0, int(global_stats.get('total_runs', 0) or 0)),
            'failures': max(0, int(global_stats.get('failures', 0) or 0)),
            'reliability_score': round(float(global_stats.get('reliability_score', 0) or 0), 1),
            'failure_breakdown': _cap_failure_breakdown(global_stats.get('failure_breakdown')),
        },
        'updated_at': summary.get('updated_at', _iso_now()),
    }
    diagnostics = summary.get('diagnostics')
    if isinstance(diagnostics, dict):
        normalized['diagnostics'] = diagnostics
    return normalized


def _classify_failure(exit_code, error_message='', output=''):
    """Classify failure into one of the known failure types."""
    code = _normalize_exit_code(exit_code)
    error_msg = (error_message or '').lower()
    output_lower = (output or '').lower()
    combined = f'{error_msg} {output_lower}'

    if code == 130 or 'interrupted' in combined or 'aborted by user' in combined:
        return 'interrupted'
    if code == 124 or 'timeout' in combined or 'timed out' in combined:
        return 'timeout'
    if code == 126 or 'permission denied' in combined or 'access is denied' in combined:
        return 'permission_error'
    if (
        'no such file' in combined
        or 'file not found' in combined
        or 'cannot find the path' in combined
    ):
        return 'missing_file'
    if (
        'modulenotfound' in combined
        or 'importerror' in combined
        or 'no module named' in combined
        or 'package not found' in combined
    ):
        return 'dependency_error'
    if code == 127 and ('command not found' in combined or 'not found' in combined):
        return 'dependency_error'
    if (
        'syntax error' in combined
        or 'unexpected token' in combined
        or 'parse error' in combined
        or code in (2, 127)
    ):
        return 'shell_error'
    if code in (1, 2):
        return 'shell_error'
    return 'unknown_failure'


def _parse_execution_log_metadata(log_name):
    """Extract lightweight metadata from execution log headers."""
    if not log_name:
        return None
    log_path = os.path.join(EXECUTION_LOG_DIR, os.path.basename(log_name))
    if not os.path.isfile(log_path):
        return None

    meta = {}
    status = None
    exit_code = None
    duration_seconds = None
    try:
        with open(log_path, 'r', encoding='utf-8', errors='replace') as handle:
            for _ in range(40):
                line = handle.readline()
                if not line:
                    break
                line = line.rstrip('\n')
                if line.startswith('[') and 'status:' in line:
                    status = line.split('status:', 1)[-1].strip()
                elif line.startswith('exit_code:'):
                    exit_code = line.split(':', 1)[-1].strip()
                elif line.startswith('duration_seconds:'):
                    duration_seconds = line.split(':', 1)[-1].strip()
                elif ': ' in line and not line.startswith('['):
                    key, value = line.split(':', 1)
                    meta[key.strip()] = value.strip()
    except OSError:
        return None

    execution_id = meta.get('id')
    if not execution_id:
        return None

    success = status == 'success'
    return {
        'id': execution_id,
        'kind': meta.get('kind', 'script'),
        'display_name': meta.get('display') or meta.get('display_name', ''),
        'command': meta.get('command', ''),
        'started_at': meta.get('started_at', ''),
        'finished_at': meta.get('finished_at', ''),
        'status': status or ('success' if success else 'failed'),
        'success': success,
        'exit_code': _normalize_exit_code(exit_code),
        'duration_seconds': _normalize_duration(duration_seconds),
        'log_file': os.path.basename(log_name),
        'source': 'execution_log',
    }


def _session_record_from_file(session_name):
    """Build a reliability record from a replay/session log file."""
    safe_name = os.path.basename(session_name)
    if not safe_name.endswith('.json'):
        safe_name += '.json'
    session_path = os.path.join(SESSION_LOG_DIR, safe_name)
    if not os.path.isfile(session_path):
        return None

    try:
        with open(session_path, 'r', encoding='utf-8') as handle:
            session_data = json.load(handle)
    except (json.JSONDecodeError, OSError, TypeError, ValueError):
        _isolate_corrupted_file(session_path)
        return None

    if not isinstance(session_data, dict):
        return None

    metadata = session_data.get('metadata')
    if not isinstance(metadata, dict):
        return None

    return _sanitize_execution_record({
        'id': metadata.get('id'),
        'kind': metadata.get('kind', 'script'),
        'display_name': metadata.get('display_name', ''),
        'command': metadata.get('command', ''),
        'started_at': metadata.get('started_at', ''),
        'finished_at': metadata.get('finished_at', ''),
        'status': metadata.get('status'),
        'success': metadata.get('success', metadata.get('status') == 'success'),
        'exit_code': metadata.get('exit_code'),
        'duration_seconds': metadata.get('duration_seconds'),
        'session_file': safe_name,
        'source': 'session_log',
    })


def _collect_reliability_records(use_cache=True):
    """Merge execution records from history, session logs, and execution metadata."""
    signature = _reliability_source_signature()
    if use_cache:
        with _reliability_cache_lock:
            if (
                _reliability_cache['records'] is not None
                and _reliability_cache['records_signature'] == signature
            ):
                return list(_reliability_cache['records'])

    merged = {}

    for entry in _read_jsonl(HISTORY_FILE, max_entries=RELIABILITY_AGGREGATION_TAIL):
        record = _sanitize_execution_record(entry)
        if not record:
            continue
        record['source'] = 'history'
        merged[record['id']] = record

    if os.path.isdir(SESSION_LOG_DIR):
        try:
            session_names = sorted(
                name for name in os.listdir(SESSION_LOG_DIR)
                if name.endswith('.json') and '.corrupted' not in name
            )
        except OSError:
            session_names = []
        for session_name in session_names[-MAX_SESSION_SCAN_FOR_DIAGNOSTICS:]:
            raw_record = _session_record_from_file(session_name)
            if not raw_record:
                continue
            record = _sanitize_execution_record(raw_record)
            if record and record['id'] not in merged:
                record['source'] = 'session_log'
                merged[record['id']] = record

    for record in list(merged.values()):
        if record.get('exit_code') is not None and record.get('duration_seconds'):
            continue
        log_record = _parse_execution_log_metadata(record.get('log_file'))
        if not log_record:
            continue
        log_sanitized = _sanitize_execution_record(log_record)
        if not log_sanitized or log_sanitized['id'] != record.get('id'):
            continue
        for key in ('exit_code', 'duration_seconds', 'finished_at', 'status', 'success'):
            if record.get(key) in (None, '', 0) and log_sanitized.get(key) not in (None, ''):
                record[key] = log_sanitized[key]

    records = sorted(
        merged.values(),
        key=lambda item: item.get('finished_at', item.get('started_at', '')),
    )
    with _reliability_cache_lock:
        _reliability_cache['records'] = records
        _reliability_cache['records_signature'] = signature
    return records


def _get_reliability_records():
    """Cached accessor for aggregation paths."""
    try:
        return _collect_reliability_records(use_cache=True)
    except Exception:
        return []


def _compute_trend_summary(entries):
    """Summarize recent success/failure trend for a script."""
    if not entries:
        return {
            'direction': 'stable',
            'recent_runs': 0,
            'recent_successes': 0,
            'recent_failures': 0,
            'recent_success_rate': 0.0,
        }

    recent = entries[-RELIABILITY_TREND_WINDOW:]
    recent_successes = sum(1 for entry in recent if entry.get('success'))
    recent_failures = len(recent) - recent_successes
    recent_success_rate = round((recent_successes / len(recent) * 100), 1) if recent else 0.0

    direction = 'stable'
    if len(recent) >= RELIABILITY_TREND_WINDOW:
        if recent_successes >= RELIABILITY_TREND_WINDOW - 1:
            direction = 'improving'
        elif recent_failures >= RELIABILITY_TREND_WINDOW - 1:
            direction = 'degrading'

    return {
        'direction': direction,
        'recent_runs': len(recent),
        'recent_successes': recent_successes,
        'recent_failures': recent_failures,
        'recent_success_rate': recent_success_rate,
    }


def _count_flaky_executions(entries):
    window = entries[-RELIABILITY_FLAKY_WINDOW:] if len(entries) >= RELIABILITY_FLAKY_WINDOW else entries
    flaky = 0
    for index in range(1, len(window)):
        if bool(window[index - 1].get('success')) != bool(window[index].get('success')):
            flaky += 1
    return flaky


def _count_slow_executions(entries):
    durations = [
        _normalize_duration(entry.get('duration_seconds'))
        for entry in entries
        if _normalize_duration(entry.get('duration_seconds')) > 0
    ]
    if not durations:
        return 0, 0.0
    average = sum(durations) / len(durations)
    if len(durations) == 1:
        return (1 if durations[0] > average * 3 else 0), average
    variance = sum((value - average) ** 2 for value in durations) / len(durations)
    threshold = average + (RELIABILITY_SLOW_STDDEV * (variance ** 0.5))
    slow_count = sum(1 for value in durations if value > threshold)
    return slow_count, average


def _history_entries_for_target(display_name=None, kind=None, limit=200):
    """Reuse execution history without duplicating storage reads elsewhere."""
    entries = _get_reliability_records()
    if display_name:
        entries = [entry for entry in entries if entry.get('display_name') == display_name]
    if kind:
        entries = [entry for entry in entries if entry.get('kind') == kind]
    return entries[-limit:]


def _reliability_event_seen(execution_id):
    if not execution_id:
        return False
    for event in _read_jsonl(RELIABILITY_EVENTS_FILE)[-RELIABILITY_SYNC_EVENT_LOOKBACK:]:
        if event.get('id') == execution_id:
            return True
    return False


def _session_record_to_history_record(session_record):
    if not session_record:
        return None
    success = bool(session_record.get('success'))
    error_message = session_record.get('error', '')
    output_excerpt = session_record.get('output_excerpt', '')
    history_record = {
        'id': session_record.get('id'),
        'kind': session_record.get('kind', 'script'),
        'display_name': session_record.get('display_name', ''),
        'command': session_record.get('command', ''),
        'session_file': session_record.get('session_file', ''),
        'started_at': session_record.get('started_at', ''),
        'finished_at': session_record.get('finished_at', ''),
        'status': session_record.get('status', 'success' if success else 'failed'),
        'success': success,
        'exit_code': session_record.get('exit_code'),
        'duration_seconds': session_record.get('duration_seconds'),
        'output_excerpt': output_excerpt,
        'error': error_message,
    }
    if not success:
        history_record['failure_type'] = session_record.get('failure_type') or _classify_failure(
            session_record.get('exit_code'),
            error_message=error_message,
            output=output_excerpt,
        )
    return history_record


def _compute_duration_regression(entries):
    """Track whether recent runs are slower than the historical baseline."""
    durations = [
        _normalize_duration(entry.get('duration_seconds'))
        for entry in entries
        if _normalize_duration(entry.get('duration_seconds')) > 0
    ]
    if len(durations) < RELIABILITY_REGRESSION_RECENT + 2:
        return {
            'regressed': False,
            'baseline_avg': round(sum(durations) / len(durations), 3) if durations else 0.0,
            'recent_avg': round(sum(durations) / len(durations), 3) if durations else 0.0,
            'change_percent': 0.0,
            'sample_size': len(durations),
        }

    baseline = durations[-(RELIABILITY_REGRESSION_BASELINE + RELIABILITY_REGRESSION_RECENT):-RELIABILITY_REGRESSION_RECENT]
    recent = durations[-RELIABILITY_REGRESSION_RECENT:]
    if not baseline:
        baseline = durations[:-RELIABILITY_REGRESSION_RECENT]
    baseline_avg = sum(baseline) / len(baseline)
    recent_avg = sum(recent) / len(recent)
    change_percent = round(((recent_avg - baseline_avg) / baseline_avg * 100), 1) if baseline_avg else 0.0
    regressed = recent_avg > (baseline_avg * RELIABILITY_REGRESSION_THRESHOLD)

    return {
        'regressed': regressed,
        'baseline_avg': round(baseline_avg, 3),
        'recent_avg': round(recent_avg, 3),
        'change_percent': change_percent,
        'sample_size': len(durations),
    }


def _detect_flaky_executions(entries):
    """Detect success/failure alternation in the recent execution window."""
    window = entries[-RELIABILITY_FLAKY_WINDOW:] if len(entries) >= RELIABILITY_FLAKY_WINDOW else entries
    transitions = []
    for index in range(1, len(window)):
        prev_success = bool(window[index - 1].get('success'))
        curr_success = bool(window[index].get('success'))
        if prev_success == curr_success:
            continue
        transitions.append({
            'from_id': window[index - 1].get('id'),
            'to_id': window[index].get('id'),
            'from_success': prev_success,
            'to_success': curr_success,
            'finished_at': window[index].get('finished_at', ''),
        })
    return {
        'count': len(transitions),
        'is_flaky': len(transitions) >= 3,
        'transitions': transitions[-10:],
    }


def _failure_signature(entry):
    error_text = (entry.get('error') or entry.get('output_excerpt') or '').strip().lower()
    error_text = re.sub(r'\s+', ' ', error_text)[:120]
    failure_type = entry.get('failure_type') or _classify_failure(
        entry.get('exit_code'),
        error_message=entry.get('error', ''),
        output=entry.get('output_excerpt', ''),
    )
    if failure_type not in FAILURE_TYPES:
        failure_type = 'unknown_failure'
    return failure_type, error_text or failure_type


def _group_recurring_failures(entries, limit=15):
    """Group repeated failures by type + normalized error signature."""
    groups = {}
    for entry in entries:
        if entry.get('success'):
            continue
        failure_type, signature = _failure_signature(entry)
        group_key = f'{failure_type}|{signature}'
        group = groups.setdefault(group_key, {
            'failure_type': failure_type,
            'signature': signature,
            'count': 0,
            'scripts': set(),
            'occurrences': [],
        })
        group['count'] += 1
        group['scripts'].add(entry.get('display_name', ''))
        if len(group['occurrences']) < 5:
            group['occurrences'].append({
                'id': entry.get('id'),
                'display_name': entry.get('display_name', ''),
                'finished_at': entry.get('finished_at', ''),
                'error': (entry.get('error') or '')[:200],
            })

    grouped = []
    for group in groups.values():
        grouped.append({
            'failure_type': group['failure_type'],
            'signature': group['signature'],
            'count': group['count'],
            'scripts': sorted(name for name in group['scripts'] if name),
            'occurrences': group['occurrences'],
        })
    grouped.sort(key=lambda item: item['count'], reverse=True)
    return grouped[:limit]


def _failure_breakdown(entries):
    breakdown = {failure_type: 0 for failure_type in FAILURE_TYPES}
    for entry in entries:
        if entry.get('success'):
            continue
        failure_type = entry.get('failure_type') or _classify_failure(
            entry.get('exit_code'),
            error_message=entry.get('error', ''),
            output=entry.get('output_excerpt', ''),
        )
        if failure_type not in FAILURE_TYPES:
            failure_type = 'unknown_failure'
        breakdown[failure_type] += 1
    return _cap_failure_breakdown(breakdown)


def _compute_script_reliability(script_name, entries):
    """Aggregate reliability metrics for a script from unified records."""
    script_entries = [entry for entry in entries if entry.get('display_name') == script_name]
    if not script_entries:
        return None

    total_runs = len(script_entries)
    failures = sum(1 for entry in script_entries if not entry.get('success', False))
    flaky_executions = _count_flaky_executions(script_entries)
    flaky_details = _detect_flaky_executions(script_entries)
    slow_executions, average_duration = _count_slow_executions(script_entries)
    reliability_score = round(((total_runs - failures) / total_runs * 100), 1) if total_runs else 0.0
    trend_summary = _compute_trend_summary(script_entries)
    duration_regression = _compute_duration_regression(script_entries)
    failed_entries = [entry for entry in script_entries if not entry.get('success')]

    return {
        'script_name': script_name,
        'total_runs': total_runs,
        'failures': failures,
        'success_rate': reliability_score,
        'flaky_executions': flaky_executions,
        'flaky': flaky_details,
        'slow_executions': slow_executions,
        'average_duration': round(average_duration, 3),
        'duration_regression': duration_regression,
        'reliability_score': reliability_score,
        'last_run': script_entries[-1].get('finished_at', ''),
        'trend': trend_summary['direction'],
        'trend_summary': trend_summary,
        'failure_breakdown': _failure_breakdown(script_entries),
        'recurring_failures': _group_recurring_failures(failed_entries),
    }


def _aggregate_script_reliability(script_name):
    """Public helper used by routes — aggregates from all reliability sources."""
    records = _get_reliability_records()
    return _compute_script_reliability(script_name, records)


def _rebuild_reliability_summary():
    """Rebuild persisted summary from execution history and log sources."""
    _invalidate_reliability_cache()
    records = _get_reliability_records()
    by_script = _index_records_by_script(records)

    scripts = {}
    all_durations = []
    total_failures = 0
    global_breakdown = {failure_type: 0 for failure_type in FAILURE_TYPES}

    for script_name in sorted(by_script.keys()):
        script_entries = by_script[script_name]
        metrics = _compute_script_reliability(script_name, script_entries)
        if metrics:
            scripts[script_name] = metrics
            total_failures += metrics['failures']
            all_durations.extend([
                _normalize_duration(entry.get('duration_seconds'))
                for entry in script_entries
                if _normalize_duration(entry.get('duration_seconds')) > 0
            ])
            for failure_type, count in metrics.get('failure_breakdown', {}).items():
                global_breakdown[failure_type] = global_breakdown.get(failure_type, 0) + count

    total_runs = len(records)
    global_score = round(((total_runs - total_failures) / total_runs * 100), 1) if total_runs else 0.0
    summary = _normalize_reliability_summary({
        'scripts': scripts,
        'global': {
            'total_runs': total_runs,
            'failures': total_failures,
            'reliability_score': global_score,
            'average_duration': round(sum(all_durations) / len(all_durations), 3) if all_durations else 0.0,
            'failure_breakdown': {key: value for key, value in global_breakdown.items() if value > 0},
        },
    })
    diagnostics = _build_orchestration_diagnostics(summary=summary, refresh=True)
    summary['diagnostics'] = diagnostics
    _save_reliability_summary(summary)
    global _last_summary_save_monotonic
    _last_summary_save_monotonic = time.perf_counter()
    return summary


def _update_reliability_after_execution(history_record):
    """Lifecycle hook after script/command execution completes."""
    _record_reliability_event(history_record, persist_force=True)


def _sync_reliability_from_session_file(session_file):
    """Backfill reliability from persisted replay/session logs (idempotent)."""
    if not session_file:
        return
    session_record = _session_record_from_file(session_file)
    if not session_record or not session_record.get('finished_at'):
        return
    if _reliability_event_seen(session_record.get('id')):
        return
    history_record = _session_record_to_history_record(session_record)
    if history_record:
        _record_reliability_event(history_record)


def _record_reliability_event(history_record, persist_force=False):
    """Append execution outcome and refresh cached per-script counters."""
    sanitized = _sanitize_execution_record(history_record)
    if not sanitized:
        return
    history_record = sanitized

    event = {
        'id': history_record.get('id'),
        'display_name': history_record.get('display_name', ''),
        'kind': history_record.get('kind', ''),
        'success': bool(history_record.get('success')),
        'failure_type': history_record.get('failure_type'),
        'duration_seconds': _normalize_duration(history_record.get('duration_seconds')),
        'finished_at': history_record.get('finished_at', _iso_now()),
    }
    _append_jsonl(RELIABILITY_EVENTS_FILE, event)
    _trim_jsonl(RELIABILITY_EVENTS_FILE, MAX_RELIABILITY_EVENTS)

    summary = _load_reliability_summary()
    script_name = history_record.get('display_name') or '_unknown'
    script_stats = summary['scripts'].setdefault(script_name, {
        'script_name': script_name,
        'total_runs': 0,
        'failures': 0,
        'flaky_executions': 0,
        'slow_executions': 0,
        'average_duration': 0.0,
        'reliability_score': 100.0,
        'success_rate': 100.0,
        'trend': 'stable',
        'trend_summary': {},
        'failure_breakdown': {},
        'last_run': '',
    })

    script_stats['total_runs'] += 1
    if not history_record.get('success'):
        script_stats['failures'] += 1
        failure_type = history_record.get('failure_type', 'unknown_failure')
        breakdown = _cap_failure_breakdown(script_stats.setdefault('failure_breakdown', {}))
        if failure_type not in FAILURE_TYPES:
            failure_type = 'unknown_failure'
        breakdown[failure_type] = breakdown.get(failure_type, 0) + 1
        script_stats['failure_breakdown'] = _cap_failure_breakdown(breakdown)

    duration = _normalize_duration(history_record.get('duration_seconds'))
    if duration > 0:
        previous_avg = _normalize_duration(script_stats.get('average_duration'))
        previous_count = max(0, script_stats['total_runs'] - 1)
        script_stats['average_duration'] = round(
            ((previous_avg * previous_count) + duration) / script_stats['total_runs'],
            3,
        )
        if previous_avg > 0 and duration > previous_avg * 2:
            script_stats['slow_executions'] = script_stats.get('slow_executions', 0) + 1

    script_stats['last_run'] = history_record.get('finished_at', '')
    script_stats['reliability_score'] = round(
        ((script_stats['total_runs'] - script_stats['failures']) / script_stats['total_runs'] * 100)
        if script_stats['total_runs'] else 0,
        1,
    )
    script_stats['success_rate'] = script_stats['reliability_score']

    global_stats = summary.setdefault('global', {})
    global_stats['total_runs'] = global_stats.get('total_runs', 0) + 1
    if not history_record.get('success'):
        global_stats['failures'] = global_stats.get('failures', 0) + 1
    global_stats['reliability_score'] = round(
        ((global_stats['total_runs'] - global_stats.get('failures', 0)) / global_stats['total_runs'] * 100)
        if global_stats.get('total_runs') else 0,
        1,
    )

    _maybe_save_reliability_summary(summary, force=persist_force)


def _build_reliability_failures_payload(script_name=None, limit=100):
    """Failures view backed by failed history + recurring groups."""
    failed_entries = _read_jsonl(FAILED_HISTORY_FILE)
    if script_name:
        failed_entries = [entry for entry in failed_entries if entry.get('display_name') == script_name]
    recent_failed = failed_entries[-limit:]

    failures_by_type = {}
    for entry in recent_failed:
        failure_type = entry.get('failure_type') or _classify_failure(
            entry.get('exit_code'),
            error_message=entry.get('error', ''),
            output=entry.get('output_excerpt', ''),
        )
        if failure_type not in FAILURE_TYPES:
            failure_type = 'unknown_failure'
        failures_by_type.setdefault(failure_type, []).append({
            'id': entry.get('id'),
            'display_name': entry.get('display_name', ''),
            'kind': entry.get('kind', ''),
            'finished_at': entry.get('finished_at', ''),
            'error': (entry.get('error') or '')[:200],
            'session_file': entry.get('session_file', ''),
        })

    history_failed = [
        entry for entry in _history_entries_for_target(display_name=script_name, limit=500)
        if not entry.get('success')
    ]

    return {
        'script': script_name,
        'total_failures': len(failed_entries),
        'recent_count': len(recent_failed),
        'failures_by_type': failures_by_type,
        'failure_breakdown': _cap_failure_breakdown(_failure_breakdown(history_failed)),
        'recurring_failures': _group_recurring_failures(history_failed),
        'failure_types': FAILURE_TYPES,
    }


def _build_reliability_trends_payload(script_name=None):
    """Trend, flaky, and duration regression data for frontend charts."""
    records = _collect_reliability_records()
    if script_name:
        script_entries = [entry for entry in records if entry.get('display_name') == script_name]
        if not script_entries:
            return None
        return {
            'script': script_name,
            'trend': _compute_trend_summary(script_entries),
            'flaky': _detect_flaky_executions(script_entries),
            'duration_regression': _compute_duration_regression(script_entries),
            'recent_runs': [
                {
                    'id': entry.get('id'),
                    'success': bool(entry.get('success')),
                    'duration_seconds': _normalize_duration(entry.get('duration_seconds')),
                    'finished_at': entry.get('finished_at', ''),
                }
                for entry in script_entries[-RELIABILITY_TREND_WINDOW:]
            ],
        }

    scripts = {}
    script_names = sorted({
        record.get('display_name')
        for record in records
        if record.get('display_name')
    })
    for name in script_names:
        script_entries = [entry for entry in records if entry.get('display_name') == name]
        scripts[name] = {
            'trend': _compute_trend_summary(script_entries),
            'flaky': _detect_flaky_executions(script_entries),
            'duration_regression': _compute_duration_regression(script_entries),
        }

    all_failed = [entry for entry in records if not entry.get('success')]
    return {
        'global_trend': _compute_trend_summary(records),
        'global_duration_regression': _compute_duration_regression(records),
        'scripts': scripts,
        'top_recurring_failures': _group_recurring_failures(all_failed, limit=10),
    }


# ─── Replay / workspace orchestration diagnostics (read-only, reuses log metadata) ──

def _scan_corrupted_artifacts():
    """List isolated .corrupted files under existing log/workspace stores."""
    scopes = (
        (SESSION_LOG_DIR, 'session'),
        (RELIABILITY_DIR, 'reliability'),
        (WORKSPACE_DIR, 'workspace'),
    )
    artifacts = []
    for root, label in scopes:
        if not os.path.isdir(root):
            continue
        try:
            names = os.listdir(root)
        except OSError:
            continue
        for name in sorted(names):
            if '.corrupted' not in name:
                continue
            artifacts.append({
                'scope': label,
                'file': name,
            })
    return artifacts


def _analyze_session_instability(session_data):
    """Score replay/session log instability from existing event metadata."""
    metadata = session_data.get('metadata', {}) if isinstance(session_data, dict) else {}
    events = session_data.get('events', []) if isinstance(session_data, dict) else []
    reasons = []
    score = 0

    if not events:
        reasons.append('empty_event_log')
        score += 30
    if not metadata.get('finished_at'):
        reasons.append('incomplete_session')
        score += 25
    if metadata.get('success') is False or metadata.get('status') == 'failed':
        reasons.append('failed_execution')
        score += 20

    error_events = [event for event in events if event.get('stream') == 'error']
    if events and len(error_events) / len(events) > 0.15:
        reasons.append('high_error_output_ratio')
        score += 15

    combined_output = ' '.join(
        (event.get('content') or '').lower()
        for event in events[:80]
    )
    if 'abort' in combined_output or 'timeout' in combined_output or 'interrupted' in combined_output:
        reasons.append('abort_or_timeout_in_replay')
        score += 12

    if len(events) >= 4:
        flips = 0
        for index in range(1, min(len(events), RELIABILITY_FLAKY_WINDOW)):
            prev_err = events[index - 1].get('stream') == 'error'
            curr_err = events[index].get('stream') == 'error'
            if prev_err != curr_err:
                flips += 1
        if flips >= 4:
            reasons.append('unstable_output_alternation')
            score += 10

    return {
        'instability_score': min(100, score),
        'is_unstable': score >= 25,
        'reasons': reasons,
        'error_events': len(error_events),
        'total_events': len(events),
    }


def _reliability_link_for_record(record, summary=None):
    """Link a history/session record to cached reliability summary stats."""
    if not record:
        return {}
    if summary is None:
        summary = _load_reliability_summary()
    script_name = record.get('display_name', '')
    stats = summary.get('scripts', {}).get(script_name, {})
    return {
        'execution_id': record.get('id'),
        'script_name': script_name,
        'session_file': record.get('session_file', ''),
        'reliability_score': stats.get('reliability_score'),
        'success_rate': stats.get('success_rate'),
        'flaky_executions': stats.get('flaky_executions', 0),
        'trend': stats.get('trend', 'stable'),
        'failure_breakdown': stats.get('failure_breakdown', {}),
    }


def _diagnose_session_data(session_data, summary=None):
    """Per-session diagnostics for replay UI and reliability linking."""
    record = None
    if isinstance(session_data, dict):
        metadata = session_data.get('metadata', {})
        if metadata.get('id'):
            record = {
                'id': metadata.get('id'),
                'display_name': metadata.get('display_name', ''),
                'session_file': metadata.get('session_file', ''),
                'success': metadata.get('success'),
                'status': metadata.get('status'),
            }
    instability = _analyze_session_instability(session_data)
    return {
        'instability': instability,
        'reliability_link': _reliability_link_for_record(record, summary=summary),
        'warnings': _session_diagnostic_warnings(session_data, instability),
    }


def _session_diagnostic_warnings(session_data, instability):
    warnings = []
    if instability.get('is_unstable'):
        warnings.append('Replay session shows execution instability.')
    metadata = session_data.get('metadata', {}) if isinstance(session_data, dict) else {}
    if not metadata.get('finished_at'):
        warnings.append('Session metadata is incomplete; replay may be partial.')
    return warnings


def _build_workspace_diagnostics(workspace_payload=None):
    """Workspace orchestration health from existing workspace_state.json metadata."""
    workspace_payload = workspace_payload if workspace_payload is not None else load_workspace_state()
    warnings = []
    indicators = {
        'workspace_ok': True,
        'snapshot_corrupted': False,
        'replay_active_in_snapshot': False,
    }

    if not workspace_payload:
        return {
            'warnings': ['No workspace snapshot persisted yet.'],
            'indicators': indicators,
            'saved_at': None,
        }

    if workspace_payload.get('corrupted'):
        indicators['workspace_ok'] = False
        indicators['snapshot_corrupted'] = True
        warnings.append(
            f'Workspace snapshot is corrupted and was isolated ({workspace_payload.get("error", "unknown")}).',
        )
        return {
            'warnings': warnings,
            'indicators': indicators,
            'saved_at': workspace_payload.get('saved_at'),
            'error': workspace_payload.get('error'),
        }

    snapshot = workspace_payload.get('workspace', workspace_payload)
    if isinstance(snapshot, dict) and snapshot.get('replayState', {}).get('active'):
        indicators['replay_active_in_snapshot'] = True
        warnings.append('Last workspace snapshot had an active replay session.')

    profile_corruption = [
        name for name in os.listdir(WORKSPACE_PROFILE_DIR)
        if os.path.isfile(os.path.join(WORKSPACE_PROFILE_DIR, name)) and '.corrupted' in name
    ] if os.path.isdir(WORKSPACE_PROFILE_DIR) else []
    if profile_corruption:
        indicators['workspace_ok'] = False
        warnings.append(f'{len(profile_corruption)} corrupted workspace profile file(s) detected.')

    return {
        'warnings': warnings,
        'indicators': indicators,
        'saved_at': workspace_payload.get('saved_at'),
        'version': workspace_payload.get('version'),
        'profile_corruption_count': len(profile_corruption),
    }


def _build_replay_diagnostics(summary=None):
    """Replay/session instability linked to reliability summaries (no extra storage)."""
    summary = summary if summary is not None else _load_reliability_summary()
    history_ids = {
        entry.get('id')
        for entry in _get_reliability_records()
        if entry.get('id')
    }

    unstable_sessions = []
    failed_sessions = []
    orphan_sessions = []
    unstable_by_id = {}
    session_by_file = {}

    if os.path.isdir(SESSION_LOG_DIR):
        try:
            session_names = sorted(
                name for name in os.listdir(SESSION_LOG_DIR)
                if name.endswith('.json') and '.corrupted' not in name
            )
        except OSError:
            session_names = []
        for session_name in session_names[-MAX_SESSION_SCAN_FOR_DIAGNOSTICS:]:
            record = _session_record_from_file(session_name)
            if not record:
                continue

            try:
                with open(os.path.join(SESSION_LOG_DIR, session_name), 'r', encoding='utf-8') as handle:
                    session_data = json.load(handle)
            except (json.JSONDecodeError, OSError):
                unstable_sessions.append({
                    'session_file': session_name,
                    'id': record.get('id'),
                    'display_name': record.get('display_name', ''),
                    'is_unstable': True,
                    'instability_score': 100,
                    'reasons': ['corrupted_session_file'],
                    'reliability_link': _reliability_link_for_record(record, summary=summary),
                })
                continue

            instability = _analyze_session_instability(session_data)
            link = _reliability_link_for_record(record, summary=summary)
            payload = {
                'session_file': session_name,
                'id': record.get('id'),
                'display_name': record.get('display_name', ''),
                'is_unstable': instability['is_unstable'],
                'instability_score': instability['instability_score'],
                'reasons': instability['reasons'],
                'reliability_link': link,
                'success': record.get('success'),
            }
            session_by_file[session_name] = payload
            if record.get('id'):
                unstable_by_id[record.get('id')] = payload

            if not record.get('success'):
                failed_sessions.append(payload)
            if instability['is_unstable']:
                unstable_sessions.append(payload)
            if record.get('id') and record.get('id') not in history_ids:
                orphan_sessions.append(payload)

    unstable_sessions.sort(key=lambda item: item.get('instability_score', 0), reverse=True)

    return {
        'total_sessions': len(session_by_file),
        'unstable_sessions': unstable_sessions[:25],
        'failed_sessions': failed_sessions[:25],
        'orphan_sessions': orphan_sessions[:15],
        'unstable_by_id': unstable_by_id,
        'session_by_file': session_by_file,
        'indicators': {
            'replay_stable': len(unstable_sessions) == 0,
            'has_failed_sessions': len(failed_sessions) > 0,
            'has_orphan_sessions': len(orphan_sessions) > 0,
        },
    }


def _compute_orchestration_severity(corrupted, workspace_diag, replay_diag, summary):
    """Derive global orchestration health: ok | warning | critical."""
    score = 0
    if corrupted:
        score += 40
    if workspace_diag.get('indicators', {}).get('snapshot_corrupted'):
        score += 50
    elif not workspace_diag.get('indicators', {}).get('workspace_ok', True):
        score += 20

    unstable_count = len(replay_diag.get('unstable_sessions', []))
    if unstable_count >= 5:
        score += 30
    elif unstable_count >= 1:
        score += 15
    if not replay_diag.get('indicators', {}).get('replay_stable'):
        score += 10
    if replay_diag.get('indicators', {}).get('has_orphan_sessions'):
        score += 8

    global_stats = summary.get('global', {}) if isinstance(summary, dict) else {}
    failures = int(global_stats.get('failures', 0) or 0)
    if failures >= 10:
        score += 15
    elif failures >= 3:
        score += 8

    reliability_score = float(global_stats.get('reliability_score', 100) or 100)
    if reliability_score < 50:
        score += 20
    elif reliability_score < 80:
        score += 10

    if score >= 50:
        return 'critical'
    if score >= 20:
        return 'warning'
    return 'ok'


def _diagnostics_staleness(summary_updated_at, diagnostics_updated_at):
    """Compare diagnostic compute time vs summary cache freshness."""
    try:
        summary_dt = datetime.fromisoformat(str(summary_updated_at).replace('Z', '+00:00'))
        diag_dt = datetime.fromisoformat(str(diagnostics_updated_at).replace('Z', '+00:00'))
        age_seconds = max(0, int((datetime.now(timezone.utc) - diag_dt).total_seconds()))
        drift_seconds = abs(int((diag_dt - summary_dt).total_seconds()))
        is_stale = age_seconds > RELIABILITY_DIAGNOSTICS_TTL_SEC or drift_seconds > RELIABILITY_DIAGNOSTICS_TTL_SEC
        return {
            'summary_updated_at': summary_updated_at,
            'diagnostics_updated_at': diagnostics_updated_at,
            'age_seconds': age_seconds,
            'summary_drift_seconds': drift_seconds,
            'is_stale': is_stale,
        }
    except (ValueError, TypeError):
        return {
            'summary_updated_at': summary_updated_at,
            'diagnostics_updated_at': diagnostics_updated_at,
            'age_seconds': None,
            'summary_drift_seconds': None,
            'is_stale': True,
        }


def _build_orchestration_diagnostics(summary=None, refresh=False):
    """Unified replay/workspace/reliability orchestration diagnostics."""
    summary = summary if summary is not None else _load_reliability_summary()
    signature = (_reliability_source_signature(), summary.get('updated_at'))
    if not refresh:
        with _reliability_cache_lock:
            if (
                _reliability_cache['diagnostics'] is not None
                and _reliability_cache['diagnostics_signature'] == signature
            ):
                return dict(_reliability_cache['diagnostics'])

    try:
        corrupted = _scan_corrupted_artifacts()
        workspace_diag = _build_workspace_diagnostics()
        workspace_diag['source'] = 'workspace'
        replay_diag = _build_replay_diagnostics(summary=summary)
        replay_diag['source'] = 'replay'
    except Exception as exc:
        return {
            'severity': 'critical',
            'diagnostics_updated_at': _iso_now(),
            'sources': dict(RELIABILITY_DIAGNOSTIC_SOURCES),
            'warnings': [f'Diagnostics computation failed: {exc}'],
            'corrupted_artifacts': [],
            'workspace': {'source': 'workspace', 'warnings': [], 'indicators': {'workspace_ok': False}},
            'replay': {'source': 'replay', 'indicators': {'replay_stable': False}},
            'indicators': {
                'has_corruption': True,
                'workspace_ok': False,
                'replay_stable': False,
            },
            'staleness': {'is_stale': True},
        }

    warnings = list(workspace_diag.get('warnings', []))
    if corrupted:
        warnings.append(f'{len(corrupted)} corrupted artifact(s) isolated on disk.')
    if not replay_diag['indicators'].get('replay_stable'):
        warnings.append(
            f'{len(replay_diag.get("unstable_sessions", []))} replay session(s) show instability.',
        )
    if replay_diag['indicators'].get('has_orphan_sessions'):
        warnings.append('Some session logs are not linked to execution history.')

    diagnostics_updated_at = _iso_now()
    severity = _compute_orchestration_severity(corrupted, workspace_diag, replay_diag, summary)
    payload = {
        'severity': severity,
        'diagnostics_updated_at': diagnostics_updated_at,
        'sources': dict(RELIABILITY_DIAGNOSTIC_SOURCES),
        'source': 'orchestration',
        'corrupted_artifacts': corrupted,
        'workspace': workspace_diag,
        'replay': replay_diag,
        'warnings': warnings,
        'indicators': {
            'has_corruption': bool(corrupted) or workspace_diag.get('indicators', {}).get('snapshot_corrupted'),
            'workspace_ok': workspace_diag.get('indicators', {}).get('workspace_ok', True),
            'replay_stable': replay_diag.get('indicators', {}).get('replay_stable', True),
            'orchestration_health': severity,
        },
        'staleness': _diagnostics_staleness(summary.get('updated_at'), diagnostics_updated_at),
    }
    with _reliability_cache_lock:
        _reliability_cache['diagnostics'] = payload
        _reliability_cache['diagnostics_signature'] = signature
    return payload


def _get_orchestration_diagnostics(summary=None, refresh=False):
    try:
        return _build_orchestration_diagnostics(summary=summary, refresh=refresh)
    except Exception:
        return {
            'severity': 'warning',
            'diagnostics_updated_at': _iso_now(),
            'sources': dict(RELIABILITY_DIAGNOSTIC_SOURCES),
            'warnings': ['Diagnostics unavailable.'],
            'indicators': {'orchestration_health': 'warning'},
            'staleness': {'is_stale': True},
        }


def _reliability_api_response(success=True, data=None, error=None, status=200):
    """Consistent vanilla-JS friendly API envelope."""
    payload = {'success': success}
    if data is not None:
        payload['data'] = data
    if error:
        payload['error'] = error
    return jsonify(payload), status


def _generate_recommendations(reliability):
    """Generate lightweight actionable recommendations."""
    recommendations = []
    if reliability is None:
        return recommendations

    success_rate = reliability.get('success_rate', reliability.get('reliability_score', 0))
    if success_rate < 50:
        recommendations.append({
            'type': 'high_failure_rate',
            'priority': 'critical',
            'message': (
                f'Script has {100 - success_rate:.1f}% failure rate. '
                'Review error logs and dependencies.'
            ),
        })
    elif success_rate < 80:
        recommendations.append({
            'type': 'moderate_failure_rate',
            'priority': 'high',
            'message': f'Script reliability is {success_rate:.1f}%. Investigate recent failures.',
        })

    dominant_failure = None
    breakdown = reliability.get('failure_breakdown', {})
    if breakdown:
        dominant_failure = max(breakdown, key=breakdown.get)
        recommendations.append({
            'type': 'dominant_failure',
            'priority': 'high',
            'message': (
                f'Most common failure is {dominant_failure} '
                f'({FAILURE_TYPES.get(dominant_failure, dominant_failure)}).'
            ),
        })

    if reliability.get('flaky_executions', 0) > 3:
        recommendations.append({
            'type': 'flaky_execution',
            'priority': 'high',
            'message': 'Script shows flaky behavior. Consider retries or stabilizing dependencies.',
        })

    if reliability.get('slow_executions', 0) > 2:
        avg_duration = reliability.get('average_duration', 0)
        recommendations.append({
            'type': 'performance_issue',
            'priority': 'medium',
            'message': f'Script is slow ({avg_duration:.1f}s avg). Optimize hot paths or IO.',
        })

    duration_regression = reliability.get('duration_regression', {})
    if duration_regression.get('regressed'):
        recommendations.append({
            'type': 'duration_regression',
            'priority': 'medium',
            'message': (
                f'Run duration regressed {duration_regression.get("change_percent", 0):.1f}% '
                f'(recent {duration_regression.get("recent_avg", 0):.1f}s vs '
                f'baseline {duration_regression.get("baseline_avg", 0):.1f}s).'
            ),
        })

    trend = reliability.get('trend', 'stable')
    if trend == 'degrading':
        recommendations.append({
            'type': 'degrading_trend',
            'priority': 'high',
            'message': 'Script reliability is declining. Review recent changes and failures.',
        })
    elif trend == 'improving':
        recommendations.append({
            'type': 'improving_trend',
            'priority': 'info',
            'message': 'Script reliability is improving.',
        })

    return recommendations


def _build_reliability_dashboard(refresh=False):
    """Build dashboard from cached summary (refresh only when requested)."""
    summary = _rebuild_reliability_summary() if refresh else _load_reliability_summary()
    records = _get_reliability_records()
    diagnostics = _get_orchestration_diagnostics(summary=summary, refresh=refresh)

    if not records:
        return {
            'summary': {
                'total_executions': 0,
                'total_failures': 0,
                'global_reliability': 0,
                'avg_duration': 0,
                'script_count': 0,
                'failure_breakdown': {},
            },
            'scripts': {},
            'recommendations': [],
            'failure_types': FAILURE_TYPES,
            'updated_at': _iso_now(),
            'orchestration': {
                'severity': diagnostics.get('severity', 'ok'),
                'diagnostics_updated_at': diagnostics.get('diagnostics_updated_at'),
                'staleness': diagnostics.get('staleness', {}),
            },
        }

    scripts_data = summary.get('scripts', {})
    total_runs = len(records)
    total_failures = sum(1 for record in records if not record.get('success'))
    durations = [
        _normalize_duration(record.get('duration_seconds'))
        for record in records
        if _normalize_duration(record.get('duration_seconds')) > 0
    ]

    all_recommendations = []
    for script_name, reliability in sorted(
        scripts_data.items(),
        key=lambda item: item[1].get('reliability_score', 0),
    ):
        for recommendation in _generate_recommendations(reliability):
            recommendation['script'] = script_name
            all_recommendations.append(recommendation)

    priority_map = {'critical': 0, 'high': 1, 'medium': 2, 'info': 3}
    all_recommendations.sort(
        key=lambda item: (priority_map.get(item.get('priority'), 4), item.get('type', '')),
    )

    return {
        'summary': {
            'total_executions': total_runs,
            'total_failures': total_failures,
            'global_reliability': summary.get('global', {}).get('reliability_score', 0),
            'avg_duration': summary.get('global', {}).get('average_duration', 0),
            'script_count': len(scripts_data),
            'failure_breakdown': summary.get('global', {}).get('failure_breakdown', {}),
        },
        'scripts': scripts_data,
        'recommendations': all_recommendations[:10],
        'failure_types': FAILURE_TYPES,
        'updated_at': summary.get('updated_at', _iso_now()),
        'orchestration': {
            'severity': diagnostics.get('severity', 'ok'),
            'diagnostics_updated_at': diagnostics.get('diagnostics_updated_at'),
            'staleness': diagnostics.get('staleness', {}),
        },
    }


_ensure_log_dirs()
_cleanup_old_execution_logs()


def load_favorites():
    if os.path.exists(FAVORITES_FILE):
        with open(FAVORITES_FILE, "r") as f:
            return json.load(f)
    return []


def save_favorites(favs):
    with open(FAVORITES_FILE, "w") as f:
        json.dump(favs, f)


def load_locks():
    if os.path.exists(LOCKS_FILE):
        with open(LOCKS_FILE, "r") as f:
            return json.load(f)
    return {}


def save_locks(locks):
    with open(LOCKS_FILE, "w") as f:
        json.dump(locks, f)


def load_sessions():
    if os.path.exists(SESSIONS_FILE):
        with open(SESSIONS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_sessions(sessions):
    with open(SESSIONS_FILE, "w", encoding="utf-8") as f:
        json.dump(sessions, f, indent=2)


def is_legacy_hash(data: any) -> bool:
    """Check if the stored lock data is a legacy SHA-256 string."""
    return isinstance(data, str)


def generate_password_hash(password: str) -> dict:
    """Generate a secure PBKDF2-HMAC-SHA256 hash dictionary for a password with a random salt."""
    if not isinstance(password, str):
        raise TypeError("Password must be a string")
    
    salt_bytes = secrets.token_bytes(16)
    salt_hex = salt_bytes.hex()
    
    hash_bytes = hashlib.pbkdf2_hmac(
        'sha256',
        password.encode('utf-8'),
        salt_bytes,
        PBKDF2_ITERATIONS
    )
    hash_hex = hash_bytes.hex()
    
    return {
        "salt": salt_hex,
        "hash": hash_hex,
        "iterations": PBKDF2_ITERATIONS
    }


def verify_password(password: str, stored_data: dict) -> bool:
    """Verify a password against stored PBKDF2 metadata safely, with exception handling."""
    if not isinstance(password, str):
        return False
    if not isinstance(stored_data, dict):
        return False
    
    try:
        salt_hex = stored_data.get("salt")
        hash_hex = stored_data.get("hash")
        iterations = stored_data.get("iterations")
        
        if not salt_hex or not isinstance(salt_hex, str):
            return False
        if not hash_hex or not isinstance(hash_hex, str):
            return False
        if iterations is None or not isinstance(iterations, int) or iterations <= 0:
            return False
            
        try:
            salt_bytes = bytes.fromhex(salt_hex)
            hash_bytes = bytes.fromhex(hash_hex)
        except (ValueError, binascii.Error, TypeError):
            return False
            
        calculated_hash = hashlib.pbkdf2_hmac(
            'sha256',
            password.encode('utf-8'),
            salt_bytes,
            iterations
        )
        
        return hmac.compare_digest(calculated_hash, hash_bytes)
    except Exception:
        return False


def check_lock(rel_path: str, provided_pass: str) -> bool:
    """Check if a script is locked and if the provided password matches."""
    locks = load_locks()
    if rel_path in locks:
        if not provided_pass:
            return False
            
        stored_data = locks[rel_path]
        
        if is_legacy_hash(stored_data):
            legacy_hash = hashlib.sha256(provided_pass.encode('utf-8')).hexdigest()
            if hmac.compare_digest(legacy_hash, stored_data):
                try:
                    new_hash = generate_password_hash(provided_pass)
                    locks[rel_path] = new_hash
                    save_locks(locks)
                except Exception:  # nosec B110
                    pass
                return True
            return False
        elif isinstance(stored_data, dict):
            return verify_password(provided_pass, stored_data)
        else:
            return False
            
    return True


def parse_script_metadata(filepath):
    """Parse metadata from script comment headers."""
    metadata = {
        "name": os.path.basename(filepath).replace(".sh", "").replace("_", " ").title(),
        "desc": "",
        "tag": "",
        "path": filepath,
    }
    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if line.startswith("# name:"):
                    metadata["name"] = line[7:].strip()
                elif line.startswith("# desc:"):
                    metadata["desc"] = line[7:].strip()
                elif line.startswith("# tag:"):
                    metadata["tag"] = line[6:].strip()
                elif not line.startswith("#") and line:
                    break
    except Exception:  # nosec B110
        pass
    return metadata


def get_all_scripts():
    """Walk scripts directory and return all scripts grouped by category."""
    categories = {}
    favorites = load_favorites()
    locks = load_locks()

    if not os.path.exists(SCRIPTS_DIR):
        os.makedirs(SCRIPTS_DIR)
        return categories

    for category in sorted(os.listdir(SCRIPTS_DIR)):
        cat_path = os.path.join(SCRIPTS_DIR, category)
        if os.path.isdir(cat_path):
            scripts = []
            for script_file in sorted(os.listdir(cat_path)):
                if script_file.endswith(".sh"):
                    full_path = os.path.join(cat_path, script_file)
                    rel_path = f"{category}/{script_file}"
                    meta = parse_script_metadata(full_path)
                    meta["file"] = script_file
                    # Ensure a display name exists; fall back to filename when metadata is missing
                    if not meta.get("name"):
                        meta["name"] = script_file
                    meta["category"] = category
                    meta["relative_path"] = rel_path
                    meta["favorite"] = rel_path in favorites
                    meta["locked"] = rel_path in locks
                    scripts.append(meta)
            if scripts:
                categories[category] = scripts

    return categories

# ─── Security Enhancements ──────────────────────────────────────────

@app.before_request
def enforce_security():
    from flask import abort
    from urllib.parse import urlparse

    # 1. Host Validation (prevents DNS Rebinding)
    host_only = request.host.split(':')[0]
    if host_only not in ('127.0.0.1', 'localhost'):
        abort(403)

    # 2. Origin/Referer Validation (prevents CSRF)
    if request.method in ['POST', 'PUT', 'DELETE', 'PATCH']:
        origin = request.headers.get('Origin')
        referer = request.headers.get('Referer')
        
        def is_valid_local(url):
            try:
                parsed = urlparse(url)
                return parsed.hostname in ('127.0.0.1', 'localhost')
            except Exception:
                return False

        if origin:
            if not is_valid_local(origin):
                abort(403)
        elif referer:
            if not is_valid_local(referer):
                abort(403)
        else:
            # Reject if neither is present and request is from a browser
            user_agent = request.headers.get('User-Agent', '')
            if any(b in user_agent for b in ['Mozilla', 'Chrome', 'Safari', 'Edge']):
                abort(403)

# ─── Routes ───────────────────────────────────────────────────────


@app.route("/")
def index():
    return send_from_directory("ui", "index.html")


@app.route("/api/scripts")
def list_scripts():
    return jsonify(get_all_scripts())


@app.route("/api/history")
def get_history():
    query = request.args.get("q", "")
    status = request.args.get("status", "all")
    kind = request.args.get("kind", "all")
    limit = request.args.get("limit", 200, type=int)
    limit = max(1, min(limit or 200, 500))

    entries = _load_history_entries(query=query, status=status, kind=kind, limit=limit)
    return jsonify(
        {
            "entries": entries,
            "summary": _history_summary(),
            "query": {
                "q": query,
                "status": status,
                "kind": kind,
                "limit": limit,
            },
        }
    )


@app.route("/api/command_history")
def get_command_history():
    return jsonify({"success": True, "history": load_command_history()})


@app.route("/api/command_history/clear", methods=["POST"])
def clear_command_history():
    try:
        # Overwrite the history JSON file with an empty array
        with open(COMMAND_HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump([], f, indent=2)

        return jsonify(
            {"success": True, "message": "Command history cleared successfully"}
        )
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/history/clear', methods=['POST'])
def clear_history():
    try:
        with open(HISTORY_FILE, 'w', encoding='utf-8') as f:
            pass
        with open(FAILED_HISTORY_FILE, 'w', encoding='utf-8') as f:
            pass
        return jsonify({
            'success': True,
            'message': 'Execution history cleared successfully'
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@app.route("/api/history/analytics")
def history_analytics():
    entries = _load_history_entries(limit=1000)

    total = len(entries)

    successful = sum(1 for e in entries if e.get("success"))

    failed = total - successful

    avg_duration = (
        round(sum(e.get("duration_seconds", 0) for e in entries) / total, 2)
        if total
        else 0
    )

    script_counts = {}

    for entry in entries:
        name = entry.get("display_name", "Unknown")
        script_counts[name] = script_counts.get(name, 0) + 1

    top_scripts = sorted(script_counts.items(), key=lambda x: x[1], reverse=True)[:5]

    slowest = sorted(entries, key=lambda e: e.get("duration_seconds", 0), reverse=True)[
        :5
    ]

    recent_failures = [e for e in entries if not e.get("success")][:5]

    return jsonify(
        {
            "success": True,
            "summary": {
                "total": total,
                "successful": successful,
                "failed": failed,
                "avg_duration": avg_duration,
            },
            "top_scripts": top_scripts,
            "slowest": slowest,
            "recent_failures": recent_failures,
        }
    )


@app.route("/api/history/export")
def export_history():
    query = request.args.get("q", "")
    status = request.args.get("status", "all")
    kind = request.args.get("kind", "all")
    export_format = request.args.get("format", "log").lower()
    entries = _load_history_entries(query=query, status=status, kind=kind, limit=500)

    lines = [
        "DevShell Execution History Export",
        f"Generated: {_iso_now()}",
        f'Filter: q={query or "*"} status={status} kind={kind}',
        "",
    ]

    if not entries:
        lines.append("No matching history entries found.")
    else:
        for entry in entries:
            lines.extend(
                [
                    f'[{entry.get("started_at", "")}] {entry.get("status", "unknown").upper()} {entry.get("kind", "execution").upper()} #{entry.get("id", "")}',
                    f'Command: {entry.get("command", "")}',
                    f'Display: {entry.get("display_name", "")}',
                    f'Exit Code: {entry.get("exit_code", "")}',
                    f'Duration: {entry.get("duration", "")}',
                    f'Log: {entry.get("log_file", "")}',
                ]
            )
            excerpt = entry.get("output_excerpt", "").strip()
            if excerpt:
                lines.append("Output:")
                lines.extend(f"  {line}" for line in excerpt.splitlines())
            error = entry.get("error", "").strip()
            if error:
                lines.append(f"Error: {error}")
            lines.append("")

    export_text = "\n".join(lines).rstrip() + "\n"
    filename = f'devshell-history-{_slugify(status + "-" + kind)}.{"txt" if export_format == "txt" else "log"}'
    return Response(
        export_text,
        mimetype="text/plain; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )


# ─── Reliability Intelligence Routes ───────────────────────────────

@app.route('/api/reliability/dashboard')
def get_reliability_dashboard():
    """Get comprehensive reliability dashboard."""
    try:
        refresh = request.args.get('refresh', '').lower() in ('1', 'true', 'yes')
        dashboard = _build_reliability_dashboard(refresh=refresh)
        return jsonify({
            'success': True,
            'data': dashboard,
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e),
        }), 500


@app.route('/api/reliability/summary')
def get_reliability_summary():
    """Get cached reliability summary (optional ?refresh=1 to rebuild)."""
    try:
        refresh = request.args.get('refresh', '').lower() in ('1', 'true', 'yes')
        summary = _rebuild_reliability_summary() if refresh else _load_reliability_summary()
        diagnostics = _get_orchestration_diagnostics(summary=summary, refresh=refresh)
        if refresh:
            summary = _load_reliability_summary()
        return _reliability_api_response(data={
            'version': summary.get('version', RELIABILITY_SUMMARY_VERSION),
            'updated_at': summary.get('updated_at'),
            'global': summary.get('global', {}),
            'scripts': summary.get('scripts', {}),
            'failure_types': FAILURE_TYPES,
            'diagnostics': diagnostics,
            'severity': diagnostics.get('severity', 'ok'),
            'diagnostics_updated_at': diagnostics.get('diagnostics_updated_at'),
            'sources': diagnostics.get('sources', {}),
            'staleness': diagnostics.get('staleness', {}),
            'generated_at': _iso_now(),
        })
    except Exception as e:
        return _reliability_api_response(success=False, error=str(e), status=500)


@app.route('/api/reliability/script/<script_name>')
def get_script_reliability(script_name):
    """Get reliability metrics for a specific script."""
    try:
        reliability = _aggregate_script_reliability(script_name)
        if reliability is None:
            return _reliability_api_response(
                success=False,
                error=f'No execution history found for script: {script_name}',
                status=404,
            )

        cached = _load_reliability_summary().get('scripts', {}).get(script_name, {})
        return _reliability_api_response(data={
            'reliability': reliability,
            'cached': cached,
            'recommendations': _generate_recommendations(reliability),
            'trends': _build_reliability_trends_payload(script_name),
            'failures': _build_reliability_failures_payload(script_name=script_name, limit=50),
        })
    except Exception as e:
        return _reliability_api_response(success=False, error=str(e), status=500)


@app.route('/api/reliability/failures')
def get_reliability_failures():
    """Recent failures, breakdown, and recurring failure groups."""
    try:
        script_name = request.args.get('script', '').strip() or None
        limit = min(200, max(1, int(request.args.get('limit', 100))))
        return _reliability_api_response(
            data=_build_reliability_failures_payload(script_name=script_name, limit=limit),
        )
    except Exception as e:
        return _reliability_api_response(success=False, error=str(e), status=500)


@app.route('/api/reliability/diagnostics')
def get_reliability_diagnostics():
    """Replay/workspace orchestration diagnostics linked to reliability summaries."""
    try:
        refresh = request.args.get('refresh', '').lower() in ('1', 'true', 'yes')
        summary = _load_reliability_summary()
        diagnostics = _get_orchestration_diagnostics(summary=summary, refresh=refresh)
        return _reliability_api_response(data=diagnostics)
    except Exception as e:
        return _reliability_api_response(success=False, error=str(e), status=500)


@app.route('/api/reliability/trends')
def get_reliability_trends():
    """Trend, flaky detection, and duration regression metrics."""
    try:
        script_name = request.args.get('script', '').strip() or None
        trends = _build_reliability_trends_payload(script_name)
        if script_name and trends is None:
            return _reliability_api_response(
                success=False,
                error=f'No execution history found for script: {script_name}',
                status=404,
            )
        return _reliability_api_response(data=trends)
    except Exception as e:
        return _reliability_api_response(success=False, error=str(e), status=500)


@app.route('/api/reliability/recommendations')
def get_recommendations():
    """Get actionable recommendations based on reliability metrics."""
    try:
        dashboard = _build_reliability_dashboard()
        recommendations = dashboard.get('recommendations', [])
        
        return jsonify({
            'success': True,
            'data': {
                'recommendations': recommendations,
                'total_count': len(recommendations),
                'by_priority': {
                    'critical': len([r for r in recommendations if r.get('priority') == 'critical']),
                    'high': len([r for r in recommendations if r.get('priority') == 'high']),
                    'medium': len([r for r in recommendations if r.get('priority') == 'medium']),
                    'info': len([r for r in recommendations if r.get('priority') == 'info']),
                },
            },
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e),
        }), 500


@app.route('/api/reliability/failures/classify')
def classify_recent_failures():
    """Legacy alias for classified failures (delegates to /api/reliability/failures)."""
    try:
        payload = _build_reliability_failures_payload(limit=100)
        return _reliability_api_response(data={
            'failures_by_type': payload.get('failures_by_type', {}),
            'failure_types': payload.get('failure_types', FAILURE_TYPES),
            'total_failures': payload.get('total_failures', 0),
            'recent_count': payload.get('recent_count', 0),
            'recurring_failures': payload.get('recurring_failures', []),
        })
    except Exception as e:
        return _reliability_api_response(success=False, error=str(e), status=500)


@app.route('/logs/executions/<path:filename>')
def get_execution_log(filename):
    safe_name = os.path.basename(filename)
    full_path = os.path.join(EXECUTION_LOG_DIR, safe_name)
    if not os.path.exists(full_path):
        return jsonify({"error": "Log not found"}), 404
    return send_from_directory(
        EXECUTION_LOG_DIR, safe_name, mimetype="text/plain", as_attachment=False
    )


@app.route("/api/history/session/<session_id>")
def get_session(session_id):
    safe_name = os.path.basename(session_id)

    if not safe_name.endswith(".json"):
        safe_name += ".json"

    session_path = os.path.join(SESSION_LOG_DIR, safe_name)

    if not os.path.exists(session_path):
        return jsonify({"error": "Session not found"}), 404

    try:
        with open(session_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        _isolate_corrupted_file(session_path)
        return jsonify({'error': 'Session file corrupted'}), 500

    _sync_reliability_from_session_file(safe_name)
    summary = _load_reliability_summary()
    data['diagnostics'] = _diagnose_session_data(data, summary=summary)
    return jsonify(data)


@app.route("/api/workspace", methods=["GET"])
def get_workspace_state():
    data = load_workspace_state()
    return jsonify({
        'success': True,
        'workspace': data,
        'diagnostics': _build_workspace_diagnostics(data),
    })


@app.route("/api/workspace", methods=["POST"])
def persist_workspace_state():
    data = request.json or {}
    success, error = save_workspace_state(data)
    return jsonify({"success": success, "error": error})


@app.route("/api/workspace/profile", methods=["POST"])
def save_workspace_profile():
    data = request.json or {}
    name = data.get("name", "").strip()
    workspace = data.get("workspace")

    if not name:
        return jsonify({"success": False, "error": "Profile name required"}), 400

    valid, error = validate_workspace_snapshot(workspace)
    if not valid:
        return jsonify({"success": False, "error": error}), 400

    profile_path = get_workspace_profile_path(name)
    payload = {
        "version": 2,
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "profile_name": name,
        "workspace": workspace,
    }

    try:
        with open(profile_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/workspace/profiles", methods=["GET"])
def get_workspace_profiles():
    return jsonify({"success": True, "profiles": list_workspace_profiles()})


@app.route("/api/workspace/profile/<name>", methods=["GET"])
def load_workspace_profile(name):
    profile_path = get_workspace_profile_path(name)
    if not os.path.exists(profile_path):
        return jsonify({"success": False, "error": "Profile not found"}), 404

    try:
        with open(profile_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return jsonify({"success": True, "profile": data})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/workspace/profile/<name>", methods=["DELETE"])
def delete_workspace_profile(name):
    profile_path = get_workspace_profile_path(name)
    if not os.path.exists(profile_path):
        return jsonify({"success": False, "error": "Profile not found"}), 404

    try:
        os.remove(profile_path)
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/scripts/content", methods=["POST"])
def get_script_content():
    data = request.json or {}
    rel_path = data.get("path", "")
    password = data.get("password", "")

    if not check_lock(rel_path, password):
        return jsonify({'error': 'Locked', 'locked': True}), 401
        
    full_path = str(validate_safe_path(SCRIPTS_DIR, rel_path))

    if not os.path.exists(full_path):
        return jsonify({"error": "Script not found"}), 404

    with open(full_path, "r", encoding="utf-8", errors="replace") as f:
        content = f.read()

    return jsonify({"content": content, "path": rel_path})


def _track_metrics(proc, result, stop_event=None):
    """
    Background telemetry thread to track execution resource utilization.
    Traverses the process hierarchy recursively to sum parent and descendant
    resource metrics (CPU % and RSS memory). Reuses Process objects to ensure
    cpu_percent() has consistent deltas.
    """
    max_mem_mb = 0.0
    samples = 0
    total_cpu = 0.0
    try:
        p = psutil.Process(proc.pid)
        # Prime cpu_percent counter for parent (first call always returns 0)
        p.cpu_percent()

        # Cache of pid → psutil.Process so cpu_percent() has prior baselines
        tracked_children = {}

        while proc.poll() is None:
            if stop_event and stop_event.is_set():
                break
            time.sleep(0.1)
            sample_cpu = 0.0
            sample_mem = 0.0

            # Discover current child pids
            current_child_pids = set()
            try:
                for child in p.children(recursive=True):
                    current_child_pids.add(child.pid)
                    if child.pid not in tracked_children:
                        tracked_children[child.pid] = child
                        # Prime new child so next cycle gets a real delta
                        try:
                            child.cpu_percent()
                        except (psutil.NoSuchProcess, psutil.AccessDenied):
                            pass
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass

            # Remove stale entries for children that have exited
            for stale_pid in list(tracked_children.keys()):
                if stale_pid not in current_child_pids:
                    del tracked_children[stale_pid]

            # Measure parent
            try:
                sample_cpu += p.cpu_percent()
                sample_mem += p.memory_info().rss / (1024 * 1024)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass

            # Measure tracked children (reused objects → accurate cpu deltas)
            for child_proc in tracked_children.values():
                try:
                    sample_cpu += child_proc.cpu_percent()
                    sample_mem += child_proc.memory_info().rss / (1024 * 1024)
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue

            total_cpu += sample_cpu
            max_mem_mb = max(max_mem_mb, sample_mem)
            samples += 1
    except (psutil.NoSuchProcess, psutil.AccessDenied, Exception):
        pass

    result["cpu"] = round(total_cpu / samples, 1) if samples > 0 else 0.0
    result["mem"] = round(max_mem_mb, 1)


def _escape_bash_echo(text):
    # Escape backslashes first, then other bash special characters in double quotes
    escaped = text.replace("\\", "\\\\")
    escaped = escaped.replace('"', '\\"')
    escaped = escaped.replace("$", "\\$")
    escaped = escaped.replace("`", "\\`")
    return escaped


def instrument_script(content):
    lines = content.splitlines()
    instrumented_lines = []
    steps = []

    # First pass: find all executable steps
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            continue
        steps.append(stripped)

    total_steps = len(steps)

    # Second pass: inject progress calls
    step_idx = 0
    for line in lines:
        stripped = line.strip()

        is_step = False
        if stripped and not stripped.startswith("#"):
            is_step = True

        if is_step:
            step_idx += 1
            # Clean command display for security and readability
            cmd_display = stripped.split("#")[0].strip()
            cmd_escaped = _escape_bash_echo(cmd_display)
            instrumented_lines.append(
                f'echo "::progress::{step_idx}::{total_steps}::{cmd_escaped}"'
            )

        instrumented_lines.append(line)

    return "\n".join(instrumented_lines), steps


def _terminate_process_tree(proc, timeout=3):
    if proc is None:
        return
    if proc.poll() is not None:
        return

    pid = proc.pid
    try:
        parent = psutil.Process(pid)
        try:
            children = parent.children(recursive=True)
        except (psutil.NoSuchProcess, psutil.AccessDenied, ProcessLookupError):
            children = []
        processes = [parent] + children

        # Terminate gracefully
        for process in processes:
            try:
                if process.is_running():
                    process.terminate()
            except (psutil.NoSuchProcess, psutil.AccessDenied, ProcessLookupError):
                pass

        # Wait for processes to exit
        try:
            gone, alive = psutil.wait_procs(processes, timeout=timeout)
        except Exception:
            alive = []
            for p in processes:
                try:
                    if p.is_running():
                        alive.append(p)
                except Exception:  # nosec B110
                    pass

        # Kill remaining processes
        for process in alive:
            try:
                if process.is_running():
                    process.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied, ProcessLookupError):
                pass

        # Wait again after kill
        if alive:
            try:
                psutil.wait_procs(alive, timeout=2)
            except Exception:  # nosec B110
                pass
    except (psutil.NoSuchProcess, ProcessLookupError):
        # Parent process already gone
        pass
    except psutil.AccessDenied:
        # Permission issue, try using standard subprocess methods on parent
        try:
            proc.terminate()
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
                proc.wait(timeout=1)
            except Exception:  # nosec B110
                pass
        except Exception:  # nosec B110
            pass
    except Exception:
        # Any other exception fallback
        try:
            proc.terminate()
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
                proc.wait(timeout=1)
            except Exception:  # nosec B110
                pass
        except Exception:  # nosec B110
            pass

    # Ensure parent python subprocess object is fully reaped
    try:
        proc.wait(timeout=1)
    except Exception:
        try:
            proc.kill()
            proc.wait(timeout=1)
        except Exception:  # nosec B110
            pass


SENTINEL = object()


def _cleanup_execution(
    proc,
    execution,
    run_id=None,
    temp_path=None,
    was_aborted=False,
    error_message=None,
    exit_code=None,
    stop_event=None,
    reader_thread=None,
):
    if execution is None:
        # If execution wasn't initialized yet, we can still kill proc and remove temp file
        if proc:
            try:
                _terminate_process_tree(proc)
            except Exception as e:
                logger.error(
                    f"Error terminating process tree during early cleanup: {e}"
                )
        if temp_path:
            for _ in range(3):
                try:
                    if os.path.exists(temp_path):
                        os.remove(temp_path)
                    break
                except PermissionError:
                    time.sleep(0.2)
                except Exception as e:
                    logger.error(f"Error removing temporary run script: {e}")
                    break
        if run_id:
            with active_processes_lock:
                if run_id in active_processes:
                    del active_processes[run_id]
        return

    # Check cleanup flag for idempotency
    if execution.get("cleaned_up"):
        return
    execution["cleaned_up"] = True

    logger.info(f"Starting centralized cleanup for run_id: {run_id}")

    # 1. Signal telemetry monitor thread to stop
    if stop_event:
        try:
            stop_event.set()
        except Exception as e:
            logger.error(f"Error setting metrics stop event: {e}")

    # 2. Hard process termination
    if proc:
        try:
            if proc.poll() is None:
                logger.info(f"Terminating process tree for pid: {proc.pid}")
                _terminate_process_tree(proc)
        except Exception as e:
            logger.error(
                f"Error during process tree termination for pid {proc.pid}: {e}"
            )

    # 3. Join the reader thread if provided
    if reader_thread:
        try:
            reader_thread.join(timeout=1.0)
        except Exception as e:
            logger.error(f"Error joining reader thread: {e}")

    # 4. Close process stream handles
    if proc:
        for stream_name in ("stdout", "stderr"):
            stream = getattr(proc, stream_name, None)
            if stream:
                try:
                    stream.close()
                except Exception as e:
                    logger.error(
                        f"Error closing stream {stream_name} for pid {proc.pid}: {e}"
                    )

    # 5. Finalize execution record if still running/unfinalized
    record = execution.get("record")
    if record and record.get("status") == "running":
        try:
            elapsed = time.perf_counter() - execution.get(
                "monotonic_start", time.perf_counter()
            )
            if exit_code is None:
                exit_code = (
                    proc.returncode if proc and proc.returncode is not None else -15
                )

            _finalize_execution(
                execution,
                success=False,
                exit_code=exit_code,
                duration_seconds=elapsed,
                error_message=error_message
                or ("Script aborted" if was_aborted else "Execution stopped"),
            )
        except Exception as e:
            logger.error(f"Error finalizing execution record during cleanup: {e}")

    # 6. Ensure the log file handle itself is closed even if finalize failed/skipped
    handle = execution.get("handle")
    if handle:
        try:
            if not handle.closed:
                handle.flush()
                handle.close()
        except Exception as e:
            logger.error(f"Error closing execution log handle: {e}")

    # 7. Clean up active_processes tracking
    if run_id:
        with active_processes_lock:
            if run_id in active_processes:
                del active_processes[run_id]

    # 8. Clean up temporary run script file if any (Windows safe with retries)
    if temp_path:
        for _ in range(3):
            try:
                if os.path.exists(temp_path):
                    os.remove(temp_path)
                    logger.info(f"Removed temporary run script: {temp_path}")
                break
            except PermissionError:
                time.sleep(0.2)
            except Exception as e:
                logger.error(f"Error removing temporary run script {temp_path}: {e}")
                break

    logger.info(f"Cleanup finished for run_id: {run_id}")


@app.route("/api/scripts/run", methods=["POST"])
def run_script():
    data = request.json
    rel_path = data.get("path", "")
    password = data.get("password", "")

    if not check_lock(rel_path, password):
        return jsonify({'error': 'Locked', 'success': False}), 401
        
    full_path = str(validate_safe_path(SCRIPTS_DIR, rel_path))

    if not os.path.exists(full_path):
        return jsonify({"error": "Script not found"}), 404

    run_id = str(uuid.uuid4())[:8]
    shell_cmd = _find_shell()

    def generate():
        proc = None
        run_path = full_path
        start_time = time.perf_counter()
        execution = None
        stop_event = threading.Event()
        t_reader = None
        try:
            # 1. Initialize execution record inside generator to prevent leaks if not iterated
            execution = _start_execution_record(
                kind="script",
                display_name=rel_path,
                command_text=f"{shell_cmd} {full_path}",
                shell_cmd=shell_cmd,
                cwd=SCRIPTS_DIR,
            )

            # Instrument script content for progress tracking
            try:
                with open(full_path, "r", encoding="utf-8", errors="replace") as f:
                    content = f.read()

                instrumented_content, steps = instrument_script(content)

                if steps:
                    temp_dir = os.path.dirname(full_path)
                    temp_fd, temp_path = tempfile.mkstemp(
                        suffix=".sh", prefix=".tmp_run_", dir=temp_dir
                    )
                    with os.fdopen(
                        temp_fd, "w", encoding="utf-8", newline="\n"
                    ) as temp_f:
                        temp_f.write(instrumented_content)

                    run_path = temp_path
                else:
                    run_path = full_path

            except Exception as e:
                logger.error(f"Error instrumenting script: {e}")
                run_path = full_path

            # Use main's Windows support with your run_path
            args = (
                [shell_cmd, run_path]
                if shell_cmd != "cmd.exe"
                else ["cmd.exe", "/c", run_path]
            )

            proc = subprocess.Popen(
                args,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                cwd=SCRIPTS_DIR,
                bufsize=1,
                universal_newlines=True,
                shell=False
            )  # nosec B603

            with active_processes_lock:
                active_processes[run_id] = {
                    "process": proc,
                    "execution": execution,
                    "start_time": time.time(),
                    "status": "running",
                    "aborted": False,
                    "stop_event": stop_event,
                }

            metrics = {"cpu": 0.0, "mem": 0.0}
            t_metrics = threading.Thread(
                target=_track_metrics, args=(proc, metrics, stop_event)
            )
            t_metrics.start()

            _append_execution_line(
                execution, "system", f"Starting script execution... (ID: {run_id})"
            )
            start_msg = f"Starting script execution... (ID: {run_id})\n"
            yield "data: " + json.dumps(
                {"type": "started", "run_id": run_id, "content": start_msg}
            ) + "\n\n"

            # Set up non-blocking stdout reading thread with sentinel
            out_queue = queue.Queue()

            def stream_reader(stream, q):
                try:
                    for line in iter(stream.readline, ""):
                        q.put(line)
                except Exception as e:
                    logger.error(f"Reader thread error: {e}")
                finally:
                    q.put(SENTINEL)
                    try:
                        stream.close()
                    except Exception:  # nosec B110
                        pass

            t_reader = threading.Thread(
                target=stream_reader, args=(proc.stdout, out_queue), daemon=True
            )
            t_reader.start()

            while True:
                try:
                    line = out_queue.get(timeout=0.2)
                    if line is SENTINEL:
                        break

                    if run_path != full_path:
                        temp_basename = os.path.basename(run_path)
                        orig_basename = os.path.basename(full_path)
                        if temp_basename in line:
                            line = line.replace(temp_basename, orig_basename)

                    if "::progress::" in line:
                        match = re.search(r"::progress::(\d+)::(\d+)::(.*)", line)
                        if match:
                            step_idx = int(match.group(1))
                            total_steps = int(match.group(2))
                            cmd_text = match.group(3).strip()
                            yield "data: " + json.dumps(
                                {
                                    "type": "progress",
                                    "step": step_idx,
                                    "total": total_steps,
                                    "command": cmd_text,
                                }
                            ) + "\n\n"
                            continue

                    # Heuristic to detect errors in the combined stream
                    l_lower = line.lower()
                    msg_type = "stdout"
                    if any(
                        err in l_lower
                        for err in [
                            "error:",
                            "failed:",
                            "not found",
                            "denied",
                            "no such file",
                        ]
                    ):
                        msg_type = "error"
                    _append_execution_line(execution, msg_type, line)
                    yield "data: " + json.dumps(
                        {"type": msg_type, "content": line}
                    ) + "\n\n"
                except queue.Empty:
                    # Timeout reached, check if process died
                    if proc.poll() is not None:
                        break

            # Process finished. Re-check the queue to drain any remaining outputs
            while True:
                try:
                    line = out_queue.get_nowait()
                    if line is SENTINEL:
                        break

                    if run_path != full_path:
                        temp_basename = os.path.basename(run_path)
                        orig_basename = os.path.basename(full_path)
                        if temp_basename in line:
                            line = line.replace(temp_basename, orig_basename)

                    if "::progress::" in line:
                        match = re.search(r"::progress::(\d+)::(\d+)::(.*)", line)
                        if match:
                            step_idx = int(match.group(1))
                            total_steps = int(match.group(2))
                            cmd_text = match.group(3).strip()
                            yield "data: " + json.dumps(
                                {
                                    "type": "progress",
                                    "step": step_idx,
                                    "total": total_steps,
                                    "command": cmd_text,
                                }
                            ) + "\n\n"
                            continue

                    l_lower = line.lower()
                    msg_type = "stdout"
                    if any(
                        err in l_lower
                        for err in [
                            "error:",
                            "failed:",
                            "not found",
                            "denied",
                            "no such file",
                        ]
                    ):
                        msg_type = "error"
                    _append_execution_line(execution, msg_type, line)
                    yield "data: " + json.dumps(
                        {"type": msg_type, "content": line}
                    ) + "\n\n"
                except queue.Empty:
                    break

            proc.wait(timeout=5)
            t_metrics.join(timeout=1.0)
            t_reader.join(timeout=1.0)

            end_time = time.perf_counter()
            elapsed = end_time - start_time

            was_aborted = False
            with active_processes_lock:
                entry = active_processes.get(run_id)
                if entry and entry.get("aborted"):
                    was_aborted = True

            if was_aborted:
                _append_execution_line(
                    execution, "system", f"Script aborted (exit code {proc.returncode})"
                )
                _finalize_execution(
                    execution,
                    success=False,
                    exit_code=proc.returncode if proc.returncode is not None else -15,
                    duration_seconds=elapsed,
                    error_message="Script aborted by user",
                )
                abort_msg = 'Script aborted\n'
                yield f"data: {json.dumps({'type': 'aborted', 'run_id': run_id, 'content': abort_msg})}\n\n"
            else:
                system_mem = psutil.virtual_memory().total / (1024 * 1024)
                mem_percent = (
                    (metrics["mem"] / system_mem * 100) if system_mem > 0 else 0
                )

                resource_info = {
                    "execution_time": round(elapsed, 3),
                    "execution_time_formatted": _format_time(elapsed),
                    "exit_code": proc.returncode,
                    "cpu_percent": metrics["cpu"],
                    "memory_used_mb": metrics["mem"],
                    "memory_total_mb": round(system_mem, 1),
                    "memory_percent": round(mem_percent, 2),
                }

                _append_execution_line(
                    execution,
                    "system",
                    f"Script completed with exit code {proc.returncode}",
                )
                _finalize_execution(
                    execution,
                    success=proc.returncode == 0,
                    exit_code=proc.returncode,
                    duration_seconds=elapsed,
                    resource_usage=resource_info,
                )
                yield "data: " + json.dumps(
                    {
                        "type": "metrics",
                        "resources": resource_info,
                        "exit_code": proc.returncode,
                        "success": proc.returncode == 0,
                    }
                ) + "\n\n"

        except (GeneratorExit, BrokenPipeError, ConnectionResetError) as e:
            logger.info(
                f"SSE script client disconnected or pipe broken (run_id: {run_id}): {type(e).__name__}"
            )
            _cleanup_execution(
                proc,
                execution,
                run_id=run_id,
                temp_path=run_path if run_path != full_path else None,
                was_aborted=True,
                error_message="Client disconnected",
                stop_event=stop_event,
                reader_thread=t_reader,
            )
            raise
        except subprocess.TimeoutExpired:
            logger.warning(f"Script run_id {run_id} execution timed out")
            _cleanup_execution(
                proc,
                execution,
                run_id=run_id,
                temp_path=run_path if run_path != full_path else None,
                was_aborted=False,
                error_message="Execution timed out",
                stop_event=stop_event,
                reader_thread=t_reader,
            )
            yield "data: " + json.dumps(
                {"type": "error", "content": "❌ Execution timed out\n"}
            ) + "\n\n"
        except Exception as e:
            logger.error(
                f"Script run_id {run_id} execution encountered exception: {e}",
                exc_info=True,
            )
            _cleanup_execution(
                proc,
                execution,
                run_id=run_id,
                temp_path=run_path if run_path != full_path else None,
                was_aborted=False,
                error_message=str(e),
                stop_event=stop_event,
                reader_thread=t_reader,
            )
            yield "data: " + json.dumps(
                {"type": "error", "content": f"❌ Execution Error: {str(e)}"}
            ) + "\n\n"
        finally:
            _cleanup_execution(
                proc,
                execution,
                run_id=run_id,
                temp_path=run_path if run_path != full_path else None,
                stop_event=stop_event,
                reader_thread=t_reader,
            )

    return Response(generate(), mimetype="text/event-stream")


@app.route("/api/scripts/kill", methods=["POST"])
def kill_script():
    data = request.json or {}
    run_id = data.get("run_id", "")

    if not run_id:
        return jsonify({"error": "run_id is required"}), 400

    with active_processes_lock:
        entry = active_processes.get(run_id)
        if not entry:
            return jsonify({"error": "No running process found for this run_id"}), 404
        proc = entry["process"]
        if proc.poll() is not None:
            return jsonify({"error": "No running process found for this run_id"}), 404
        entry["aborted"] = True

    _terminate_process_tree(proc)

    return jsonify({"success": True, "run_id": run_id})


@app.route("/api/exec", methods=["POST"])
def exec_command():
    data = request.json
    command = data.get("command", "")

    if not command:
        return jsonify({"error": "No command provided"}), 400

    save_command_history(command)

    shell_cmd = _find_shell()
    run_id = f"cmd_{uuid.uuid4().hex[:8]}"

    def generate():
        proc = None
        start_time = time.perf_counter()
        execution = None
        t_reader = None
        try:
            # Initialize execution record inside generator to prevent leaks if not iterated
            execution = _start_execution_record(
                kind="command",
                display_name=command,
                command_text=command,
                shell_cmd=shell_cmd,
                cwd=SCRIPTS_DIR,
            )

            # Need to format for Windows/Linux subshells correctly
            args = (
                [shell_cmd, "-c", command]
                if shell_cmd != "cmd.exe"
                else ["cmd.exe", "/c", command]
            )

            proc = subprocess.Popen(
                args,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                cwd=SCRIPTS_DIR,
                bufsize=1,
                universal_newlines=True,
                shell=False
            )  # nosec B603

            with active_processes_lock:
                active_processes[run_id] = {
                    "process": proc,
                    "execution": execution,
                    "start_time": time.time(),
                    "status": "running",
                    "aborted": False,
                }

            # Set up non-blocking stdout reading thread with sentinel
            out_queue = queue.Queue()

            def stream_reader(stream, q):
                try:
                    for line in iter(stream.readline, ""):
                        q.put(line)
                except Exception as e:
                    logger.error(f"Command reader thread error: {e}")
                finally:
                    q.put(SENTINEL)
                    try:
                        stream.close()
                    except Exception:  # nosec B110
                        pass

            t_reader = threading.Thread(
                target=stream_reader, args=(proc.stdout, out_queue), daemon=True
            )
            t_reader.start()

            while True:
                try:
                    line = out_queue.get(timeout=0.2)
                    if line is SENTINEL:
                        break

                    l_lower = line.lower()
                    msg_type = "stdout"
                    if any(
                        err in l_lower
                        for err in [
                            "error:",
                            "failed:",
                            "not found",
                            "denied",
                            "no such file",
                        ]
                    ):
                        msg_type = "error"
                    _append_execution_line(execution, msg_type, line)
                    yield "data: " + json.dumps(
                        {"type": msg_type, "content": line}
                    ) + "\n\n"
                except queue.Empty:
                    # Timeout reached, check if process died
                    if proc.poll() is not None:
                        break

            # Process finished. Drain queue of any remaining logs
            while True:
                try:
                    line = out_queue.get_nowait()
                    if line is SENTINEL:
                        break

                    l_lower = line.lower()
                    msg_type = "stdout"
                    if any(
                        err in l_lower
                        for err in [
                            "error:",
                            "failed:",
                            "not found",
                            "denied",
                            "no such file",
                        ]
                    ):
                        msg_type = "error"
                    _append_execution_line(execution, msg_type, line)
                    yield "data: " + json.dumps(
                        {"type": msg_type, "content": line}
                    ) + "\n\n"
                except queue.Empty:
                    break

            proc.wait(timeout=5)
            t_reader.join(timeout=1.0)

            elapsed = time.perf_counter() - start_time
            _append_execution_line(
                execution,
                "system",
                f"Command completed with exit code {proc.returncode}",
            )
            _finalize_execution(
                execution,
                success=proc.returncode == 0,
                exit_code=proc.returncode,
                duration_seconds=elapsed,
            )
            yield "data: " + json.dumps(
                {
                    "type": "metrics",
                    "exit_code": proc.returncode,
                    "success": proc.returncode == 0,
                    "duration": round(elapsed, 3),
                }
            ) + "\n\n"

        except (GeneratorExit, BrokenPipeError, ConnectionResetError) as e:
            logger.info(
                f"SSE command client disconnected or pipe broken (run_id: {run_id}): {type(e).__name__}"
            )
            _cleanup_execution(
                proc,
                execution,
                run_id=run_id,
                was_aborted=True,
                error_message="Client disconnected",
                reader_thread=t_reader,
            )
            raise
        except subprocess.TimeoutExpired:
            logger.warning(f"Command execution timed out (run_id: {run_id})")
            _cleanup_execution(
                proc,
                execution,
                run_id=run_id,
                was_aborted=False,
                error_message="Execution timed out",
                reader_thread=t_reader,
            )
            yield "data: " + json.dumps(
                {"type": "error", "content": "❌ Execution timed out\n"}
            ) + "\n\n"
        except Exception as e:
            logger.error(
                f"Command run_id {run_id} execution encountered exception: {e}",
                exc_info=True,
            )
            _cleanup_execution(
                proc,
                execution,
                run_id=run_id,
                was_aborted=False,
                error_message=str(e),
                reader_thread=t_reader,
            )
            yield "data: " + json.dumps(
                {"type": "error", "content": f"❌ Command Error: {str(e)}"}
            ) + "\n\n"
        finally:
            _cleanup_execution(proc, execution, run_id=run_id, reader_thread=t_reader)

    return Response(generate(), mimetype="text/event-stream")


@app.route("/api/sessions/save", methods=["POST"])
def save_session():
    data = request.json
    session_data = data.get("session", {})

    try:
        sessions = load_sessions()

        sessions["last_session"] = session_data
        sessions["last_updated"] = time.time()

        save_sessions(sessions)

        return jsonify({"success": True})

    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/sessions/restore", methods=["GET"])
def restore_session():
    try:
        sessions = load_sessions()

        return jsonify({"success": True, "session": sessions.get("last_session", {})})

    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/scripts/save", methods=["POST"])
def save_script():
    data = request.json
    category = data.get("category", "").strip()
    filename = data.get("filename", "").strip()
    content = data.get("content", "")
    provided_pass = data.get("password", "")

    if not category or not filename:
        return jsonify({"error": "Category and filename required"}), 400

    if not filename.endswith(".sh"):
        filename += ".sh"

    category = category.replace("..", "").replace("/", "").replace("\\", "")
    filename = filename.replace("..", "").replace("/", "").replace("\\", "")
    rel_path = f"{category}/{filename}"

    rel_path = f'{category}/{filename}'
    
    # Secure path validation
    full_path = str(validate_safe_path(SCRIPTS_DIR, rel_path))
    
    if not check_lock(rel_path, provided_pass):
        return jsonify({"error": "Locked", "success": False}), 401

    os.makedirs(os.path.dirname(full_path), exist_ok=True)

    with open(full_path, 'w', encoding='utf-8', newline='\n') as f:
        f.write(content)

    return jsonify({"success": True, "path": rel_path})


@app.route("/api/scripts/delete", methods=["DELETE"])
def delete_script():
    data = request.json or {}
    rel_path = request.args.get("path", "") or data.get("path", "")
    provided_pass = data.get("password", "")

    if not check_lock(rel_path, provided_pass):
        return jsonify({'error': 'Locked', 'success': False}), 401
        
    full_path = str(validate_safe_path(SCRIPTS_DIR, rel_path))

    if os.path.exists(full_path):
        os.remove(full_path)
        # Clean up favs
        favs = load_favorites()
        if rel_path in favs:
            favs.remove(rel_path)
            save_favorites(favs)
        # Clean up locks
        locks = load_locks()
        if rel_path in locks:
            del locks[rel_path]
            save_locks(locks)
        return jsonify({"success": True})

    return jsonify({"error": "Script not found"}), 404


@app.route("/api/scripts/favorite", methods=["POST"])
def toggle_favorite():
    data = request.json
    rel_path = data.get("path", "")
    favs = load_favorites()

    if rel_path in favs:
        favs.remove(rel_path)
        is_fav = False
    else:
        favs.append(rel_path)
        is_fav = True

    save_favorites(favs)
    return jsonify({"favorite": is_fav})


@app.route("/api/scripts/lock", methods=["POST"])
def manage_lock():
    data = request.json
    rel_path = data.get("path", "")
    old_pass = data.get("old_password", "")
    new_pass = data.get("new_password", "")  # empty string removes lock!

    # Verify current lock
    if not check_lock(rel_path, old_pass):
        return jsonify({"error": "Incorrect current password", "success": False}), 401

    locks = load_locks()
    if new_pass:
        locks[rel_path] = generate_password_hash(new_pass)
    else:
        if rel_path in locks:
            del locks[rel_path]

    save_locks(locks)
    return jsonify({"success": True, "locked": bool(new_pass)})

class BlockRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(
            newurl,
            code,
            "Redirects are not allowed",
            headers,
            fp
        )
        
@app.route('/api/scripts/import_github', methods=['POST'])
def import_github():
    data = request.json
    url = data.get("url", "").strip()
    category = data.get("category", "").strip()
    filename = data.get("filename", "").strip()

    if not url or not category or not filename:
        return jsonify({"error": "Missing fields", "success": False}), 400

    if not filename.endswith(".sh"):
        filename += ".sh"

    # Convert standard GitHub URL → raw URL
    if "github.com" in url and "/blob/" in url:
        url = url.replace("github.com", "raw.githubusercontent.com").replace(
            "/blob/", "/"
        )

    # SSRF guard: only allow GitHub domains after rewrite
    _parsed = urllib.parse.urlparse(url)
    _ALLOWED = {"github.com", "raw.githubusercontent.com"}
    _ALLOWED_SCHEMES = {"http", "https"}
    if (
        _parsed.scheme.lower() not in _ALLOWED_SCHEMES
        or _parsed.hostname not in _ALLOWED
    ):
        return jsonify({"error": "Only GitHub URLs are allowed", "success": False}), 400

    # Reconstruct the URL using only the validated components to prevent parser differentials
    safe_url = f"{_parsed.scheme}://{_parsed.hostname}{_parsed.path}"
    if _parsed.query:
        safe_url += f"?{_parsed.query}"

    try:
        req = urllib.request.Request(safe_url, headers={'User-Agent': 'Mozilla/5.0 DevShell'})
        opener = urllib.request.build_opener(BlockRedirectHandler)

        with opener.open(req, timeout=10) as response:
            raw_bytes = response.read()

        # Prevent huge imports
        if len(raw_bytes) > 500000:
            return (
                jsonify({"error": "File too large (max 500KB)", "success": False}),
                400,
            )

        try:
            content = raw_bytes.decode("utf-8")

        except UnicodeDecodeError:
            return (
                jsonify(
                    {"error": "Only UTF-8 text files are supported", "success": False}
                ),
                400,
            )

        # Reject binary payloads
        if "\0" in content:
            return (
                jsonify({"error": "Binary files are not supported", "success": False}),
                400,
            )

    except Exception as e:
        return (
            jsonify(
                {"error": f"Failed to fetch from GitHub: {str(e)}", "success": False}
            ),
            400,
        )

    rel_path = f'{category}/{filename}'
    
    # Secure path validation
    full_path = str(validate_safe_path(SCRIPTS_DIR, rel_path))
    # Respect existing lock protection
    if not check_lock(rel_path, ""):
        return jsonify({"error": "File exists and is locked!", "success": False}), 401

    os.makedirs(os.path.dirname(full_path), exist_ok=True)

    with open(full_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(content)

    return jsonify({"success": True, "path": rel_path})


# --- NEW FEATURE: Raise PR / Push to Git ---
@app.route("/api/git/pr", methods=["POST"])
def raise_pr():
    # Parse the request payload for the script path, branch, commit message, and optional target repo
    data = request.json
    rel_path = data.get("path", "")
    branch_name = data.get("branch", f"script-contribution-{str(uuid.uuid4())[:4]}")
    commit_msg = data.get("message", f"Contribution: {rel_path}")
    target_repo = data.get("target_repo", "").strip()

    if not rel_path:
        return jsonify({"error": "No script path provided", "success": False}), 400

    full_path = str(validate_safe_path(SCRIPTS_DIR, rel_path))

    if target_repo:
        target_repo = validate_repo_name(target_repo)
    branch_name = validate_git_branch(branch_name)

    git_path = shutil.which("git") or "git"

    try:
        # Check if we are in a git repo
        subprocess.run([git_path, 'rev-parse', '--is-inside-work-tree'], check=True, capture_output=True, shell=False)  # nosec B603 B607
        
        # 1. Create new local branch for the contribution
        checkout_existing = subprocess.run([git_path, 'checkout', branch_name], capture_output=True, shell=False)  # nosec B603 B607
        if checkout_existing.returncode != 0:
            subprocess.run([git_path, 'checkout', '-b', branch_name], check=True, capture_output=True, shell=False)  # nosec B603 B607
        
        # 2. Stage only the specific script file
        subprocess.run([git_path, 'add', full_path], check=True, capture_output=True, shell=False)  # nosec B603 B607
        
        # 3. Commit the changes
        subprocess.run([git_path, 'commit', '-m', commit_msg], check=True, capture_output=True, shell=False)  # nosec B603 B607
        
        # 4. Push to target remote
        # If the user provided a specific target repository URL, we push directly to it.
        # Otherwise, we push to the default 'origin'.
        remote_to_push = target_repo if target_repo else 'origin'
        subprocess.run([git_path, 'push', '-u', remote_to_push, branch_name], check=True, capture_output=True, shell=False)  # nosec B603 B607
        # 5. Generate a GitHub PR Link
        # If an external repo URL was provided, use that to construct the base URL.
        if target_repo:
            remote_url = target_repo.replace(".git", "")
        else:
            remote_res = subprocess.run([git_path, 'remote', 'get-url', 'origin'], check=True, capture_output=True, text=True, shell=False)  # nosec B603 B607
            remote_url = remote_res.stdout.strip().replace('.git', '')
            
        if remote_url.startswith('git@github.com:'):
            remote_url = remote_url.replace('git@github.com:', 'https://github.com/')
            
        # Append the /compare path to take the user directly to the PR creation screen
        pr_url = (
            f"{remote_url}/compare/main...{branch_name}"
            if "github.com" in remote_url
            else remote_url
        )

        # 6. Switch back to the main branch to keep the workspace stable
        default_branch = get_default_branch()
        subprocess.run([git_path, 'checkout', default_branch], check=True, capture_output=True, shell=False)  # nosec B603 B607
        
        return jsonify({'success': True, 'pr_url': pr_url, 'branch': branch_name})
        
    except subprocess.CalledProcessError as e:
        err_msg = e.stderr.decode() if e.stderr else str(e)
        # Attempt recovery to main
        default_branch = get_default_branch()
        subprocess.run([git_path, 'checkout', default_branch], capture_output=True, shell=False)  # nosec B603 B607
        return jsonify({'error': err_msg, 'success': False}), 500
    except Exception as e:
        return jsonify({"error": str(e), "success": False}), 500


# ─── Helpers ──────────────────────────────────────────────────────


def _find_shell():
    """Find available bash shell on the system."""
    import platform
    import shutil

    candidates = [
        r"C:\Program Files\Git\bin\bash.exe",
        r"C:\Program Files (x86)\Git\bin\bash.exe",
    ]
    for candidate in candidates:
        if os.path.isfile(candidate):
            return candidate

    for shell in ["bash", "sh"]:
        found = shutil.which(shell)
        if found:
            return found

    if platform.system() == "Windows":
        return "cmd.exe"

    return "sh"


def get_default_branch():
    try:
        git_path = shutil.which("git") or "git"
        result = subprocess.run(
            [git_path, "symbolic-ref", "refs/remotes/origin/HEAD"],
            capture_output=True,
            text=True,
            check=True,
            shell=False
        )  # nosec B603 B607

        ref = result.stdout.strip()

        return ref.split("/")[-1]

    except Exception:
        return "main"


def _format_time(seconds):
    if seconds < 0.001:
        return f"{seconds * 1_000_000:.0f}µs"
    elif seconds < 1:
        return f"{seconds * 1000:.1f}ms"
    elif seconds < 60:
        return f"{seconds:.3f}s"
    else:
        mins = int(seconds // 60)
        secs = seconds % 60
        return f"{mins}m {secs:.1f}s"


# ─── Main ─────────────────────────────────────────────────────────

DEFAULT_PORT = 5000


def _server_port() -> int:
    raw = os.environ.get("DEVSHELL_PORT", "").strip()
    if not raw:
        return DEFAULT_PORT
    try:
        port = int(raw)
    except ValueError:
        raise SystemExit(
            f"Invalid DEVSHELL_PORT: {raw!r} (must be integer 1-65535)"
        )
    if not (1 <= port <= 65535):
        raise SystemExit(
            f"Invalid DEVSHELL_PORT: {raw!r} (must be integer 1-65535)"
        )
    return port


if __name__ == "__main__":
    port = _server_port()
    debug = os.environ.get("FLASK_DEBUG") == "1"
    print(f"[*] DevShell starting on http://127.0.0.1:{port}")
    print(f"[*] Scripts directory: {SCRIPTS_DIR}")
    app.run(
        debug=debug,
        host="127.0.0.1",
        port=port,
        use_reloader=False,
    )
