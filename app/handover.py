import re
from datetime import date, datetime, timedelta, timezone

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy.exc import IntegrityError

from . import audit, permissions
from .db import db
from .models import (
    Admission, Handover, HandoverEntry, HandoverStatus, Priority, Shift, Staff, Task, Ward,
)
from .permissions import requires

bp = Blueprint("handover", __name__)


@bp.before_request
@login_required
def require_login():
    """Every page in this blueprint shows patient data, so all need a signed-in user
    whose role is allowed to see it."""
    if not permissions.can(permissions.VIEW):
        if request.endpoint == "handover.index":
            return render_template("no_patient_access.html")
        permissions.deny(permissions.VIEW)

# Order shifts within a day so "latest handover" picks the right one
SHIFT_ORDER = {Shift.EARLY: 0, Shift.LONG_DAY: 1, Shift.LATE: 2, Shift.NIGHT: 3}

TEXT_LIMIT = 2000  # max characters for free-text fields

# Editable entry fields: name -> max length (None for yes/no tick boxes)
ENTRY_TEXT_FIELDS = {
    "situation": TEXT_LIMIT, "background": TEXT_LIMIT,
    "assessment": TEXT_LIMIT, "recommendation": TEXT_LIMIT,
    "obs_frequency": 50, "mobility": 100, "diet": 100, "infection_control": 100,
}
ENTRY_FLAG_FIELDS = ["falls_risk", "pressure_risk", "is_flagged"]


def bed_sort_key(label):
    """Sort "Bay 1 Bed 2" before "Bay 1 Bed 10", and side rooms after bays."""
    return [(0, int(part)) if part.isdigit() else (1, part) for part in re.split(r"(\d+)", label)]


def bed_label(admission):
    return admission.bed.label if admission.bed else "zzz"  


def age_on(dob, today=None):
    today = today or date.today()
    return today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))


def news2_band(score):
    if score is None:
        return "unknown"
    if score >= 7:
        return "high"
    if score >= 5:
        return "medium"
    return "low"


def version_of(entry):
    """A marker that changes whenever the entry is saved, to catch two people editing at once."""
    return entry.updated_at.isoformat() if entry.updated_at else ""


@bp.app_template_filter("age")
def age_filter(dob):
    return age_on(dob)


@bp.app_template_global()
def news2_class(score):
    return news2_band(score)


def shift_key(shift_date, shift):
    return (shift_date, SHIFT_ORDER[shift])


def latest_of(handovers):
    return max(handovers, key=lambda h: shift_key(h.shift_date, h.shift), default=None)


def open_draft(ward):
    return next((h for h in ward.handovers if h.status == HandoverStatus.DRAFT), None)


def can_start_next(handover):
    """The "Start next handover" button shows on the ward's latest handover once it's completed."""
    ward = handover.ward
    return (handover.status == HandoverStatus.COMPLETED
            and latest_of(ward.handovers) is handover
            and open_draft(ward) is None)


def get_draft_handover(handover_id):
    """Load a handover for editing, refusing if it has been completed."""
    handover = db.get_or_404(Handover, handover_id)
    if handover.status != HandoverStatus.DRAFT:
        flash("This handover has been completed and can no longer be changed.", "error")
        return None, redirect(url_for("handover.view_handover", handover_id=handover.id))
    return handover, None


def get_entry(handover, entry_id):
    entry = db.get_or_404(HandoverEntry, entry_id)
    if entry.handover_id != handover.id:
        abort(404)
    return entry


def back_to(handover, entry=None):
    anchor = f"entry-{entry.id}" if entry else None
    return redirect(url_for("handover.view_handover", handover_id=handover.id, _anchor=anchor))


# --- Viewing ---------------------------------------------------------------------

@bp.route("/")
def index():
    wards = db.session.scalars(
        db.select(Ward).where(Ward.is_active).order_by(Ward.name)
    ).all()
    current_patients = {
        ward_id: count
        for ward_id, count in db.session.execute(
            db.select(Admission.ward_id, db.func.count())
            .where(Admission.discharged_at.is_(None))
            .group_by(Admission.ward_id)
        )
    }
    return render_template("index.html", wards=wards, current_patients=current_patients)


@bp.route("/wards/<code>/handover")
def latest_handover(code):
    ward = db.session.scalar(db.select(Ward).where(Ward.code == code)) or abort(404)
    if not ward.handovers:
        return render_template("no_handover.html", ward=ward)
    return redirect(url_for("handover.view_handover", handover_id=latest_of(ward.handovers).id))


