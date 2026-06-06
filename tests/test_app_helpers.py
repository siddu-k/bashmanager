import os
import json
import time
import shutil
import tempfile
from unittest.mock import patch

import pytest


def make_history_entry(id, display_name, success, duration, finished_at=None):
    return {
        "id": id,
        "display_name": display_name,
        "kind": "script",
        "success": success,
        "failure_type": None if success else "shell_error",
        "duration_seconds": duration,
        "finished_at": finished_at or "2026-06-03T00:00:00Z",
    }


def test_trim_jsonl_trims_file(app_module, tmp_path):
    f = tmp_path / "j.jsonl"
    with open(f, "w", encoding="utf-8") as fh:
        for i in range(5):
            fh.write(json.dumps({"i": i}) + "\n")

    app_module._trim_jsonl(str(f), 3)
    lines = open(f, "r", encoding="utf-8").read().strip().splitlines()
    assert len(lines) == 3


def test_parse_execution_log_metadata_reads_headers(app_module, tmp_path):
    exec_dir = tmp_path / "exec"
    exec_dir.mkdir()
    log = exec_dir / "20260603_script_test.log"
    with open(log, "w", encoding="utf-8") as fh:
        fh.write("[2026-06-03T00:00:00Z] execution started\n")
        fh.write("kind: script\n")
        fh.write("id: abc123\n")
        fh.write("display: myscript\n")
        fh.write("exit_code: 2\n")
        fh.write("status: failed\n")

    with patch.object(app_module, "EXECUTION_LOG_DIR", str(exec_dir)):
        meta = app_module._parse_execution_log_metadata(os.path.basename(str(log)))
    assert meta is not None
    assert meta.get("status") == "failed"
    assert meta.get("exit_code") == 2


def test_start_and_append_excerpt_trimming(app_module, tmp_path):
    exec_dir = tmp_path / "exec"
    sess_dir = tmp_path / "sessions"
    exec_dir.mkdir()
    sess_dir.mkdir()

    with patch.object(app_module, "EXECUTION_LOG_DIR", str(exec_dir)), patch.object(app_module, "SESSION_LOG_DIR", str(sess_dir)), patch.object(app_module, "MAX_HISTORY_EXCERPT_CHARS", 50):
        execution = app_module._start_execution_record(kind="script", display_name="long", command_text="echo")
        for i in range(10):
            app_module._append_execution_line(execution, "stdout", "line-with-data-" + str(i) + "\n")
        assert execution["excerpt_size"] <= 50


def test_record_reliability_and_rebuild_summary(app_module, tmp_path):
    hist_file = tmp_path / "history.jsonl"
    failed_file = tmp_path / "failed.jsonl"
    rel_dir = tmp_path / "reliability"
    rel_dir.mkdir()
    session_dir = tmp_path / "sessions"
    exec_dir = tmp_path / "exec"
    session_dir.mkdir()
    exec_dir.mkdir()

    entries = [make_history_entry(f"id{i}", "myscript", i % 2 == 0, i * 0.5) for i in range(1, 6)]
    with open(hist_file, "w", encoding="utf-8") as fh:
        for e in entries:
            fh.write(json.dumps(e) + "\n")

    with patch.object(app_module, "HISTORY_FILE", str(hist_file)), patch.object(app_module, "FAILED_HISTORY_FILE", str(failed_file)), patch.object(app_module, "RELIABILITY_DIR", str(rel_dir)), patch.object(app_module, "RELIABILITY_EVENTS_FILE", str(rel_dir / "events.jsonl")), patch.object(app_module, "RELIABILITY_SUMMARY_FILE", str(rel_dir / "summary.json")), patch.object(app_module, "SESSION_LOG_DIR", str(session_dir)), patch.object(app_module, "EXECUTION_LOG_DIR", str(exec_dir)):
        summary = app_module._rebuild_reliability_summary()
        assert isinstance(summary, dict)
        assert "scripts" in summary
        total_runs = summary.get("global", {}).get("total_runs")
        assert total_runs == len(entries)


def test_append_and_read_jsonl(app_module, tmp_path):
    f = tmp_path / "a.jsonl"
    app_module._append_jsonl(str(f), {"x": 1})
    app_module._append_jsonl(str(f), {"x": 2})
    entries = app_module._read_jsonl(str(f))
    assert isinstance(entries, list)
    assert len(entries) == 2
    assert entries[0]["x"] == 1


def test_classify_failure_various_messages(app_module):
    assert app_module._classify_failure(2, error_message="syntax error: foo") == "shell_error"
    assert app_module._classify_failure(127, output="command not found") == "dependency_error"
    assert app_module._classify_failure(0, output="weird") == "unknown_failure"


def test_safe_load_json_and_isolate(app_module, tmp_path):
    f = tmp_path / "bad.json"
    f.write_text("notjson")
    with patch.object(app_module, "WORKSPACE_DIR", str(tmp_path)):
        res = app_module._safe_load_json(str(f), default={})
    assert res == {}
    corrupted = str(f) + ".corrupted"
    assert (not os.path.exists(str(f))) or os.path.exists(corrupted)


def test_session_record_from_file_and_diagnostics(app_module, tmp_path):
    sess = tmp_path / "s.json"
    data = {"metadata": {"id": "i1", "display_name": "d1", "finished_at": "2026-06-03T00:00:00Z", "success": True}}
    sess.write_text(json.dumps(data))
    with patch.object(app_module, "SESSION_LOG_DIR", str(tmp_path)):
        record = app_module._session_record_from_file(str(sess.name))
        assert record is not None
        diag = app_module._diagnose_session_data(data)
        assert isinstance(diag, dict)


def test_save_and_load_command_history(app_module, tmp_path):
    cmd_file = tmp_path / "commands.json"
    with patch.object(app_module, "COMMAND_HISTORY_FILE", str(cmd_file)):
        app_module.save_command_history("  ")
        app_module.save_command_history("one")
        app_module.save_command_history("two")
        app_module.save_command_history("one")
        h = app_module.load_command_history()
        assert h[0] == "one" or h[0] == "two"


def test_isolate_corrupted_file_no_exist(app_module, tmp_path):
    f = tmp_path / "nope.json"
    app_module._isolate_corrupted_file(str(f))
