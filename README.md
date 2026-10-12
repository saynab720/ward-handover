# Ward Handover

A Flask app for nursing shift handovers: SBAR handover per patient, NEWS2, risks,
outstanding tasks, and admitting, moving and discharging patients.

> **Demo only.** This app contains fictional data and has not been through NHS
> information governance, DSPT, DPIA or DCB0129/0160 clinical safety assessment.
> Do not enter real patient information.

## Run locally

```
python -m venv venv
venv\Scripts\activate            # Windows (macOS/Linux: source venv/bin/activate)
pip install -r requirements.txt
flask --app app seed-demo        # create tables and fictional demo data
flask --app app --debug run
```

Open http://127.0.0.1:5000 and sign in as `amara.okafor@demo.nhs.test` / `demo1234`.

`flask --app app seed-demo --reset` wipes the database and starts again.

## Tests

```
pip install pytest
python -m pytest
```

## Deploy on Render

`render.yaml` sets up the web app and a PostgreSQL database.

1. Push this repository to GitHub.
2. In Render: **New > Blueprint**, pick the repository.
3. Enter a `DEMO_PASSWORD` when asked. All demo accounts use it.
4. Deploy. The first start creates the tables and demo data.

Settings (environment variables):

| Variable | Purpose |
|---|---|
| `DATABASE_URL` | Database connection. Local SQLite file if not set. |
| `SECRET_KEY` | Signs login sessions. Generated in `instance/secret_key` locally if not set. |
| `SESSION_COOKIE_SECURE` | `1` to send the login cookie over HTTPS only. |
| `BEHIND_PROXY` | `1` when running behind Render's HTTPS proxy. |
| `DEMO_PASSWORD` | Password for the demo accounts. `demo1234` if not set. |