@bp.route("/handovers/<int:handover_id>")
def view_handover(handover_id):
    handover = db.get_or_404(Handover, handover_id)

    entries = sorted(handover.entries, key=lambda e: bed_sort_key(bed_label(e.admission)))
    admission_ids = [e.admission_id for e in entries]
    open_tasks = db.session.scalars(
        db.select(Task)
        .where(Task.admission_id.in_(admission_ids), Task.completed_at.is_(None))
        .order_by(Task.due_at.is_(None), Task.due_at)
    ).all()
    tasks_by_admission = {}
    for task in open_tasks:
        tasks_by_admission.setdefault(task.admission_id, []).append(task)

    others = sorted(
        (h for h in handover.ward.handovers if h.id != handover.id),
        key=lambda h: shift_key(h.shift_date, h.shift),
        reverse=True,
    )

    receivers = []
    if handover.status == HandoverStatus.DRAFT:
        receivers = db.session.scalars(
            db.select(Staff)
            .where(Staff.is_active, Staff.id != handover.handed_over_by_id)
            .order_by(Staff.home_ward_id != handover.ward_id, Staff.last_name)
        ).all()

    page = render_template(
        "handover.html",
        handover=handover,
        is_draft=handover.status == HandoverStatus.DRAFT,
        entries=entries,
        tasks_by_admission=tasks_by_admission,
        flagged_count=sum(1 for e in entries if e.is_flagged),
        open_task_count=len(open_tasks),
        other_handovers=others[:10],
        receivers=receivers,
        priorities=list(Priority),
        not_updated_count=sum(1 for e in entries if e.not_updated_this_shift),
        can_start_next=can_start_next(handover),
    )
    audit.log("view", "handovers", handover.id)
    db.session.commit()
    return page


# --- Editing a patient entry -------------------------------------------------------

def read_entry_form(form):
    """Check the submitted form. Returns (values, errors)."""
    values, errors = {}, {}
    for name, limit in ENTRY_TEXT_FIELDS.items():
        value = form.get(name, "").strip()
        if len(value) > limit:
            errors[name] = f"Must be {limit} characters or fewer (currently {len(value)})."
        values[name] = value or None
    for name in ENTRY_FLAG_FIELDS:
        values[name] = form.get(name) == "on"

    raw = form.get("news2_score", "").strip()
    if raw == "":
        values["news2_score"] = None
    elif raw.isdigit() and 0 <= int(raw) <= 20:
        values["news2_score"] = int(raw)
    else:
        errors["news2_score"] = "NEWS2 must be a whole number from 0 to 20, or left blank."
        values["news2_score"] = raw
    return values, errors


@bp.route("/handovers/<int:handover_id>/entries/<int:entry_id>/edit", methods=["GET", "POST"])
@requires(permissions.EDIT_ENTRY)
def edit_entry(handover_id, entry_id):
    handover, refused = get_draft_handover(handover_id)
    if refused:
        return refused
    entry = get_entry(handover, entry_id)

    if request.method == "GET":
        values = {name: getattr(entry, name) for name in [*ENTRY_TEXT_FIELDS, *ENTRY_FLAG_FIELDS, "news2_score"]}
        return render_template("entry_edit.html", handover=handover, entry=entry,
                               values=values, errors={}, version=version_of(entry))

    values, errors = read_entry_form(request.form)
    conflict = request.form.get("version") != version_of(entry)
    if conflict:
        errors["_conflict"] = (
            f"{entry.updated_by.full_name} saved changes to this patient while you were editing. "
            "Their version is shown below yours. Check it, then save again to keep your changes."
        )
    if errors:
        return render_template("entry_edit.html", handover=handover, entry=entry,
                               values=values, errors=errors, version=version_of(entry),
                               conflict=conflict), 400 if not conflict else 409

    changed = [name for name, value in values.items() if getattr(entry, name) != value]
    if changed:
        for name in changed:
            setattr(entry, name, values[name])
        entry.updated_by_id = current_user.id
        audit.log("update", "handover_entries", entry.id, details="changed: " + ", ".join(changed))
        db.session.commit()
        flash(f"Saved changes for {entry.admission.patient.first_name} {entry.admission.patient.last_name}.", "info")
    return back_to(handover, entry)


# --- Ward notes -----------------------------------------------------------------

