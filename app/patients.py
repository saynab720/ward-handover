"""Admitting, moving and discharging patients."""
import re
from datetime import date, datetime, timezone

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy.exc import IntegrityError

from . import audit, permissions
from .db import db
from .handover import (
    ENTRY_FLAG_FIELDS, ENTRY_TEXT_FIELDS, SHIFT_ORDER, TEXT_LIMIT, bed_label, bed_sort_key, open_draft,
)
from .models import Admission, Bed, HandoverEntry, Patient, ResusStatus, Ward
from .permissions import requires

bp = Blueprint("patients", __name__)

SEX_OPTIONS = ["Female", "Male", "Other", "Not known"]
DISCHARGE_DESTINATIONS = ["Home", "Care home", "Another hospital", "Hospice", "Died", "Other"]


@bp.before_request
@login_required
def require_login():
    """Every page here shows patient data, so all need a signed-in user allowed to see it."""
    if not permissions.can(permissions.VIEW):
        permissions.deny(permissions.VIEW)


# --- Helpers --------------------------------------------------------------------

def nhs_number_is_valid(number):
    """Check a 10-digit NHS number using its modulus 11 check digit."""
    if not re.fullmatch(r"\d{10}", number):
        return False
    total = sum(int(d) * w for d, w in zip(number[:9], range(10, 1, -1)))
    check = 11 - total % 11
    check = 0 if check == 11 else check
    return check != 10 and check == int(number[9])


def clean_nhs_number(raw):
    return re.sub(r"[\s-]", "", raw or "")


def get_ward(code):
    return db.session.scalar(db.select(Ward).where(Ward.code == code)) or abort(404)


def current_admissions(ward):
    admissions = db.session.scalars(
        db.select(Admission).where(Admission.ward_id == ward.id, Admission.discharged_at.is_(None))
    ).all()
    return sorted(admissions, key=lambda a: bed_sort_key(bed_label(a)))


def empty_beds(ward):
    occupied = db.select(Admission.bed_id).where(
        Admission.discharged_at.is_(None), Admission.bed_id.is_not(None)
    )
    beds = db.session.scalars(
        db.select(Bed).where(Bed.ward_id == ward.id, Bed.is_active, Bed.id.not_in(occupied))
    ).all()
    return sorted(beds, key=lambda b: bed_sort_key(b.label))


def check_bed(ward, raw_bed_id):
    """Validate a chosen bed on this ward. Returns (bed or None, error)."""
    if not raw_bed_id:
        return None, None
    bed = db.session.get(Bed, int(raw_bed_id)) if raw_bed_id.isdigit() else None
    if bed is None or bed.ward_id != ward.id or not bed.is_active:
        return None, "Choose a bed on this ward."
    if bed not in empty_beds(ward):
        return None, f"{bed.label} already has a patient in it."
    return bed, None


def latest_entry(admission):
    """The patient's most recent handover entry for this admission, if any."""
    return max(
        admission.handover_entries,
        key=lambda e: (e.handover.shift_date, SHIFT_ORDER[e.handover.shift]),
        default=None,
    )


def add_to_draft(admission, copy_from=None):
    """If the ward has a handover in progress, add this patient to it, marked as not yet updated."""
    draft = open_draft(admission.ward)
    if draft is None:
        return
    now = datetime.now(timezone.utc)
    copied = {}
    updated_by_id = current_user.id
    if copy_from is not None:
        copied = {name: getattr(copy_from, name)
                  for name in [*ENTRY_TEXT_FIELDS, *ENTRY_FLAG_FIELDS, "news2_score"]}
        updated_by_id = copy_from.updated_by_id
    db.session.add(HandoverEntry(handover=draft, admission=admission, created_at=now, updated_at=now,
                                 updated_by_id=updated_by_id, **copied))


