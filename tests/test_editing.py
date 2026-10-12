import re

import pytest

from app.db import db
from app.models import AuditLog, Handover, HandoverEntry, HandoverStatus, Patient, Staff, Task

from .conftest import get_csrf

TOM = "tom.hughes@demo.nhs.test"


@pytest.fixture
def ids(app):
    """IDs from the demo data: today's draft handover, last night's completed one, and patient entries."""
    with app.app_context():
        draft = db.session.scalar(db.select(Handover).where(Handover.status == HandoverStatus.DRAFT))
        done = db.session.scalar(db.select(Handover).where(Handover.status == HandoverStatus.COMPLETED))

        def entry_for(handover, last_name):
            return next(e for e in handover.entries if e.admission.patient.last_name == last_name)

        whitfield = entry_for(draft, "Whitfield")
        return {
            "draft": draft.id,
            "done": done.id,
            "whitfield": whitfield.id,
            "whitfield_admission": whitfield.admission_id,
            "done_entry": done.entries[0].id,
            "amara": draft.handed_over_by_id,
            "tom": db.session.scalar(db.select(Staff.id).where(Staff.email == TOM)),
        }


def entry_form(client, handover_id, entry_id, **changes):
    """Load the edit form and return its current values, with changes applied."""
    html = client.get(f"/handovers/{handover_id}/entries/{entry_id}/edit").get_data(as_text=True)
    data = {
        "csrf_token": re.search(r'name="csrf_token" value="([^"]+)"', html).group(1),
        "version": re.search(r'name="version" value="([^"]*)"', html).group(1),
    }
    for name in ["obs_frequency", "mobility", "diet", "infection_control", "news2_score"]:
        data[name] = re.search(rf'name="{name}"[^>]*value="([^"]*)"', html).group(1)
    for name in ["situation", "background", "assessment", "recommendation"]:
        data[name] = re.search(rf'name="{name}"[^>]*>(.*?)</textarea>', html, re.S).group(1)
    for name in ["falls_risk", "pressure_risk", "is_flagged"]:
        if re.search(rf'name="{name}" checked', html):
            data[name] = "on"
    data.update(changes)
    return {k: v for k, v in data.items() if v is not False}


def get_entry(app, entry_id):
    with app.app_context():
        entry = db.session.get(HandoverEntry, entry_id)
        db.session.expunge(entry)
        return entry


def audit_actions(app, table):
    with app.app_context():
        return db.session.scalars(
            db.select(AuditLog.action).where(AuditLog.table_name == table).order_by(AuditLog.id)
        ).all()


# --- Editing entries ------------------------------------------------------------

def test_edit_entry_saves_and_records_who(app, client, login, ids):
    login(email=TOM)
    data = entry_form(client, ids["draft"], ids["whitfield"],
                      situation="NEWS2 now 7, registrar called.", news2_score="7")
    response = client.post(f"/handovers/{ids['draft']}/entries/{ids['whitfield']}/edit", data=data)
    assert response.status_code == 302
    assert response.headers["Location"].endswith(f"#entry-{ids['whitfield']}")

    entry = get_entry(app, ids["whitfield"])
    assert entry.situation == "NEWS2 now 7, registrar called."
    assert entry.news2_score == 7
    assert entry.updated_by_id == ids["tom"]
    assert entry.is_flagged  # unchanged fields keep their values
    assert "update" in audit_actions(app, "handover_entries")


def test_unticking_a_box_clears_it(app, client, login, ids):
    login()
    data = entry_form(client, ids["draft"], ids["whitfield"], is_flagged=False)
    client.post(f"/handovers/{ids['draft']}/entries/{ids['whitfield']}/edit", data=data)
    assert get_entry(app, ids["whitfield"]).is_flagged is False


def test_blank_news2_is_allowed(app, client, login, ids):
    login()
    data = entry_form(client, ids["draft"], ids["whitfield"], news2_score="")
    client.post(f"/handovers/{ids['draft']}/entries/{ids['whitfield']}/edit", data=data)
    assert get_entry(app, ids["whitfield"]).news2_score is None


