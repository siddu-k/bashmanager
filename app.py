from importlib import resources
import os
import json
import time
import subprocess
import tempfile
import threading
import queue
import uuid
import psutil
import hashlib
import urllib.request
import urllib.parse
import re
import shutil
from datetime import datetime, timezone
from flask import Flask, request, jsonify, send_from_directory, Response

app = Flask(__name__, static_folder='ui', static_url_path='')

BASE_DIR = os.environ.get('DEV_SHELL_DATA_DIR', os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'scripts')
FAVORITES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'favorites.json')
LOCKS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'locks.json')
LOG_ROOT = os.path.join(BASE_DIR, 'logs')
EXECUTION_LOG_DIR = os.path.join(LOG_ROOT, 'executions')
SESSION_LOG_DIR = os.path.join(LOG_ROOT, 'sessions')
HISTORY_FILE = os.path.join(LOG_ROOT, 'history.jsonl')
FAILED_HISTORY_FILE = os.path.join(LOG_ROOT, 'failed.jsonl')
COMMAND_HISTORY_FILE = os.path.join(LOG_ROOT, 'command_history.json')
WORKSPACE_DIR = os.path.join(LOG_ROOT, 'workspaces')
WORKSPACE_STATE_FILE = os.path.join(WORKSPACE_DIR, 'workspace_state.json')
WORKSPACE_PROFILE_DIR = os.path.join(WORKSPACE_DIR, 'profiles')
os.makedirs(WORKSPACE_DIR, exist_ok=True)
os.makedirs(WORKSPACE_PROFILE_DIR, exist_ok=True)

SESSIONS_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    'sessions.json'
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
        return False, 'Workspace snapshot must be an object'

    terminals = data.get('terminals')
    if terminals is not None and not isinstance(terminals, list):
        return False, 'Invalid terminals structure'

    active_terminal = data.get('activeTerminalId')
    if active_terminal is not None and not isinstance(active_terminal, int):
        return False, 'Invalid active terminal'

    version = data.get('version')
    if version is not None and not isinstance(version, int):
        return False, 'Invalid snapshot version'

    active_script = data.get('activeScript')
    if active_script is not None and not isinstance(active_script, str):
        return False, 'Invalid active script reference'

    return True, None