def text_field(form, name, label, limit, errors, required=False):
    value = form.get(name, "").strip()
    if required and not value:
        errors[name] = f"Enter {label.lower()}."
    elif len(value) > limit:
        errors[name] = f"{label} must be {limit} characters or fewer."
    return value or None


def date_field(form, name, label, errors, required=False):
    raw = form.get(name, "").strip()
    if not raw:
        if required:
            errors[name] = f"Enter {label.lower()}."
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        errors[name] = f"{label} is not a valid date."
        return None


# --- Ward patient list ------------------------------------------------------------

@bp.route("/wards/<code>/patients")
def ward_patients(code):
    ward = get_ward(code)
    return render_template("patients.html", ward=ward, admissions=current_admissions(ward),
                           empty_beds=empty_beds(ward))


# --- Admit ----------------------------------------------------------------------

def find_patient(query):
    query = query.strip()
    if not query:
        return None
    digits = clean_nhs_number(query)
    return db.session.scalar(db.select(Patient).where(
        (Patient.hospital_number == query.upper()) | (Patient.nhs_number == digits)
    ))


def read_patient_form(form, errors):
    values = {
        "hospital_number": text_field(form, "hospital_number", "Hospital number", 20, errors, required=True),
        "first_name": text_field(form, "first_name", "First name", 100, errors, required=True),
        "last_name": text_field(form, "last_name", "Last name", 100, errors, required=True),
        "date_of_birth": date_field(form, "date_of_birth", "Date of birth", errors, required=True),
        "sex": form.get("sex") if form.get("sex") in SEX_OPTIONS else None,
    }
    if values["hospital_number"]:
        values["hospital_number"] = values["hospital_number"].upper()
        if not re.fullmatch(r"[A-Z0-9]+", values["hospital_number"]):
            errors["hospital_number"] = "Hospital number can only contain letters and numbers."
        elif db.session.scalar(db.select(Patient).where(Patient.hospital_number == values["hospital_number"])):
            errors["hospital_number"] = "A patient with this hospital number already exists. Use Find patient above."

    nhs = clean_nhs_number(form.get("nhs_number"))
    if nhs:
        if not nhs_number_is_valid(nhs):
            errors["nhs_number"] = "NHS number must be 10 digits with a valid check digit. Check it's typed correctly."
        elif db.session.scalar(db.select(Patient).where(Patient.nhs_number == nhs)):
            errors["nhs_number"] = "A patient with this NHS number already exists. Use Find patient above."
    values["nhs_number"] = nhs or None

    dob = values["date_of_birth"]
    if dob and dob > date.today():
        errors["date_of_birth"] = "Date of birth can't be in the future."
    elif dob and dob.year < date.today().year - 130:
        errors["date_of_birth"] = "Check the date of birth."
    return values


def read_safety_fields(form, errors):
    """Allergies and resus status are asked on every admission, even for known patients."""
    allergies = text_field(form, "allergies", "Allergies", 500, errors, required=True)
    try:
        resus = ResusStatus(form.get("resus_status", ""))
    except ValueError:
        errors["resus_status"] = "Choose a resus status."
        resus = None
    return allergies, resus


def read_admission_form(ward, form, errors):
    bed, bed_error = check_bed(ward, form.get("bed_id", ""))
    if bed_error:
        errors["bed_id"] = bed_error
    values = {
        "bed": bed,
        "consultant": text_field(form, "consultant", "Consultant", 100, errors),
        "reason_for_admission": text_field(form, "reason_for_admission", "Reason for admission",
                                           TEXT_LIMIT, errors, required=True),
        "past_medical_history": text_field(form, "past_medical_history", "Past medical history",
                                           TEXT_LIMIT, errors),
        "expected_discharge_date": date_field(form, "expected_discharge_date", "Expected discharge date", errors),
    }
    edd = values["expected_discharge_date"]
    if edd and edd < date.today():
        errors["expected_discharge_date"] = "Expected discharge date can't be in the past."
    return values


