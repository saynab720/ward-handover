import secrets
from urllib.parse import urlsplit

from flask import (
    Blueprint, abort, flash, redirect, render_template, request, session, url_for,
)
from flask_login import LoginManager, current_user, login_required, login_user, logout_user
from werkzeug.security import check_password_hash, generate_password_hash

from . import audit as audit_log
from .db import db
from .models import Staff

bp = Blueprint("auth", __name__)

login_manager = LoginManager()
login_manager.login_view = "auth.login"
login_manager.login_message = "Please sign in to view patient information."
login_manager.session_protection = "strong"

# Compared against when the email isn't found, so a wrong email takes as long as a wrong password
_DUMMY_HASH = generate_password_hash(secrets.token_hex(16))


@login_manager.user_loader
def load_user(staff_id):
    staff = db.session.get(Staff, int(staff_id))
    return staff if staff and staff.is_active else None


def audit(action, staff_id=None, details=None):
    audit_log.log(action, "staff", record_id=staff_id, details=details, staff_id=staff_id)
    db.session.commit()


def is_safe_next(target):
    """Only allow redirects back to pages on this site."""
    if not target or "\\" in target:  # browsers treat "\" like "/", so "/\evil.com" means "//evil.com"
        return False
    parts = urlsplit(target)
    return not parts.scheme and not parts.netloc and target.startswith("/") and not target.startswith("//")


# --- CSRF protection for every form --------------------------------------------

def csrf_token():
    if "_csrf" not in session:
        session["_csrf"] = secrets.token_urlsafe(32)
    return session["_csrf"]


@bp.before_app_request
def check_csrf():
    if request.method == "POST":
        sent = request.form.get("csrf_token", "")
        expected = session.get("_csrf", "")
        if not expected or not secrets.compare_digest(sent, expected):
            abort(400, "Form expired or invalid. Go back, refresh the page and try again.")


@bp.app_context_processor
def inject_csrf():
    return {"csrf_token": csrf_token}


# --- Routes --------------------------------------------------------------------

@bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("handover.index"))

    email = ""
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        staff = db.session.scalar(db.select(Staff).where(db.func.lower(Staff.email) == email))

        password_ok = check_password_hash(staff.password_hash if staff else _DUMMY_HASH, password)
        if staff and password_ok and staff.is_active:
            session.clear()  # new session on login, so an old session ID can't be reused
            login_user(staff)
            session.permanent = True  # expires after PERMANENT_SESSION_LIFETIME of inactivity
            audit("login", staff.id)
            next_page = request.args.get("next")
            return redirect(next_page if is_safe_next(next_page) else url_for("handover.index"))

        audit("login_failed", staff.id if staff else None, details=f"email={email[:255]}")
        flash("Email or password is incorrect.", "error")

    return render_template("login.html", email=email)


@bp.route("/logout", methods=["POST"])
@login_required
def logout():
    audit("logout", current_user.id)
    logout_user()
    session.clear()
    flash("You have signed out.", "info")
    return redirect(url_for("auth.login"))
