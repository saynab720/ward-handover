"""Database models for the ward handover app.

Overview:
    Ward 1--* Bed
    Patient 1--* Admission (one stay on the ward; links patient, ward and bed)
    Handover (one per ward per shift) 1--* HandoverEntry (one per admitted patient, SBAR format)
    Admission 1--* Task (outstanding jobs that carry over between shifts)
    Staff writes handovers, entries and tasks; AuditLog records who did what.
"""
import enum
from datetime import date, datetime, timezone

from flask_login import UserMixin
from sqlalchemy import CheckConstraint, Enum, ForeignKey, Index, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import db


def utcnow():
    return datetime.now(timezone.utc)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


# --- Enums -------------------------------------------------------------------

class StaffRole(enum.Enum):
    HCA = "hca"
    NURSE = "nurse"
    NURSE_IN_CHARGE = "nurse_in_charge"
    DOCTOR = "doctor"
    WARD_MANAGER = "ward_manager"
    ADMIN = "admin"


class Shift(enum.Enum):
    EARLY = "early"
    LATE = "late"
    LONG_DAY = "long_day"
    NIGHT = "night"


class HandoverStatus(enum.Enum):
    DRAFT = "draft"
    COMPLETED = "completed"


class ResusStatus(enum.Enum):
    FOR_RESUS = "for_resus"
    DNACPR = "dnacpr"
    NOT_DOCUMENTED = "not_documented"


class Priority(enum.Enum):
    ROUTINE = "routine"
    SOON = "soon"
    URGENT = "urgent"


# --- Ward structure ----------------------------------------------------------

class Ward(TimestampMixin, db.Model):
    __tablename__ = "wards"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(20), unique=True)  # e.g. "W12"
    name: Mapped[str] = mapped_column(String(100))  # e.g. "Elderly Care"
    specialty: Mapped[str | None] = mapped_column(String(100))
    is_active: Mapped[bool] = mapped_column(default=True)

    beds: Mapped[list["Bed"]] = relationship(back_populates="ward")
    admissions: Mapped[list["Admission"]] = relationship(back_populates="ward")
    handovers: Mapped[list["Handover"]] = relationship(back_populates="ward")