@bp.route("/wards/<code>/admit", methods=["GET", "POST"])
@requires(permissions.MANAGE_PATIENTS)
def admit(code):
    ward = get_ward(code)
    context = dict(ward=ward, beds=empty_beds(ward), sex_options=SEX_OPTIONS, resus_options=list(ResusStatus))

    if request.method == "GET":
        query = request.args.get("find", "")
        patient = find_patient(query)
        if patient:
            current = next((a for a in patient.admissions if a.is_current), None)
            if current:
                flash(f"{patient.first_name} {patient.last_name} is already admitted to {current.ward.code}"
                      f"{' (' + current.bed.label + ')' if current.bed else ''}. "
                      "To bring them here, use Move on that ward's patient list.", "error")
                patient = None
                query = ""
        elif query:
            flash("No patient found with that number. Enter their details below as a new patient.", "info")
        values = {"resus_status": ResusStatus.NOT_DOCUMENTED.value}
        if patient:
            values.update(allergies=patient.allergies, resus_status=patient.resus_status.value)
        elif query:
            digits = clean_nhs_number(query)
            values["nhs_number" if digits.isdigit() else "hospital_number"] = digits if digits.isdigit() else query.upper()
        return render_template("admit.html", patient=patient, values=values, errors={}, **context)

    errors = {}
    patient = None
    if request.form.get("patient_id"):
        patient = db.session.get(Patient, request.form.get("patient_id", type=int)) or abort(400)
        patient_values = {}
    else:
        patient_values = read_patient_form(request.form, errors)
    allergies, resus = read_safety_fields(request.form, errors)
    admission_values = read_admission_form(ward, request.form, errors)

    if patient and any(a.is_current for a in patient.admissions):
        errors["patient_id"] = "This patient has already been admitted somewhere else."

    if errors:
        values = {**request.form.to_dict()}
        return render_template("admit.html", patient=patient, values=values, errors=errors, **context), 400

    if patient is None:
        patient = Patient(**patient_values)
        db.session.add(patient)
    else:
        changed = [n for n, v in [("allergies", allergies), ("resus_status", resus)] if getattr(patient, n) != v]
        if changed:
            audit.log("update", "patients", patient.id, details="changed: " + ", ".join(changed))
    patient.allergies = allergies
    patient.resus_status = resus

    admission = Admission(patient=patient, ward=ward, **admission_values)
    db.session.add(admission)
    try:
        db.session.flush()
    except IntegrityError:  # someone took the bed or admitted the patient a moment ago
        db.session.rollback()
        flash("That bed or patient was just updated by someone else. Please check and try again.", "error")
        return redirect(url_for("patients.admit", code=ward.code))

    add_to_draft(admission)
    audit.log("admit", "admissions", admission.id, details=f"ward={ward.code} bed={bed_label(admission)}")
    db.session.commit()
    flash(f"{patient.first_name} {patient.last_name} admitted to "
          f"{admission.bed.label if admission.bed else ward.code + ' (no bed yet)'}.", "info")
    return redirect(url_for("patients.ward_patients", code=ward.code))


# --- Move -----------------------------------------------------------------------

def get_current_admission(admission_id):
    admission = db.get_or_404(Admission, admission_id)
    if not admission.is_current:
        flash("This patient has already been discharged.", "error")
        abort(redirect(url_for("patients.ward_patients", code=admission.ward.code)))
    return admission


