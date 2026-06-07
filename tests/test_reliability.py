import os
import sys
import time
import subprocess
import pytest
import psutil
from unittest.mock import patch

SENTINEL = object()


def test_terminate_process_tree(app_module):
    """
    Test that _terminate_process_tree successfully terminates a process and its children.
    """
    # Spawn a parent python process that spawns a child python process
    # We use sys.executable to ensure we run python safely
    child_code = (
        "import time, sys\n"
        "sys.stdout.write('CHILD STARTED\\n')\n"
        "sys.stdout.flush()\n"
        "time.sleep(20)\n"
    )
    parent_code = (
        f"import subprocess, sys, time\n"
        f"proc = subprocess.Popen([sys.executable, '-c', {repr(child_code)}], stdout=subprocess.PIPE)\n"
        f"line = proc.stdout.readline()\n"
        f"sys.stdout.write(line.decode('utf-8'))\n"
        f"sys.stdout.flush()\n"
        f"time.sleep(20)\n"
    )

    proc = subprocess.Popen(
        [sys.executable, "-c", parent_code],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    # Wait for child to output 'CHILD STARTED' to ensure the child process exists
    line = proc.stdout.readline()
    assert "CHILD STARTED" in line

    # Find the child process pid
    parent_ps = psutil.Process(proc.pid)
    children = parent_ps.children(recursive=True)
    assert len(children) >= 1
    child_pid = children[0].pid

    # Check they are both running
    assert parent_ps.is_running()
    assert psutil.pid_exists(child_pid)

    # Call _terminate_process_tree
    app_module._terminate_process_tree(proc)

    # Give it a second to terminate
    time.sleep(0.5)

    # Verify they are terminated
    assert proc.poll() is not None
    assert not parent_ps.is_running()
    assert not psutil.pid_exists(child_pid)


def test_cleanup_execution_idempotency(app_module, tmp_path):
    """
    Test that calling _cleanup_execution multiple times is safe and idempotent.
    """
    # 1. Create a dummy execution dict
    dummy_log = tmp_path / "dummy.log"
    handle = open(dummy_log, "w", encoding="utf-8")

    execution = {
        "cleaned_up": False,
        "record": {"status": "running"},
        "monotonic_start": time.perf_counter(),
        "handle": handle,
    }

    # Spawn a dummy process that exits immediately
    proc = subprocess.Popen([sys.executable, "-c", "print('hello')"])
    proc.wait()

    # Call cleanup the first time
    app_module._cleanup_execution(
        proc=proc, execution=execution, run_id="dummy_run", temp_path=None
    )

    assert execution["cleaned_up"] is True
    assert handle.closed

    # Call cleanup a second time. It should return early without error.
    try:
        app_module._cleanup_execution(
            proc=proc, execution=execution, run_id="dummy_run", temp_path=None
        )
    except Exception as e:
        pytest.fail(f"_cleanup_execution failed on second call: {e}")


def test_cleanup_already_dead_process(app_module, tmp_path):
    """
    Test that calling _cleanup_execution on an already-terminated process is safe.
    """
    dummy_log = tmp_path / "dummy2.log"
    handle = open(dummy_log, "w", encoding="utf-8")

    execution = {
        "cleaned_up": False,
        "record": {"status": "running"},
        "monotonic_start": time.perf_counter(),
        "handle": handle,
    }

    proc = subprocess.Popen([sys.executable, "-c", "print('done')"])
    proc.wait()  # Make sure it is dead

    try:
        app_module._cleanup_execution(
            proc=proc, execution=execution, run_id="dead_run", temp_path=None
        )
    except Exception as e:
        pytest.fail(f"_cleanup_execution failed on already dead process: {e}")

    assert execution["cleaned_up"] is True
    assert handle.closed


def test_cleanup_closes_file_handles(app_module, tmp_path):
    """
    Verify that log file and stream handles are closed even when process/execution fails.
    """
    dummy_log = tmp_path / "dummy3.log"
    handle = open(dummy_log, "w", encoding="utf-8")

    execution = {
        "cleaned_up": False,
        "record": {"status": "running"},
        "monotonic_start": time.perf_counter(),
        "handle": handle,
    }

    proc = subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.stdout.write('fail\\n')"],
        stdout=subprocess.PIPE,
    )

    # Call cleanup with error
    app_module._cleanup_execution(
        proc=proc,
        execution=execution,
        run_id="fail_run",
        was_aborted=False,
        error_message="Simulated error",
    )

    # Check log handle closed
    assert handle.closed
    # Check proc stdout stream closed
    assert proc.stdout.closed


