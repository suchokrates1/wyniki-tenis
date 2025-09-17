import pytest

from main import app, db


@pytest.fixture
def client():
    app.config.update(TESTING=True)
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
