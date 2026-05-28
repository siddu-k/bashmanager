# Testing Guide

This repository uses `pytest` for backend testing. The current test layout is centered on the Flask app in `app.py` and the fixtures in `tests/conftest.py`, which create an isolated temporary data directory through `DEV_SHELL_DATA_DIR`.

## Setup

Use a virtual environment and install the project dependencies first:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

If you want coverage reports, install the coverage tooling in the same environment:

```bash
pip install pytest-cov coverage
```

## Run the test suite

Run all tests:

```bash
python -m pytest
```

Run the suite in quiet mode:

```bash
python -m pytest -q
```

Run a specific file while iterating on one area:

```bash
python -m pytest tests/test_api_basic.py -q
python -m pytest tests/test_security.py -q
python -m pytest tests/test_sse.py -q
```

Stop on the first failure when debugging:

```bash
python -m pytest --maxfail=1 -q
```

## Coverage

Generate a terminal coverage summary:

```bash
python -m pytest --cov=app --cov=utils --cov-report=term-missing
```

Generate an HTML report:

```bash
python -m pytest --cov=app --cov=utils --cov-report=html
```

If you prefer the standalone `coverage` command, use:

```bash
coverage run -m pytest
coverage report -m
coverage html
```

The most useful areas to watch for coverage growth are:

- request handlers in `app.py`
- validation helpers in `utils/validators.py`
- lock/password flows
- SSE stream behavior and error handling
- GitHub import and Git PR routes

## Smoke tests

Use these quick checks when you want to verify the app still starts and the main endpoints respond:

```bash
python app.py
```

Then in another terminal:

```bash
curl http://127.0.0.1:5000/api/workspace
curl http://127.0.0.1:5000/api/scripts
```

If you are validating the streaming path, the repository already includes an SSE-style test in `tests/test_sse.py` that exercises script execution and kill handling.

For the Electron wrapper, smoke test with:

```bash
npm start
```

## How to write tests here

- Keep tests isolated. The existing fixture in `tests/conftest.py` points `DEV_SHELL_DATA_DIR` at a temporary directory so tests do not touch real user data.
- Prefer `tmp_path` and `tmp_path_factory` for filesystem work.
- Test Flask routes with `app.test_client()`.
- Assert both success cases and failure cases, especially for validation and security-sensitive paths.
- Add focused unit tests for pure helpers before adding broader route tests.

Example patterns that fit this repo:

- use `monkeypatch` or `unittest.mock.patch` for filesystem paths and environment variables
- patch `subprocess.run` and `subprocess.Popen` for process execution code
- patch `urllib.request.urlopen` for GitHub import tests
- patch `shutil.which`, `os.path.exists`, and similar OS-dependent helpers when needed

## Mock or sandbox dangerous operations

Do not let tests run real Git commands, spawn uncontrolled subprocesses, or contact live remote services unless a test is explicitly designed for that and runs in a sandbox.

For this repository, the risky areas are:

- subprocess execution and streaming
- the Git PR flow in `/api/git/pr`
- GitHub import requests in `/api/scripts/import_github`

Use mocks for those paths and assert the command arguments instead of executing them. For example, patch `subprocess.run` so you can verify the command list, return code handling, and cleanup logic without touching the real repository state.

If you need to exercise a Git flow end to end, do it inside a throwaway temporary directory with a fake repository, not in the project root and not against a real remote.

## Improving coverage

Good next targets for coverage additions:

- edge cases in `validate_safe_path`, `validate_git_branch`, and `validate_repo_name`
- malformed lock data and password migration paths
- error responses from the Git PR endpoint
- SSE abort, timeout, and cleanup behavior
- import and JSON parsing failures for external resources

When adding tests, keep them small and focused. One behavior per test usually makes failures easier to understand and keeps coverage reports more useful.
