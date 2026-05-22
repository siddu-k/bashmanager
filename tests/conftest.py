import os
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(scope="session")
def app_module(tmp_path_factory):
    data_dir = tmp_path_factory.mktemp("devshell_data")
    os.environ["DEV_SHELL_DATA_DIR"] = str(data_dir)
    sys.modules.pop("app", None)
    import app as app_module
    app_module.app.config.update({"TESTING": True})
    return app_module


@pytest.fixture
def client(app_module):
    return app_module.app.test_client()
