import os
import secrets
from datetime import timedelta

from flask import Flask

from . import db


def load_secret_key(app):
    """Use SECRET_KEY from the environment, or create one in the instance folder for development."""
    if os.environ.get("SECRET_KEY"):
        return os.environ["SECRET_KEY"]
    path = os.path.join(app.instance_path, "secret_key")
    if not os.path.exists(path):
        with open(path, "w") as f:
            f.write(secrets.token_hex(32))
    with open(path) as f:
        return f.read().strip()


def create_app(test_config=None):
    app = Flask(__name__, instance_relative_config=True)
    os.makedirs(app.instance_path, exist_ok=True)
    app.config.update(
        SECRET_KEY=load_secret_key(app),
        PERMANENT_SESSION_LIFETIME=timedelta(minutes=30),  # sign out after 30 min of inactivity
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.environ.get("SESSION_COOKIE_SECURE") == "1",  # set to 1 behind HTTPS
    )
    if test_config:
        app.config.update(test_config)

    if os.environ.get("BEHIND_PROXY") == "1":
        # On Render, requests arrive through a proxy that handles HTTPS; trust its headers
        from werkzeug.middleware.proxy_fix import ProxyFix
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    db.init_app(app)

    from .auth import bp as auth_bp, login_manager
    login_manager.init_app(app)
    app.register_blueprint(auth_bp)

    from .handover import bp as handover_bp
    app.register_blueprint(handover_bp)

    from .patients import bp as patients_bp
    app.register_blueprint(patients_bp)

    return app
