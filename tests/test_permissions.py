"""Each role against each action, following the table in app/permissions.py."""
import pytest

from app.db import db
from app.models import Admission, AuditLog, Handover, HandoverStatus, Patient, Staff, StaffRole, Task
from app.permissions import ROLE_PERMISSIONS

from .conftest import get_csrf

ACCOUNTS = {
    "hca": "dan.morris@demo.nhs.test",
    "nurse": "tom.hughes@demo.nhs.test",
    # Sam (AMU), not Amara: Amara gave today's demo handover, which changes what she can complete
    "nurse_in_charge": "sam.patel@demo.nhs.test",
    "doctor": "li.chen@demo.nhs.test",
    "ward_manager": "grace.adeyemi@demo.nhs.test",
    "admin": "admin@demo.nhs.test",
}

CLINICAL = {"hca", "nurse", "nurse_in_charge", "doctor", "ward_manager"}
WRITERS = {"nurse", "nurse_in_charge", "doctor", "ward_manager"}
LEADS = {"nurse_in_charge", "ward_manager"}


@pytest.fixture
def demo(app):
    with app.app_context():
        draft = db.session.scalar(db.select(Handover).where(Handover.status == HandoverStatus.DRAFT))
        entry = draft.entries[0]
        return {
            "draft": draft.id,
            "entry": entry.id,
            "task": db.session.scalar(db.select(Task.id).where(Task.description == "Repeat ABG")),
            "admission": db.session.scalar(
                db.select(Admission.id).join(Patient).where(Patient.last_name == "Briggs")),
            "tom": db.session.scalar(db.select(Staff.id).where(Staff.email == "tom.hughes@demo.nhs.test")),
        }


def sign_in(app, role):
    client = app.test_client()
    client.post("/login", data={"email": ACCOUNTS[role], "password": "demo1234",
                                "csrf_token": get_csrf(client)})
    return client


def post(client, url, data=None):
    return client.post(url, data={**(data or {}), "csrf_token": get_csrf(client, "/")})


ACTIONS = {
    # name: (allowed roles, request, status when allowed)
    "view handover": (CLINICAL, lambda c, d: c.get(f"/handovers/{d['draft']}"), 200),
    "view patient list": (CLINICAL, lambda c, d: c.get("/wards/W12/patients"), 200),
    "tick off task": (CLINICAL, lambda c, d: post(c, f"/handovers/{d['draft']}/tasks/{d['task']}/complete"), 302),
    "add task": (WRITERS, lambda c, d: post(c, f"/handovers/{d['draft']}/entries/{d['entry']}/tasks",
                                          {"description": "Check obs", "priority": "routine"}), 302),
    "edit entry": (WRITERS, lambda c, d: c.get(f"/handovers/{d['draft']}/entries/{d['entry']}/edit"), 200),
    "edit ward notes": (LEADS, lambda c, d: c.get(f"/handovers/{d['draft']}/notes"), 200),
    "start handover": (LEADS, lambda c, d: c.get("/wards/AMU/handovers/new"), 200),
    "complete handover": (LEADS, lambda c, d: post(c, f"/handovers/{d['draft']}/complete",
                                                   {"received_by_id": d["tom"]}), 302),
    "admit": (WRITERS, lambda c, d: c.get("/wards/W12/admit"), 200),
    "move": (WRITERS, lambda c, d: c.get(f"/admissions/{d['admission']}/move"), 200),
    "discharge": (WRITERS, lambda c, d: c.get(f"/admissions/{d['admission']}/discharge"), 200),
}


@pytest.mark.parametrize("role", list(ACCOUNTS))
@pytest.mark.parametrize("action", list(ACTIONS))
def test_role_can_or_cannot(app, demo, role, action):
    allowed_roles, do, ok_status = ACTIONS[action]
    response = do(sign_in(app, role), demo)
    if role in allowed_roles:
        assert response.status_code == ok_status, f"{role} should be able to {action}"
    else:
        assert response.status_code == 403, f"{role} should NOT be able to {action}"
        assert "You don't have permission" in response.get_data(as_text=True)


def test_every_role_has_permissions_defined():
    assert set(ROLE_PERMISSIONS) == set(StaffRole)


def test_refused_changes_are_not_saved(app, demo):
    hca = sign_in(app, "hca")
    with app.app_context():
        before = db.session.scalar(db.select(db.func.count()).select_from(Task))
    post(hca, f"/handovers/{demo['draft']}/entries/{demo['entry']}/tasks",
         {"description": "Sneaky task", "priority": "urgent"})
    post(hca, f"/handovers/{demo['draft']}/complete", {"received_by_id": demo["tom"]})
    post(hca, f"/admissions/{demo['admission']}/discharge", {"destination": "Home"})
    with app.app_context():
        assert db.session.scalar(db.select(db.func.count()).select_from(Task)) == before
        assert db.session.get(Handover, demo["draft"]).status == HandoverStatus.DRAFT
        assert db.session.get(Admission, demo["admission"]).is_current


def test_refused_attempts_are_audited(app, demo):
    sign_in(app, "hca").get(f"/handovers/{demo['draft']}/notes")
    with app.app_context():
        log = db.session.scalar(db.select(AuditLog).where(AuditLog.action == "denied"))
        assert log.details == f"edit_notes GET /handovers/{demo['draft']}/notes"
        assert log.staff.email == ACCOUNTS["hca"]


# --- What each role sees ----------------------------------------------------------

def test_hca_sees_tick_boxes_but_no_edit_buttons(app, demo):
    page = sign_in(app, "hca").get(f"/handovers/{demo['draft']}").get_data(as_text=True)
    assert "Mark &#39;" in page or "Mark '" in page  # can tick tasks off
    assert "/edit" not in page
    assert "Add task" not in page
    assert "Edit notes" not in page
    assert "Complete handover" not in page


def test_nurse_sees_edit_but_not_complete(app, demo):
    page = sign_in(app, "nurse").get(f"/handovers/{demo['draft']}").get_data(as_text=True)
    assert "/edit" in page and "Add task" in page
    assert "Edit notes" not in page
    assert "Complete handover" not in page


def test_nurse_in_charge_sees_everything(app, demo):
    page = sign_in(app, "nurse_in_charge").get(f"/handovers/{demo['draft']}").get_data(as_text=True)
    for text in ["/edit", "Add task", "Edit notes", "Complete handover"]:
        assert text in page


def test_hca_patient_list_has_no_admit_or_discharge(app):
    page = sign_in(app, "hca").get("/wards/W12/patients").get_data(as_text=True)
    assert "WHITFIELD" in page
    assert "Admit patient" not in page and "Discharge" not in page and ">Move<" not in page


def test_start_first_handover_button_only_for_leads(app):
    assert "Start first handover" not in sign_in(app, "nurse").get("/wards/AMU/handover").get_data(as_text=True)
    assert "Start first handover" in sign_in(app, "ward_manager").get("/wards/AMU/handover").get_data(as_text=True)


def test_admin_gets_landing_page_without_patient_data(app):
    admin = sign_in(app, "admin")
    page = admin.get("/").get_data(as_text=True)
    assert "doesn't have access to patient information" in page
    assert "Elderly Care" not in page and "W12" not in page
    assert "Sign out" in page
