from datetime import date, timedelta

import pytest
from sqlalchemy.exc import IntegrityError

from app.db import db
from app.models import (
    Admission, AuditLog, Bed, Handover, HandoverStatus, Patient, ResusStatus, Staff, Ward,
)
from app.patients import nhs_number_is_valid
from app.seed import fake_nhs_number

from .conftest import get_csrf


def ward(code):
    return db.session.scalar(db.select(Ward).where(Ward.code == code))


def bed(code, label):
    return db.session.scalar(db.select(Bed).join(Ward).where(Ward.code == code, Bed.label == label))


def admission_of(last_name):
    return db.session.scalar(
        db.select(Admission).join(Patient)
        .where(Patient.last_name == last_name, Admission.discharged_at.is_(None))
    )


def new_patient_form(app, **changes):
    with app.app_context():
        bed_id = bed("W12", "Bay 2 Bed 4").id
    data = {
        "hospital_number": "demo00777", "nhs_number": "999 777 0005", "first_name": "Ivy",
        "last_name": "Newman", "date_of_birth": "1945-02-02", "sex": "Female",
        "allergies": "NKDA", "resus_status": "for_resus", "bed_id": str(bed_id),
        "reason_for_admission": "Fall at home", "consultant": "Dr R. Bennett",
        "past_medical_history": "", "expected_discharge_date": "",
    }
    data.update(changes)
    return data


def post(client, url, data, csrf_from="/"):
    return client.post(url, data={**data, "csrf_token": get_csrf(client, csrf_from)})


@pytest.fixture
def signed_in(client, login):
    login(email="tom.hughes@demo.nhs.test")
    return client


# --- NHS numbers ------------------------------------------------------------------

def fake_nhs_number_check_digit():
    assert nhs_number_is_valid("9434765919")  # NHS England's documented example
    assert not nhs_number_is_valid("9434765918")
    assert not nhs_number_is_valid("943476591")
    assert not nhs_number_is_valid("94347659AB")
    for n in range(1, 200, 7):
        assert nhs_number_is_valid(fake_nhs_number(n * 1000))


# --- Ward patient list ----------------------------------------------------------

def test_patient_list(signed_in):
    page = signed_in.get("/wards/W12/patients").get_data(as_text=True)
    assert "6 patients · 4 empty beds" in page
    assert "WHITFIELD, George" in page
    assert "GREY, Walter" not in page  # discharged yesterday


def test_patient_pages_need_login(client, app):
    with app.app_context():
        admission_id = admission_of("Ellis").id
    for path in ["/wards/W12/patients", "/wards/W12/admit",
                 f"/admissions/{admission_id}/move", f"/admissions/{admission_id}/discharge"]:
        assert client.get(path).status_code == 302


# --- Admitting ------------------------------------------------------------------

def test_admit_new_patient(app, signed_in):
    response = post(signed_in, "/wards/W12/admit", new_patient_form(app))
    assert response.status_code == 302
    with app.app_context():
        a = admission_of("Newman")
        assert a.ward.code == "W12" and a.bed.label == "Bay 2 Bed 4"
        assert a.patient.hospital_number == "DEMO00777"  # stored in capitals
        assert a.patient.nhs_number == "9997770005"  # spaces removed
        assert a.patient.resus_status == ResusStatus.FOR_RESUS
        assert db.session.scalar(db.select(AuditLog).where(AuditLog.action == "admit")).record_id == a.id
    assert "5 empty beds" not in signed_in.get("/wards/W12/patients").get_data(as_text=True)


def test_admitted_patient_joins_handover_in_progress(app, signed_in):
    post(signed_in, "/wards/W12/admit", new_patient_form(app))
    with app.app_context():
        draft = db.session.scalar(db.select(Handover).where(Handover.status == HandoverStatus.DRAFT))
        entry = next(e for e in draft.entries if e.admission.patient.last_name == "Newman")
        assert entry.situation is None and entry.not_updated_this_shift
        draft_id = draft.id
    page = signed_in.get(f"/handovers/{draft_id}").get_data(as_text=True)
    assert "NEWMAN, Ivy" in page and "New patient: no handover written yet" in page