@bp.route("/handovers/<int:handover_id>/notes", methods=["GET", "POST"])
@requires(permissions.EDIT_NOTES)
def edit_notes(handover_id):
    handover, refused = get_draft_handover(handover_id)
    if refused:
        return refused

    if request.method == "POST":
        notes = request.form.get("ward_notes", "").strip()
        if len(notes) > TEXT_LIMIT:
            return render_template("notes_edit.html", handover=handover, notes=notes,
                                   error=f"Must be {TEXT_LIMIT} characters or fewer."), 400
        if (notes or None) != handover.ward_notes:
            handover.ward_notes = notes or None
            audit.log("update", "handovers", handover.id, details="changed: ward_notes")
            db.session.commit()
            flash("Ward notes saved.", "info")
        return back_to(handover)

    return render_template("notes_edit.html", handover=handover, notes=handover.ward_notes or "", error=None)


# --- Tasks ----------------------------------------------------------------------

def parse_due(raw):
    """Read a datetime-local input ("2026-10-12T14:30"). Returns (value, error)."""
    if not raw:
        return None, None
    try:
        return datetime.strptime(raw, "%Y-%m-%dT%H:%M").replace(tzinfo=timezone.utc), None
    except ValueError:
        return None, "Due time is not a valid date and time."


@bp.route("/handovers/<int:handover_id>/entries/<int:entry_id>/tasks", methods=["POST"])
@requires(permissions.ADD_TASK)
def add_task(handover_id, entry_id):
    handover, refused = get_draft_handover(handover_id)
    if refused:
        return refused
    entry = get_entry(handover, entry_id)

    description = request.form.get("description", "").strip()
    due_at, due_error = parse_due(request.form.get("due_at", "").strip())
    try:
        priority = Priority(request.form.get("priority", "routine"))
    except ValueError:
        priority = None

    if not description or len(description) > 500 or due_error or priority is None:
        flash(due_error or ("Task not added: description is required (max 500 characters)."
                            if priority else "Task not added: choose a valid priority."), "error")
        return back_to(handover, entry)

    task = Task(admission_id=entry.admission_id, description=description, priority=priority,
                due_at=due_at, created_by_id=current_user.id)
    db.session.add(task)
    db.session.flush()
    audit.log("create", "tasks", task.id)
    db.session.commit()
    flash("Task added.", "info")
    return back_to(handover, entry)


@bp.route("/handovers/<int:handover_id>/tasks/<int:task_id>/complete", methods=["POST"])
@requires(permissions.COMPLETE_TASK)
def complete_task(handover_id, task_id):
    handover, refused = get_draft_handover(handover_id)
    if refused:
        return refused
    task = db.get_or_404(Task, task_id)
    entry = next((e for e in handover.entries if e.admission_id == task.admission_id), None)
    if entry is None:
        abort(404)

    if task.completed_at is None:
        task.completed_at = datetime.now(timezone.utc)
        task.completed_by_id = current_user.id
        audit.log("complete", "tasks", task.id)
        db.session.commit()
        flash(f"Task done: {task.description}", "info")
    return back_to(handover, entry)


# --- Completing the handover ----------------------------------------------------

@bp.route("/handovers/<int:handover_id>/complete", methods=["POST"])
@requires(permissions.MANAGE_HANDOVER)
def complete_handover(handover_id):
    handover, refused = get_draft_handover(handover_id)
    if refused:
        return refused

    receiver_id = request.form.get("received_by_id", type=int)
    receiver = db.session.get(Staff, receiver_id) if receiver_id else None
    if receiver is None or not receiver.is_active:
        flash("Choose who is receiving the handover.", "error")
        return back_to(handover)
    if receiver.id == handover.handed_over_by_id:
        flash("The person receiving the handover can't be the person giving it.", "error")
        return back_to(handover)

    handover.received_by_id = receiver.id
    handover.status = HandoverStatus.COMPLETED
    handover.completed_at = datetime.now(timezone.utc)
    audit.log("complete", "handovers", handover.id, details=f"received_by={receiver.id}")
    db.session.commit()
    flash(f"Handover completed and received by {receiver.full_name}. It is now locked.", "info")
    return back_to(handover)


# --- Starting the next handover -------------------------------------------------