@bp.route("/admissions/<int:admission_id>/move", methods=["GET", "POST"])
@requires(permissions.MANAGE_PATIENTS)
def move(admission_id):
    admission = get_current_admission(admission_id)
    wards = db.session.scalars(db.select(Ward).where(Ward.is_active).order_by(Ward.name)).all()
    options = [(w, empty_beds(w)) for w in wards]

    if request.method == "GET":
        return render_template("move.html", admission=admission, options=options, error=None)

    # Destination is "bed:<id>" or "ward:<id>" (a ward, with no bed yet)
    kind, _, raw_id = request.form.get("destination", "").partition(":")
    error = None
    bed = ward = None
    if kind == "bed" and raw_id.isdigit():
        bed = db.session.get(Bed, int(raw_id))
        ward = bed.ward if bed else None
    elif kind == "ward" and raw_id.isdigit():
        ward = db.session.get(Ward, int(raw_id))
    if ward is None or not ward.is_active:
        error = "Choose where the patient is moving to."
    elif bed is not None:
        bed, error = check_bed(ward, str(bed.id))
    elif ward.id == admission.ward_id and admission.bed is None:
        error = "The patient is already on this ward without a bed."

    if error:
        return render_template("move.html", admission=admission, options=options, error=error), 400

    patient = admission.patient
    try:
        if ward.id == admission.ward_id:
            old = bed_label(admission) if admission.bed else "no bed"
            admission.bed = bed
            db.session.flush()
            audit.log("move", "admissions", admission.id, details=f"bed {old} -> {bed.label if bed else 'no bed'}")
            message = f"{patient.first_name} {patient.last_name} moved to {bed.label if bed else 'no bed'}."
        else:
            new = transfer(admission, ward, bed)
            message = f"{patient.first_name} {patient.last_name} transferred to {ward.code}" \
                      f"{' (' + bed.label + ')' if bed else ''}."
            admission = new
    except IntegrityError:
        db.session.rollback()
        flash("That bed was just taken by someone else. Please choose another.", "error")
        return redirect(url_for("patients.move", admission_id=admission_id))

    db.session.commit()
    flash(message, "info")
    return redirect(url_for("patients.ward_patients", code=ward.code))


def transfer(admission, ward, bed):
    """End this ward stay and start a new one on another ward, keeping the clinical details."""
    now = datetime.now(timezone.utc)
    admission.discharged_at = now
    admission.discharge_destination = f"Transferred to {ward.code}"
    db.session.flush()  # close the old stay first, so the one-current-admission rule holds

    new = Admission(
        patient=admission.patient, ward=ward, bed=bed, admitted_at=now,
        consultant=admission.consultant, reason_for_admission=admission.reason_for_admission,
        past_medical_history=admission.past_medical_history,
        expected_discharge_date=admission.expected_discharge_date,
    )
    db.session.add(new)
    db.session.flush()

    # Outstanding tasks follow the patient
    for task in list(admission.tasks):  # copy: moving a task removes it from admission.tasks
        if task.completed_at is None:
            task.admission = new

    add_to_draft(new, copy_from=latest_entry(admission))
    audit.log("transfer", "admissions", admission.id, details=f"to ward={ward.code} new_admission={new.id}")
    return new


# --- Discharge ------------------------------------------------------------------

@bp.route("/admissions/<int:admission_id>/discharge", methods=["GET", "POST"])
@requires(permissions.MANAGE_PATIENTS)
def discharge(admission_id):
    admission = get_current_admission(admission_id)
    open_tasks = [t for t in admission.tasks if t.completed_at is None]

    if request.method == "GET":
        return render_template("discharge.html", admission=admission, open_tasks=open_tasks,
                               destinations=DISCHARGE_DESTINATIONS, error=None)

    destination = request.form.get("destination", "")
    if destination not in DISCHARGE_DESTINATIONS:
        return render_template("discharge.html", admission=admission, open_tasks=open_tasks,
                               destinations=DISCHARGE_DESTINATIONS,
                               error="Choose where the patient is going."), 400

    admission.discharged_at = datetime.now(timezone.utc)
    admission.discharge_destination = destination
    audit.log("discharge", "admissions", admission.id, details=f"destination={destination}")
    db.session.commit()
    patient = admission.patient
    freed = f" {admission.bed.label} is now free." if admission.bed else ""
    flash(f"{patient.first_name} {patient.last_name} discharged ({destination.lower()}).{freed}", "info")
    return redirect(url_for("patients.ward_patients", code=admission.ward.code))
