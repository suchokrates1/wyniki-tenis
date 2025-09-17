from pathlib import Path

import pytest

from main import CONFIG_PATH, app


@pytest.fixture
def client():
    app.config.update(TESTING=True)
    with app.test_client() as client:
        yield client


@pytest.fixture(autouse=True)
def restore_config_file():
    config_path = Path(CONFIG_PATH)
    if config_path.exists():
        original_bytes = config_path.read_bytes()
    else:
        original_bytes = None

    yield

    if original_bytes is None:
        if config_path.exists():
            config_path.unlink()
    else:
        config_path.write_bytes(original_bytes)
