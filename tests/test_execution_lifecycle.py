import os
import time
import subprocess
import json
from unittest.mock import Mock, patch


def test_cleanup_execution_when_execution_is_none_removes_temp_and_terminates(app_module, tmp_path):
    temp = tmp_path / "tmp_run.sh"
    temp.write_text("echo hi")

    class FakeProc:
        def __init__(self):
            self.killed = False

        def poll(self):
            return None

    proc = FakeProc()

    called = {}

    def fake_terminate(p):
        called['terminated'] = True

    with patch.object(app_module, "_terminate_process_tree", fake_terminate):
        app_module._cleanup_execution(proc, None, run_id="r1", temp_path=str(temp))

    assert called.get('terminated') is True
    assert not os.path.exists(str(temp))


def test_cleanup_execution_full_flow_calls_finalize_and_closes_handle(app_module, tmp_path):
    # prepare fake proc with streams
    class FakeStream:
        def close(self):
            self.closed = True

    class FakeProc:
        def __init__(self):
            self.pid = 9999
            self.stdout = FakeStream()
            self.stderr = FakeStream()
            self.returncode = 1

        def poll(self):
            return None

    proc = FakeProc()

    # create an open file handle for execution['handle']
    logf = tmp_path / "log.txt"
    h = open(logf, "w", encoding="utf-8")

    execution = {
        "record": {"status": "running", "id": "x"},
        "handle": h,
        "monotonic_start": time.perf_counter() - 0.5,
    }

    # put an active process to be removed
    with app_module.active_processes_lock:
        app_module.active_processes["r2"] = {"process": proc}

    called = {}

    def fake_finalize(execution_obj, success, exit_code, duration_seconds, **kwargs):
        called['finalized'] = True
        return {"id": "x"}

    reader = Mock()
    reader.join.side_effect = None

    with patch.object(app_module, "_finalize_execution", fake_finalize):
        app_module._cleanup_execution(proc, execution, run_id="r2", temp_path=None, was_aborted=False, error_message="err", exit_code=2, stop_event=None, reader_thread=reader)

    assert called.get('finalized') is True
    assert h.closed
    with app_module.active_processes_lock:
        assert "r2" not in app_module.active_processes


def test_exec_command_timeout_triggers_cleanup_and_yields_error(app_module, client, tmp_path):
    # Patch check_lock to allow execution
    with patch.object(app_module, "check_lock", lambda *a, **k: True), patch.object(app_module, "_find_shell", lambda: "sh"):
        # fake Popen that has stdout.readline returning empty string so reader ends, and wait raises TimeoutExpired
        class FakePopen:
            def __init__(self, *a, **k):
                class Out:
                    def readline(self):
                        return ""

                self.stdout = Out()
                self.stderr = None
                self.returncode = None
                self.pid = 12345

            def poll(self):
                return None

            def wait(self, timeout=None):
                raise subprocess.TimeoutExpired(cmd="x", timeout=timeout)

        with patch('app.subprocess.Popen', FakePopen):
            resp = client.post('/api/exec', json={"command": "echo hi", "password": ""})
            data = b"".join(resp.response).decode('utf-8')
            assert "Execution timed out" in data or "❌ Execution timed out" in data