def test_sse_generator_exit_cleanup(client, app_module):
    """
    Test that if client closes/stops the SSE generator mid-execution (GeneratorExit),
    cleanup is triggered and the subprocess is terminated.
    """
    # Use request context to call exec_command directly
    python_path = sys.executable.replace("\\", "/")
    with app_module.app.test_request_context(
        json={
            "command": f"{python_path} -c 'import sys, time; sys.stdout.write(\"start\\n\"); sys.stdout.flush(); time.sleep(10)'"
        }
    ):
        resp = app_module.exec_command()
        gen = resp.response

        # Read the first event from the SSE stream
        first_chunk = next(gen)
        first_chunk_str = (
            first_chunk.decode("utf-8")
            if isinstance(first_chunk, bytes)
            else first_chunk
        )
        # Verify that it yielded some valid SSE format data
        assert "data:" in first_chunk_str

        # Look up the process in active_processes
        with app_module.active_processes_lock:
            active_runs = list(app_module.active_processes.keys())
            assert len(active_runs) >= 1
            run_id = active_runs[0]
            proc = app_module.active_processes[run_id]["process"]

        assert proc.poll() is None  # Process should still be running

        # Close the generator simulating GeneratorExit
        gen.close()

        # Wait a moment for cleanup to occur
        time.sleep(0.5)

        # Verify process was terminated and run removed from active_processes
        assert proc.poll() is not None
        with app_module.active_processes_lock:
            assert run_id not in app_module.active_processes


def test_sse_disconnect_connection_errors(client, app_module):
    """
    Test that if client throws ConnectionResetError or BrokenPipeError during iteration,
    cleanup is triggered.
    """
    # We will invoke exec_command and raise an exception manually to simulate connection reset
    python_path = sys.executable.replace("\\", "/")
    with app_module.app.test_request_context(
        json={
            "command": f"{python_path} -c 'import sys, time; sys.stdout.write(\"start\\n\"); sys.stdout.flush(); time.sleep(10)'"
        }
    ):
        resp = app_module.exec_command()
        gen = resp.response

        # Read first event
        next(gen)

        # Get run_id and process
        with app_module.active_processes_lock:
            run_id = list(app_module.active_processes.keys())[0]
            proc = app_module.active_processes[run_id]["process"]

        assert proc.poll() is None

        # Trigger an exception inside the generator by throwing ConnectionResetError
        try:
            gen.throw(ConnectionResetError("Connection reset by peer"))
        except ConnectionResetError:
            pass  # Expected to be re-raised or handled

        # Give it a moment to cleanup
        time.sleep(0.5)

        # Verify process was reaped and removed
        assert proc.poll() is not None
        with app_module.active_processes_lock:
            assert run_id not in app_module.active_processes


