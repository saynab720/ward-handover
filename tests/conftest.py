import re

import pytest

from app import create_app


@pytest.fixture
def app():
    app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:"})
    result = app.test_cli_runner().invoke(args=["seed-demo"])
    assert result.exit_code == 0, result.output
    yield app


@pytest.fixture
def client(app):
    return app.test_client()


def get_csrf(client, path="/login"):
    html = client.get(path).get_data(as_text=True)
    return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)


@pytest.fixture
def login(client):
    def _login(email="amara.okafor@demo.nhs.test", password="demo1234", next_url=None):
        url = "/login" + (f"?next={next_url}" if next_url else "")
        return client.post(url, data={
            "email": email, "password": password, "csrf_token": get_csrf(client),
        })
    return _login
