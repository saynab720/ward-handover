from app.db import db
from app.models import AuditLog, Staff

from .conftest import get_csrf


def test_pages_need_login(client):
    for path in ["/", "/wards/W12/handover", "/handovers/1"]:
        response = client.get(path)
        assert response.status_code == 302
        assert response.headers["Location"].startswith("/login")


def test_login_shows_wards(client, login):
    response = login()
    assert response.status_code == 302
    page = client.get("/").get_data(as_text=True)
    assert "Elderly Care" in page
    assert "Amara Okafor" in page


def test_wrong_password_is_rejected(client, login):
    response = login(password="wrong")
    assert response.status_code == 200
    assert "Email or password is incorrect" in response.get_data(as_text=True)
    assert client.get("/").status_code == 302


def test_unknown_email_gets_same_message(client, login):
    response = login(email="nobody@demo.nhs.test")
    assert "Email or password is incorrect" in response.get_data(as_text=True)


def test_email_is_case_insensitive(client, login):
    login(email="Amara.Okafor@DEMO.nhs.test")
    assert client.get("/").status_code == 200


def test_inactive_staff_cannot_log_in(app, client, login):
    with app.app_context():
        staff = db.session.scalar(db.select(Staff).where(Staff.email == "tom.hughes@demo.nhs.test"))
        staff.is_active = False
        db.session.commit()
    login(email="tom.hughes@demo.nhs.test")
    assert client.get("/").status_code == 302


def test_redirects_back_to_requested_page(login):
    response = login(next_url="/handovers/1")
    assert response.headers["Location"] == "/handovers/1"


def test_does_not_redirect_to_other_sites(app):
    for bad in ["https://evil.example", "//evil.example", "/\\evil.example"]:
        client = app.test_client()  # fresh session for each attempt
        response = client.post(f"/login?next={bad}", data={
            "email": "amara.okafor@demo.nhs.test", "password": "demo1234",
            "csrf_token": get_csrf(client),
        })
        assert response.headers["Location"] == "/", bad


def test_login_form_needs_csrf_token(client):
    response = client.post("/login", data={
        "email": "amara.okafor@demo.nhs.test", "password": "demo1234",
    })
    assert response.status_code == 400


def test_logout(client, login):
    login()
    response = client.post("/logout", data={"csrf_token": get_csrf(client, "/")})
    assert response.status_code == 302
    assert client.get("/").status_code == 302


def test_logout_needs_post(client, login):
    login()
    assert client.get("/logout").status_code == 405


def test_logins_are_audited(app, login):
    login(password="wrong")
    login()
    with app.app_context():
        actions = db.session.scalars(db.select(AuditLog.action).order_by(AuditLog.id)).all()
    assert actions == ["login_failed", "login"]