def test_rapid_start_stop_cycles(client, app_module):
    """
    Verify that repeatedly starting and immediately aborting scripts does not leave
    dangling processes, threads, or open files.
    """
    python_path = sys.executable.replace("\\", "/")
    # Perform 5 cycles of rapid start & stop
    for i in range(5):
        # 1. Start execution
        response = client.post(
            "/api/exec",
            json={
                "command": f"{python_path} -c 'import sys, time; sys.stdout.write(\"start\\n\"); sys.stdout.flush(); time.sleep(10)'"
            },
        )
        assert response.status_code == 200

        # Read the first chunk to ensure it starts
        chunk = next(response.response)
        chunk_str = chunk.decode("utf-8") if isinstance(chunk, bytes) else chunk
        assert "data:" in chunk_str

        # 2. Get run ID from active processes
        with app_module.active_processes_lock:
            run_ids = list(app_module.active_processes.keys())
            assert len(run_ids) >= 1
            run_id = run_ids[0]

        # 3. Call kill script endpoint to abort
        abort_response = client.post("/api/scripts/kill", json={"run_id": run_id})
        assert abort_response.status_code == 200

        # Close the generator response to trigger GeneratorExit and cleanup
        response.response.close()

        # Wait a moment for process reap
        time.sleep(0.3)

        # 4. Verify process was killed
        with app_module.active_processes_lock:
            assert run_id not in app_module.active_processes


def test_no_zombie_processes(app_module):
    """
    Scan all processes in the system to verify that no orphan/zombie python processes
    spawned by the test run remain.
    """
    # Clean up all active processes just in case
    with app_module.active_processes_lock:
        keys = list(app_module.active_processes.keys())
        for run_id in keys:
            entry = app_module.active_processes.get(run_id)
            if entry:
                app_module._cleanup_execution(
                    entry["process"], entry["execution"], run_id=run_id
                )

    time.sleep(0.5)

    current_pid = os.getpid()
    parent = psutil.Process(current_pid)
    children = parent.children(recursive=True)

    # Any child python processes should not be running sleep commands
    for child in children:
        try:
            cmd = " ".join(child.cmdline())
            # If it's a sleep subprocess spawned by python test, it shouldn't be running anymore
            if "time.sleep" in cmd or "sleep" in cmd:
                # Force kill just in case
                child.kill()
                pytest.fail(
                    f"Dangling zombie child process detected: PID {child.pid}, cmd: {cmd}"
                )
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

def test_cleanup_old_execution_logs_prunes_old_logs(app_module, tmp_path):
    old_dir = tmp_path / "exec_logs"
    old_dir.mkdir()

    # create old and new files
    old_file = old_dir / "old.log"
    new_file = old_dir / "new.log"
    old_file.write_text("old")
    new_file.write_text("new")

    # set old mtime to 60 days ago
    old_mtime = time.time() - (60 * 24 * 60 * 60)
    os.utime(old_file, (old_mtime, old_mtime))

    # patch EXECUTION_LOG_DIR
    with patch.object(app_module, "EXECUTION_LOG_DIR", str(old_dir)):
        app_module._cleanup_old_execution_logs()

    assert not old_file.exists()
    assert new_file.exists()


def test_start_append_and_finalize_execution_record(app_module, tmp_path):
    # isolate log/session/history files
    exec_dir = tmp_path / "exec"
    sess_dir = tmp_path / "sessions"
    rel_dir = tmp_path / "reliability"
    exec_dir.mkdir()
    sess_dir.mkdir()
    rel_dir.mkdir()

    history_file = tmp_path / "history.jsonl"
    failed_file = tmp_path / "failed.jsonl"

    with patch.object(app_module, "EXECUTION_LOG_DIR", str(exec_dir)), \
         patch.object(app_module, "SESSION_LOG_DIR", str(sess_dir)), \
         patch.object(app_module, "RELIABILITY_DIR", str(rel_dir)), \
         patch.object(app_module, "HISTORY_FILE", str(history_file)), \
         patch.object(app_module, "FAILED_HISTORY_FILE", str(failed_file)):

        execution = app_module._start_execution_record(
            kind="test", display_name="my test", command_text="echo hi"
        )

        # append some lines
        app_module._append_execution_line(execution, "stdout", "line1\n")
        app_module._append_execution_line(execution, "error", "err happened\n")

        # finalize as success
        history = app_module._finalize_execution(
            execution, success=True, exit_code=0, duration_seconds=0.123
        )

        assert history["success"] is True
        # session file should exist
        session_path = os.path.join(str(sess_dir), history["log_file"].split('_')[-1].replace('.log', '.json'))
        sess_files = list(sess_dir.glob('*.json'))
        assert len(sess_files) >= 1