@pytest.mark.parametrize("bad", ["21", "-1", "five", "3.5"])
def test_invalid_news2_is_rejected(app, client, login, ids, bad):
    login()
    data = entry_form(client, ids["draft"], ids["whitfield"], news2_score=bad, situation="Changed")
    response = client.post(f"/handovers/{ids['draft']}/entries/{ids['whitfield']}/edit", data=data)
    assert response.status_code == 400
    page = response.get_data(as_text=True)
    assert "NEWS2 must be a whole number from 0 to 20" in page
    assert "Changed" in page  # what they typed is kept
    assert get_entry(app, ids["whitfield"]).news2_score == 5  # nothing saved


def test_too_long_text_is_rejected(app, client, login, ids):
    login()
    data = entry_form(client, ids["draft"], ids["whitfield"], situation="x" * 2001)
    response = client.post(f"/handovers/{ids['draft']}/entries/{ids['whitfield']}/edit", data=data)
    assert response.status_code == 400
    assert get_entry(app, ids["whitfield"]).situation.startswith("NEWS2 rose")


def test_two_people_editing_at_once(app, login, ids):
    url = f"/handovers/{ids['draft']}/entries/{ids['whitfield']}/edit"
    amara, tom = app.test_client(), app.test_client()
    for client, email in [(amara, "amara.okafor@demo.nhs.test"), (tom, TOM)]:
        client.post("/login", data={"email": email, "password": "demo1234", "csrf_token": get_csrf(client)})

    # Both open the form, Amara saves first
    amara_form = entry_form(amara, ids["draft"], ids["whitfield"], situation="Amara's update")
    tom_form = entry_form(tom, ids["draft"], ids["whitfield"], situation="Tom's update")
    assert amara.post(url, data=amara_form).status_code == 302

    # Tom is warned instead of silently overwriting Amara's change
    response = tom.post(url, data=tom_form)
    assert response.status_code == 409
    page = response.get_data(as_text=True)
    assert "Amara Okafor saved changes" in page
    assert "Amara&#39;s update" in page and "Tom&#39;s update" in page
    assert get_entry(app, ids["whitfield"]).situation == "Amara's update"

    # Saving again from the warning page keeps Tom's version
    retry = {**tom_form, "version": re.search(r'name="version" value="([^"]*)"', page).group(1)}
    assert tom.post(url, data=retry).status_code == 302
    assert get_entry(app, ids["whitfield"]).situation == "Tom's update"


def test_entry_from_another_handover_is_not_found(client, login, ids):
    login()
    response = client.get(f"/handovers/{ids['draft']}/entries/{ids['done_entry']}/edit")
    assert response.status_code == 404


def test_saving_without_changes_does_not_audit(app, client, login, ids):
    login()
    data = entry_form(client, ids["draft"], ids["whitfield"])
    client.post(f"/handovers/{ids['draft']}/entries/{ids['whitfield']}/edit", data=data)
    assert "update" not in audit_actions(app, "handover_entries")


def test_html_is_escaped(app, client, login, ids):
    login()
    data = entry_form(client, ids["draft"], ids["whitfield"], situation="<script>alert(1)</script>")
    client.post(f"/handovers/{ids['draft']}/entries/{ids['whitfield']}/edit", data=data)
    page = client.get(f"/handovers/{ids['draft']}").get_data(as_text=True)
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;" in page


# --- Ward notes -----------------------------------------------------------------

def test_edit_ward_notes(app, client, login, ids):
    login()
    csrf = get_csrf(client, f"/handovers/{ids['draft']}/notes")
    client.post(f"/handovers/{ids['draft']}/notes", data={"ward_notes": "Bay 2 closed, norovirus.", "csrf_token": csrf})
    with app.app_context():
        assert db.session.get(Handover, ids["draft"]).ward_notes == "Bay 2 closed, norovirus."
    assert "update" in audit_actions(app, "handovers")


# --- Tasks ----------------------------------------------------------------------

def test_add_task(app, client, login, ids):
    login(email=TOM)
    csrf = get_csrf(client, f"/handovers/{ids['draft']}")
    client.post(f"/handovers/{ids['draft']}/entries/{ids['whitfield']}/tasks", data={
        "description": "Repeat VBG", "priority": "urgent", "due_at": "2026-10-12T14:30", "csrf_token": csrf,
    })
    with app.app_context():
        task = db.session.scalar(db.select(Task).where(Task.description == "Repeat VBG"))
        assert task.admission_id == ids["whitfield_admission"]
        assert task.priority.value == "urgent"
        assert task.due_at.hour == 14 and task.due_at.minute == 30
        assert task.created_by_id == ids["tom"]