def suggest_next(ward, now=None):
    """Suggest the shift after the ward's latest handover. Returns (date, Shift)."""
    now = now or datetime.now()
    latest = latest_of(ward.handovers)
    if latest is None:
        return now.date(), (Shift.NIGHT if now.hour >= 19 or now.hour < 7 else Shift.LONG_DAY)
    if latest.shift == Shift.EARLY:
        return latest.shift_date, Shift.LATE
    if latest.shift in (Shift.LATE, Shift.LONG_DAY):
        return latest.shift_date, Shift.NIGHT
    # After a night: use whichever day pattern this ward used last
    day_shifts = [h for h in ward.handovers if h.shift in (Shift.EARLY, Shift.LONG_DAY)]
    pattern = latest_of(day_shifts).shift if day_shifts else Shift.LONG_DAY
    return latest.shift_date + timedelta(days=1), pattern


def plan_next(ward):
    """Work out which patients carry forward, which are new and which have left the ward."""
    source = latest_of(ward.handovers)
    source_entries = {e.admission_id: e for e in source.entries} if source else {}
    current = db.session.scalars(
        db.select(Admission).where(Admission.ward_id == ward.id, Admission.discharged_at.is_(None))
    ).all()
    current = sorted(current, key=lambda a: bed_sort_key(bed_label(a)))
    current_ids = {a.id for a in current}
    return {
        "source": source,
        "carried": [(a, source_entries[a.id]) for a in current if a.id in source_entries],
        "new": [a for a in current if a.id not in source_entries],
        "leaving": [e.admission for e in source_entries.values() if e.admission_id not in current_ids],
    }


def check_new_shift(ward, form):
    """Validate the chosen date and shift. Returns (shift_date, shift, error)."""
    try:
        shift_date = date.fromisoformat(form.get("shift_date", ""))
        shift = Shift(form.get("shift", ""))
    except ValueError:
        return None, None, "Choose a valid date and shift."
    if shift_date > date.today() + timedelta(days=1):
        return shift_date, shift, "A handover can't be started more than a day ahead."
    latest = latest_of(ward.handovers)
    if latest and shift_key(shift_date, shift) <= shift_key(latest.shift_date, latest.shift):
        return shift_date, shift, (
            f"This must be after the latest handover "
            f"({latest.shift.value.replace('_', ' ')} on {latest.shift_date.strftime('%d/%m/%Y')})."
        )
    return shift_date, shift, None


@bp.route("/wards/<code>/handovers/new", methods=["GET", "POST"])
@requires(permissions.MANAGE_HANDOVER)
def start_handover(code):
    ward = db.session.scalar(db.select(Ward).where(Ward.code == code)) or abort(404)

    draft = open_draft(ward)
    if draft:
        flash("This ward already has a handover in progress, so it has been opened instead.", "info")
        return redirect(url_for("handover.view_handover", handover_id=draft.id))

    plan = plan_next(ward)
    if request.method == "GET":
        shift_date, shift = suggest_next(ward)
        return render_template("handover_new.html", ward=ward, plan=plan, shifts=list(Shift),
                               shift_date=shift_date, shift=shift, error=None)

    shift_date, shift, error = check_new_shift(ward, request.form)
    if error:
        return render_template("handover_new.html", ward=ward, plan=plan, shifts=list(Shift),
                               shift_date=shift_date, shift=shift, error=error), 400

    now = datetime.now(timezone.utc)
    handover = Handover(ward=ward, shift_date=shift_date, shift=shift, handed_over_by_id=current_user.id,
                        ward_notes=plan["source"].ward_notes if plan["source"] else None)
    db.session.add(handover)

    # created_at == updated_at marks each entry as "not yet updated this shift"
    for admission, previous in plan["carried"]:
        copied = {name: getattr(previous, name)
                  for name in [*ENTRY_TEXT_FIELDS, *ENTRY_FLAG_FIELDS, "news2_score"]}
        db.session.add(HandoverEntry(handover=handover, admission=admission, created_at=now, updated_at=now,
                                     updated_by_id=previous.updated_by_id, **copied))
    for admission in plan["new"]:
        db.session.add(HandoverEntry(handover=handover, admission=admission, created_at=now, updated_at=now,
                                     updated_by_id=current_user.id))

    try:
        db.session.flush()
    except IntegrityError:  # someone else started the same shift a moment ago
        db.session.rollback()
        flash("A handover for that shift already exists.", "error")
        return redirect(url_for("handover.latest_handover", code=ward.code))

    audit.log("create", "handovers", handover.id, details=(
        f"carried={len(plan['carried'])} new={len(plan['new'])} left={len(plan['leaving'])}"
    ))
    db.session.commit()
    flash("Handover started. Patients marked \u201cNot updated this shift\u201d still need reviewing.", "info")
    return redirect(url_for("handover.view_handover", handover_id=handover.id))
