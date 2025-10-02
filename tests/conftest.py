import base64

import pytest

from main import app, db


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
        db.drop_all()
        db.create_all()

    yield

    with app.app_context():
        db.session.remove()
        db.drop_all()
        db.engine.dispose()