@pytest.mark.parametrize("form", [
    {"description": "", "priority": "routine"},
    {"description": "x" * 501, "priority": "routine"},
    {"description": "Valid", "priority": "whenever"},
    {"description": "Valid", "priority": "routine", "due_at": "tomorrow"},
])
def test_invalid_task_is_rejected(app, client, login, ids, form):
    login()
    with app.app_context():
        before = db.session.scalar(db.select(db.func.count()).select_from(Task))
    csrf = get_csrf(client, f"/handovers/{ids['draft']}")
    client.post(f"/handovers/{ids['draft']}/entries/{ids['whitfield']}/tasks", data={**form, "csrf_token": csrf})
    with app.app_context():
        assert db.session.scalar(db.select(db.func.count()).select_from(Task)) == before


def test_complete_task(app, client, login, ids):
    login(email=TOM)
    with app.app_context():
        task_id = db.session.scalar(db.select(Task.id).where(Task.description == "Repeat ABG"))
    before = client.get(f"/handovers/{ids['draft']}").get_data(as_text=True)
    assert "Mark 'Repeat ABG' as done" in before
    csrf = get_csrf(client, f"/handovers/{ids['draft']}")
    client.post(f"/handovers/{ids['draft']}/tasks/{task_id}/complete", data={"csrf_token": csrf})
    with app.app_context():
        task = db.session.get(Task, task_id)
        assert task.completed_at is not None
        assert task.completed_by_id == ids["tom"]
    page = client.get(f"/handovers/{ids['draft']}").get_data(as_text=True)
    assert "Task done: Repeat ABG" in page
    assert "Mark 'Repeat ABG' as done" not in page  # gone from the task list


# --- Completing the handover ----------------------------------------------------

def complete(client, handover_id, receiver_id):
    csrf = get_csrf(client, f"/handovers/{handover_id}")
    return client.post(f"/handovers/{handover_id}/complete",
                       data={"received_by_id": receiver_id, "csrf_token": csrf})


def test_complete_handover_locks_it(app, client, login, ids):
    login(email=TOM)
    complete(client, ids["draft"], ids["tom"])
    with app.app_context():
        handover = db.session.get(Handover, ids["draft"])
        assert handover.status == HandoverStatus.COMPLETED
        assert handover.received_by_id == ids["tom"]
        assert handover.completed_at is not None
    assert "complete" in audit_actions(app, "handovers")

    # The page no longer offers editing
    page = client.get(f"/handovers/{ids['draft']}").get_data(as_text=True)
    assert "/edit" not in page and "Complete handover" not in page

    # And the edit routes refuse
    csrf = get_csrf(client, f"/handovers/{ids['draft']}")
    data = {"situation": "Too late", "csrf_token": csrf}
    client.post(f"/handovers/{ids['draft']}/entries/{ids['whitfield']}/edit", data=data)
    assert get_entry(app, ids["whitfield"]).situation != "Too late"


def test_cannot_receive_your_own_handover(app, client, login, ids):
    login()
    complete(client, ids["draft"], ids["amara"])
    with app.app_context():
        assert db.session.get(Handover, ids["draft"]).status == HandoverStatus.DRAFT


def test_must_choose_receiver(app, client, login, ids):
    login()
    complete(client, ids["draft"], "")
    with app.app_context():
        assert db.session.get(Handover, ids["draft"]).status == HandoverStatus.DRAFT


def test_completed_handover_cannot_be_edited(app, client, login, ids):
    login()
    response = client.get(f"/handovers/{ids['done']}/entries/{ids['done_entry']}/edit")
    assert response.status_code == 302
    page = client.get(response.headers["Location"]).get_data(as_text=True)
    assert "can no longer be changed" in page


def test_viewing_is_audited(app, client, login, ids):
    login()
    client.get(f"/handovers/{ids['draft']}")
    assert "view" in audit_actions(app, "handovers")


def test_editing_needs_login(client, ids):
    assert client.get(f"/handovers/{ids['draft']}/entries/{ids['whitfield']}/edit").status_code == 302
    assert client.post(f"/handovers/{ids['draft']}/complete").status_code in (302, 400)