@pytest.mark.parametrize("changes, message", [
    ({"hospital_number": ""}, "Enter hospital number"),
    ({"hospital_number": "DEMO00001"}, "already exists"),
    ({"hospital_number": "AB-12"}, "only contain letters and numbers"),
    ({"nhs_number": "9997770001"}, "valid check digit"),
    ({"nhs_number": fake_nhs_number(1000)}, "already exists"),
    ({"first_name": ""}, "Enter first name"),
    ({"date_of_birth": ""}, "Enter date of birth"),
    ({"date_of_birth": (date.today() + timedelta(days=1)).isoformat()}, "can&#39;t be in the future"),
    ({"allergies": ""}, "Enter allergies"),
    ({"resus_status": "maybe"}, "Choose a resus status"),
    ({"reason_for_admission": ""}, "Enter reason for admission"),
    ({"expected_discharge_date": "2020-01-01"}, "can&#39;t be in the past"),
])
def test_admit_validation(app, signed_in, changes, message):
    response = post(signed_in, "/wards/W12/admit", new_patient_form(app, **changes))
    assert response.status_code == 400
    page = response.get_data(as_text=True)
    assert message in page
    assert 'value="Newman"' in page  # typed values kept
    with app.app_context():
        assert admission_of("Newman") is None


def test_cannot_use_an_occupied_bed(app, signed_in):
    with app.app_context():
        taken = bed("W12", "Bay 1 Bed 1").id
    response = post(signed_in, "/wards/W12/admit", new_patient_form(app, bed_id=str(taken)))
    assert response.status_code == 400
    assert "Bay 1 Bed 1 already has a patient" in response.get_data(as_text=True)


def test_cannot_use_a_bed_on_another_ward(app, signed_in):
    with app.app_context():
        amu_bed = bed("AMU", "Bay 2 Bed 4").id
    response = post(signed_in, "/wards/W12/admit", new_patient_form(app, bed_id=str(amu_bed)))
    assert response.status_code == 400


def test_admit_without_bed(app, signed_in):
    post(signed_in, "/wards/W12/admit", new_patient_form(app, bed_id=""))
    with app.app_context():
        assert admission_of("Newman").bed is None


def test_find_existing_patient(app, signed_in):
    page = signed_in.get("/wards/W12/admit?find=demo00099").get_data(as_text=True)  # Walter Grey, discharged
    assert "GREY, Walter" in page and 'name="patient_id"' in page

    with app.app_context():
        walter_id = db.session.scalar(db.select(Patient.id).where(Patient.last_name == "Grey"))
    response = post(signed_in, "/wards/W12/admit", {
        "patient_id": walter_id, "allergies": "Penicillin (rash)", "resus_status": "dnacpr",
        "reason_for_admission": "Readmitted with cellulitis", "bed_id": "",
    })
    assert response.status_code == 302
    with app.app_context():
        walter = db.session.get(Patient, walter_id)
        assert len(walter.admissions) == 2  # history kept
        assert walter.allergies == "Penicillin (rash)"  # safety info updated
        assert walter.resus_status == ResusStatus.DNACPR
        log = db.session.scalar(db.select(AuditLog).where(AuditLog.table_name == "patients"))
        assert log.details == "changed: allergies, resus_status"


def test_find_by_nhs_number_with_spaces(signed_in):
    number = fake_nhs_number(99000)
    page = signed_in.get(f"/wards/W12/admit?find={number[:3]} {number[3:6]} {number[6:]}").get_data(as_text=True)
    assert "GREY, Walter" in page


def test_find_patient_already_admitted(signed_in):
    response = signed_in.get("/wards/AMU/admit?find=DEMO00001")  # Margaret Ellis, on W12
    page = response.get_data(as_text=True)
    assert "already admitted to W12" in page
    assert 'name="patient_id"' not in page