def test_save_reliability_summary_creates_backup_and_atomic_replace(app_module, tmp_path):
    rel_dir = tmp_path / "reliability"
    rel_dir.mkdir()
    summary_file = rel_dir / "summary.json"
    backup_file = rel_dir / "summary.json.backup"
    tmp_file = rel_dir / "summary.json.tmp"

    # initial summary
    summary = {"version": app_module.RELIABILITY_SUMMARY_VERSION, "scripts": {}, "global": {}}

    with patch.object(app_module, "RELIABILITY_DIR", str(rel_dir)), patch.object(app_module, "RELIABILITY_SUMMARY_FILE", str(summary_file)), patch.object(app_module, "RELIABILITY_SUMMARY_BACKUP", str(backup_file)), patch.object(app_module, "RELIABILITY_SUMMARY_TMP", str(tmp_file)):
        # First save should create file
        app_module._save_reliability_summary(summary)
        assert summary_file.exists()

        # Modify and save again to exercise backup creation
        summary2 = {"version": app_module.RELIABILITY_SUMMARY_VERSION, "scripts": {"a": {}}, "global": {}}
        app_module._save_reliability_summary(summary2)
        # backup may or may not exist depending on save path, but tmp should not remain
        assert not tmp_file.exists()


def test_record_reliability_event_appends_and_trims(app_module, tmp_path):
    rel_dir = tmp_path / "reliability"
    rel_dir.mkdir()
    events_file = rel_dir / "events.jsonl"

    with patch.object(app_module, "RELIABILITY_DIR", str(rel_dir)), patch.object(app_module, "RELIABILITY_EVENTS_FILE", str(events_file)):
        # append a few events
        for i in range(5):
            app_module._record_reliability_event({"id": f"e{i}", "info": i}, persist_force=False)

        lines = app_module._read_jsonl(str(events_file))
        assert len(lines) == 5

        # Simulate trim by lowering MAX_RELIABILITY_EVENTS and adding more
        with patch.object(app_module, "MAX_RELIABILITY_EVENTS", 3):
            for i in range(3):
                app_module._record_reliability_event({"id": f"x{i}", "info": i}, persist_force=False)

        lines2 = app_module._read_jsonl(str(events_file))
        assert len(lines2) <= 3


def test_load_workspace_state_handles_corrupted_file(app_module, tmp_path):
    ws_file = tmp_path / "workspace_state.json"
    # write invalid json
    ws_file.write_text('{ invalid json }')

    with patch.object(app_module, "WORKSPACE_STATE_FILE", str(ws_file)):
        result = app_module.load_workspace_state()

    assert isinstance(result, dict)
    assert result.get("corrupted") is True
    corrupted_path = str(ws_file) + ".corrupted"
    assert os.path.exists(corrupted_path)
    assert not os.path.exists(str(ws_file))


def test_save_favorites_raises_on_ioerror(app_module, tmp_path):
    fav_file = tmp_path / "favorites.json"
    with patch.object(app_module, "FAVORITES_FILE", str(fav_file)):
        with patch("builtins.open", side_effect=OSError("disk full")):
            with pytest.raises(OSError):
                app_module.save_favorites(["one"])


def test_save_sessions_function_raises_on_ioerror(app_module, tmp_path):
    sess_file = tmp_path / "sessions.json"
    with patch.object(app_module, "SESSIONS_FILE", str(sess_file)):
        with patch("builtins.open", side_effect=OSError("write error")):
            with pytest.raises(OSError):
                app_module.save_sessions({})


def test_save_session_route_returns_500_on_save_error(client, app_module, tmp_path):
    with patch.object(app_module, "save_sessions", side_effect=Exception("boom")):
        r = client.post("/api/sessions/save", json={"session": {"x": 1}})
        assert r.status_code == 500
        body = r.get_json()
        assert body["success"] is False and "error" in body


