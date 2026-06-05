import os
import json
import pytest

from utils import validators


def test_validate_safe_path_ok(tmp_path):
    base = tmp_path / "base"
    base.mkdir()
    child = base / "subdir"
    child.mkdir()
    p = validators.validate_safe_path(str(base), "subdir/file.txt")
    assert str(base) in str(p)


def test_validate_safe_path_empty():
    with pytest.raises(ValueError):
        validators.validate_safe_path("/tmp", "")


def test_validate_safe_path_traversal(tmp_path):
    base = tmp_path / "base"
    base.mkdir()
    with pytest.raises(ValueError):
        validators.validate_safe_path(str(base), "../etc/passwd")


def test_validate_git_branch_valid():
    assert validators.validate_git_branch("feature/x") == "feature/x"


@pytest.mark.parametrize("bad", ["", "-bad", "bad..name", "bad/", "name.lock", "bad*name"])
def test_validate_git_branch_invalid(bad):
    with pytest.raises(ValueError):
        validators.validate_git_branch(bad)


def test_validate_repo_name_valid():
    assert validators.validate_repo_name("git@github.com:owner/repo.git")


@pytest.mark.parametrize("bad", ["", "-bad", "bad name", "bad$name"])
def test_validate_repo_name_invalid(bad):
    with pytest.raises(ValueError):
        validators.validate_repo_name(bad)


def test_append_and_read_jsonl(app_module, tmp_path):
    f = tmp_path / "logs.jsonl"
    records = [{"a": 1}, {"b": 2}]
    for r in records:
        app_module._append_jsonl(str(f), r)

    got = app_module._read_jsonl(str(f))
    assert len(got) == 2
    assert got[0]["a"] == 1


def test_read_jsonl_ignores_invalid_lines(app_module, tmp_path):
    f = tmp_path / "mix.jsonl"
    with open(f, "w", encoding="utf-8") as fh:
        fh.write("{\"ok\": 1}\n")
        fh.write("not a json\n")
        fh.write("{\"ok\": 2}\n")

    got = app_module._read_jsonl(str(f))
    assert len(got) == 2
    assert got[1]["ok"] == 2
