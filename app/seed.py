"""Fictional demo data for development. Never use real patient details here.

NHS numbers are generated in the 999 range, which NHS England reserves for
testing and never issues to real patients.
"""
import os
from datetime import date, datetime, time, timedelta, timezone

import click
from flask.cli import with_appcontext
from werkzeug.security import generate_password_hash

from .db import db
from .models import (
    Admission, Bed, Handover, HandoverEntry, HandoverStatus, Patient, Priority,
    ResusStatus, Shift, Staff, StaffRole, Task, Ward,
)

# Set DEMO_PASSWORD on a public server, so the demo accounts don't use a password anyone can read here
DEMO_PASSWORD = os.environ.get("DEMO_PASSWORD") or "demo1234"


def fake_nhs_number(n):
    """Build a valid 10-digit NHS number in the reserved 999 test range."""
    while True:
        first9 = f"999{n:06d}"
        total = sum(int(d) * w for d, w in zip(first9, range(10, 1, -1)))
        check = 11 - total % 11
        if check == 11:
            check = 0
        if check != 10:  # 10 is invalid, so try the next number
            return first9 + str(check)
        n += 1


def at(day, hour, minute=0):
    return datetime.combine(day, time(hour, minute), tzinfo=timezone.utc)


@click.command("seed-demo")
@with_appcontext
@click.option("--reset", is_flag=True, help="Delete all existing data first.")
@click.option("--if-empty", is_flag=True, help="Only add data to an empty database; otherwise do nothing.")
def seed_demo_command(reset, if_empty):
    """Fill the database with fictional demo data."""
    if reset:
        db.drop_all()
    db.create_all()

    if db.session.execute(db.select(Ward)).first() and not reset:
        if if_empty:
            click.echo("Database already has data, leaving it alone.")
            return
        raise click.ClickException("Database already has data. Use --reset to wipe it first.")

    today = date.today()
    yesterday = today - timedelta(days=1)

    # Wards and beds
    w12 = Ward(code="W12", name="Elderly Care", specialty="Care of the Elderly")
    amu = Ward(code="AMU", name="Acute Medical Unit", specialty="Acute Medicine")
    db.session.add_all([w12, amu])

    beds = {}
    for ward in (w12, amu):
        for bay in (1, 2):
            for n in range(1, 5):
                label = f"Bay {bay} Bed {n}"
                beds[(ward.code, label)] = Bed(ward=ward, label=label)
        for n in (1, 2):
            label = f"SR{n}"
            beds[(ward.code, label)] = Bed(ward=ward, label=label, is_side_room=True)
    db.session.add_all(beds.values())

    # Staff (all fictional)
    pw = generate_password_hash(DEMO_PASSWORD)
    staff = {
        "amara": Staff(first_name="Amara", last_name="Okafor", email="amara.okafor@demo.nhs.test",
                       role=StaffRole.NURSE_IN_CHARGE, registration_number="12A3456E", home_ward=w12),
        "tom": Staff(first_name="Tom", last_name="Hughes", email="tom.hughes@demo.nhs.test",
                     role=StaffRole.NURSE, registration_number="15B7890W", home_ward=w12),
        "priya": Staff(first_name="Priya", last_name="Shah", email="priya.shah@demo.nhs.test",
                       role=StaffRole.NURSE, registration_number="19C2468S", home_ward=w12),
        "dan": Staff(first_name="Dan", last_name="Morris", email="dan.morris@demo.nhs.test",
                     role=StaffRole.HCA, home_ward=w12),
        "li": Staff(first_name="Li", last_name="Chen", email="li.chen@demo.nhs.test",
                    role=StaffRole.DOCTOR, registration_number="7654321", home_ward=w12),
        "grace": Staff(first_name="Grace", last_name="Adeyemi", email="grace.adeyemi@demo.nhs.test",
                       role=StaffRole.WARD_MANAGER, registration_number="08D1357E", home_ward=w12),
        "sam": Staff(first_name="Sam", last_name="Patel", email="sam.patel@demo.nhs.test",
                     role=StaffRole.NURSE_IN_CHARGE, registration_number="16E9753A", home_ward=amu),
        "admin": Staff(first_name="Demo", last_name="Admin", email="admin@demo.nhs.test",
                       role=StaffRole.ADMIN),
    }
    for s in staff.values():
        s.password_hash = pw
    db.session.add_all(staff.values())

    # Patients and admissions (all fictional)
    patient_rows = [
        # name, dob, sex, allergies, resus, ward, bed, days ago, consultant, reason, pmh
        ("Margaret", "Ellis", date(1938, 3, 14), "Female", "Penicillin (rash)", ResusStatus.DNACPR,
         w12, "Bay 1 Bed 1", 6, "Dr R. Bennett", "Fall at home, # NOF repaired day 3",
         "AF, hypertension, mild dementia"),
        ("George", "Whitfield", date(1941, 11, 2), "Male", "NKDA", ResusStatus.FOR_RESUS,
         w12, "Bay 1 Bed 2", 3, "Dr R. Bennett", "Community-acquired pneumonia",
         "COPD, T2DM"),
        ("Edna", "Farrow", date(1935, 7, 21), "Female", "Codeine (vomiting)", ResusStatus.DNACPR,
         w12, "Bay 1 Bed 3", 9, "Dr S. Iqbal", "Delirium secondary to UTI",
         "Alzheimer's disease, CKD stage 3"),
        ("Harold", "Briggs", date(1944, 1, 9), "Male", "NKDA", ResusStatus.FOR_RESUS,
         w12, "Bay 2 Bed 1", 2, "Dr S. Iqbal", "Decompensated heart failure",
         "IHD, previous MI 2015, heart failure"),
        ("Joan", "Pemberton", date(1939, 5, 30), "Female", "Latex", ResusStatus.NOT_DOCUMENTED,
         w12, "Bay 2 Bed 2", 1, "Dr R. Bennett", "Collapse ?cause, for investigation",
         "Osteoporosis, hypothyroidism"),
        ("Arthur", "Lowe", date(1936, 9, 17), "Male", "NKDA", ResusStatus.DNACPR,
         w12, "SR1", 4, "Dr S. Iqbal", "C. difficile diarrhoea",
         "Parkinson's disease"),
        ("Kwame", "Mensah", date(1958, 4, 3), "Male", "NKDA", ResusStatus.FOR_RESUS,
         amu, "Bay 1 Bed 1", 0, "Dr L. Novak", "Chest pain, troponin pending",
         "Hypertension, smoker"),
        ("Sophie", "Turner", date(1990, 12, 11), "Female", "Trimethoprim", ResusStatus.FOR_RESUS,
         amu, "Bay 1 Bed 2", 1, "Dr L. Novak", "Pyelonephritis",
         "Nil"),
    ]
    admissions = []
    for i, (first, last, dob, sex, allergies, resus, ward, bed, days_ago,
            consultant, reason, pmh) in enumerate(patient_rows, start=1):
        patient = Patient(
            nhs_number=fake_nhs_number(i * 1000),
            hospital_number=f"DEMO{i:05d}",
            first_name=first, last_name=last, date_of_birth=dob, sex=sex,
            allergies=allergies, resus_status=resus,
        )
        admissions.append(Admission(
            patient=patient, ward=ward, bed=beds[(ward.code, bed)],
            admitted_at=at(today - timedelta(days=days_ago), 14),
            consultant=consultant, reason_for_admission=reason, past_medical_history=pmh,
            expected_discharge_date=today + timedelta(days=2) if days_ago > 3 else None,
        ))
    db.session.add_all(admissions)

    # A discharged patient, so there is history to look at
    discharged = Patient(
        nhs_number=fake_nhs_number(99000), hospital_number="DEMO00099",
        first_name="Walter", last_name="Grey", date_of_birth=date(1940, 6, 6), sex="Male",
        allergies="NKDA", resus_status=ResusStatus.FOR_RESUS,
    )
    db.session.add(Admission(
        patient=discharged, ward=w12, bed=beds[("W12", "Bay 2 Bed 3")],
        admitted_at=at(today - timedelta(days=8), 11), discharged_at=at(yesterday, 15),
        discharge_destination="Home",
        consultant="Dr R. Bennett", reason_for_admission="Cellulitis left leg",
    ))

    # Last night's handover on W12 (completed) and today's day handover (draft)
    night = Handover(
        ward=w12, shift_date=yesterday, shift=Shift.NIGHT, status=HandoverStatus.COMPLETED,
        handed_over_by=staff["priya"], received_by=staff["amara"], completed_at=at(today, 7, 45),
        ward_notes="One HCA short overnight. Bay 2 Bed 3 empty after discharge, cleaned.",
    )
    day = Handover(
        ward=w12, shift_date=today, shift=Shift.LONG_DAY, status=HandoverStatus.DRAFT,
        handed_over_by=staff["amara"],
        ward_notes="Fully staffed. Expecting one admission from ED this afternoon.",
    )
    db.session.add_all([night, day])

    m_ellis, g_whitfield, e_farrow, h_briggs, j_pemberton, a_lowe = admissions[:6]
    entries = [
        dict(admission=m_ellis, news2_score=1, obs_frequency="12 hourly", mobility="Zimmer frame + 1",
             diet="Normal", falls_risk=True, pressure_risk=True,
             situation="Day 6 post # NOF repair, settled night.",
             background="Fall at home. AF on apixaban (restarted day 2).",
             assessment="Pain controlled on regular paracetamol. Wound clean and dry.",
             recommendation="Physio to review mobility today. OT home visit being arranged."),
        dict(admission=g_whitfield, news2_score=5, obs_frequency="Hourly", mobility="Independent",
             diet="Normal, diabetic", is_flagged=True,
             situation="NEWS2 rose from 2 to 5 at 04:00. RR 24, SpO2 89% on 2L.",
             background="Day 3 of IV co-amoxiclav for CAP. Known COPD, target SpO2 88-92%.",
             assessment="Increased work of breathing. Registrar reviewed at 04:30.",
             recommendation="Repeat ABG due 08:00. Chest X-ray requested. Escalate if NEWS2 7 or more."),
        dict(admission=e_farrow, news2_score=0, obs_frequency="12 hourly", mobility="Hoist",
             diet="IDDSI level 5", falls_risk=True, pressure_risk=True,
             situation="Less confused overnight, slept well.",
             background="Delirium from UTI, finished antibiotics yesterday.",
             assessment="4AT score 3 (was 7 on admission). Sacrum red but skin intact.",
             recommendation="2-hourly repositioning. Family meeting at 14:00 about discharge."),
        dict(admission=h_briggs, news2_score=2, obs_frequency="4 hourly", mobility="Independent",
             diet="Fluid restriction 1.5L", infection_control=None,
             situation="Settled. Passed 2.1L urine overnight.",
             background="Decompensated HF, on IV furosemide.",
             assessment="Weight down 1.2kg since yesterday. Ankle oedema improving.",
             recommendation="Daily weight before breakfast. U&Es due today."),
        dict(admission=j_pemberton, news2_score=1, obs_frequency="4 hourly", mobility="Independent",
             diet="Normal", falls_risk=True, is_flagged=True,
             situation="Day 1 admission for collapse.",
             background="Found on floor by carer. Osteoporosis, on levothyroxine.",
             assessment="Lying/standing BP shows postural drop of 25 mmHg.",
             recommendation="Resus status not yet documented, needs discussion with team today."),
        dict(admission=a_lowe, news2_score=1, obs_frequency="4 hourly", mobility="Frame + 1",
             diet="Normal, encourage fluids", infection_control="C. diff, side room, enteric precautions",
             situation="3 episodes of type 7 stool overnight.",
             background="C. diff on oral vancomycin, day 4. Parkinson's.",
             assessment="Mildly dehydrated. Parkinson's meds must be given on time.",
             recommendation="Stool chart. Give Parkinson's medication on time (critical medicine)."),
    ]
    for e in entries:
        db.session.add(HandoverEntry(handover=night, updated_by=staff["priya"], **e))
    # Today's draft starts as a copy of the night entries, ready to be updated
    for e in entries:
        db.session.add(HandoverEntry(handover=day, updated_by=staff["amara"], **e))

    # Outstanding and completed tasks
    db.session.add_all([
        Task(admission=g_whitfield, description="Repeat ABG", priority=Priority.URGENT,
             due_at=at(today, 8), created_by=staff["priya"]),
        Task(admission=g_whitfield, description="Chase chest X-ray report", priority=Priority.SOON,
             due_at=at(today, 11), created_by=staff["priya"]),
        Task(admission=h_briggs, description="Daily weight", priority=Priority.ROUTINE,
             due_at=at(today, 7, 30), created_by=staff["priya"],
             completed_by=staff["dan"], completed_at=at(today, 7, 20)),
        Task(admission=h_briggs, description="Chase U&E results", priority=Priority.SOON,
             due_at=at(today, 12), created_by=staff["amara"]),
        Task(admission=j_pemberton, description="Ask team to document resus status",
             priority=Priority.URGENT, created_by=staff["amara"]),
        Task(admission=e_farrow, description="Family meeting re discharge planning",
             priority=Priority.ROUTINE, due_at=at(today, 14), created_by=staff["amara"]),
        Task(admission=m_ellis, description="Book OT home visit", priority=Priority.ROUTINE,
             created_by=staff["tom"]),
    ])

    db.session.commit()
    click.echo(f"Demo data added: 2 wards, {len(staff)} staff, {len(patient_rows) + 1} patients.")
    if os.environ.get("DEMO_PASSWORD"):
        click.echo("All demo staff log in with the DEMO_PASSWORD you set.")  # don't print it into server logs
    else:
        click.echo(f"All demo staff log in with password: {DEMO_PASSWORD}")
    click.echo("e.g. amara.okafor@demo.nhs.test (nurse in charge, W12)")