def test_find_unknown_number_prefills_new_patient(signed_in):
    page = signed_in.get("/wards/W12/admit?find=9997770000").get_data(as_text=True)
    assert "No patient found" in page
    assert 'name="nhs_number" type="text" value="9997770000"' in page


def test_cannot_admit_patient_twice(app, signed_in):
    with app.app_context():
        ellis_id = admission_of("Ellis").patient_id
    response = post(signed_in, "/wards/AMU/admit", {
        "patient_id": ellis_id, "allergies": "NKDA", "resus_status": "for_resus",
        "reason_for_admission": "Duplicate", "bed_id": "",
    })
    assert response.status_code == 400
    assert "already been admitted somewhere else" in response.get_data(as_text=True)


def test_database_blocks_double_booking(app):
    """Even if the page checks were bypassed, the database refuses two patients in one bed."""
    with app.app_context():
        ellis = admission_of("Ellis")
        db.session.add(Admission(patient=admission_of("Lowe").patient, ward=ellis.ward, bed=ellis.bed))
        with pytest.raises(IntegrityError):
            db.session.flush()


# --- Moving ---------------------------------------------------------------------

def test_move_bed_on_same_ward(app, signed_in):
    with app.app_context():
        a = admission_of("Ellis")
        admission_id, target = a.id, bed("W12", "SR2").id
    response = post(signed_in, f"/admissions/{admission_id}/move", {"destination": f"bed:{target}"})
    assert response.status_code == 302
    with app.app_context():
        a = db.session.get(Admission, admission_id)
        assert a.is_current and a.bed.label == "SR2"  # same stay, new bed
        log = db.session.scalar(db.select(AuditLog).where(AuditLog.action == "move"))
        assert log.details == "bed Bay 1 Bed 1 -> SR2"


def test_move_to_occupied_bed_is_refused(app, signed_in):
    with app.app_context():
        admission_id, taken = admission_of("Ellis").id, bed("W12", "Bay 1 Bed 2").id
    response = post(signed_in, f"/admissions/{admission_id}/move", {"destination": f"bed:{taken}"})
    assert response.status_code == 400
    assert "already has a patient" in response.get_data(as_text=True)


def test_transfer_to_another_ward(app, signed_in):
    with app.app_context():
        old = admission_of("Whitfield")
        old_id, target = old.id, bed("AMU", "Bay 2 Bed 1").id
        open_tasks = {t.id for t in old.tasks if t.completed_at is None}
        assert open_tasks
    response = post(signed_in, f"/admissions/{old_id}/move", {"destination": f"bed:{target}"})
    assert response.status_code == 302
    assert response.headers["Location"] == "/wards/AMU/patients"

    with app.app_context():
        old = db.session.get(Admission, old_id)
        new = admission_of("Whitfield")
        assert not old.is_current and old.discharge_destination == "Transferred to AMU"
        assert new.ward.code == "AMU" and new.bed.label == "Bay 2 Bed 1"
        assert new.reason_for_admission == old.reason_for_admission
        assert {t.id for t in new.tasks} == open_tasks  # open tasks moved with the patient


def test_transfer_shows_on_both_handovers(app, signed_in, login):
    # Give AMU a handover in progress, so the patient should join it
    csrf = get_csrf(signed_in, "/")
    signed_in.post("/wards/AMU/handovers/new", data={
        "shift_date": date.today().isoformat(), "shift": "long_day", "csrf_token": csrf})
    with app.app_context():
        old_id, target = admission_of("Whitfield").id, bed("AMU", "Bay 2 Bed 1").id
    post(signed_in, f"/admissions/{old_id}/move", {"destination": f"bed:{target}"})

    with app.app_context():
        amu_draft = ward("AMU").handovers[0]
        entry = next(e for e in amu_draft.entries if e.admission.patient.last_name == "Whitfield")
        assert entry.news2_score == 5 and entry.situation.startswith("NEWS2 rose")  # copied across
        assert entry.not_updated_this_shift
        w12_draft = next(h for h in ward("W12").handovers if h.status == HandoverStatus.DRAFT)
        w12_id = w12_draft.id
    page = signed_in.get(f"/handovers/{w12_id}").get_data(as_text=True)
    assert "Transferred to AMU at" in page


