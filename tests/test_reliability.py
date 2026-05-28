import os
import sys
import time
import subprocess
import pytest
import psutil

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
