"""Who can do what. Change ROLE_PERMISSIONS to change access for a whole role."""
from functools import wraps

from flask import abort, request
from flask_login import current_user

from .models import StaffRole

VIEW = "view"                        # handovers and patient lists
COMPLETE_TASK = "complete_task"
ADD_TASK = "add_task"
EDIT_ENTRY = "edit_entry"            # SBAR and care details for a patient
EDIT_NOTES = "edit_notes"            # ward notes at the top of a handover
MANAGE_HANDOVER = "manage_handover"  # start and complete handovers
MANAGE_PATIENTS = "manage_patients"  # admit, move, discharge
MANAGE_STAFF = "manage_staff"

DESCRIPTIONS = {
    VIEW: "view patient information",
    COMPLETE_TASK: "tick off tasks",
    ADD_TASK: "add tasks",
    EDIT_ENTRY: "edit handover entries",
    EDIT_NOTES: "edit ward notes",
    MANAGE_HANDOVER: "start or complete handovers",
    MANAGE_PATIENTS: "admit, move or discharge patients",
    MANAGE_STAFF: "manage staff accounts",
}

_CLINICAL = {VIEW, COMPLETE_TASK, ADD_TASK, EDIT_ENTRY, MANAGE_PATIENTS}

ROLE_PERMISSIONS = {
    StaffRole.HCA: {VIEW, COMPLETE_TASK},
    StaffRole.NURSE: _CLINICAL,
    StaffRole.DOCTOR: _CLINICAL,
    StaffRole.NURSE_IN_CHARGE: _CLINICAL | {EDIT_NOTES, MANAGE_HANDOVER},
    StaffRole.WARD_MANAGER: _CLINICAL | {EDIT_NOTES, MANAGE_HANDOVER},
    StaffRole.ADMIN: {MANAGE_STAFF},  # no access to patient data
}


def can(permission, user=None):
    user = user or current_user
    if not user or not user.is_authenticated:
        return False
    return permission in ROLE_PERMISSIONS.get(user.role, set())


def deny(permission):
    """Record the refused attempt, then show the "no permission" page."""
    from . import audit
    from .db import db

    audit.log("denied", "permissions", details=f"{permission} {request.method} {request.path}"[:500])
    db.session.commit()
    abort(403, description=DESCRIPTIONS.get(permission, permission))


def requires(permission):
    """Route decorator: refuse with 403 unless the signed-in user's role allows this."""
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if not can(permission):
                deny(permission)
            return view(*args, **kwargs)
        return wrapped
    return decorator