class Bed(db.Model):
    __tablename__ = "beds"
    __table_args__ = (UniqueConstraint("ward_id", "label"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    ward_id: Mapped[int] = mapped_column(ForeignKey("wards.id"))
    label: Mapped[str] = mapped_column(String(20))  # e.g. "Bay 2 Bed 4", "SR1"
    is_side_room: Mapped[bool] = mapped_column(default=False)
    is_active: Mapped[bool] = mapped_column(default=True)

    ward: Mapped[Ward] = relationship(back_populates="beds")


# --- People ------------------------------------------------------------------

class Staff(UserMixin, TimestampMixin, db.Model):
    __tablename__ = "staff"

    id: Mapped[int] = mapped_column(primary_key=True)
    first_name: Mapped[str] = mapped_column(String(100))
    last_name: Mapped[str] = mapped_column(String(100))
    email: Mapped[str] = mapped_column(String(255), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[StaffRole] = mapped_column(Enum(StaffRole))
    # NMC PIN for nurses, GMC number for doctors
    registration_number: Mapped[str | None] = mapped_column(String(20))
    home_ward_id: Mapped[int | None] = mapped_column(ForeignKey("wards.id"))
    is_active: Mapped[bool] = mapped_column(default=True)

    home_ward: Mapped[Ward | None] = relationship()

    @property
    def full_name(self):
        return f"{self.first_name} {self.last_name}"


class Patient(TimestampMixin, db.Model):
    __tablename__ = "patients"
    __table_args__ = (
        CheckConstraint("nhs_number IS NULL OR length(nhs_number) = 10", name="nhs_number_10_digits"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    nhs_number: Mapped[str | None] = mapped_column(String(10), unique=True)
    hospital_number: Mapped[str] = mapped_column(String(20), unique=True)  # MRN
    first_name: Mapped[str] = mapped_column(String(100))
    last_name: Mapped[str] = mapped_column(String(100))
    date_of_birth: Mapped[date]
    sex: Mapped[str | None] = mapped_column(String(20))
    allergies: Mapped[str | None] = mapped_column(Text)  # free text, "NKDA" if none
    resus_status: Mapped[ResusStatus] = mapped_column(
        Enum(ResusStatus), default=ResusStatus.NOT_DOCUMENTED
    )

    admissions: Mapped[list["Admission"]] = relationship(back_populates="patient")


# --- Clinical ----------------------------------------------------------------

class Admission(TimestampMixin, db.Model):
    """One patient's stay on a ward. Discharged admissions keep their history.
    Moving to another ward ends this admission and starts a new one there."""
    __tablename__ = "admissions"
    __table_args__ = (
        # While an admission is open: one per patient, and one patient per bed
        Index("one_current_admission_per_patient", "patient_id", unique=True,
              sqlite_where=text("discharged_at IS NULL"), postgresql_where=text("discharged_at IS NULL")),
        Index("one_patient_per_bed", "bed_id", unique=True,
              sqlite_where=text("discharged_at IS NULL"), postgresql_where=text("discharged_at IS NULL")),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"))
    ward_id: Mapped[int] = mapped_column(ForeignKey("wards.id"))
    bed_id: Mapped[int | None] = mapped_column(ForeignKey("beds.id"))
    admitted_at: Mapped[datetime] = mapped_column(default=utcnow)
    discharged_at: Mapped[datetime | None]
    discharge_destination: Mapped[str | None] = mapped_column(String(100))  # e.g. "Home", "Transferred to AMU"
    consultant: Mapped[str | None] = mapped_column(String(100))
    reason_for_admission: Mapped[str | None] = mapped_column(Text)
    past_medical_history: Mapped[str | None] = mapped_column(Text)
    expected_discharge_date: Mapped[date | None]

    patient: Mapped[Patient] = relationship(back_populates="admissions")
    ward: Mapped[Ward] = relationship(back_populates="admissions")
    bed: Mapped[Bed | None] = relationship()
    handover_entries: Mapped[list["HandoverEntry"]] = relationship(back_populates="admission")
    tasks: Mapped[list["Task"]] = relationship(back_populates="admission")

    @property
    def is_current(self):
        return self.discharged_at is None


class Handover(TimestampMixin, db.Model):
    """A handover for one ward at one shift change."""
    __tablename__ = "handovers"
    __table_args__ = (UniqueConstraint("ward_id", "shift_date", "shift"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    ward_id: Mapped[int] = mapped_column(ForeignKey("wards.id"))
    shift_date: Mapped[date]
    shift: Mapped[Shift] = mapped_column(Enum(Shift))
    status: Mapped[HandoverStatus] = mapped_column(
        Enum(HandoverStatus), default=HandoverStatus.DRAFT
    )
    handed_over_by_id: Mapped[int] = mapped_column(ForeignKey("staff.id"))
    received_by_id: Mapped[int | None] = mapped_column(ForeignKey("staff.id"))
    completed_at: Mapped[datetime | None]
    ward_notes: Mapped[str | None] = mapped_column(Text)  # staffing, beds, safety issues

    ward: Mapped[Ward] = relationship(back_populates="handovers")
    handed_over_by: Mapped[Staff] = relationship(foreign_keys=[handed_over_by_id])
    received_by: Mapped[Staff | None] = relationship(foreign_keys=[received_by_id])
    entries: Mapped[list["HandoverEntry"]] = relationship(
        back_populates="handover", cascade="all, delete-orphan"
    )


class HandoverEntry(TimestampMixin, db.Model):
    """One patient's section of a handover, in SBAR format."""
    __tablename__ = "handover_entries"
    __table_args__ = (
        UniqueConstraint("handover_id", "admission_id"),
        CheckConstraint("news2_score IS NULL OR news2_score BETWEEN 0 AND 20", name="news2_range"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    handover_id: Mapped[int] = mapped_column(ForeignKey("handovers.id"))
    admission_id: Mapped[int] = mapped_column(ForeignKey("admissions.id"))

    situation: Mapped[str | None] = mapped_column(Text)
    background: Mapped[str | None] = mapped_column(Text)
    assessment: Mapped[str | None] = mapped_column(Text)
    recommendation: Mapped[str | None] = mapped_column(Text)

    news2_score: Mapped[int | None]
    obs_frequency: Mapped[str | None] = mapped_column(String(50))  # e.g. "4 hourly"
    mobility: Mapped[str | None] = mapped_column(String(100))
    diet: Mapped[str | None] = mapped_column(String(100))  # e.g. "NBM", "IDDSI level 5"
    falls_risk: Mapped[bool] = mapped_column(default=False)
    pressure_risk: Mapped[bool] = mapped_column(default=False)
    infection_control: Mapped[str | None] = mapped_column(String(100))  # e.g. "MRSA, side room"
    is_flagged: Mapped[bool] = mapped_column(default=False)  # needs attention this shift

    updated_by_id: Mapped[int] = mapped_column(ForeignKey("staff.id"))

    handover: Mapped[Handover] = relationship(back_populates="entries")
    admission: Mapped[Admission] = relationship(back_populates="handover_entries")
    updated_by: Mapped[Staff] = relationship()

    @property
    def not_updated_this_shift(self):
        """True until someone saves this entry. New entries are created with
        created_at == updated_at, and every save moves updated_at on."""
        return self.updated_at == self.created_at


class Task(TimestampMixin, db.Model):
    """An outstanding job for a patient. Stays open across shifts until completed."""
    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(primary_key=True)
    admission_id: Mapped[int] = mapped_column(ForeignKey("admissions.id"))
    description: Mapped[str] = mapped_column(Text)
    priority: Mapped[Priority] = mapped_column(Enum(Priority), default=Priority.ROUTINE)
    due_at: Mapped[datetime | None]
    created_by_id: Mapped[int] = mapped_column(ForeignKey("staff.id"))
    completed_by_id: Mapped[int | None] = mapped_column(ForeignKey("staff.id"))
    completed_at: Mapped[datetime | None]

    admission: Mapped[Admission] = relationship(back_populates="tasks")
    created_by: Mapped[Staff] = relationship(foreign_keys=[created_by_id])
    completed_by: Mapped[Staff | None] = relationship(foreign_keys=[completed_by_id])

    @property
    def is_done(self):
        return self.completed_at is not None


# --- Audit -------------------------------------------------------------------

class AuditLog(db.Model):
    """Append-only record of who viewed or changed patient data."""
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    timestamp: Mapped[datetime] = mapped_column(default=utcnow, index=True)
    staff_id: Mapped[int | None] = mapped_column(ForeignKey("staff.id"))
    action: Mapped[str] = mapped_column(String(20))  # "view", "create", "update", "delete"
    table_name: Mapped[str] = mapped_column(String(50))
    record_id: Mapped[int | None]
    details: Mapped[str | None] = mapped_column(Text)

    staff: Mapped[Staff | None] = relationship()
