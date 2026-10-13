import os

import click
from flask.cli import with_appcontext
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()


def database_url(app):
    """DATABASE_URL from the environment (e.g. Render's PostgreSQL), or a local SQLite file."""
    url = os.environ.get("DATABASE_URL")
    if not url:
        return "sqlite:///" + os.path.join(app.instance_path, "ward_handover.db")

    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


def init_app(app):
    """Connect the database to the Flask app and register the init-db command."""
    app.config.setdefault("SQLALCHEMY_DATABASE_URI", database_url(app))

    app.config.setdefault("SQLALCHEMY_ENGINE_OPTIONS", {"pool_pre_ping": True})
    db.init_app(app)
    app.cli.add_command(init_db_command)

    from .seed import seed_demo_command
    app.cli.add_command(seed_demo_command)


@click.command("init-db")
@with_appcontext
def init_db_command():
    """Create all database tables."""
    from . import models  

    db.create_all()
    click.echo("Database initialised.")
