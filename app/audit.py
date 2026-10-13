from flask_login import current_user

from .db import db
from .models import AuditLog


def log(action, table_name, record_id=None, details=None, staff_id=None):
    if staff_id is None and current_user and current_user.is_authenticated:
        staff_id = current_user.id
    db.session.add(AuditLog(
        staff_id=staff_id, action=action, table_name=table_name,
        record_id=record_id, details=details,
    ))