def test_move_to_ward_without_bed(app, signed_in):
    with app.app_context():
        admission_id, amu_id = admission_of("Ellis").id, ward("AMU").id
    post(signed_in, f"/admissions/{admission_id}/move", {"destination": f"ward:{amu_id}"})
    with app.app_context():
        new = admission_of("Ellis")
        assert new.ward.code == "AMU" and new.bed is None


@pytest.mark.parametrize("destination", ["", "bed:99999", "ward:99999", "nonsense"])
def test_bad_move_destination(app, signed_in, destination):
    with app.app_context():
        admission_id = admission_of("Ellis").id
    response = post(signed_in, f"/admissions/{admission_id}/move", {"destination": destination})
    assert response.status_code == 400


# --- Discharging ----------------------------------------------------------------

def test_discharge_frees_bed(app, signed_in):
    with app.app_context():
        admission_id = admission_of("Briggs").id
    page = signed_in.get(f"/admissions/{admission_id}/discharge").get_data(as_text=True)
    assert "1 task still open" in page and "Chase U&amp;E results" in page

    response = post(signed_in, f"/admissions/{admission_id}/discharge", {"destination": "Home"})
    assert response.status_code == 302
    with app.app_context():
        a = db.session.get(Admission, admission_id)
        assert not a.is_current and a.discharge_destination == "Home"
    page = signed_in.get("/wards/W12/patients").get_data(as_text=True)
    assert "BRIGGS" not in page
    assert "Harold Briggs discharged (home). Bay 2 Bed 1 is now free." in page


def test_discharge_needs_destination(app, signed_in):
    with app.app_context():
        admission_id = admission_of("Briggs").id
    response = post(signed_in, f"/admissions/{admission_id}/discharge", {"destination": "Mars"})
    assert response.status_code == 400
    with app.app_context():
        assert db.session.get(Admission, admission_id).is_current


def test_discharged_patient_shows_on_current_handover_then_drops_off(app, signed_in):
    with app.app_context():
        admission_id = admission_of("Briggs").id
        draft_id = db.session.scalar(db.select(Handover.id).where(Handover.status == HandoverStatus.DRAFT))
    post(signed_in, f"/admissions/{admission_id}/discharge", {"destination": "Care home"})
    page = signed_in.get(f"/handovers/{draft_id}").get_data(as_text=True)
    assert "Discharged: Care home at" in page

    # Not carried into the next handover
    csrf = get_csrf(signed_in, f"/handovers/{draft_id}")
    with app.app_context():
        receiver = db.session.scalar(db.select(Staff.id).where(Staff.email == "priya.shah@demo.nhs.test"))
    signed_in.post(f"/handovers/{draft_id}/complete", data={"received_by_id": receiver, "csrf_token": csrf})
    page = signed_in.get("/wards/W12/handovers/new").get_data(as_text=True)
    assert "Left out: discharged or moved (1)" in page and "BRIGGS, Harold" in page


def test_cannot_discharge_twice(app, signed_in):
    with app.app_context():
        admission_id = admission_of("Briggs").id
    post(signed_in, f"/admissions/{admission_id}/discharge", {"destination": "Home"})
    response = signed_in.get(f"/admissions/{admission_id}/discharge")
    assert response.status_code == 302
    response = post(signed_in, f"/admissions/{admission_id}/discharge", {"destination": "Died"})
    with app.app_context():
        assert db.session.get(Admission, admission_id).discharge_destination == "Home"