def load_workspace_state():
    if not os.path.exists(WORKSPACE_STATE_FILE):
        return None
    try:
        with open(WORKSPACE_STATE_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        corrupted_path = WORKSPACE_STATE_FILE + '.corrupted'
        try:
            shutil.move(WORKSPACE_STATE_FILE, corrupted_path)
        except Exception:
            pass
        return {
            'corrupted': True,
            'error': str(e)
        }


def save_workspace_state(data):
    valid, error = validate_workspace_snapshot(data)
    if not valid:
        return False, error

    payload = {
        'version': 2,
        'saved_at': datetime.now(timezone.utc).isoformat(),
        'workspace': data
    }

    try:
        with open(WORKSPACE_STATE_FILE, 'w', encoding='utf-8') as f:
            json.dump(payload, f, indent=2)
        return True, None
    except Exception as e:
        return False, str(e)


def get_workspace_profile_path(name):
    safe_name = re.sub(r'[^a-zA-Z0-9_-]', '_', name)
    return os.path.join(WORKSPACE_PROFILE_DIR, f'{safe_name}.json')


def list_workspace_profiles():
    profiles = []
    for file in os.listdir(WORKSPACE_PROFILE_DIR):
        if not file.endswith('.json'):
            continue
        profiles.append(file[:-5])
    return sorted(profiles)


def _ensure_log_dirs():
    os.makedirs(EXECUTION_LOG_DIR, exist_ok=True)
    os.makedirs(SESSION_LOG_DIR, exist_ok=True)


def _utc_now():
    return datetime.now(timezone.utc)


def _iso_now():
    return _utc_now().isoformat(timespec='seconds')


def _slugify(value, fallback='execution'):
    safe = re.sub(r'[^A-Za-z0-9._-]+', '-', str(value or '')).strip('-._')
    return safe[:48] or fallback


def _append_jsonl(file_path, record):
    os.makedirs(os.path.dirname(file_path), exist_ok=True)
    with open(file_path, 'a', encoding='utf-8', newline='\n') as f:
        json.dump(record, f, ensure_ascii=False)
        f.write('\n')


def _read_jsonl(file_path):
    records = []
    if not os.path.exists(file_path):
        return records
    with open(file_path, 'r', encoding='utf-8', errors='replace') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records


def _trim_jsonl(file_path, max_entries):
    if not os.path.exists(file_path):
        return
    with open(file_path, 'r', encoding='utf-8', errors='replace') as f:
        lines = f.readlines()
    if len(lines) <= max_entries:
        return
    with open(file_path, 'w', encoding='utf-8', newline='\n') as f:
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
        return f'{seconds:.2f}s'
    minutes = int(seconds // 60)
    remaining = seconds % 60
    return f'{minutes}m {remaining:.1f}s'


def _start_execution_record(kind, display_name, command_text, shell_cmd='', cwd=''):
    _ensure_log_dirs()
    started_at = _utc_now()
    monotonic_start = time.perf_counter()
    execution_id = uuid.uuid4().hex[:8]
    timestamp_token = started_at.strftime('%Y%m%dT%H%M%SZ')
    log_name = f'{timestamp_token}_{kind}_{_slugify(display_name)}_{execution_id}.log'
    log_path = os.path.join(EXECUTION_LOG_DIR, log_name)
    log_handle = open(log_path, 'w', encoding='utf-8', newline='\n')

    record = {
        'id': execution_id,
        'kind': kind,
        'display_name': display_name,
        'command': command_text,
        'shell': shell_cmd,
        'cwd': cwd,
        'started_at': started_at.isoformat(),
        'status': 'running',
        'exit_code': None,
        'duration_seconds': None,
        'log_file': log_name,
        'log_path': log_path,
        'output_excerpt': '',
        'success': False,
        'session_file': f'{execution_id}.json',
    }

    log_handle.write(f'[{record["started_at"]}] execution started\n')
    log_handle.write(f'kind: {kind}\n')
    log_handle.write(f'id: {execution_id}\n')
    log_handle.write(f'display: {display_name}\n')
    log_handle.write(f'command: {command_text}\n')
    if shell_cmd:
        log_handle.write(f'shell: {shell_cmd}\n')
    if cwd:
        log_handle.write(f'cwd: {cwd}\n')
    log_handle.write('\n')
    log_handle.flush()

    session_data = {
    'metadata': {
        'id': execution_id,
        'kind': kind,
        'display_name': display_name,
        'command': command_text,
        'shell': shell_cmd,
        'cwd': cwd,
        'started_at': started_at.isoformat(),
    },
    'events': []
    }

    return {
    'record': record,
    'handle': log_handle,
    'excerpt_lines': [],
    'excerpt_size': 0,
    'session_data': session_data,
    'monotonic_start': monotonic_start,
    }


def _append_execution_line(execution, stream_type, content):
    if execution is None:
        return
    line = content.rstrip('\n')
    if not line and stream_type != 'system':
        return
    timestamp = _iso_now()
    elapsed = round(
    time.perf_counter() - execution['monotonic_start'],
    4
    )
    execution['session_data']['events'].append({
    'timestamp': elapsed,
    'stream': stream_type,
    'content': line
    })
    execution['handle'].write(f'[{timestamp}] {stream_type}: {line}\n')
    execution['handle'].flush()
    excerpt_line = f'{stream_type}: {line}'
    execution['excerpt_lines'].append(excerpt_line)
    execution['excerpt_size'] += len(excerpt_line) + 1
    while execution['excerpt_lines'] and execution['excerpt_size'] > MAX_HISTORY_EXCERPT_CHARS:
        removed = execution['excerpt_lines'].pop(0)
        execution['excerpt_size'] -= len(removed) + 1


def _finalize_execution(execution, success, exit_code, duration_seconds, resources=None, error_message=''):
    if execution is None:
        return None

    record = execution['record']
    record['status'] = 'success' if success else 'failed'
    record['success'] = bool(success)
    record['exit_code'] = int(exit_code) if exit_code is not None else None
    record['duration_seconds'] = round(duration_seconds, 3) if duration_seconds is not None else None
    record['duration'] = _format_duration(duration_seconds or 0)
    record['finished_at'] = _iso_now()
    record['output_excerpt'] = '\n'.join(execution['excerpt_lines'])[-MAX_HISTORY_EXCERPT_CHARS:]
    if resources:
        record['resources'] = resources
    if error_message:
        record['error'] = error_message

    execution['handle'].write('\n')
    execution['handle'].write(f'[{record["finished_at"]}] status: {record["status"]}\n')
    if record['exit_code'] is not None:
        execution['handle'].write(f'exit_code: {record["exit_code"]}\n')
    if record['duration_seconds'] is not None:
        execution['handle'].write(f'duration_seconds: {record["duration_seconds"]}\n')
    if error_message:
        execution['handle'].write(f'error: {error_message}\n')
    if resources:
        execution['handle'].write(f'resources: {json.dumps(resources, ensure_ascii=False)}\n')
    session_path = os.path.join(
    SESSION_LOG_DIR,
    record['session_file']
    )
    execution['session_data']['metadata'].update({
    'finished_at': record['finished_at'],
    'duration_seconds': record['duration_seconds'],
    'exit_code': record['exit_code'],
    'status': record['status'],
    'success': record['success'],
    })
    if resources:
        execution['session_data']['metadata']['resources'] = resources
    with open(session_path, 'w', encoding='utf-8') as sf:
        json.dump(
            execution['session_data'],
            sf,
            indent=2,
            ensure_ascii=False
        )
    execution['handle'].close()

    history_record = {
        'id': record['id'],
        'kind': record['kind'],
        'session_file': record['session_file'],
        'display_name': record['display_name'],
        'command': record['command'],
        'shell': record['shell'],
        'cwd': record['cwd'],
        'started_at': record['started_at'],
        'finished_at': record['finished_at'],
        'status': record['status'],
        'success': record['success'],
        'exit_code': record['exit_code'],
        'duration_seconds': record['duration_seconds'],
        'duration': record['duration'],
        'log_file': record['log_file'],
        'output_excerpt': record['output_excerpt'],
    }
    if error_message:
        history_record['error'] = error_message
    if resources:
        history_record['resources'] = resources

    _append_jsonl(HISTORY_FILE, history_record)
    if not success:
        _append_jsonl(FAILED_HISTORY_FILE, history_record)

    _trim_jsonl(HISTORY_FILE, MAX_HISTORY_ENTRIES)
    _trim_jsonl(FAILED_HISTORY_FILE, MAX_FAILED_HISTORY_ENTRIES)
    _cleanup_old_execution_logs()

    return history_record


def load_command_history():
    if not os.path.exists(COMMAND_HISTORY_FILE):
        return []

    try:
        with open(COMMAND_HISTORY_FILE, 'r', encoding='utf-8') as f:
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

    with open(COMMAND_HISTORY_FILE, 'w', encoding='utf-8') as f:
        json.dump(history, f, indent=2)


def _load_history_entries(query='', status='all', kind='all', limit=200):
    entries = _read_jsonl(HISTORY_FILE)
    query = (query or '').strip().lower()
    status = (status or 'all').strip().lower()
    kind = (kind or 'all').strip().lower()

    def matches(entry):
        if status != 'all' and entry.get('status', '').lower() != status:
            return False
        if kind != 'all' and entry.get('kind', '').lower() != kind:
            return False
        if not query:
            return True
        haystack = ' '.join([
            str(entry.get('command', '')),
            str(entry.get('display_name', '')),
            str(entry.get('output_excerpt', '')),
            str(entry.get('status', '')),
            str(entry.get('kind', '')),
            str(entry.get('exit_code', '')),
        ]).lower()
        return query in haystack

    filtered = [entry for entry in reversed(entries) if matches(entry)]
    return filtered[:limit]


def _history_summary():
    entries = _read_jsonl(HISTORY_FILE)
    total = len(entries)
    failed = sum(1 for entry in entries if entry.get('status') == 'failed')
    scripts = sum(1 for entry in entries if entry.get('kind') == 'script')
    commands = sum(1 for entry in entries if entry.get('kind') == 'command')
    return {
        'total': total,
        'failed': failed,
        'successful': total - failed,
        'scripts': scripts,
        'commands': commands,
    }


_ensure_log_dirs()
_cleanup_old_execution_logs()


def load_favorites():
    if os.path.exists(FAVORITES_FILE):
        with open(FAVORITES_FILE, 'r') as f:
            return json.load(f)
    return []


def save_favorites(favs):
    with open(FAVORITES_FILE, 'w') as f:
        json.dump(favs, f)


def load_locks():
    if os.path.exists(LOCKS_FILE):
        with open(LOCKS_FILE, 'r') as f:
            return json.load(f)
    return {}


def save_locks(locks):
    with open(LOCKS_FILE, 'w') as f:
        json.dump(locks, f)


def load_sessions():
    if os.path.exists(SESSIONS_FILE):
        with open(SESSIONS_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}


def save_sessions(sessions):
    with open(SESSIONS_FILE, 'w', encoding='utf-8') as f:
        json.dump(sessions, f, indent=2)


def check_lock(rel_path, provided_pass):
    locks = load_locks()
    if rel_path in locks:
        if not provided_pass:
            return False
        if hashlib.sha256(provided_pass.encode()).hexdigest() != locks[rel_path]:
            return False
    return True

def is_safe_path(base_dir, target_path):
    base_dir = os.path.abspath(base_dir)
    target_path = os.path.abspath(target_path)

    return os.path.commonpath([base_dir, target_path]) == base_dir


def parse_script_metadata(filepath):
    """Parse metadata from script comment headers."""
    metadata = {
        'name': os.path.basename(filepath).replace('.sh', '').replace('_', ' ').title(),
        'desc': '',
        'tag': '',
        'path': filepath
    }
    try:
        with open(filepath, 'r', encoding='utf-8', errors='replace') as f:
            for line in f:
                line = line.strip()
                if line.startswith('# name:'):
                    metadata['name'] = line[7:].strip()
                elif line.startswith('# desc:'):
                    metadata['desc'] = line[7:].strip()
                elif line.startswith('# tag:'):
                    metadata['tag'] = line[6:].strip()
                elif not line.startswith('#') and line:
                    break
    except Exception:
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
                if script_file.endswith('.sh'):
                    full_path = os.path.join(cat_path, script_file)
                    rel_path = f"{category}/{script_file}"
                    meta = parse_script_metadata(full_path)
                    meta['file'] = script_file
                    meta['category'] = category
                    meta['relative_path'] = rel_path
                    meta['favorite'] = rel_path in favorites
                    meta['locked'] = rel_path in locks
                    scripts.append(meta)
            if scripts:
                categories[category] = scripts

    return categories


# ─── Routes ───────────────────────────────────────────────────────

@app.route('/')
def index():
    return send_from_directory('ui', 'index.html')


@app.route('/api/scripts')
def list_scripts():
    return jsonify(get_all_scripts())


@app.route('/api/history')
def get_history():
    query = request.args.get('q', '')
    status = request.args.get('status', 'all')
    kind = request.args.get('kind', 'all')
    limit = request.args.get('limit', 200, type=int)
    limit = max(1, min(limit or 200, 500))

    entries = _load_history_entries(query=query, status=status, kind=kind, limit=limit)
    return jsonify({
        'entries': entries,
        'summary': _history_summary(),
        'query': {
            'q': query,
            'status': status,
            'kind': kind,
            'limit': limit,
        }
    })


@app.route('/api/command_history')
def get_command_history():
    return jsonify({
        'success': True,
        'history': load_command_history()
    })

@app.route('/api/command_history/clear', methods=['POST'])
def clear_command_history():
    try:
        # Overwrite the history JSON file with an empty array
        with open(COMMAND_HISTORY_FILE, 'w', encoding='utf-8') as f:
            json.dump([], f, indent=2)
            
        return jsonify({
            'success': True,
            'message': 'Command history cleared successfully'
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@app.route('/api/history/analytics')
def history_analytics():
    entries = _load_history_entries(limit=1000)

    total = len(entries)

    successful = sum(
        1 for e in entries if e.get('success')
    )

    failed = total - successful

    avg_duration = round(
        sum(
            e.get('duration_seconds', 0)
            for e in entries
        ) / total,
        2
    ) if total else 0

    script_counts = {}

    for entry in entries:
        name = entry.get('display_name', 'Unknown')
        script_counts[name] = (
            script_counts.get(name, 0) + 1
        )

    top_scripts = sorted(
        script_counts.items(),
        key=lambda x: x[1],
        reverse=True
    )[:5]

    slowest = sorted(
        entries,
        key=lambda e: e.get('duration_seconds', 0),
        reverse=True
    )[:5]

    recent_failures = [
        e for e in entries
        if not e.get('success')
    ][:5]

    return jsonify({
        'success': True,
        'summary': {
            'total': total,
            'successful': successful,
            'failed': failed,
            'avg_duration': avg_duration
        },
        'top_scripts': top_scripts,
        'slowest': slowest,
        'recent_failures': recent_failures
    })


@app.route('/api/history/export')
def export_history():
    query = request.args.get('q', '')
    status = request.args.get('status', 'all')
    kind = request.args.get('kind', 'all')
    export_format = request.args.get('format', 'log').lower()
    entries = _load_history_entries(query=query, status=status, kind=kind, limit=500)

    lines = [
        'DevShell Execution History Export',
        f'Generated: {_iso_now()}',
        f'Filter: q={query or "*"} status={status} kind={kind}',
        ''
    ]

    if not entries:
        lines.append('No matching history entries found.')
    else:
        for entry in entries:
            lines.extend([
                f'[{entry.get("started_at", "")}] {entry.get("status", "unknown").upper()} {entry.get("kind", "execution").upper()} #{entry.get("id", "")}',
                f'Command: {entry.get("command", "")}',
                f'Display: {entry.get("display_name", "")}',
                f'Exit Code: {entry.get("exit_code", "")}',
                f'Duration: {entry.get("duration", "")}',
                f'Log: {entry.get("log_file", "")}',
            ])
            excerpt = entry.get('output_excerpt', '').strip()
            if excerpt:
                lines.append('Output:')
                lines.extend(f'  {line}' for line in excerpt.splitlines())
            error = entry.get('error', '').strip()
            if error:
                lines.append(f'Error: {error}')
            lines.append('')

    export_text = '\n'.join(lines).rstrip() + '\n'
    filename = f'devshell-history-{_slugify(status + "-" + kind)}.{"txt" if export_format == "txt" else "log"}'
    return Response(
        export_text,
        mimetype='text/plain; charset=utf-8',
        headers={
            'Content-Disposition': f'attachment; filename="{filename}"',
            'Cache-Control': 'no-store',
        }
    )


@app.route('/logs/executions/<path:filename>')
def get_execution_log(filename):
    safe_name = os.path.basename(filename)
    full_path = os.path.join(EXECUTION_LOG_DIR, safe_name)
    if not os.path.exists(full_path):
        return jsonify({'error': 'Log not found'}), 404
    return send_from_directory(EXECUTION_LOG_DIR, safe_name, mimetype='text/plain', as_attachment=False)

@app.route('/api/history/session/<session_id>')
def get_session(session_id):
    safe_name = os.path.basename(session_id)

    if not safe_name.endswith('.json'):
        safe_name += '.json'

    session_path = os.path.join(
        SESSION_LOG_DIR,
        safe_name
    )

    if not os.path.exists(session_path):
        return jsonify({'error': 'Session not found'}), 404

    with open(session_path, 'r', encoding='utf-8') as f:
        data = json.load(f)

    return jsonify(data)


@app.route('/api/workspace', methods=['GET'])
def get_workspace_state():
    data = load_workspace_state()
    return jsonify({
        'success': True,
        'workspace': data
    })


@app.route('/api/workspace', methods=['POST'])
def persist_workspace_state():
    data = request.json or {}
    success, error = save_workspace_state(data)
    return jsonify({
        'success': success,
        'error': error
    })


@app.route('/api/workspace/profile', methods=['POST'])
def save_workspace_profile():
    data = request.json or {}
    name = data.get('name', '').strip()
    workspace = data.get('workspace')

    if not name:
        return jsonify({'success': False, 'error': 'Profile name required'}), 400

    valid, error = validate_workspace_snapshot(workspace)
    if not valid:
        return jsonify({'success': False, 'error': error}), 400

    profile_path = get_workspace_profile_path(name)
    payload = {
        'version': 2,
        'saved_at': datetime.now(timezone.utc).isoformat(),
        'profile_name': name,
        'workspace': workspace
    }

    try:
        with open(profile_path, 'w', encoding='utf-8') as f:
            json.dump(payload, f, indent=2)
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/workspace/profiles', methods=['GET'])
def get_workspace_profiles():
    return jsonify({
        'success': True,
        'profiles': list_workspace_profiles()
    })


@app.route('/api/workspace/profile/<name>', methods=['GET'])
def load_workspace_profile(name):
    profile_path = get_workspace_profile_path(name)
    if not os.path.exists(profile_path):
        return jsonify({'success': False, 'error': 'Profile not found'}), 404

    try:
        with open(profile_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return jsonify({'success': True, 'profile': data})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/workspace/profile/<name>', methods=['DELETE'])
def delete_workspace_profile(name):
    profile_path = get_workspace_profile_path(name)
    if not os.path.exists(profile_path):
        return jsonify({'success': False, 'error': 'Profile not found'}), 404

    try:
        os.remove(profile_path)
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/scripts/content', methods=['POST'])
def get_script_content():
    data = request.json or {}
    rel_path = data.get('path', '')
    password = data.get('password', '')
    
    if not check_lock(rel_path, password):
        return jsonify({'error': 'Locked', 'locked': True}), 401
        
    full_path = os.path.join(SCRIPTS_DIR, rel_path)
    full_path = os.path.normpath(full_path)

    # Security check
    if not is_safe_path(SCRIPTS_DIR, full_path):
        return jsonify({'error': 'Invalid path'}), 403

    if not os.path.exists(full_path):
        return jsonify({'error': 'Script not found'}), 404

    with open(full_path, 'r', encoding='utf-8', errors='replace') as f:
        content = f.read()

    return jsonify({'content': content, 'path': rel_path})


def _track_metrics(proc, result):
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

    result['cpu'] = round(total_cpu / samples, 1) if samples > 0 else 0.0
    result['mem'] = round(max_mem_mb, 1)


def _escape_bash_echo(text):
    # Escape backslashes first, then other bash special characters in double quotes
    escaped = text.replace('\\', '\\\\')
    escaped = escaped.replace('"', '\\"')
    escaped = escaped.replace('$', '\\$')
    escaped = escaped.replace('`', '\\`')
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
        if stripped.startswith('#'):
            continue
        steps.append(stripped)
        
    total_steps = len(steps)
    
    # Second pass: inject progress calls
    step_idx = 0
    for line in lines:
        stripped = line.strip()
        
        is_step = False
        if stripped and not stripped.startswith('#'):
            is_step = True
                
        if is_step:
            step_idx += 1
            # Clean command display for security and readability
            cmd_display = stripped.split('#')[0].strip()
            cmd_escaped = _escape_bash_echo(cmd_display)
            instrumented_lines.append(f'echo "::progress::{step_idx}::{total_steps}::{cmd_escaped}"')
            
        instrumented_lines.append(line)
        
    return '\n'.join(instrumented_lines), steps
def _terminate_process_tree(proc, timeout=3):
    if proc.poll() is not None:
        return

    try:
        parent = psutil.Process(proc.pid)
        processes = [parent] + parent.children(recursive=True)
        for process in processes:
            try:
                process.terminate()
            except psutil.NoSuchProcess:
                pass

        _, alive = psutil.wait_procs(processes, timeout=timeout)
        for process in alive:
            try:
                process.kill()
            except psutil.NoSuchProcess:
                pass

        if alive:
            psutil.wait_procs(alive, timeout=2)
    except psutil.NoSuchProcess:
        pass
    except Exception:
        try:
            proc.terminate()
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()

    try:
        proc.wait(timeout=1)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=1)


@app.route('/api/scripts/run', methods=['POST'])
def run_script():
    data = request.json
    rel_path = data.get('path', '')
    password = data.get('password', '')
    
    if not check_lock(rel_path, password):
        return jsonify({'error': 'Locked', 'success': False}), 401
        
    full_path = os.path.join(SCRIPTS_DIR, rel_path)
    full_path = os.path.normpath(full_path)

    # Security check
    if not is_safe_path(SCRIPTS_DIR, full_path):
        return jsonify({'error': 'Invalid path'}), 403

    if not os.path.exists(full_path):
        return jsonify({'error': 'Script not found'}), 404

    run_id = str(uuid.uuid4())[:8]
    shell_cmd = _find_shell()
    execution = _start_execution_record(
        kind='script',
        display_name=rel_path,
        command_text=f'{shell_cmd} {full_path}',
        shell_cmd=shell_cmd,
        cwd=SCRIPTS_DIR,
    )

    def generate():
        proc = None
        run_path = full_path
        start_time = time.time()
        try:
            # Instrument script content for progress tracking
            try:
                with open(full_path, 'r', encoding='utf-8', errors='replace') as f:
                    content = f.read()

                instrumented_content, steps = instrument_script(content)

                if steps:
                    temp_dir = os.path.dirname(full_path)
                    temp_fd, temp_path = tempfile.mkstemp(
                        suffix='.sh',
                        prefix='.tmp_run_',
                        dir=temp_dir
                    )
                    with os.fdopen(temp_fd, 'w', encoding='utf-8', newline='\n') as temp_f:
                        temp_f.write(instrumented_content)
                    
                    run_path = temp_path
                else:
                    run_path = full_path

            except Exception:
                run_path = full_path

            # Use main's Windows support with your run_path
            args = (
                [shell_cmd, run_path]
                if shell_cmd != 'cmd.exe'
                else ['cmd.exe', '/c', run_path]
            )

            proc = subprocess.Popen(
                args,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                cwd=SCRIPTS_DIR,
                bufsize=1,
                universal_newlines=True
            )

            with active_processes_lock:
                active_processes[run_id] = {
                    'process': proc,
                    'execution': execution,
                    'start_time': start_time,
                    'status': 'running',
                    'aborted': False,
                }

            metrics = {'cpu': 0.0, 'mem': 0.0}
            t_metrics = threading.Thread(target=_track_metrics, args=(proc, metrics))
            t_metrics.start()

            _append_execution_line(execution, 'system', f'Starting script execution... (ID: {run_id})')
            yield f"data: {json.dumps({'type': 'started', 'run_id': run_id, 'content': f'Starting script execution... (ID: {run_id})\n'})}\n\n"

            for line in iter(proc.stdout.readline, ''):
                if line:
                    if run_path != full_path:
                        temp_basename = os.path.basename(run_path)
                        orig_basename = os.path.basename(full_path)
                        if temp_basename in line:
                            line = line.replace(temp_basename, orig_basename)

                    if '::progress::' in line:
                        match = re.search(r'::progress::(\d+)::(\d+)::(.*)', line)
                        if match:
                            step_idx = int(match.group(1))
                            total_steps = int(match.group(2))
                            cmd_text = match.group(3).strip()
                            yield f"data: {json.dumps({'type': 'progress', 'step': step_idx, 'total': total_steps, 'command': cmd_text})}\n\n"
                            continue

                    # Heuristic to detect errors in the combined stream
                    l_lower = line.lower()
                    msg_type = 'stdout'
                    if any(err in l_lower for err in ['error:', 'failed:', 'not found', 'denied', 'no such file']):
                        msg_type = 'error'
                    _append_execution_line(execution, msg_type, line)
                    yield f"data: {json.dumps({'type': msg_type, 'content': line})}\n\n"

            proc.stdout.close()
            proc.wait(timeout=10)
            t_metrics.join(timeout=1)

            end_time = time.time()
            elapsed = end_time - start_time

            was_aborted = False
            with active_processes_lock:
                entry = active_processes.get(run_id)
                if entry and entry.get('aborted'):
                    was_aborted = True

            if was_aborted:
                _append_execution_line(execution, 'system', f'Script aborted (exit code {proc.returncode})')
                _finalize_execution(
                    execution,
                    success=False,
                    exit_code=proc.returncode if proc.returncode is not None else -15,
                    duration_seconds=elapsed,
                    error_message='Script aborted by user',
                )
                yield f"data: {json.dumps({'type': 'aborted', 'run_id': run_id, 'content': 'Script aborted\n'})}\n\n"
            else:
                system_mem = psutil.virtual_memory().total / (1024 * 1024)
                mem_percent = (metrics['mem'] / system_mem * 100) if system_mem > 0 else 0

                resource_info = {
                    'execution_time': round(elapsed, 3),
                    'execution_time_formatted': _format_time(elapsed),
                    'exit_code': proc.returncode,
                    'cpu_percent': metrics['cpu'],
                    'memory_used_mb': metrics['mem'],
                    'memory_total_mb': round(system_mem, 1),
                    'memory_percent': round(mem_percent, 2),
                }

                _append_execution_line(execution, 'system', f'Script completed with exit code {proc.returncode}')
                _finalize_execution(
                    execution,
                    success=proc.returncode == 0,
                    exit_code=proc.returncode,
                    duration_seconds=elapsed,
                    resources=resource_info,
                )
                yield f"data: {json.dumps({'type': 'metrics', 'resources': resource_info, 'exit_code': proc.returncode, 'success': proc.returncode == 0})}\n\n"
        except subprocess.TimeoutExpired:
            was_aborted = False
            with active_processes_lock:
                entry = active_processes.get(run_id)
                if entry and entry.get('aborted'):
                    was_aborted = True

            if proc:
                _terminate_process_tree(proc)

            if was_aborted:
                _append_execution_line(execution, 'system', f'Script aborted (exit code {proc.returncode})')
                _finalize_execution(
                    execution,
                    success=False,
                    exit_code=proc.returncode if proc and proc.returncode is not None else -15,
                    duration_seconds=time.time() - start_time,
                    error_message='Script aborted by user',
                )
                yield f"data: {json.dumps({'type': 'aborted', 'run_id': run_id, 'content': 'Script aborted\n'})}\n\n"
            else:
                _append_execution_line(execution, 'error', '❌ Execution timed out')
                _finalize_execution(
                    execution,
                    success=False,
                    exit_code=-1,
                    duration_seconds=time.time() - start_time,
                    error_message='Process timed out',
                )
                yield f"data: {json.dumps({'type': 'error', 'content': '❌ Execution timed out\n'})}\n\n"
        except Exception as e:
            _append_execution_line(execution, 'error', f'❌ Execution Error: {str(e)}')
            if proc is not None and getattr(proc, 'returncode', None) is not None:
                exit_code = proc.returncode
            else:
                exit_code = -1
            _finalize_execution(
                execution,
                success=False,
                exit_code=exit_code,
                duration_seconds=time.time() - start_time,
                error_message=str(e),
            )
            yield f"data: {json.dumps({'type': 'error', 'content': f'❌ Execution Error: {str(e)}'})}\n\n"
        finally:
            if 'run_path' in locals() and run_path != full_path:
                try:
                    if os.path.exists(run_path):
                        os.remove(run_path)
                except Exception:
                    pass
            with active_processes_lock:
                if run_id in active_processes:
                    del active_processes[run_id]

    return Response(generate(), mimetype='text/event-stream')


@app.route('/api/scripts/kill', methods=['POST'])
def kill_script():
    data = request.json or {}
    run_id = data.get('run_id', '')

    if not run_id:
        return jsonify({'error': 'run_id is required'}), 400

    with active_processes_lock:
        entry = active_processes.get(run_id)
        if not entry:
            return jsonify({'error': 'No running process found for this run_id'}), 404
        proc = entry['process']
        if proc.poll() is not None:
            return jsonify({'error': 'No running process found for this run_id'}), 404
        entry['aborted'] = True

    _terminate_process_tree(proc)

    return jsonify({'success': True, 'run_id': run_id})


@app.route('/api/exec', methods=['POST'])
def exec_command():
    data = request.json
    command = data.get('command', '')

    if not command:
        return jsonify({'error': 'No command provided'}), 400

    save_command_history(command)

    shell_cmd = _find_shell()
    execution = _start_execution_record(
        kind='command',
        display_name=command,
        command_text=command,
        shell_cmd=shell_cmd,
        cwd=SCRIPTS_DIR,
    )

    def generate():
        proc = None
        start_time = time.time()
        try:
            # Need to format for Windows/Linux subshells correctly
            args = [shell_cmd, '-c', command] if shell_cmd != 'cmd.exe' else ['cmd.exe', '/c', command]
            
            proc = subprocess.Popen(
                args,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                cwd=SCRIPTS_DIR,
                bufsize=1,
                universal_newlines=True
            )
            
            for line in iter(proc.stdout.readline, ''):
                if line:
                    l_lower = line.lower()
                    msg_type = 'stdout'
                    if any(err in l_lower for err in ['error:', 'failed:', 'not found', 'denied', 'no such file']):
                        msg_type = 'error'
                    _append_execution_line(execution, msg_type, line)
                    yield f"data: {json.dumps({'type': msg_type, 'content': line})}\n\n"
                    
            proc.stdout.close()
            proc.wait(timeout=10)
            elapsed = time.time() - start_time
            _append_execution_line(execution, 'system', f'Command completed with exit code {proc.returncode}')
            _finalize_execution(
                execution,
                success=proc.returncode == 0,
                exit_code=proc.returncode,
                duration_seconds=elapsed,
            )
            yield f"data: {json.dumps({'type': 'metrics', 'exit_code': proc.returncode, 'success': proc.returncode == 0, 'duration': round(elapsed, 3)})}\n\n"
        except Exception as e:
            _append_execution_line(execution, 'error', f'❌ Command Error: {str(e)}')
            if proc is not None and getattr(proc, 'returncode', None) is not None:
                exit_code = proc.returncode
            else:
                exit_code = -1
            _finalize_execution(
                execution,
                success=False,
                exit_code=exit_code,
                duration_seconds=time.time() - start_time,
                error_message=str(e),
            )
            yield f"data: {json.dumps({'type': 'error', 'content': f'❌ Command Error: {str(e)}'})}\n\n"

    return Response(generate(), mimetype='text/event-stream')


@app.route('/api/sessions/save', methods=['POST'])
def save_session():
    data = request.json
    session_data = data.get('session', {})

    try:
        sessions = load_sessions()

        sessions['last_session'] = session_data
        sessions['last_updated'] = time.time()

        save_sessions(sessions)

        return jsonify({
            'success': True
        })

    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@app.route('/api/sessions/restore', methods=['GET'])
def restore_session():
    try:
        sessions = load_sessions()

        return jsonify({
            'success': True,
            'session': sessions.get('last_session', {})
        })

    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@app.route('/api/scripts/save', methods=['POST'])
def save_script():
    data = request.json
    category = data.get('category', '').strip()
    filename = data.get('filename', '').strip()
    content = data.get('content', '')
    provided_pass = data.get('password', '')

    if not category or not filename:
        return jsonify({'error': 'Category and filename required'}), 400

    if not filename.endswith('.sh'):
        filename += '.sh'

    category = category.replace('..', '').replace('/', '').replace('\\', '')
    filename = filename.replace('..', '').replace('/', '').replace('\\', '')
    rel_path = f'{category}/{filename}'
    
    if not check_lock(rel_path, provided_pass):
        return jsonify({'error': 'Locked', 'success': False}), 401

    cat_dir = os.path.join(SCRIPTS_DIR, category)
    os.makedirs(cat_dir, exist_ok=True)

    full_path = os.path.join(cat_dir, filename)
    with open(full_path, 'w', encoding='utf-8', newline='\n') as f:
        f.write(content)

    return jsonify({'success': True, 'path': rel_path})


@app.route('/api/scripts/delete', methods=['DELETE'])
def delete_script():
    data = request.json or {}
    rel_path = request.args.get('path', '') or data.get('path', '')
    provided_pass = data.get('password', '')
    
    if not check_lock(rel_path, provided_pass):
        return jsonify({'error': 'Locked', 'success': False}), 401
        
    full_path = os.path.join(SCRIPTS_DIR, rel_path)
    full_path = os.path.normpath(full_path)

    if not is_safe_path(SCRIPTS_DIR, full_path):
        return jsonify({'error': 'Invalid path'}), 403

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
        return jsonify({'success': True})

    return jsonify({'error': 'Script not found'}), 404


@app.route('/api/scripts/favorite', methods=['POST'])
def toggle_favorite():
    data = request.json
    rel_path = data.get('path', '')
    favs = load_favorites()

    if rel_path in favs:
        favs.remove(rel_path)
        is_fav = False
    else:
        favs.append(rel_path)
        is_fav = True

    save_favorites(favs)
    return jsonify({'favorite': is_fav})


@app.route('/api/scripts/lock', methods=['POST'])
def manage_lock():
    data = request.json
    rel_path = data.get('path', '')
    old_pass = data.get('old_password', '')
    new_pass = data.get('new_password', '') # empty string removes lock!
    
    # Verify current lock
    if not check_lock(rel_path, old_pass):
        return jsonify({'error': 'Incorrect current password', 'success': False}), 401
        
    locks = load_locks()
    if new_pass:
        locks[rel_path] = hashlib.sha256(new_pass.encode()).hexdigest()
    else:
        if rel_path in locks:
            del locks[rel_path]

    save_locks(locks)
    return jsonify({'success': True, 'locked': bool(new_pass)})


@app.route('/api/scripts/import_github', methods=['POST'])
def import_github():
    data = request.json
    url = data.get('url', '').strip()
    category = data.get('category', '').strip()
    filename = data.get('filename', '').strip()

    if not url or not category or not filename:
        return jsonify({
            'error': 'Missing fields',
            'success': False
        }), 400

    if not filename.endswith('.sh'):
        filename += '.sh'

    # Convert standard GitHub URL → raw URL
    if "github.com" in url and "/blob/" in url:
        url = (
            url.replace(
                "github.com",
                "raw.githubusercontent.com"
            )
            .replace("/blob/", "/")
        )

    # SSRF guard: only allow GitHub domains after rewrite
    _parsed = urllib.parse.urlparse(url)
    _ALLOWED = {'github.com', 'raw.githubusercontent.com'}
    _ALLOWED_SCHEMES = {'http', 'https'}
    if _parsed.scheme.lower() not in _ALLOWED_SCHEMES or _parsed.hostname not in _ALLOWED:
        return jsonify({'error': 'Only GitHub URLs are allowed', 'success': False}), 400

    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 DevShell'})
        with urllib.request.urlopen(req, timeout=10) as response:
            raw_bytes = response.read()

        # Prevent huge imports
        if len(raw_bytes) > 500000:
            return jsonify({
                'error': 'File too large (max 500KB)',
                'success': False
            }), 400

        try:
            content = raw_bytes.decode('utf-8')

        except UnicodeDecodeError:
            return jsonify({
                'error': 'Only UTF-8 text files are supported',
                'success': False
            }), 400

        # Reject binary payloads
        if '\0' in content:
            return jsonify({
                'error': 'Binary files are not supported',
                'success': False
            }), 400

    except Exception as e:
        return jsonify({
            'error': f'Failed to fetch from GitHub: {str(e)}',
            'success': False
        }), 400

    # Sanitize paths
    category = (
        category
        .replace('..', '')
        .replace('/', '')
        .replace('\\', '')
    )

    filename = (
        filename
        .replace('..', '')
        .replace('/', '')
        .replace('\\', '')
    )

    rel_path = f'{category}/{filename}'
    # Respect existing lock protection
    if not check_lock(rel_path, ''):
        return jsonify({
            'error': 'File exists and is locked!',
            'success': False
        }), 401

    cat_dir = os.path.join(SCRIPTS_DIR, category)
    os.makedirs(cat_dir, exist_ok=True)
    full_path = os.path.join(cat_dir, filename)

    with open(
        full_path,
        'w',
        encoding='utf-8',
        newline='\n'
    ) as f:
        f.write(content)

    return jsonify({
        'success': True,
        'path': rel_path
    })

# --- NEW FEATURE: Raise PR / Push to Git ---
@app.route('/api/git/pr', methods=['POST'])
def raise_pr():
    # Parse the request payload for the script path, branch, commit message, and optional target repo
    data = request.json
    rel_path = data.get('path', '')
    branch_name = data.get('branch', f'script-contribution-{str(uuid.uuid4())[:4]}')
    commit_msg = data.get('message', f'Contribution: {rel_path}')
    target_repo = data.get('target_repo', '').strip()
    
    if not rel_path:
        return jsonify({'error': 'No script path provided', 'success': False}), 400

    full_path = os.path.join(SCRIPTS_DIR, rel_path)
    full_path = os.path.normpath(full_path)

    # Security check: prevent path traversal outside scripts directory
    if not is_safe_path(SCRIPTS_DIR, full_path):
        return jsonify({'error': 'Invalid path'}), 403

    try:
        # Check if we are in a git repo
        subprocess.run(['git', 'rev-parse', '--is-inside-work-tree'], check=True, capture_output=True)
        
        # 1. Create new local branch for the contribution
        checkout_existing = subprocess.run(['git', 'checkout', branch_name], capture_output=True)
        if checkout_existing.returncode != 0:
            subprocess.run(['git', 'checkout', '-b', branch_name], check=True, capture_output=True)
        
        # 2. Stage only the specific script file
        subprocess.run(['git', 'add', full_path], check=True, capture_output=True)
        
        # 3. Commit the changes
        subprocess.run(['git', 'commit', '-m', commit_msg], check=True, capture_output=True)
        
        # 4. Push to target remote
        # If the user provided a specific target repository URL, we push directly to it.
        # Otherwise, we push to the default 'origin'.
        remote_to_push = target_repo if target_repo else 'origin'
        subprocess.run(['git', 'push', '-u', remote_to_push, branch_name], check=True, capture_output=True)
        
        # 5. Generate a GitHub PR Link
        # If an external repo URL was provided, use that to construct the base URL.
        if target_repo:
            remote_url = target_repo.replace('.git', '')
        else:
            remote_res = subprocess.run(['git', 'remote', 'get-url', 'origin'], check=True, capture_output=True, text=True)
            remote_url = remote_res.stdout.strip().replace('.git', '')
            
        if remote_url.startswith('git@github.com:'):
            remote_url = remote_url.replace('git@github.com:', 'https://github.com/')
            
        # Append the /compare path to take the user directly to the PR creation screen
        pr_url = f"{remote_url}/compare/main...{branch_name}" if "github.com" in remote_url else remote_url
        
        # 6. Switch back to the main branch to keep the workspace stable
        default_branch = get_default_branch()
        subprocess.run(['git', 'checkout', default_branch], check=True, capture_output=True)
        
        return jsonify({'success': True, 'pr_url': pr_url, 'branch': branch_name})
        
    except subprocess.CalledProcessError as e:
        err_msg = e.stderr.decode() if e.stderr else str(e)
        # Attempt recovery to main
        default_branch = get_default_branch()
        subprocess.run(['git', 'checkout', default_branch], capture_output=True)
        return jsonify({'error': err_msg, 'success': False}), 500
    except Exception as e:
        return jsonify({'error': str(e), 'success': False}), 500


# ─── Helpers ──────────────────────────────────────────────────────

def _find_shell():
    """Find available bash shell on the system."""
    import platform
    import shutil
    candidates = [
        r'C:\Program Files\Git\bin\bash.exe',
        r'C:\Program Files (x86)\Git\bin\bash.exe',
    ]
    for candidate in candidates:
        if os.path.isfile(candidate):
            return candidate

    for shell in ['bash', 'sh']:
        found = shutil.which(shell)
        if found:
            return found

    if platform.system() == 'Windows':
        return 'cmd.exe'

    return 'sh'

def get_default_branch():
    try:
        result = subprocess.run(
            ['git', 'symbolic-ref', 'refs/remotes/origin/HEAD'],
            capture_output=True,
            text=True,
            check=True
        )

        ref = result.stdout.strip()

        return ref.split('/')[-1]

    except Exception:
        return 'main'

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

if __name__ == '__main__':
    print("[*] DevShell starting on http://localhost:5000")
    print(f"[*] Scripts directory: {SCRIPTS_DIR}")
    app.run(debug=True, port=5000)