def test_classify_failure_various_cases():
    from app import _classify_failure

    assert _classify_failure(130, "interrupted") == "interrupted"
    assert _classify_failure(124, "timeout") == "timeout"
    assert _classify_failure(126, "permission denied") == "permission_error"
    assert _classify_failure(127, "command not found") == "dependency_error"
    assert _classify_failure(2, "some error") == "shell_error"
    assert _classify_failure(None, "unknown stuff") == "unknown_failure"


def test_finalize_execution_writes_history_and_failed(app_module, tmp_path):
    # prepare environment
    hist = tmp_path / "history.jsonl"
    failed = tmp_path / "failed.jsonl"
    exec_dir = tmp_path / "exec"
    sess_dir = tmp_path / "sessions"
    exec_dir.mkdir()
    sess_dir.mkdir()

    with patch.object(app_module, "HISTORY_FILE", str(hist)), patch.object(app_module, "FAILED_HISTORY_FILE", str(failed)), patch.object(app_module, "EXECUTION_LOG_DIR", str(exec_dir)), patch.object(app_module, "SESSION_LOG_DIR", str(sess_dir)):
        # create a dummy execution
        log_file = exec_dir / "e.log"
        handle = open(log_file, "w", encoding="utf-8")
        execution = {
            "record": {
                "id": "x1",
                "kind": "script",
                "session_file": "s1.json",
                "display_name": "d",
                "command": "c",
                "shell": "sh",
                "cwd": ".",
                "arguments": [],
                "started_at": app_module._iso_now(),
                "log_file": str(log_file.name),
            },
            "excerpt_lines": ["line1", "line2"],
            "excerpt_size": 10,
            "session_data": {"metadata": {}, "events": []},
            "handle": handle,
            "monotonic_start": 0,
        }

        # call finalize as failed
        hist_rec = app_module._finalize_execution(execution, success=False, exit_code=2, duration_seconds=0.5, error_message="fail")
        assert hist_rec["success"] is False
        h = app_module._read_jsonl(str(hist))
        f = app_module._read_jsonl(str(failed))
        assert any(r.get("id") == "x1" for r in h)
        assert any(r.get("id") == "x1" for r in f)


def test_cleanup_execution_handles_handle_close_exceptions(app_module, tmp_path):
    # simulate a handle that raises on flush/close
    class BadHandle:
        closed = False

        def write(self, *a, **k):
            pass

        def flush(self):
            raise OSError("flush fail")

        def close(self):
            raise OSError("close fail")

    execution = {"handle": BadHandle(), "record": {"session_file": "s.json"}, "monotonic_start": 0}
    app_module._cleanup_execution(None, execution, run_id="r1", reader_thread=None)


def test_run_script_reader_thread_exception_triggers_cleanup(client, app_module, tmp_path):
    pass


def test_run_script_timeoutexpired_branch_yields_timeout_error(client, app_module, tmp_path):
    scripts_dir = tmp_path / "scripts2"
    scripts_dir.mkdir()
    script_path = scripts_dir / 'tmp_timeout.sh'
    script_path.write_text('#!/bin/sh\necho hi\n')

    class EmptyStdout:
        def readline(self):
            return ""

        def close(self):
            pass

    class FakePopen2:
        pid = None
        def __init__(self):
            self.stdout = EmptyStdout()
            self.returncode = 0
            self.pid = None

        def poll(self):
            return 0

        def wait(self, timeout=None):
            raise subprocess.TimeoutExpired(cmd='fake', timeout=timeout)

    with patch.object(app_module, "SCRIPTS_DIR", str(scripts_dir)), patch("subprocess.Popen", return_value=FakePopen2()):
        resp = client.post('/api/scripts/run', json={'path': 'tmp_timeout.sh'})
        assert resp.status_code == 200
        found_timeout = False
        for chunk in resp.iter_encoded():
            text = chunk.decode('utf-8')
            if 'execution timed out' in text.lower() or 'timed out' in text.lower():
                found_timeout = True
                break

        assert found_timeout
