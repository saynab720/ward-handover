from datetime import date, datetime, timedelta, timezone

import pytest

from app.db import db
from app.handover import suggest_next
from app.models import (
    Admission, AuditLog, Bed, Handover, HandoverEntry, HandoverStatus, Patient, Shift, Staff, Ward,
)

from .conftest import get_csrf
from .test_editing import entry_form

TOM = "tom.hughes@demo.nhs.test"
TODAY = date.today()


def w12(app):
    return db.session.scalar(db.select(Ward).where(Ward.code == "W12"))


def draft_id(app):
    with app.app_context():
        return db.session.scalar(
            db.select(Handover.id).where(Handover.status == HandoverStatus.DRAFT)
        )


def complete_draft(app, client):
    """Complete today's demo draft (long day) so the next one can be started."""
    with app.app_context():
        tom_id = db.session.scalar(db.select(Staff.id).where(Staff.email == TOM))
    handover_id = draft_id(app)
    csrf = get_csrf(client, f"/handovers/{handover_id}")
    client.post(f"/handovers/{handover_id}/complete", data={"received_by_id": tom_id, "csrf_token": csrf})
    return handover_id


def start(client, shift_date=TODAY, shift="night", code="W12"):
    csrf = get_csrf(client, f"/handovers/1")
    return client.post(f"/wards/{code}/handovers/new", data={
        "shift_date": shift_date.isoformat(), "shift": shift, "csrf_token": csrf,
    })


@pytest.fixture
def ready(app, client, login):
    """Signed in as Tom, with today's long day completed."""
    login(email=TOM)
    return complete_draft(app, client)


def test_button_shows_once_completed(app, client, login):
    login(email=TOM)
    handover_id = draft_id(app)
    assert "Start next handover" not in client.get(f"/handovers/{handover_id}").get_data(as_text=True)
    complete_draft(app, client)
    assert "Start next handover" in client.get(f"/handovers/{handover_id}").get_data(as_text=True)


def test_button_only_on_latest_handover(app, client, ready):
    last_night = 1
    assert "Start next handover" not in client.get(f"/handovers/{last_night}").get_data(as_text=True)


def test_form_suggests_night_after_long_day(client, ready):
    page = client.get("/wards/W12/handovers/new").get_data(as_text=True)
    assert f'value="{TODAY.isoformat()}"' in page
    assert '<option value="night" selected>' in page
    assert "Carried forward (6)" in page


def test_start_copies_entries_forward(app, client, ready):
    previous_id = ready
    response = start(client)
    assert response.status_code == 302

    with app.app_context():
        new = db.session.scalar(db.select(Handover).where(Handover.status == HandoverStatus.DRAFT))
        previous = db.session.get(Handover, previous_id)
        tom = db.session.scalar(db.select(Staff).where(Staff.email == TOM))

        assert (new.shift_date, new.shift) == (TODAY, Shift.NIGHT)
        assert new.handed_over_by_id == tom.id
        assert new.ward_notes == previous.ward_notes
        assert len(new.entries) == 6

        old_by_admission = {e.admission_id: e for e in previous.entries}
        for entry in new.entries:
            old = old_by_admission[entry.admission_id]
            assert entry.id != old.id
            assert entry.situation == old.situation
            assert entry.news2_score == old.news2_score
            assert entry.is_flagged == old.is_flagged
            assert entry.updated_by_id == old.updated_by_id  # keeps the real author
            assert entry.not_updated_this_shift

        log = db.session.scalar(db.select(AuditLog).where(AuditLog.action == "create",
                                                          AuditLog.table_name == "handovers"))
        assert log.record_id == new.id and log.details == "carried=6 new=0 left=0"


def test_new_page_shows_not_updated(app, client, ready):
    start(client)
    page = client.get(f"/handovers/{draft_id(app)}").get_data(as_text=True)
    assert page.count('badge-stale">Not updated this shift') == 6


def test_editing_clears_not_updated(app, client, ready):
    start(client)
    handover_id = draft_id(app)
    with app.app_context():
        entry_id = db.session.get(Handover, handover_id).entries[0].id
    data = entry_form(client, handover_id, entry_id, situation="Reviewed on nights.")
    client.post(f"/handovers/{handover_id}/entries/{entry_id}/edit", data=data)
    with app.app_context():
        assert not db.session.get(HandoverEntry, entry_id).not_updated_this_shift
    page = client.get(f"/handovers/{handover_id}").get_data(as_text=True)
    assert page.count('badge-stale">Not updated this shift') == 5


def test_editing_new_handover_leaves_old_one_alone(app, client, ready):
    start(client)
    handover_id = draft_id(app)
    with app.app_context():
        entry = db.session.get(Handover, handover_id).entries[0]
        entry_id, admission_id = entry.id, entry.admission_id
    data = entry_form(client, handover_id, entry_id, situation="Changed tonight")
    client.post(f"/handovers/{handover_id}/entries/{entry_id}/edit", data=data)
    with app.app_context():
        old = db.session.scalar(db.select(HandoverEntry).where(
            HandoverEntry.handover_id == ready, HandoverEntry.admission_id == admission_id))
        assert old.situation != "Changed tonight"


