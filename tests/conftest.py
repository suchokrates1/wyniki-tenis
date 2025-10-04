import base64

import copy

import pytest

from main import app, db
import results


@pytest.fixture
def auth_headers(monkeypatch):
    username = "test-user"
    password = "test-pass"
    monkeypatch.setenv("CONFIG_AUTH_USERNAME", username)
    monkeypatch.setenv("CONFIG_AUTH_PASSWORD", password)
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


@pytest.fixture
def client():
    app.config.update(TESTING=True, SECRET_KEY="test-secret-key")
    with app.test_client() as client:
        yield client


@pytest.fixture(autouse=True)
def restore_config_file(tmp_path):
    database_path = tmp_path / "overlay.sqlite"
    app.config.update(SQLALCHEMY_DATABASE_URI=f"sqlite:///{database_path}")

    with app.app_context():
        db.session.remove()
        db.engine.dispose()
        if database_path.exists():
            database_path.unlink()
        db.drop_all()
        db.create_all()

    yield

    with app.app_context():
        db.session.remove()
        db.drop_all()
        db.engine.dispose()


@pytest.fixture
def snapshot_injector():
    def apply(entries: dict[str, dict] | list[dict], *, clear: bool = True) -> None:
        if isinstance(entries, list):
            mapping: dict[str, dict] = {}
            for entry in entries:
                kort_id = entry.get("kort_id")
                if kort_id is None:
                    continue
                mapping[str(kort_id)] = copy.deepcopy(entry)
        else:
            mapping = {str(key): copy.deepcopy(value) for key, value in entries.items()}

        with results.snapshots_lock:
            if clear:
                results.snapshots.clear()
            results.snapshots.update(mapping)

    yield apply

    with results.snapshots_lock:
        results.snapshots.clear()