def test_new_admission_gets_blank_entry_and_discharged_left_out(app, client, ready):
    with app.app_context():
        ward = w12(app)
        bed = db.session.scalar(db.select(Bed).where(Bed.ward_id == ward.id, Bed.label == "Bay 2 Bed 4"))
        db.session.add(Admission(
            patient=Patient(nhs_number="9990999992", hospital_number="DEMO00500", first_name="Ivy",
                            last_name="Newman", date_of_birth=date(1945, 2, 2)),
            ward=ward, bed=bed, reason_for_admission="Fall",
        ))
        ellis = db.session.scalar(db.select(Admission).join(Patient).where(Patient.last_name == "Ellis"))
        ellis.discharged_at = datetime.now(timezone.utc)
        db.session.commit()

    page = client.get("/wards/W12/handovers/new").get_data(as_text=True)
    assert "Carried forward (5)" in page
    assert "New to the ward, blank entry (1)" in page and "NEWMAN, Ivy" in page
    assert "Left out: discharged or moved (1)" in page and "ELLIS, Margaret" in page

    start(client)
    with app.app_context():
        new = db.session.get(Handover, draft_id(app))
        names = {e.admission.patient.last_name: e for e in new.entries}
        assert "Ellis" not in names
        assert names["Newman"].situation is None and names["Newman"].news2_score is None
    page = client.get(f"/handovers/{draft_id(app)}").get_data(as_text=True)
    assert "New patient: no handover written yet" in page


def test_open_tasks_still_show(app, client, ready):
    start(client)
    page = client.get(f"/handovers/{draft_id(app)}").get_data(as_text=True)
    assert "Mark 'Chase U&amp;E results' as done" in page


def test_existing_draft_is_opened_instead(app, client, login):
    login(email=TOM)
    existing = draft_id(app)
    response = client.get("/wards/W12/handovers/new")
    assert response.headers["Location"] == f"/handovers/{existing}"
    start(client)
    with app.app_context():
        assert db.session.scalar(db.select(db.func.count()).select_from(Handover)) == 2


@pytest.mark.parametrize("shift_date, shift, message", [
    (TODAY, "long_day", "must be after the latest handover"),
    (TODAY - timedelta(days=1), "night", "must be after the latest handover"),
    (TODAY + timedelta(days=3), "night", "more than a day ahead"),
])
def test_invalid_shift_is_rejected(app, client, ready, shift_date, shift, message):
    response = start(client, shift_date, shift)
    assert response.status_code == 400
    assert message in response.get_data(as_text=True)
    assert draft_id(app) is None


def test_bad_form_values_are_rejected(app, client, ready):
    csrf = get_csrf(client, "/handovers/1")
    response = client.post("/wards/W12/handovers/new",
                           data={"shift_date": "nonsense", "shift": "brunch", "csrf_token": csrf})
    assert response.status_code == 400
    assert draft_id(app) is None


def test_first_handover_for_a_ward(app, client, login):
    login(email="sam.patel@demo.nhs.test")
    assert "Start first handover" in client.get("/wards/AMU/handover").get_data(as_text=True)
    start(client, shift="long_day", code="AMU")
    with app.app_context():
        amu = db.session.scalar(db.select(Ward).where(Ward.code == "AMU"))
        handover = amu.handovers[0]
        assert len(handover.entries) == 2
        assert all(e.situation is None for e in handover.entries)


def test_unknown_ward(client, login):
    login()
    assert client.get("/wards/NOPE/handovers/new").status_code == 404


def test_suggestions(app):
    with app.app_context():
        ward = w12(app)
        # Latest demo handover is today's long day
        assert suggest_next(ward) == (TODAY, Shift.NIGHT)

        night = Handover(ward=ward, shift_date=TODAY, shift=Shift.NIGHT, handed_over_by_id=1)
        db.session.add(night)
        db.session.flush()
        assert suggest_next(ward) == (TODAY + timedelta(days=1), Shift.LONG_DAY)

        early = Handover(ward=ward, shift_date=TODAY + timedelta(days=1), shift=Shift.EARLY, handed_over_by_id=1)
        db.session.add(early)
        db.session.flush()
        assert suggest_next(ward) == (TODAY + timedelta(days=1), Shift.LATE)
        db.session.rollback()

        amu = db.session.scalar(db.select(Ward).where(Ward.code == "AMU"))
        assert suggest_next(amu, now=datetime(2026, 10, 12, 10)) == (date(2026, 10, 12), Shift.LONG_DAY)
        assert suggest_next(amu, now=datetime(2026, 10, 12, 21)) == (date(2026, 10, 12), Shift.NIGHT)
